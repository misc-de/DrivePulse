"""Scan history chart sub-page with multi-car / dual-value comparison."""
from __future__ import annotations

import json
import math
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from drivepulse_app.cars.metadata import _parse_profile_pid_key, _unit_display
from drivepulse_app.common import LOG_DIR
from drivepulse_app.diagnostics import atomic_write_text, get_logger
from drivepulse_app.ui.draw_helpers import _txt

_log = get_logger(__name__)
_PREFS_FILE = LOG_DIR / "scan_chart_prefs.json"


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

_CHART_H = 260
_PAD_L = 48
_PAD_R = 16
_PAD_R_VAL2 = 56
_PAD_T = 26
_PAD_B = 36

_COLOR_MAIN = (0.35, 0.60, 1.00)  # Hauptfahrzeug = blau
_DEFAULT_COMPARE_COLORS: list[tuple[float, float, float]] = [
    (1.00, 0.60, 0.20),  # orange
    (0.30, 0.80, 0.45),  # grün
    (0.75, 0.45, 0.95),  # violett
    (1.00, 0.85, 0.30),  # gelb
    (0.95, 0.40, 0.50),  # rosa
    (0.40, 0.85, 0.85),  # türkis
]


def _fmt(v: float) -> str:
    if abs(v) >= 100:
        return f"{v:.0f}"
    if abs(v) >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"


def _fmt_ts(ts: str) -> str:
    return ts[:10] if len(ts) >= 10 else ts


def _rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    r, g, b = (max(0, min(255, int(round(c * 255)))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _safe_pids_count(scan_meta) -> int:
    try:
        return int(scan_meta["pids_count"] or 0)
    except (KeyError, TypeError, ValueError):
        return 0


def _fmt_scan_label(ts: str) -> str:
    # ISO 8601 → "YYYY-MM-DD HH:MM"
    if len(ts) >= 16:
        return ts[:16].replace("T", " ")
    return ts


def _lookup_card_bg(widget) -> tuple[float, float, float] | None:
    try:
        ok, rgba = widget.get_style_context().lookup_color("card_bg_color")
    except Exception:
        return None
    if not ok:
        return None
    return (rgba.red, rgba.green, rgba.blue)


# ---------------------------------------------------------------------------
# Background stat computation (reusable for any car_id)
# ---------------------------------------------------------------------------

def _compute_stats_for_car(db, car_id: int) -> dict:
    from drivepulse_app.cars.metadata import _parse_profile_pid_key
    stats: dict = {}
    raw_values: dict = {}
    try:
        scans = db.list_scans_for_car(car_id)
    except Exception:
        return {}
    for scan_meta in scans:
        ts_str = str(scan_meta["scanned_at"] or "")
        try:
            data = db.get_scan_data(int(scan_meta["id"]))
        except Exception:
            continue
        for raw_key, raw_val in (data.get("live_data") or {}).items():
            pid = _parse_profile_pid_key(raw_key)
            if not pid:
                continue
            v = raw_val.get("value") if isinstance(raw_val, dict) else raw_val
            unit = str(raw_val.get("unit", "")) if isinstance(raw_val, dict) else ""
            try:
                num = float(v)
            except (TypeError, ValueError):
                continue
            if pid not in stats:
                stats[pid] = {"min": num, "max": num, "sum": num, "count": 1, "unit": unit}
            else:
                s = stats[pid]
                s["min"] = min(s["min"], num)
                s["max"] = max(s["max"], num)
                s["sum"] += num
                s["count"] += 1
            raw_values.setdefault(pid, []).append((ts_str, num))
    for pid, s in stats.items():
        s["avg"] = s["sum"] / s["count"]
        s["values"] = sorted(raw_values.get(pid) or [], key=lambda t: t[0])
    return stats


# ---------------------------------------------------------------------------
# Chart drawing — multi-series with up to two value axes
# ---------------------------------------------------------------------------

def _draw_chart(
    cr,
    w: int,
    h: int,
    series_groups: list[dict],
    val1_unit: str,
    val2_unit: str,
    has_val2: bool,
    main_ts: list[str] | None,
    bg_rgb: tuple[float, float, float] | None = None,
) -> None:
    """
    series_groups: each entry is {
        'color': (r,g,b),
        'val1': list[float] | None,
        'val2': list[float] | None,
    }
    """
    pl = _PAD_L
    pr = _PAD_R_VAL2 if has_val2 else _PAD_R
    pt = _PAD_T
    plot_w = max(1.0, float(w - pl - pr))
    plot_h = max(1.0, float(h - pt - _PAD_B))

    try:
        dark = Adw.StyleManager.get_default().get_dark()
    except Exception:
        dark = True
    fg = (1.0, 1.0, 1.0) if dark else (0.0, 0.0, 0.0)
    axis_rgba = (*fg, 0.55)
    grid_rgba = (*fg, 0.16)
    lbl_rgba  = (*fg, 0.95)

    if not dark and bg_rgb is not None:
        cr.set_source_rgb(*bg_rgb)
        cr.rectangle(0, 0, w, h)
        cr.fill()

    # Wertebereich pro Achse über alle Serien
    val1_all: list[float] = []
    val2_all: list[float] = []
    for g in series_groups:
        if g.get("val1"):
            val1_all.extend(g["val1"])
        if g.get("val2"):
            val2_all.extend(g["val2"])

    v1_mn, v1_mx = (min(val1_all), max(val1_all)) if val1_all else (0.0, 1.0)
    v2_mn, v2_mx = (min(val2_all), max(val2_all)) if val2_all else (0.0, 1.0)
    v1_same = abs(v1_mx - v1_mn) <= 1e-9
    v2_same = abs(v2_mx - v2_mn) <= 1e-9

    # Gitterlinien (1/3, 2/3) + L-Achse
    tick_fracs = (0.0, 1.0 / 3.0, 2.0 / 3.0, 1.0)
    cr.set_source_rgba(*grid_rgba)
    cr.set_line_width(1.0)
    cr.set_dash([2.0, 3.0], 0)
    for f in tick_fracs[1:-1]:
        ty = pt + plot_h * (1.0 - f)
        cr.move_to(pl, ty)
        cr.line_to(pl + plot_w, ty)
        cr.stroke()
    cr.set_dash([], 0)

    cr.set_source_rgba(*axis_rgba)
    cr.set_line_width(1.0)
    cr.move_to(pl, pt)
    cr.line_to(pl, pt + plot_h)
    cr.line_to(pl + plot_w, pt + plot_h)
    cr.stroke()

    # Linke Y-Achse: Wert 1
    if val1_all:
        for f in tick_fracs:
            val = v1_mn + f * (v1_mx - v1_mn)
            ty = pt + plot_h * (1.0 - f)
            _txt(cr, _fmt(val), pl - 5, ty, 9.5, rgba=lbl_rgba, align="right")
            if v1_same:
                break
    if val1_unit:
        _txt(cr, val1_unit, pl, pt - 10, 9.0, rgba=lbl_rgba, align="left")

    # Rechte Y-Achse: Wert 2
    if has_val2:
        cr.set_source_rgba(*axis_rgba)
        cr.set_line_width(1.0)
        cr.move_to(pl + plot_w, pt)
        cr.line_to(pl + plot_w, pt + plot_h)
        cr.stroke()
        if val2_all:
            for f in tick_fracs:
                val = v2_mn + f * (v2_mx - v2_mn)
                ty = pt + plot_h * (1.0 - f)
                _txt(cr, _fmt(val), pl + plot_w + 5, ty, 9.5, rgba=lbl_rgba, align="left")
                if v2_same:
                    break
        if val2_unit:
            _txt(cr, val2_unit, pl + plot_w, pt - 10, 9.0, rgba=lbl_rgba, align="right")

    # X-Achse: erster/letzter Timestamp der Hauptserie (sofern vorhanden)
    if main_ts:
        first_ts = _fmt_ts(main_ts[0])
        last_ts = _fmt_ts(main_ts[-1])
        ty_x = pt + plot_h + 14
        if first_ts == last_ts:
            _txt(cr, first_ts, pl + plot_w / 2, ty_x, 9.5, rgba=lbl_rgba, align="center")
        else:
            _txt(cr, first_ts, pl, ty_x, 9.5, rgba=lbl_rgba, align="left")
            _txt(cr, last_ts, pl + plot_w, ty_x, 9.5, rgba=lbl_rgba, align="right")

    def _draw_line(
        vals: list[float],
        mn: float,
        mx: float,
        color: tuple[float, float, float],
        dashed: bool,
    ) -> None:
        n = len(vals)
        if n == 0:
            return
        rng = mx - mn if abs(mx - mn) > 1e-9 else 1.0
        r, g, b = color

        def xp(i: int) -> float:
            return pl + plot_w / 2 if n == 1 else pl + i * plot_w / (n - 1)

        def yp(v: float) -> float:
            return pt + plot_h * (1.0 - (v - mn) / rng)

        if n > 1:
            cr.set_source_rgba(r, g, b, 0.50)
            cr.set_line_width(1.6)
            if dashed:
                cr.set_dash([5.0, 4.0], 0)
            for i, v in enumerate(vals):
                cr.move_to(xp(i), yp(v)) if i == 0 else cr.line_to(xp(i), yp(v))
            cr.stroke()
            if dashed:
                cr.set_dash([], 0)

        cr.set_source_rgba(r, g, b, 0.92)
        dot_r = 2.6
        for i, v in enumerate(vals):
            cr.arc(xp(i), yp(v), dot_r, 0, 2 * math.pi)
            cr.fill()

    for g in series_groups:
        color = g.get("color") or _COLOR_MAIN
        v1 = g.get("val1") or []
        v2 = g.get("val2") or []
        if v1:
            _draw_line(v1, v1_mn, v1_mx, color, dashed=False)
        if v2:
            _draw_line(v2, v2_mn, v2_mx, color, dashed=True)


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
            if _cid is None or _cid == main_car_id:
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

        main_pid_label = pid_labels.get(main_pid, main_pid)

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

        # Wert 1 row — auf den geöffneten PID fixiert (nicht änderbar)
        self._val1_row = Adw.ActionRow()
        self._val1_row.set_title("Wert 1")
        self._val1_dd = self._make_pid_dropdown(self._main_pid)
        self._val1_dd.set_sensitive(False)
        self._val1_dd.set_tooltip_text("Wert 1 ist fest auf den geöffneten Sensor gesetzt")
        self._val1_row.add_suffix(self._val1_dd)
        self._val1_add_btn = Gtk.Button.new_from_icon_name("list-add-symbolic")
        self._val1_add_btn.add_css_class("flat")
        self._val1_add_btn.add_css_class("circular")
        self._val1_add_btn.set_valign(Gtk.Align.CENTER)
        self._val1_add_btn.set_tooltip_text("Zweiten Wert hinzufügen")
        self._val1_add_btn.connect("clicked", self._on_add_val2)
        self._val1_row.add_suffix(self._val1_add_btn)
        self._values_list.append(self._val1_row)

        # Wert 2 row (initial versteckt)
        self._val2_row = Adw.ActionRow()
        self._val2_row.set_title("Wert 2")
        self._val2_dd = self._make_pid_dropdown(None)
        self._val2_dd.connect("notify::selected", self._on_val2_changed)
        self._val2_row.add_suffix(self._val2_dd)
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

        # Hauptfahrzeug-Zeile (immer da, nicht entfernbar)
        main_car_name = self._lookup_car_name(main_car_id) if main_car_id is not None else "Hauptfahrzeug"
        self._main_car_row = Adw.ActionRow()
        self._main_car_row.set_title(main_car_name)
        main_dot = Gtk.Label()
        main_dot.set_markup(
            f'<span foreground="{_rgb_to_hex(_COLOR_MAIN)}" size="large">⬤</span>'
        )
        main_dot.set_valign(Gtk.Align.CENTER)
        self._main_car_row.add_prefix(main_dot)

        # Scan-Auswahl fürs Hauptauto: "Neuester"-Sentinel + alle Scans mit
        # Sensordaten, neuester zuerst.
        self._main_scans_meta: list = []
        self._main_scan_ts: str | None = None  # None = "Neuester"
        if self._db is not None and main_car_id is not None:
            try:
                _ms = list(self._db.list_scans_for_car(main_car_id))
                self._main_scans_meta = [s for s in _ms if _safe_pids_count(s) > 0]
            except Exception:
                self._main_scans_meta = []
        self._main_scan_dd: Gtk.DropDown | None = None
        if self._main_scans_meta:
            main_scan_sl = Gtk.StringList()
            main_scan_sl.append("Alle Scans")
            for s in self._main_scans_meta:
                main_scan_sl.append(_fmt_scan_label(str(s["scanned_at"])))
            self._main_scan_dd = Gtk.DropDown(model=main_scan_sl)
            self._main_scan_dd.set_valign(Gtk.Align.CENTER)
            self._main_scan_dd.set_selected(0)
            self._main_scan_dd.connect("notify::selected", self._on_main_scan_changed)
            self._main_car_row.add_suffix(self._main_scan_dd)

        self._cars_list.append(self._main_car_row)

        # "+ Fahrzeug"-Zeile mit Dropdown-Selektor
        self._add_car_row = Adw.ActionRow()
        self._add_car_row.set_title("Fahrzeug hinzufügen")
        self._add_car_dd = Gtk.DropDown()
        self._add_car_dd.set_valign(Gtk.Align.CENTER)
        self._refresh_add_car_dropdown()
        self._add_car_dd.connect("notify::selected", self._on_add_car_selected)
        self._add_car_row.add_suffix(self._add_car_dd)
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

    def _car_has_pid_values(self, car_id: int, pid: str) -> bool:
        if self._db is None:
            return False
        try:
            scans = self._db.list_scans_for_car(car_id)
        except Exception:
            return False
        for scan_meta in scans:
            try:
                data = self._db.get_scan_data(int(scan_meta["id"]))
            except Exception:
                continue
            for raw_key, raw_val in (data.get("live_data") or {}).items():
                if _parse_profile_pid_key(raw_key) != pid:
                    continue
                v = raw_val.get("value") if isinstance(raw_val, dict) else raw_val
                try:
                    float(v)
                except (TypeError, ValueError):
                    continue
                return True
        return False

    def _refresh_add_car_dropdown(self) -> None:
        used = {self._main_car_id} | {c["car_id"] for c in self._compare_cars}
        self._add_car_candidates: list[int] = []
        sl = Gtk.StringList()
        sl.append("—")
        for p in self._profiles:
            cid = p.get("car_id")
            if cid is None or cid in used or cid not in self._cars_with_data:
                continue
            sl.append(self._lookup_car_name(cid))
            self._add_car_candidates.append(cid)
        self._add_car_dd.set_model(sl)
        self._add_car_dd.set_selected(0)
        # Verstecken, wenn keine Kandidaten mehr da sind
        self._add_car_row.set_visible(len(self._add_car_candidates) > 0)

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
        else:
            if len(self._value_pids) >= 2:
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
        sel = dd.get_selected()
        if sel == 0:
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

        row = Adw.ActionRow()
        row.set_title(entry["name"])

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
        row.add_prefix(color_btn)

        suffix_box = Gtk.Box(spacing=4)
        suffix_box.set_valign(Gtk.Align.CENTER)

        spinner = Gtk.Spinner()
        spinner.start()
        suffix_box.append(spinner)
        row.add_suffix(suffix_box)

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
        except Exception:
            scans_meta = []
        scans_meta = [s for s in scans_meta if _safe_pids_count(s) > 0]
        entry["scans_meta"] = scans_meta
        # Default: "Neuester"-Sentinel (None). Wenn Persistenz einen konkreten
        # Scan kennt und dieser noch existiert, wird er stattdessen gewählt.
        restored = entry.pop("_restored_scan_ts", None)
        if restored and any(str(s["scanned_at"]) == restored for s in scans_meta):
            entry["scan_ts"] = restored
        else:
            entry["scan_ts"] = None

        box = entry["suffix_box"]
        child = box.get_first_child()
        while child is not None:
            box.remove(child)
            child = box.get_first_child()

        # Scan-Dropdown: "Neuester" + alle Scans mit Sensordaten
        if scans_meta:
            scan_sl = Gtk.StringList()
            scan_sl.append("Alle Scans")
            for s in scans_meta:
                scan_sl.append(_fmt_scan_label(str(s["scanned_at"])))
            scan_dd = Gtk.DropDown(model=scan_sl)
            scan_dd.set_valign(Gtk.Align.CENTER)
            target_idx = 0  # "Neuester" (Sentinel = None)
            if entry.get("scan_ts"):
                for i, s in enumerate(scans_meta):
                    if str(s["scanned_at"]) == entry["scan_ts"]:
                        target_idx = i + 1
                        break
            scan_dd.set_selected(target_idx)
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
        self._save_prefs()
        self._da.queue_draw()
        return False

    def _on_compare_scan_changed(self, dd: Gtk.DropDown, _prop, entry: dict) -> None:
        sel = dd.get_selected()
        scans = entry.get("scans_meta") or []
        if sel == 0:
            entry["scan_ts"] = None  # "Neuester"
        else:
            idx = sel - 1
            entry["scan_ts"] = str(scans[idx]["scanned_at"]) if 0 <= idx < len(scans) else None
        self._save_prefs()
        self._da.queue_draw()

    def _on_main_scan_changed(self, dd: Gtk.DropDown, _prop) -> None:
        sel = dd.get_selected()
        if sel == 0:
            self._main_scan_ts = None  # "Neuester"
        else:
            idx = sel - 1
            if 0 <= idx < len(self._main_scans_meta):
                self._main_scan_ts = str(self._main_scans_meta[idx]["scanned_at"])
            else:
                self._main_scan_ts = None
        self._save_prefs()
        self._da.queue_draw()

    def _on_remove_car(self, _btn, entry: dict) -> None:
        if entry not in self._compare_cars:
            return
        self._compare_cars.remove(entry)
        self._cars_list.remove(entry["row"])
        self._refresh_add_car_dropdown()
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
                    self._main_scan_dd.set_selected(i + 1)
                    break

        # Vergleichs-Fahrzeuge wiederherstellen (nur die mit Sensordaten,
        # gespeicherte Farbe und Scan-Auswahl rekonstruieren)
        for car_pref in saved.get("cars", []):
            cid = car_pref.get("car_id")
            if cid is None or cid == self._main_car_id:
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

    def _series_for(
        self,
        stats: dict | None,
        pid: str | None,
        scan_ts: str | None = None,
    ) -> tuple[list[float], list[str], str]:
        if not stats or not pid or pid not in stats:
            return [], [], ""
        pairs = stats[pid].get("values") or []
        # scan_ts None → "Alle Scans": komplette Verlaufslinie aus allen
        # Datapoints (Liste ist nach Timestamp ASC sortiert).
        # Konkreter Timestamp → exakter Filter auf nur diesen einen Scan.
        if scan_ts is not None:
            pairs = [(t, v) for t, v in pairs if t == scan_ts]
        vals = [v for _, v in pairs]
        ts = [t for t, _ in pairs]
        unit = _unit_display(stats[pid].get("unit", ""), self._language)
        return vals, ts, unit

    def _draw(self, _da, cr, w: int, h: int) -> None:
        val1_pid = self._value_pids[0] if self._value_pids else None
        val2_pid = self._value_pids[1] if len(self._value_pids) > 1 else None

        # Hauptauto + Vergleichsautos als Serien-Gruppen
        groups: list[dict] = []
        main_v1, main_ts, val1_unit = self._series_for(self._main_stats, val1_pid, self._main_scan_ts)
        main_v2, _main_ts2, val2_unit = self._series_for(self._main_stats, val2_pid, self._main_scan_ts)
        groups.append({"color": _COLOR_MAIN, "val1": main_v1, "val2": main_v2})

        for entry in self._compare_cars:
            stats = entry.get("stats")
            if not stats:
                continue
            scan_ts = entry.get("scan_ts")
            v1, _ts1, u1 = self._series_for(stats, val1_pid, scan_ts)
            v2, _ts2, u2 = self._series_for(stats, val2_pid, scan_ts)
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
