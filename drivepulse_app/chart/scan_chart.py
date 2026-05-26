"""Scan history chart sub-page with multi-car / dual-value comparison.

Pure helpers (formatting, stat aggregation, cairo draw routine) live in
``drivepulse_app.chart._helpers`` and are re-exported here so existing
imports (notably the test suite) keep resolving.
"""
from __future__ import annotations

import json
import sqlite3
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from drivepulse_app.cars.metadata import _parse_profile_pid_key, _unit_display
from drivepulse_app.chart._helpers import (
    _CHART_H,
    _COLOR_MAIN,
    _DEFAULT_COMPARE_COLORS,
    _compute_stats_for_car,
    _draw_chart,
    _fmt,
    _fmt_rel_s,
    _fmt_scan_label,
    _fmt_ts,
    _lookup_card_bg,
    _rgb_to_hex,
    _safe_pids_count,
)
from drivepulse_app.common import LOG_DIR
from drivepulse_app.diagnostics import atomic_write_text, get_logger

_log = get_logger(__name__)
_PREFS_FILE = LOG_DIR / "scan_chart_prefs.json"

# Re-exports for tests/callers. ScanChartContent is the public widget; the
# underscore-prefixed names stay reachable through ``scan_chart.<name>`` so
# tests/test_scan_chart_helpers.py keeps importing them from here, and
# ``monkeypatch.setattr(scan_chart, "_PREFS_FILE", …)`` keeps working.
__all__ = [
    "_PREFS_FILE",
    "ScanChartContent",
    "_compute_stats_for_car",
    "_fmt",
    "_fmt_scan_label",
    "_fmt_ts",
    "_prefs_load",
    "_prefs_save",
    "_rgb_to_hex",
    "_safe_pids_count",
]


def _prefs_load() -> dict:
    try:
        return json.loads(_PREFS_FILE.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        _log.warning("scan_chart_prefs unreadable: %s", exc)
        return {}


def _prefs_save(prefs: dict) -> None:
    try:
        atomic_write_text(_PREFS_FILE, json.dumps(prefs, indent=2))
    except OSError as exc:
        _log.warning("scan_chart_prefs save failed: %s", exc)



# ---------------------------------------------------------------------------
# Main widget
# ---------------------------------------------------------------------------

class ScanChartContent(Gtk.Box):
    """Scrollable chart content with multi-car / dual-value comparison."""

    def __init__(
        self,
        main_pid: str,
        all_stats: dict,
        profiles: list,
        db,
        pid_labels: dict,
        language: str = "de",
        main_car_id: int | None = None,
        main_scan_id: int | None = None,
        on_navigate_pid=None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_vexpand(True)

        self._main_pid = main_pid
        self._main_stats = all_stats
        self._profiles = profiles
        self._db = db
        self._pid_labels = pid_labels
        self._language = language
        self._main_car_id = main_car_id
        self._on_navigate_pid = on_navigate_pid

        # Vergleichs-Kandidaten: nur Autos die mindestens einen Datapoint
        # für den aktuell betrachteten Sensor (main_pid) haben — sonst wäre
        # der Vergleich leer.  Einmal beim Öffnen berechnet, damit das
        # Dropdown später ohne DB-Roundtrip auskommt.
        self._cars_with_data: set[int] = set()
        for _p in profiles:
            _cid = _p.get("car_id")
            if _cid is None:
                continue
            if self._car_has_pid_values(_cid, main_pid):
                self._cars_with_data.add(_cid)

        # PID-Auswahloptionen (Liste der bekannten PIDs aus Hauptauto)
        self._pid_options: list[tuple[str, str]] = []
        for pid, s in sorted(all_stats.items(), key=lambda kv: pid_labels.get(kv[0], kv[0])):
            if not (s.get("values") or []):
                continue
            lbl = pid_labels.get(pid, pid)
            ud = _unit_display(s.get("unit", ""), language)
            self._pid_options.append((pid, f"{lbl}  ({ud})" if ud else lbl))

        # Werte (PIDs): Wert 1 vordefiniert auf main_pid, Wert 2 optional
        self._value_pids: list[str] = [main_pid]
        # Vergleichs-Autos (ohne Hauptauto), jeweils {car_id, name, color, stats, row, suffix_box, remove_btn}
        self._compare_cars: list[dict] = []
        self._next_color_idx = 0
        self._refreshing_add_car_dd = False

        # ── Info-Strip ────────────────────────────────────────────────────
        main_pid_stats = all_stats.get(main_pid) or {}
        mean = main_pid_stats.get("avg", 0.0)
        unit_disp = _unit_display(main_pid_stats.get("unit", ""), language)
        n = len(main_pid_stats.get("values") or [])

        info = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        info.set_margin_start(16)
        info.set_margin_end(16)
        info.set_margin_top(12)
        info.set_margin_bottom(10)

        dot = Gtk.Label()
        dot.set_markup(
            f'<span foreground="{_rgb_to_hex(_COLOR_MAIN)}" size="large">⬤</span>'
        )
        dot.set_valign(Gtk.Align.CENTER)
        info.append(dot)

        info_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_text.set_hexpand(True)
        mean_str = (
            f"⌀ {_fmt(mean)} {unit_disp}".strip()
            if unit_disp
            else f"⌀ {_fmt(mean)}"
        )
        sub_text = f"{mean_str}   ·   {n} {'Messung' if n == 1 else 'Messungen'}"
        sub_lbl = Gtk.Label(label=sub_text, xalign=0.0)
        sub_lbl.add_css_class("dim-label")
        sub_lbl.add_css_class("caption")
        info_text.append(sub_lbl)
        info.append(info_text)
        self.append(info)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Chart ─────────────────────────────────────────────────────────
        self._da = Gtk.DrawingArea()
        self._da.set_content_height(_CHART_H)
        self._da.set_hexpand(True)
        self._da.set_draw_func(self._draw)

        # Vertikaler Wisch auf dem Chart-Canvas wechselt zum nächsten/
        # vorherigen Sensor (hoch = nächster, runter = vorheriger).
        if self._on_navigate_pid is not None:
            chart_drag = Gtk.GestureDrag()
            chart_drag.connect("drag-end", self._on_chart_swipe)
            self._da.add_controller(chart_drag)

        self.append(self._da)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Werte-Sektion ─────────────────────────────────────────────────
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        wrap.set_margin_start(16)
        wrap.set_margin_end(16)
        wrap.set_margin_top(14)
        wrap.set_margin_bottom(16)

        wrap.append(self._build_section_header("Werte"))
        self._values_list = Gtk.ListBox()
        self._values_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._values_list.add_css_class("boxed-list")
        self._values_list.set_valign(Gtk.Align.START)

        # Wert 1 row — auf den geöffneten PID fixiert (nicht änderbar).
        # Dropdown sitzt als Prefix linksbündig, der "+"-Button bleibt rechts.
        self._val1_row = Adw.ActionRow()
        self._val1_dd = self._make_pid_dropdown(self._main_pid)
        self._val1_dd.set_sensitive(False)
        self._val1_dd.set_hexpand(True)
        self._val1_dd.set_halign(Gtk.Align.FILL)
        self._val1_dd.set_tooltip_text("Fest auf den geöffneten Sensor gesetzt")
        self._val1_row.add_prefix(self._val1_dd)
        self._val1_add_btn = Gtk.Button.new_from_icon_name("list-add-symbolic")
        self._val1_add_btn.add_css_class("flat")
        self._val1_add_btn.add_css_class("circular")
        self._val1_add_btn.set_valign(Gtk.Align.CENTER)
        self._val1_add_btn.set_tooltip_text("Zweiten Wert hinzufügen")
        self._val1_add_btn.connect("clicked", self._on_add_val2)
        self._val1_row.add_suffix(self._val1_add_btn)
        self._values_list.append(self._val1_row)

        # Wert 2 row (initial versteckt) — gleicher Layout-Aufbau wie Wert 1.
        self._val2_row = Adw.ActionRow()
        self._val2_dd = self._make_pid_dropdown(None)
        self._val2_dd.set_hexpand(True)
        self._val2_dd.set_halign(Gtk.Align.FILL)
        self._val2_dd.connect("notify::selected", self._on_val2_changed)
        self._val2_row.add_prefix(self._val2_dd)
        self._val2_remove_btn = Gtk.Button.new_from_icon_name("list-remove-symbolic")
        self._val2_remove_btn.add_css_class("flat")
        self._val2_remove_btn.add_css_class("circular")
        self._val2_remove_btn.set_valign(Gtk.Align.CENTER)
        self._val2_remove_btn.set_tooltip_text("Zweiten Wert entfernen")
        self._val2_remove_btn.connect("clicked", self._on_remove_val2)
        self._val2_row.add_suffix(self._val2_remove_btn)
        self._val2_row.set_visible(False)
        self._values_list.append(self._val2_row)

        wrap.append(self._values_list)

        # ── Fahrzeuge-Sektion ─────────────────────────────────────────────
        wrap.append(self._build_section_header("Fahrzeuge"))
        self._cars_list = Gtk.ListBox()
        self._cars_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._cars_list.add_css_class("boxed-list")
        self._cars_list.set_valign(Gtk.Align.START)

        # Hauptfahrzeug-Zeile (immer da, nicht entfernbar). Eigenes Row-
        # Layout statt Adw.ActionRow, weil sonst der Scan-Dropdown als
        # Suffix neben dem Auto-Namen klemmt — Namen + 16er-Datums-Combo
        # bekommen in einer Zeile auf jeder realistischen Bildschirm-
        # breite zu wenig Platz. Wir setzen Auto-Name oben, Scan-Auswahl
        # darunter (etwas eingerückt unter dem Farb-Dot).
        main_car_name = self._lookup_car_name(main_car_id) if main_car_id is not None else "Hauptfahrzeug"
        self._main_car_row = Gtk.ListBoxRow()
        self._main_car_row.set_selectable(False)
        self._main_car_row.set_activatable(False)
        _main_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        _main_container.set_margin_top(8)
        _main_container.set_margin_bottom(8)
        _main_container.set_margin_start(12)
        _main_container.set_margin_end(12)
        _main_top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        main_dot = Gtk.Label()
        main_dot.set_markup(
            f'<span foreground="{_rgb_to_hex(_COLOR_MAIN)}" size="large">⬤</span>'
        )
        main_dot.set_valign(Gtk.Align.CENTER)
        _main_top.append(main_dot)
        _main_name_lbl = Gtk.Label(label=main_car_name, xalign=0.0)
        _main_name_lbl.set_halign(Gtk.Align.START)
        _main_name_lbl.set_hexpand(True)
        _main_name_lbl.add_css_class("heading")
        _main_name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        _main_top.append(_main_name_lbl)
        _main_container.append(_main_top)
        self._main_car_dropdown_slot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self._main_car_dropdown_slot.set_halign(Gtk.Align.END)
        _main_container.append(self._main_car_dropdown_slot)
        self._main_car_row.set_child(_main_container)

        # Scan-Auswahl fürs Hauptauto: alle Scans mit Sensordaten, neuester
        # zuerst. Default ist der aktuell ausgewählte Scan (falls bekannt),
        # sonst der neueste.
        self._main_scans_meta: list = []
        self._main_scan_ts: str | None = None
        if self._db is not None and main_car_id is not None:
            try:
                _ms = list(self._db.list_scans_for_car(main_car_id))
                self._main_scans_meta = [s for s in _ms if _safe_pids_count(s) > 0]
            except sqlite3.Error:
                _log.warning("Could not list scans for main car_id=%s", main_car_id, exc_info=True)
                self._main_scans_meta = []
        if self._main_scans_meta:
            preselect_ts: str | None = None
            if main_scan_id is not None:
                for s in self._main_scans_meta:
                    if int(s["id"]) == int(main_scan_id):
                        preselect_ts = str(s["scanned_at"])
                        break
            if preselect_ts is None:
                preselect_ts = str(self._main_scans_meta[0]["scanned_at"])
            self._main_scan_ts = preselect_ts
        self._main_scan_dd: Gtk.DropDown | None = None
        if self._main_scans_meta:
            self._main_scan_dd = self._make_scan_dd(self._main_scans_meta, self._main_scan_ts, set())
            self._main_scan_dd.connect("notify::selected", self._on_main_scan_changed)
            self._main_car_dropdown_slot.append(self._main_scan_dd)

        self._cars_list.append(self._main_car_row)

        # "+ Fahrzeug"-Zeile: Titel oben, Dropdown darunter.
        self._add_car_row = Gtk.ListBoxRow()
        self._add_car_row.set_selectable(False)
        self._add_car_row.set_activatable(False)
        _add_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        _add_box.set_margin_top(8)
        _add_box.set_margin_bottom(8)
        _add_box.set_margin_start(12)
        _add_box.set_margin_end(12)
        _add_title = Gtk.Label(label="Fahrzeug hinzufügen", xalign=0.0)
        _add_title.add_css_class("heading")
        self._add_car_dd = Gtk.DropDown()
        self._add_car_dd.set_halign(Gtk.Align.FILL)
        self._add_car_dd.set_hexpand(True)
        self._refresh_add_car_dropdown()
        self._add_car_dd.connect("notify::selected", self._on_add_car_selected)
        _add_box.append(_add_title)
        _add_box.append(self._add_car_dd)
        self._add_car_row.set_child(_add_box)
        self._cars_list.append(self._add_car_row)

        wrap.append(self._cars_list)
        self.append(wrap)

        # Gespeicherte Vergleichs-Konfiguration (Wert 2 + Vergleichs-Autos)
        # für diesen (Auto, Sensor)-Kontext rekonstruieren.
        self._restore_prefs()

    # ── Helpers: build ────────────────────────────────────────────────────

    def _build_section_header(self, title: str) -> Gtk.Widget:
        lbl = Gtk.Label(label=title, xalign=0.0)
        lbl.add_css_class("heading")
        return lbl

    def _make_pid_dropdown(self, preselect_pid: str | None) -> Gtk.DropDown:
        sl = Gtk.StringList()
        sl.append("—")
        for _, disp in self._pid_options:
            sl.append(disp)
        dd = Gtk.DropDown(model=sl)
        dd.set_valign(Gtk.Align.CENTER)
        if preselect_pid is not None:
            for i, (pid, _) in enumerate(self._pid_options):
                if pid == preselect_pid:
                    dd.set_selected(i + 1)
                    break
        return dd

    def _lookup_car_name(self, car_id: int) -> str:
        for p in self._profiles:
            if p.get("car_id") == car_id:
                disp = p.get("label") or p.get("brand") or f"Fahrzeug {car_id}"
                vin = p.get("vin", "")
                if vin:
                    disp = f"{disp}  …{vin[-5:]}"
                return disp
        return f"Fahrzeug {car_id}"

    def _comparison_scan_ts_for_car(
        self, car_id: int | None, exclude_entry: dict | None = None
    ) -> set[str]:
        """Konkrete scan_ts-Werte, die Vergleichseinträge des Fahrzeugs nutzen.

        car_id=None gibt ein leeres Set zurück (kein Hauptfahrzeug → kein
        Vergleichsfilter).
        """
        result: set[str] = set()
        if car_id is None:
            return result
        for entry in self._compare_cars:
            if entry is exclude_entry:
                continue
            if entry.get("car_id") == car_id:
                ts = entry.get("scan_ts")
                if ts:
                    result.add(ts)
        return result

    def _make_scan_dd(
        self,
        scans_meta: list,
        preselect_ts: str | None,
        green_ts: set[str],
    ) -> Gtk.DropDown:
        """Scan-DropDown mit optionaler Grün-Markierung geladener Scans."""
        sl = Gtk.StringList()
        for s in scans_meta:
            sl.append(_fmt_scan_label(str(s["scanned_at"])))

        _green = frozenset(
            i
            for i, s in enumerate(scans_meta)
            if str(s["scanned_at"]) in green_ts
        )

        fac = Gtk.SignalListItemFactory()

        def _setup(_f, item):
            item.set_child(Gtk.Label(xalign=0.0))

        def _bind(_f, item, _sl=sl, _g=_green):
            lbl = item.get_child()
            pos = item.get_position()
            text = _sl.get_string(pos)
            if pos in _g:
                lbl.set_markup(
                    f'<span foreground="#33d17a">'
                    f"{GLib.markup_escape_text(text)}</span>"
                )
            else:
                lbl.set_text(text)

        fac.connect("setup", _setup)
        fac.connect("bind", _bind)

        dd = Gtk.DropDown(model=sl, factory=fac)
        dd.set_valign(Gtk.Align.CENTER)
        target_idx = 0
        if preselect_ts:
            for i, s in enumerate(scans_meta):
                if str(s["scanned_at"]) == preselect_ts:
                    target_idx = i
                    break
        dd.set_selected(target_idx)
        return dd

    def _rebuild_main_scan_dd(self) -> None:
        """Haupt-Scan-Dropdown neu aufbauen, um Vergleichs-Scans grün zu markieren."""
        if not self._main_scans_meta or self._main_scan_dd is None:
            return
        green_ts = self._comparison_scan_ts_for_car(self._main_car_id)
        new_dd = self._make_scan_dd(self._main_scans_meta, self._main_scan_ts, green_ts)
        new_dd.connect("notify::selected", self._on_main_scan_changed)
        if self._main_car_dropdown_slot is not None:
            self._main_car_dropdown_slot.remove(self._main_scan_dd)
            self._main_car_dropdown_slot.append(new_dd)
        self._main_scan_dd = new_dd

    def _car_has_pid_values(self, car_id: int, pid: str) -> bool:
        if self._db is None:
            return False
        try:
            scans = self._db.list_scans_for_car(car_id)
        except sqlite3.Error:
            _log.debug("Could not list scans for car_id=%s in _car_has_pid_values", car_id, exc_info=True)
            return False
        for scan_meta in scans:
            try:
                data = self._db.get_scan_data(int(scan_meta["id"]))
            except (sqlite3.Error, json.JSONDecodeError, ValueError):
                _log.debug("Could not load scan_data for id=%s in _car_has_pid_values", scan_meta.get("id"), exc_info=True)
                continue
            for raw_key, raw_val in (data.get("live_data") or {}).items():
                if _parse_profile_pid_key(raw_key) != pid:
                    continue
                v = raw_val.get("value") if isinstance(raw_val, dict) else raw_val
                if v is None:
                    continue
                try:
                    float(v)
                except (TypeError, ValueError):
                    continue
                return True
        return False

    def _refresh_add_car_dropdown(self) -> None:
        # Das gleiche Fahrzeug darf mehrfach als Vergleichseintrag hinzugefügt
        # werden (z. B. um verschiedene Scan-Historien zu vergleichen) — aber
        # nur wenn dort wirklich noch nicht geladene Scans übrig sind.
        # Sonst landet man bei einem leeren Compare-Eintrag (scan_ts=None)
        # mit unklarem Verhalten beim Zeichnen.
        self._add_car_candidates: list[int] = []
        sl = Gtk.StringList()
        sl.append("—")
        for p in self._profiles:
            cid = p.get("car_id")
            if cid is None or cid not in self._cars_with_data:
                continue
            if not self._car_has_unused_scans(cid):
                continue
            sl.append(self._lookup_car_name(cid))
            self._add_car_candidates.append(cid)
        # Signal blockieren: set_model() löst notify::selected aus, bevor
        # _add_car_candidates fertig ist – das würde zu einem Absturz führen.
        self._refreshing_add_car_dd = True
        self._add_car_dd.set_model(sl)
        self._add_car_dd.set_selected(0)
        self._refreshing_add_car_dd = False

    def _car_has_unused_scans(self, car_id: int) -> bool:
        """True iff the car has at least one scan that's not already loaded
        (neither as the main scan nor in an existing compare entry)."""
        if self._db is None:
            return False
        try:
            scans = list(self._db.list_scans_for_car(car_id))
        except sqlite3.Error:
            return False
        used: set[str] = self._comparison_scan_ts_for_car(car_id)
        if self._main_car_id == car_id and self._main_scan_ts:
            used.add(self._main_scan_ts)
        for s in scans:
            if _safe_pids_count(s) <= 0:
                continue
            if str(s["scanned_at"]) not in used:
                return True
        return False

    # ── Value handlers ────────────────────────────────────────────────────

    def _selected_pid_from(self, dd: Gtk.DropDown) -> str | None:
        sel = dd.get_selected()
        if sel == 0:
            return None
        return self._pid_options[sel - 1][0]

    def _on_val2_changed(self, dd: Gtk.DropDown, _prop) -> None:
        pid = self._selected_pid_from(dd)
        if pid is None:
            # nichts gewählt → Wert 2 vorhanden aber inaktiv
            if len(self._value_pids) > 1:
                self._value_pids = self._value_pids[:1]
        elif len(self._value_pids) >= 2:
            self._value_pids[1] = pid
        else:
            self._value_pids.append(pid)
        self._save_prefs()
        self._da.queue_draw()

    def _on_add_val2(self, _btn) -> None:
        self._val2_row.set_visible(True)
        self._val1_add_btn.set_sensitive(False)

    def _on_remove_val2(self, _btn) -> None:
        self._val2_row.set_visible(False)
        self._val1_add_btn.set_sensitive(True)
        self._val2_dd.set_selected(0)
        if len(self._value_pids) > 1:
            self._value_pids = self._value_pids[:1]
        self._save_prefs()
        self._da.queue_draw()

    # ── Car handlers ──────────────────────────────────────────────────────

    def _on_add_car_selected(self, dd: Gtk.DropDown, _prop) -> None:
        if self._refreshing_add_car_dd:
            return
        sel = dd.get_selected()
        if sel == 0 or sel > len(self._add_car_candidates):
            return
        car_id = self._add_car_candidates[sel - 1]
        self._add_compare_car(car_id)
        self._refresh_add_car_dropdown()

    def _add_compare_car(
        self,
        car_id: int,
        restored_color: tuple[float, float, float] | None = None,
        restored_scan_ts: str | None = None,
    ) -> None:
        if restored_color is not None:
            color = restored_color
        else:
            color = _DEFAULT_COMPARE_COLORS[self._next_color_idx % len(_DEFAULT_COMPARE_COLORS)]
            self._next_color_idx += 1

        entry: dict = {
            "car_id": car_id,
            "name": self._lookup_car_name(car_id),
            "color": color,
            "stats": None,
            "_restored_scan_ts": restored_scan_ts,
        }

        # Two-row layout: top has color, name and (filled later) remove
        # button; bottom holds the scan dropdown indented under the dot.
        # Auto name + Scan-Dropdown fit nowhere on one line on phone
        # widths, so we stack them.
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        container.set_margin_top(8)
        container.set_margin_bottom(8)
        container.set_margin_start(12)
        container.set_margin_end(12)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        color_btn = Gtk.Button()
        color_btn.add_css_class("flat")
        color_btn.add_css_class("circular")
        color_btn.set_valign(Gtk.Align.CENTER)
        color_btn.set_tooltip_text("Farbe ändern")
        color_lbl = Gtk.Label()
        color_lbl.set_markup(
            f'<span foreground="{_rgb_to_hex(color)}" size="large">⬤</span>'
        )
        color_btn.set_child(color_lbl)
        color_btn.connect("clicked", self._on_color_clicked, entry)
        top.append(color_btn)

        name_lbl = Gtk.Label(label=entry["name"], xalign=0.0)
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.set_hexpand(True)
        name_lbl.add_css_class("heading")
        name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        top.append(name_lbl)

        # suffix_box (kept name for backwards compat with the rest of
        # the code that reaches in to swap spinner ↔ dropdown ↔ remove).
        # Sits on the bottom row, right-aligned so the scan-date combo
        # and the remove-X button line up cleanly under the row's
        # right edge.
        suffix_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        suffix_box.set_valign(Gtk.Align.CENTER)
        suffix_box.set_halign(Gtk.Align.END)

        spinner = Gtk.Spinner()
        spinner.start()
        suffix_box.append(spinner)

        container.append(top)
        container.append(suffix_box)
        row.set_child(container)

        entry["row"] = row
        entry["suffix_box"] = suffix_box
        entry["color_lbl"] = color_lbl
        self._compare_cars.append(entry)

        # Vor der "Fahrzeug hinzufügen"-Zeile einsortieren
        self._cars_list.remove(self._add_car_row)
        self._cars_list.append(row)
        self._cars_list.append(self._add_car_row)

        threading.Thread(
            target=lambda: GLib.idle_add(
                self._on_compare_stats_loaded,
                entry, _compute_stats_for_car(self._db, car_id),
            ),
            daemon=True,
        ).start()

    def _on_compare_stats_loaded(self, entry: dict, stats: dict) -> bool:
        entry["stats"] = stats
        # Scan-Liste laden (newest-first), nur mit Sensordaten
        try:
            scans_meta = list(self._db.list_scans_for_car(entry["car_id"])) if self._db else []
        except sqlite3.Error:
            _log.warning("Could not list scans for compare car_id=%s", entry.get("car_id"), exc_info=True)
            scans_meta = []
        scans_meta = [s for s in scans_meta if _safe_pids_count(s) > 0]

        # Bereits anderweitig geladene Scans des gleichen Fahrzeugs ausblenden.
        loaded_ts: set[str] = self._comparison_scan_ts_for_car(
            entry["car_id"], exclude_entry=entry
        )
        if self._main_car_id == entry["car_id"] and self._main_scan_ts:
            loaded_ts.add(self._main_scan_ts)
        scans_meta = [s for s in scans_meta if str(s["scanned_at"]) not in loaded_ts]

        entry["scans_meta"] = scans_meta
        # Default: neuester verfügbarer Scan. Wenn Persistenz einen konkreten
        # Scan kennt und dieser noch existiert, wird er stattdessen gewählt.
        restored = entry.pop("_restored_scan_ts", None)
        if restored and any(str(s["scanned_at"]) == restored for s in scans_meta):
            entry["scan_ts"] = restored
        elif scans_meta:
            entry["scan_ts"] = str(scans_meta[0]["scanned_at"])
        else:
            entry["scan_ts"] = None

        box = entry["suffix_box"]
        child = box.get_first_child()
        while child is not None:
            box.remove(child)
            child = box.get_first_child()

        # Scan-Dropdown mit gefilterten Scans
        if scans_meta:
            scan_dd = self._make_scan_dd(scans_meta, entry.get("scan_ts"), set())
            scan_dd.connect("notify::selected", self._on_compare_scan_changed, entry)
            box.append(scan_dd)
            entry["scan_dd"] = scan_dd

        # Fahrzeug-Entfernen-Button
        remove_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        remove_btn.add_css_class("flat")
        remove_btn.add_css_class("circular")
        remove_btn.set_valign(Gtk.Align.CENTER)
        remove_btn.set_tooltip_text("Fahrzeug entfernen")
        remove_btn.connect("clicked", self._on_remove_car, entry)
        box.append(remove_btn)

        # Haupt-Dropdown neu aufbauen, um aktive Vergleichs-Scans grün zu zeigen.
        if self._main_car_id == entry["car_id"]:
            self._rebuild_main_scan_dd()

        self._save_prefs()
        self._da.queue_draw()
        return False

    def _on_compare_scan_changed(self, dd: Gtk.DropDown, _prop, entry: dict) -> None:
        sel = dd.get_selected()
        scans = entry.get("scans_meta") or []
        entry.pop("scan_id_resolved", None)
        idx = sel
        entry["scan_ts"] = str(scans[idx]["scanned_at"]) if 0 <= idx < len(scans) else None
        if self._main_car_id == entry.get("car_id"):
            self._rebuild_main_scan_dd()
        self._save_prefs()
        self._da.queue_draw()

    def _on_main_scan_changed(self, dd: Gtk.DropDown, _prop) -> None:
        sel = dd.get_selected()
        if 0 <= sel < len(self._main_scans_meta):
            self._main_scan_ts = str(self._main_scans_meta[sel]["scanned_at"])
        else:
            self._main_scan_ts = None
        self._save_prefs()
        self._da.queue_draw()

    def _on_remove_car(self, _btn, entry: dict) -> None:
        if entry not in self._compare_cars:
            return
        car_id = entry.get("car_id")
        self._compare_cars.remove(entry)
        self._cars_list.remove(entry["row"])
        self._refresh_add_car_dropdown()
        if self._main_car_id == car_id:
            self._rebuild_main_scan_dd()
        self._save_prefs()
        self._da.queue_draw()

    def _on_color_clicked(self, _btn, entry: dict) -> None:
        cur = entry["color"]
        rgba = Gdk.RGBA()
        rgba.red, rgba.green, rgba.blue, rgba.alpha = cur[0], cur[1], cur[2], 1.0

        root = self.get_root()
        parent = root if isinstance(root, Gtk.Window) else None

        try:
            dialog = Gtk.ColorDialog()
            dialog.set_title("Farbe wählen")
            dialog.set_with_alpha(False)

            def _done(d, result) -> None:
                try:
                    picked = d.choose_rgba_finish(result)
                except Exception:
                    _log.debug("ColorDialog choose_rgba_finish failed (likely user cancel)", exc_info=True)
                    return
                if picked is None:
                    return
                entry["color"] = (picked.red, picked.green, picked.blue)
                entry["color_lbl"].set_markup(
                    f'<span foreground="{_rgb_to_hex(entry["color"])}" size="large">⬤</span>'
                )
                self._save_prefs()
                self._da.queue_draw()

            dialog.choose_rgba(parent, rgba, None, _done)
        except Exception:
            _log.debug("Gtk.ColorDialog unavailable, falling back to ColorChooserDialog", exc_info=True)
            # Fallback für ältere GTK4-Versionen
            dlg = Gtk.ColorChooserDialog(title="Farbe wählen", transient_for=parent)
            dlg.set_rgba(rgba)
            dlg.set_use_alpha(False)

            def _resp(d, response) -> None:
                if response == Gtk.ResponseType.OK:
                    picked = d.get_rgba()
                    entry["color"] = (picked.red, picked.green, picked.blue)
                    entry["color_lbl"].set_markup(
                        f'<span foreground="{_rgb_to_hex(entry["color"])}" size="large">⬤</span>'
                    )
                    self._save_prefs()
                    self._da.queue_draw()
                d.destroy()

            dlg.connect("response", _resp)
            dlg.present()

    # ── Chart-Wisch (Sensor wechseln) ────────────────────────────────────

    def _on_chart_swipe(self, _gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self._on_navigate_pid is None:
            return
        # Vertikaler Wisch muss klar dominieren und mindestens 40 px sein
        if abs(dy) < 40 or abs(dy) <= abs(dx) * 1.2:
            return
        # Hoch (dy < 0) = nächster Sensor, runter (dy > 0) = vorheriger
        direction = 1 if dy < 0 else -1
        try:
            self._on_navigate_pid(direction)
        except Exception:
            _log.exception("scan-chart pid-navigation failed")

    # ── Persistenz ────────────────────────────────────────────────────────

    def _prefs_key(self) -> str | None:
        if self._main_car_id is None or self._main_pid is None:
            return None
        return f"{self._main_car_id}:{self._main_pid}"

    def _save_prefs(self) -> None:
        key = self._prefs_key()
        if key is None:
            return
        val2 = self._value_pids[1] if len(self._value_pids) > 1 else None
        cars_data: list[dict] = []
        for entry in self._compare_cars:
            cars_data.append({
                "car_id": entry["car_id"],
                "color": [float(c) for c in entry["color"]],
                "scan_ts": entry.get("scan_ts"),
            })
        prefs = _prefs_load()
        prefs[key] = {
            "value2": val2,
            "main_scan_ts": self._main_scan_ts,
            "cars": cars_data,
        }
        _prefs_save(prefs)

    def _restore_prefs(self) -> None:
        key = self._prefs_key()
        if key is None:
            return
        prefs = _prefs_load()
        saved = prefs.get(key)
        if not saved:
            return

        # Wert 2 wiederherstellen
        val2 = saved.get("value2")
        if val2:
            for i, (pid, _) in enumerate(self._pid_options):
                if pid == val2:
                    self._val2_dd.set_selected(i + 1)  # löst _on_val2_changed aus
                    self._on_add_val2(None)
                    break

        # Haupt-Scan-Auswahl wiederherstellen
        saved_main_scan = saved.get("main_scan_ts")
        if saved_main_scan and self._main_scan_dd is not None:
            for i, s in enumerate(self._main_scans_meta):
                if str(s["scanned_at"]) == saved_main_scan:
                    self._main_scan_dd.set_selected(i)
                    break

        # Vergleichs-Fahrzeuge wiederherstellen (nur die mit Sensordaten,
        # gespeicherte Farbe und Scan-Auswahl rekonstruieren)
        for car_pref in saved.get("cars", []):
            cid = car_pref.get("car_id")
            if cid is None:
                continue
            if not any(p.get("car_id") == cid for p in self._profiles):
                continue
            if cid not in self._cars_with_data:
                continue
            color_list = car_pref.get("color")
            restored_color: tuple[float, float, float] | None = None
            if isinstance(color_list, list) and len(color_list) >= 3:
                try:
                    restored_color = (
                        float(color_list[0]),
                        float(color_list[1]),
                        float(color_list[2]),
                    )
                except (TypeError, ValueError):
                    restored_color = None
            self._add_compare_car(
                cid,
                restored_color=restored_color,
                restored_scan_ts=car_pref.get("scan_ts"),
            )
        self._refresh_add_car_dropdown()

    # ── Drawing ───────────────────────────────────────────────────────────

    def _scan_id_for_ts(self, scan_ts: str) -> int | None:
        """Return the scan_id whose scanned_at matches scan_ts, or None."""
        for s in self._main_scans_meta:
            if str(s["scanned_at"]) == scan_ts:
                return int(s["id"])
        return None

    def _series_for(
        self,
        stats: dict | None,
        pid: str | None,
        scan_ts: str | None = None,
        scan_id: int | None = None,
    ) -> tuple[list[float], list[str], str]:
        if not stats or not pid or pid not in stats:
            return [], [], ""

        # Intra-scan Modus: konkreter Scan + Zeitreihe vorhanden
        if scan_ts is not None and scan_id is not None:
            intra = (stats[pid].get("intra_series") or {}).get(scan_id)
            if intra:
                vals = [v for _, v in intra]
                ts_labels = [_fmt_rel_s(t) for t, _ in intra]
                unit = _unit_display(stats[pid].get("unit", ""), self._language)
                return vals, ts_labels, unit

        # No intra-series for the selected scan → fall back to the
        # per-scan trend (one point per scan_at across all scans).
        # Previously this code path filtered ``values`` down to the
        # selected scan_ts, which collapsed to a single datapoint and
        # left the chart looking empty for every PID without intra-
        # series. Showing the cross-scan trend is the useful signal:
        # min/max in the overview row already implies there's
        # variation to plot.
        pairs = stats[pid].get("values") or []
        if scan_ts is not None and len(pairs) <= 1:
            # If there's literally only one stored value (or none) we
            # can't show a trend — keep the original snapshot behavior.
            pairs = [(t, v) for t, v in pairs if t == scan_ts]
        vals = [v for _, v in pairs]
        ts = [t for t, _ in pairs]
        unit = _unit_display(stats[pid].get("unit", ""), self._language)
        return vals, ts, unit

    def _draw(self, _da, cr, w: int, h: int) -> None:
        val1_pid = self._value_pids[0] if self._value_pids else None
        val2_pid = self._value_pids[1] if len(self._value_pids) > 1 else None

        main_sid = self._scan_id_for_ts(self._main_scan_ts) if self._main_scan_ts else None

        # Hauptauto + Vergleichsautos als Serien-Gruppen
        groups: list[dict] = []
        main_v1, main_ts, val1_unit = self._series_for(
            self._main_stats, val1_pid, self._main_scan_ts, main_sid)
        main_v2, _main_ts2, val2_unit = self._series_for(
            self._main_stats, val2_pid, self._main_scan_ts, main_sid)
        groups.append({"color": _COLOR_MAIN, "val1": main_v1, "val2": main_v2})

        for entry in self._compare_cars:
            stats = entry.get("stats")
            if not stats:
                continue
            scan_ts = entry.get("scan_ts")
            cmp_sid = entry.get("scan_id_resolved") if scan_ts else None
            if scan_ts and cmp_sid is None:
                # Resolve once and cache
                for s in (entry.get("scans_meta") or []):
                    if str(s["scanned_at"]) == scan_ts:
                        cmp_sid = int(s["id"])
                        entry["scan_id_resolved"] = cmp_sid
                        break
            v1, _ts1, u1 = self._series_for(stats, val1_pid, scan_ts, cmp_sid)
            v2, _ts2, u2 = self._series_for(stats, val2_pid, scan_ts, cmp_sid)
            if not val1_unit and u1:
                val1_unit = u1
            if not val2_unit and u2:
                val2_unit = u2
            groups.append({"color": entry["color"], "val1": v1, "val2": v2})

        bg_rgb = _lookup_card_bg(self._da)
        _draw_chart(
            cr, w, h, groups,
            val1_unit=val1_unit,
            val2_unit=val2_unit,
            has_val2=val2_pid is not None,
            main_ts=main_ts,
            bg_rgb=bg_rgb,
        )
