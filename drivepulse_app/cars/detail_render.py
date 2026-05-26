"""Detail data and value rendering for the Cars page."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango

from drivepulse_app.cars.metadata import (
    _SPECIAL_BRAND,
    _SPECIAL_CAL,
    _SPECIAL_CVN,
    _SPECIAL_DTC,
    _SPECIAL_PENDING,
    _SPECIAL_PROTO,
    _SPECIAL_SCAN_DATE,
    _SPECIAL_VIN,
    CATEGORIES,
    LIVE_KEY_TO_PID,
    VIN_DATA_SPECIAL_KEYS,
    _extract_inner_string,
    _format_value_unit,
    _parse_profile_pid_key,
    _unit_display,
    localize_vehicle_type,
)
from drivepulse_app.common import _translate
from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)

_PID_TO_LIVE_KEY: dict[str, str] = {pid: key for key, pid in LIVE_KEY_TO_PID.items()}

# OBD-Sensor-Gruppen unter „OBD Daten" — alle Kategorien ab `engine` in
# CATEGORIES.  Diese werden ausgegraut, wenn das geladene Auto keinen
# echten (≠ None, ≠ 0) Wert für irgendeine PID der Gruppe hat.
_OBD_SENSOR_CATEGORIES: frozenset[str] = frozenset({
    "engine", "temperatures", "throttle", "mixture", "fuel", "drive",
})
from drivepulse_app.cars.scan_widgets import _dtc_parts, _format_scan_date, _format_scan_date_stack
from drivepulse_app.chart.scan_chart import ScanChartContent


def _format_dtc(entry: Any) -> str:
    code, desc = _dtc_parts(entry)
    return f"{code}: {desc}" if desc else code


def _chart_ordered_pids(all_stats: dict) -> list[str]:
    """PIDs in CATEGORIES-Reihenfolge, gefiltert auf solche, für die das
    Hauptauto bereits Werte aufgezeichnet hat. Reihenfolge ist die im UI
    sichtbare und entscheidet damit über „nächster/voriger Sensor"-Wisch."""
    ordered: list[str] = []
    for _ck, _cnk, _ic, items in CATEGORIES:
        for pid_key, _lk in items:
            if pid_key.startswith("__"):
                continue
            s = all_stats.get(pid_key)
            if s and (s.get("values") or []):
                ordered.append(pid_key)
    return ordered


class CarsDetailRenderMixin:
    def _category_has_sensor_values(self, cat_key: str) -> bool:
        cat = next((c for c in CATEGORIES if c[0] == cat_key), None)
        if cat is None:
            return False
        for pid_key, _ in cat[3]:
            if pid_key.startswith("__"):
                continue
            stats = self._scan_pid_stats.get(pid_key)
            if not stats:
                continue
            for entry in stats.get("values") or []:
                num = entry[1] if isinstance(entry, tuple) else entry
                if num is not None and num != 0:
                    return True
        return False

    def _update_sensor_category_greying(self) -> None:
        is_live = self._selected_source == self.LIVE_ID
        for row in getattr(self, "_cat_rows", []):
            cat_key = getattr(row, "cat_key", "")
            if cat_key not in _OBD_SENSOR_CATEGORIES:
                continue
            has_values = is_live or self._category_has_sensor_values(cat_key)
            row.set_sensitive(has_values)
            for w in (getattr(row, "cat_label_widget", None),
                      getattr(row, "cat_icon_widget", None)):
                if w is None:
                    continue
                if has_values:
                    w.remove_css_class("dim-label")
                else:
                    w.add_css_class("dim-label")
            icon = getattr(row, "cat_icon_widget", None)
            if icon is not None:
                icon.set_opacity(1.0 if has_values else 0.35)

    def _current_data(self) -> tuple[dict[str, Any], str]:
        if self._selected_source == self.LIVE_ID:
            d: dict[str, Any] = {}
            for live_key, pid in LIVE_KEY_TO_PID.items():
                if live_key in self._latest_live:
                    d[pid] = self._latest_live[live_key]
            for special_key, identity_key in (
                (_SPECIAL_VIN, "VIN"),
                (_SPECIAL_CAL, "CALIBRATION_ID"),
                (_SPECIAL_CVN, "CVN"),
                (_SPECIAL_PROTO, "protocol"),
            ):
                if self._live_identity.get(identity_key):
                    d[special_key] = self._live_identity[identity_key]
            return d, _translate(self.language, "cars.live.title")
        if (self._selected_scan_id is not None
                and self.db is not None
                and self._selected_car_id is not None):
            try:
                data = self.db.get_scan_data(self._selected_scan_id)
                scans = self.db.list_scans_for_car(self._selected_car_id)
                scan_meta = next(
                    (s for s in scans if int(s["id"]) == self._selected_scan_id), None
                )
                if scan_meta:
                    ts = self._parse_ts(scan_meta["scanned_at"])
                    label = ts.strftime("%d.%m.%Y  %H:%M") if ts else str(self._selected_scan_id)
                    # Scans only carry live_data/DTCs/vehicle_info — VIN-decoded
                    # master data lives on the car. Merge it in so switching
                    # back to the vehicle category after viewing a scan keeps
                    # the extended master data visible.
                    if not (data.get("vin_data") or {}):
                        car_entry = next(
                            (e for e in self._profiles
                             if e.get("car_id") == self._selected_car_id),
                            None,
                        )
                        if car_entry:
                            car_vin_data = (car_entry.get("data") or {}).get("vin_data") or {}
                            if car_vin_data:
                                data = {**data, "vin_data": car_vin_data}
                    return self._flatten_profile(data), label
            except (sqlite3.Error, KeyError, ValueError, TypeError):
                log.debug("Could not render selected scan_id=%s in detail view", self._selected_scan_id, exc_info=True)
        for entry in self._profiles:
            if str(entry["path"]) == self._selected_source:
                raw_ts = entry.get("latest_scan_at")
                ts = self._parse_ts(raw_ts) if raw_ts else None
                label = ts.strftime("%d.%m.%Y  %H:%M") if ts else (entry.get("scan_label") or "—")
                # Ensure the flatten step has a scanned_at to format — the
                # blob from get_scan_data sometimes lacks it, but the entry
                # always knows when the most recent scan happened.
                data_blob = entry["data"]
                if raw_ts and not (data_blob or {}).get("scanned_at"):
                    data_blob = {**(data_blob or {}), "scanned_at": raw_ts}
                return self._flatten_profile(data_blob), label
        return {}, "—"

    def _flatten_profile(self, data: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for raw_key, raw_val in (data.get("live_data") or {}).items():
            pid = _parse_profile_pid_key(raw_key)
            if pid:
                out[pid] = raw_val
        info = data.get("vehicle_info") or {}
        if info.get("VIN"):
            out[_SPECIAL_VIN] = _extract_inner_string(info["VIN"])
        if info.get("CALIBRATION_ID"):
            out[_SPECIAL_CAL] = _extract_inner_string(info["CALIBRATION_ID"])
        if info.get("CVN"):
            out[_SPECIAL_CVN] = _extract_inner_string(info["CVN"])
        if data.get("protocol"):
            out[_SPECIAL_PROTO] = str(data["protocol"])
        if data.get("scanned_at"):
            out[_SPECIAL_SCAN_DATE] = _format_scan_date(data["scanned_at"])
            stack = _format_scan_date_stack(data["scanned_at"], self.language)
            if stack is not None:
                out["__scan_date_stack__"] = stack
        dtcs = data.get("dtcs") or []
        pending = data.get("pending_dtcs") or []
        none_text = _translate(self.language, "cars.dtc.none")
        # Single-line summary string kept around for callers that only need
        # the rendered text (e.g. fallback paths); the detail renderer
        # itself consumes the raw lists below to build a proper table.
        out[_SPECIAL_DTC] = none_text if not dtcs else "  ".join(_format_dtc(d) for d in dtcs)
        out[_SPECIAL_PENDING] = none_text if not pending else "  ".join(_format_dtc(d) for d in pending)
        out["__dtcs_list__"] = list(dtcs)
        out["__pending_dtcs_list__"] = list(pending)
        # Convenience flag for the sidebar highlight: yellow-tint the
        # diagnostics row when the loaded scan actually has DTCs.
        out["__has_dtc__"] = bool(dtcs) or bool(pending)
        for field_key, special_key in VIN_DATA_SPECIAL_KEYS.items():
            val = (data.get("vin_data") or {}).get(field_key)
            if val:
                if field_key == "vehicle_type":
                    val = localize_vehicle_type(str(val), self.language)
                out[special_key] = str(val)
        return out

    def _format_entry(self, pid_key: str, raw: Any) -> tuple[str, bool]:
        if pid_key == _SPECIAL_VIN and raw:
            return (_extract_inner_string(raw), False)
        if pid_key == _SPECIAL_BRAND and raw:
            return (str(raw), False)
        if pid_key.startswith("__"):
            if raw is None or raw == "":
                return ("—", True)
            return (_extract_inner_string(raw) if isinstance(raw, str) else str(raw), False)
        if raw is None:
            return ("—", True)
        text = _format_value_unit(raw)
        return (text, text == "—")

    def _render_detail(self) -> None:
        # Restore scroll child to value_list if photos grid was shown
        _scroll = getattr(self, "_value_scroll", None)
        if _scroll is not None and _scroll.get_child() is not self.value_list:
            _scroll.set_child(self.value_list)

        while True:
            child = self.value_list.get_first_child()
            if child is None:
                break
            self.value_list.remove(child)

        cat_meta = next((c for c in CATEGORIES if c[0] == self._selected_category), CATEGORIES[0])
        cat_key, cat_name_key, _icon_name, items = cat_meta
        self.content_title.set_text(_translate(self.language, cat_name_key))

        is_live = self._selected_source == self.LIVE_ID
        data, _source_label = self._current_data()

        # Sidebar header row: loaded scan's timestamp. On mobile render as
        # a centred three-line stack (year dimmed / MM.DD / HH:MM); on
        # desktop keep the compact single-line "dd.mm.yyyy HH:MM" so the
        # sidebar stays narrow. Hidden for the live view.
        scan_row = getattr(self, "_scan_date_row", None)
        scan_lbl = getattr(self, "_scan_date_label", None)
        if scan_row is not None and scan_lbl is not None:
            stack = data.get("__scan_date_stack__")
            scan_date = data.get(_SPECIAL_SCAN_DATE)
            has_date = (
                not is_live
                and scan_date
                and scan_date != "—"
            )
            if has_date:
                mobile = self._split_view.get_collapsed()
                if mobile and stack:
                    scan_lbl.set_justify(Gtk.Justification.CENTER)
                    scan_lbl.set_xalign(0.5)
                    scan_lbl.set_halign(Gtk.Align.CENTER)
                    scan_lbl.set_markup(stack)
                else:
                    scan_lbl.set_xalign(0.0)
                    scan_lbl.set_halign(Gtk.Align.START)
                    scan_lbl.set_use_markup(False)
                    scan_lbl.set_text(scan_date)
                scan_row.set_visible(True)
            else:
                scan_row.set_visible(False)

        # Yellow-tint the "Diagnose" category row only when the loaded scan
        # actually carries DTCs. The flag is set in _flatten_profile.
        has_dtc = bool(data.get("__has_dtc__"))
        for _row in getattr(self, "_cat_rows", []):
            if getattr(_row, "cat_key", None) != "diagnostics":
                continue
            _lbl = getattr(_row, "cat_label_widget", None)
            _icon = getattr(_row, "cat_icon_widget", None)
            for _w in (_lbl, _icon):
                if _w is None:
                    continue
                if has_dtc:
                    _w.add_css_class("warning")
                else:
                    _w.remove_css_class("warning")
            break

        self._update_sensor_category_greying()

        if cat_key == "trips":
            self._render_trips_into_value_list()
            return

        if cat_key == "scans":
            self._render_scans_into_value_list()
            return

        if cat_key == "stopwatch_runs":
            self._render_stopwatch_runs_into_value_list()
            return

        if cat_key == "photos":
            self._render_photos_into_view()
            return

        _vin_data_keys = set(VIN_DATA_SPECIAL_KEYS.values())
        _lang = getattr(self, "language", "de")
        _pid_labels: dict[str, str] = {
            pk: _translate(_lang, lk)
            for _, _, _, _items in CATEGORIES
            for pk, lk in _items
            if not pk.startswith("__")
        }

        # Inject car-level VIN and brand from DB profile into data dict
        _car_entry: dict[str, Any] | None = None
        if cat_key == "vehicle" and not is_live and self._selected_car_id is not None:
            _car_entry = next(
                (e for e in self._profiles if e.get("car_id") == self._selected_car_id),
                None,
            )
            if _car_entry:
                data = dict(data)
                if _car_entry.get("vin"):
                    data[_SPECIAL_VIN] = _car_entry["vin"]
                if _car_entry.get("brand"):
                    data[_SPECIAL_BRAND] = _car_entry["brand"]

        for pid_key, label_key in items:
            raw = data.get(pid_key)
            value_text, is_unknown = self._format_entry(pid_key, raw)
            if is_unknown and pid_key in _vin_data_keys:
                continue
            # Im Scan-Modus nur PIDs anzeigen, für die tatsächlich Sensordaten
            # vorliegen. Section-Header (__-Prefix) bleiben unberührt; im
            # Live-Modus wird ebenfalls nicht gefiltert, da Werte dort
            # in Echtzeit ankommen.
            if not is_live and not pid_key.startswith("__"):
                _scan_stats = self._scan_pid_stats.get(pid_key)
                if not _scan_stats or not (_scan_stats.get("values") or []):
                    continue
            label = _translate(self.language, label_key)
            if pid_key == _SPECIAL_VIN and not is_live and self._selected_car_id is not None:
                row = self._make_editable_field_row(pid_key, label, value_text, is_unknown)
                self.value_list.append(row)
                continue
            if pid_key in (_SPECIAL_DTC, _SPECIAL_PENDING):
                src_key = "__dtcs_list__" if pid_key == _SPECIAL_DTC else "__pending_dtcs_list__"
                entries = data.get(src_key) or []
                for r in self._make_dtc_table_rows(label, entries):
                    self.value_list.append(r)
                # Show the "Clear fault memory" button right after the
                # stored-faults table when in live mode, faults are
                # present (stored OR pending) and the host has wired up
                # the OBD Mode-04 callback.
                if (
                    pid_key == _SPECIAL_DTC
                    and is_live
                    and getattr(self, "on_clear_dtcs", None) is not None
                    and (
                        (data.get("__dtcs_list__") or [])
                        or (data.get("__pending_dtcs_list__") or [])
                    )
                ):
                    self.value_list.append(self._make_dtc_clear_button_row())
                continue
            if not pid_key.startswith("__"):
                if is_live:
                    live_key = _PID_TO_LIVE_KEY.get(pid_key)
                    stats = self._live_session_stats.get(live_key) if live_key else None
                    on_click = None
                else:
                    stats = self._scan_pid_stats.get(pid_key)
                    # Wenn der Scan-Blob kein rohes live_data für diese PID hat,
                    # fällt _format_entry auf "—" zurück. Dann den aggregierten
                    # Mittelwert als Wert einsetzen — ohne ⌀-Präfix, das Symbol
                    # ist der Punkt der vorigen Iteration, der entfernt werden
                    # sollte.
                    if is_unknown and stats and "avg" in stats:
                        avg = stats["avg"]
                        unit = _unit_display(stats.get("unit", ""), _lang)
                        if abs(avg) >= 100:
                            avg_str = f"{avg:.0f}"
                        elif abs(avg) >= 10:
                            avg_str = f"{avg:.1f}"
                        else:
                            avg_str = f"{avg:.2f}"
                        value_text = f"{avg_str} {unit}".strip()
                        is_unknown = False
                    # Chart only makes sense when there is actual
                    # variation to plot. A single datapoint (min == max
                    # AND no intra-series with movement) gives just a
                    # dot — show only the value, no clickable chart.
                    has_data = bool(
                        (stats or {}).get("values")
                        or (stats or {}).get("intra_series")
                    )
                    has_variation = False
                    if has_data:
                        mn = (stats or {}).get("min")
                        mx = (stats or {}).get("max")
                        if mn is not None and mx is not None and mn != mx:
                            has_variation = True
                        else:
                            has_variation = any(
                                len(pts) > 1
                                for pts in ((stats or {}).get("intra_series") or {}).values()
                            )
                    if has_variation:
                        def on_click(_lbl=label, _pk=pid_key, _st=self._scan_pid_stats, _pl=_pid_labels, _lg=_lang):
                            return self._push_scan_chart(_lbl, _pk, _st, _pl, _lg)
                    else:
                        on_click = None
                row = self._make_live_stats_row(label, value_text, stats, is_unknown, on_click)
            else:
                row = self._make_stacked_row(label, value_text, is_unknown)
            self.value_list.append(row)

    def _make_dtc_table_rows(
        self, section_label: str, entries: list[Any]
    ) -> list[Gtk.ListBoxRow]:
        """Render a DTC / pending-DTC section as a header row plus one
        ActionRow per fault code (code as title, description as subtitle).
        Falls back to a single dim "no faults" row when the list is empty.
        Entries are normalised via _dtc_parts, so dict / JSON-string /
        "CODE: desc" / bytes inputs all render the same way."""
        rows: list[Gtk.ListBoxRow] = []

        header = Gtk.ListBoxRow()
        header.set_activatable(False)
        header.set_selectable(False)
        h_lbl = Gtk.Label(label=section_label, xalign=0.0)
        h_lbl.add_css_class("caption-heading")
        h_lbl.set_margin_top(10)
        h_lbl.set_margin_bottom(4)
        h_lbl.set_margin_start(14)
        h_lbl.set_margin_end(14)
        header.set_child(h_lbl)
        rows.append(header)

        if not entries:
            empty_row = Adw.ActionRow()
            empty_row.set_title(_translate(self.language, "cars.dtc.none"))
            empty_row.add_css_class("dim-label")
            empty_row.set_activatable(False)
            rows.append(empty_row)
            return rows

        for entry in entries:
            code, desc = _dtc_parts(entry)
            r = Gtk.ListBoxRow()
            r.set_activatable(False)
            r.set_selectable(False)

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_margin_top(10)
            box.set_margin_bottom(10)
            box.set_margin_start(14)
            box.set_margin_end(14)

            # Code stays in the error accent so it pops, description renders
            # in the normal text colour for readability.
            code_lbl = Gtk.Label(label=code or "?", xalign=0.0)
            code_lbl.set_halign(Gtk.Align.START)
            code_lbl.add_css_class("error")
            code_lbl.add_css_class("heading")
            box.append(code_lbl)

            if desc:
                desc_lbl = Gtk.Label(label=desc, xalign=0.0)
                desc_lbl.set_halign(Gtk.Align.START)
                desc_lbl.set_hexpand(True)
                desc_lbl.set_wrap(True)
                desc_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                desc_lbl.set_selectable(True)
                box.append(desc_lbl)

            r.set_child(box)
            rows.append(r)
        return rows

    def _make_dtc_clear_button_row(self) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)

        btn = Gtk.Button(label=_translate(self.language, "cars.dtc.clear"))
        btn.add_css_class("destructive-action")
        btn.set_halign(Gtk.Align.FILL)
        btn.set_hexpand(True)
        btn.set_margin_top(8)
        btn.set_margin_bottom(8)
        btn.set_margin_start(14)
        btn.set_margin_end(14)
        btn.connect("clicked", lambda _b: self._confirm_clear_dtcs())
        row.set_child(btn)
        return row

    def _confirm_clear_dtcs(self) -> None:
        if getattr(self, "on_clear_dtcs", None) is None:
            return
        dialog = Adw.AlertDialog()
        dialog.set_heading(_translate(self.language, "cars.dtc.clear.confirm.heading"))
        dialog.set_body(_translate(self.language, "cars.dtc.clear.confirm.body"))
        dialog.add_response("cancel", _translate(self.language, "cars.dtc.clear.confirm.cancel"))
        dialog.add_response("clear", _translate(self.language, "cars.dtc.clear.confirm.ok"))
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)

        def _on_response(_d: Adw.AlertDialog, response: str) -> None:
            if response != "clear":
                return
            on_clear = getattr(self, "on_clear_dtcs", None)
            if on_clear is None:
                return

            def _done(ok: bool) -> None:
                toast_key = "cars.dtc.clear.toast_ok" if ok else "cars.dtc.clear.toast_err"
                self._show_toast(_translate(self.language, toast_key))
                if ok and self._detail_pushed:
                    # The clear succeeded; re-render so the freshly empty
                    # tables show right away. The reader's force-rescan
                    # will eventually overwrite this with real data.
                    self._render_detail()

            try:
                on_clear(_done)
            except Exception:
                log.exception("on_clear_dtcs callback raised")
                self._show_toast(_translate(self.language, "cars.dtc.clear.toast_err"))

        dialog.connect("response", _on_response)
        root = self.get_root()
        if root:
            dialog.present(root)

    def _make_inline_row(self, pid_key: str, label: str, value_text: str, is_unknown: bool) -> Adw.ActionRow:
        row = Adw.ActionRow()
        row.set_use_markup(True)
        row.set_title(f'<span alpha="55%">{GLib.markup_escape_text(label)}</span>')
        if not pid_key.startswith("__"):
            row.set_subtitle(f'<span alpha="40%">{GLib.markup_escape_text(pid_key)}</span>')

        value_label = Gtk.Label(label=value_text, xalign=1.0)
        value_label.add_css_class("monospace")
        value_label.set_wrap(True)
        value_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        value_label.set_max_width_chars(28)
        if is_unknown:
            value_label.add_css_class("dim-label")
        row.add_suffix(value_label)
        return row

    # ---------------------------------------------------- Scan-Chart sub-page

    def _push_scan_chart(
        self,
        label: str,
        pid: str,
        all_stats: dict,
        pid_labels: dict,
        language: str,
    ) -> None:
        """Push a ScanChartContent sub-page for *pid* onto nav_view.

        The vertical-swipe navigator calls back into this method to replace
        the current page with the next/previous sensor's chart, so the
        per-(car, pid) persistence is reset cleanly for each sensor.
        """
        def _navigate(direction: int) -> None:
            ordered = _chart_ordered_pids(all_stats)
            if pid not in ordered or len(ordered) <= 1:
                return
            idx = ordered.index(pid)
            new_pid = ordered[(idx + direction) % len(ordered)]
            new_label = pid_labels.get(new_pid, new_pid)
            self.nav_view.pop()
            self._push_scan_chart(new_label, new_pid, all_stats, pid_labels, language)

        content = ScanChartContent(
            pid, all_stats,
            getattr(self, "_profiles", []),
            getattr(self, "db", None),
            pid_labels, language,
            main_car_id=getattr(self, "_selected_car_id", None),
            main_scan_id=getattr(self, "_selected_scan_id", None),
            on_navigate_pid=_navigate,
        )
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_hexpand(True)
        scroll.set_child(content)
        page = Adw.NavigationPage(
            child=self._wrap_sub_page(scroll, label),
            title=label,
        )
        page.set_tag(f"scan-chart-{pid}")
        self.nav_view.push(page)

    # ---------------------------------------------------- Fahrten-Rendering


    # ---------------------------------------------------- Scan-Liste & Detail


    # ------------------------------------------ Beschleunigungsläufe-Liste

    def _make_stacked_row(self, label: str, value_text: str, is_unknown: bool) -> Gtk.ListBoxRow:
        """Titel oben, Wert rechtsbündig darunter — passend für lange Werte wie VIN."""
        row = Gtk.ListBoxRow()
        row.set_activatable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(14)
        box.set_margin_end(14)

        title_lbl = Gtk.Label(label=label, xalign=0.0)
        title_lbl.set_halign(Gtk.Align.START)
        title_lbl.set_hexpand(True)
        title_lbl.add_css_class("caption-heading")
        box.append(title_lbl)

        value_lbl = Gtk.Label(label=value_text, xalign=1.0)
        value_lbl.set_halign(Gtk.Align.END)
        value_lbl.set_hexpand(True)
        value_lbl.set_wrap(True)
        value_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        value_lbl.set_selectable(True)
        if is_unknown:
            value_lbl.add_css_class("dim-label")
        box.append(value_lbl)

        row.set_child(box)
        return row

    def _make_editable_field_row(
        self, pid_key: str, label: str, value_text: str, is_unknown: bool
    ) -> Gtk.ListBoxRow:
        """Stacked row with a pencil icon; long-press opens the edit dialog."""
        row = Gtk.ListBoxRow()
        row.set_activatable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(14)
        box.set_margin_end(14)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        title_lbl = Gtk.Label(label=label, xalign=0.0)
        title_lbl.set_halign(Gtk.Align.START)
        title_lbl.set_hexpand(True)
        title_lbl.add_css_class("caption-heading")
        header.append(title_lbl)

        edit_icon = Gtk.Image.new_from_icon_name("document-edit-symbolic")
        edit_icon.set_opacity(0.3)
        edit_icon.set_pixel_size(14)
        header.append(edit_icon)
        box.append(header)

        value_lbl = Gtk.Label(label=value_text if not is_unknown else "—", xalign=1.0)
        value_lbl.set_halign(Gtk.Align.END)
        value_lbl.set_hexpand(True)
        value_lbl.set_wrap(True)
        value_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        value_lbl.set_selectable(True)
        if is_unknown:
            value_lbl.add_css_class("dim-label")
        box.append(value_lbl)
        row.set_child(box)

        gesture = Gtk.GestureLongPress()
        gesture.set_touch_only(False)
        gesture.connect(
            "pressed",
            lambda _g, _x, _y, _pk=pid_key, _v=value_text, _iu=is_unknown:
                self._show_field_edit_dialog(_pk, "" if _iu else _v),
        )
        row.add_controller(gesture)
        return row

    def _show_field_edit_dialog(self, pid_key: str, current_value: str) -> None:
        # Only the VIN field is user-editable. The manufacturer/brand row is
        # populated from VIN-decoded data and treated as permanent master
        # data — no edit affordance.
        if pid_key != _SPECIAL_VIN:
            return
        car_id = self._selected_car_id
        if car_id is None or self.db is None:
            return
        heading = _translate(self.language, "cars.field.edit_vin")
        entry_title = _translate(self.language, "cars.pid.VIN")

        dialog = Adw.AlertDialog()
        dialog.set_heading(heading)

        entry = Adw.EntryRow(title=entry_title)
        entry.set_text(current_value)
        lb = Gtk.ListBox()
        lb.set_selection_mode(Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        lb.set_margin_top(8)
        lb.append(entry)
        dialog.set_extra_child(lb)

        dialog.add_response("cancel", _translate(self.language, "cars.trip.delete_cancel"))
        dialog.add_response("save", _translate(self.language, "cars.field.save"))
        dialog.set_default_response("save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        def _on_response(d: Adw.AlertDialog, response: str) -> None:
            if response != "save":
                return
            value = entry.get_text().strip()
            try:
                self.db.update_car_vin(car_id, value)
            except Exception:
                log.exception("Could not update VIN for car_id=%s", car_id)
                self._show_toast(_translate(self.language, "cars.field.save_error"))
                return
            self.refresh_profiles()

        dialog.connect("response", _on_response)
        root = self.get_root()
        if root:
            dialog.present(root)

    def _make_live_stats_row(
        self,
        label: str,
        value_text: str,
        stats: dict | None,
        is_unknown: bool,
        on_click: Callable[[], None] | None = None,
    ) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_activatable(on_click is not None)

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        outer.set_margin_top(10)
        outer.set_margin_bottom(10)
        outer.set_margin_start(14)
        outer.set_margin_end(14)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_hexpand(True)
        outer.append(box)

        if on_click is not None:
            arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
            arrow.set_pixel_size(12)
            arrow.add_css_class("dim-label")
            arrow.set_valign(Gtk.Align.CENTER)
            outer.append(arrow)

            gesture = Gtk.GestureClick()
            gesture.connect("released", lambda g, _n, _x, _y: on_click())
            row.add_controller(gesture)

        row.set_child(outer)

        title_lbl = Gtk.Label(label=label, xalign=0.0)
        title_lbl.set_halign(Gtk.Align.START)
        title_lbl.set_hexpand(True)
        title_lbl.add_css_class("caption-heading")
        box.append(title_lbl)

        value_lbl = Gtk.Label(label=value_text, xalign=1.0)
        value_lbl.set_halign(Gtk.Align.END)
        value_lbl.set_hexpand(True)
        value_lbl.set_wrap(True)
        value_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        value_lbl.set_selectable(on_click is None)
        if is_unknown:
            value_lbl.add_css_class("dim-label")

        if stats and "min" in stats and "max" in stats:
            unit = _unit_display(stats.get("unit", ""), getattr(self, "language", "de"))
            mn = stats["min"]
            mx = stats["max"]

            def _fmt(v: float) -> str:
                if abs(v) >= 100:
                    return f"{v:.0f}"
                if abs(v) >= 10:
                    return f"{v:.1f}"
                return f"{v:.2f}"

            if mn == mx:
                # Single recorded value: showing „↓ 8 km/h  ↑ 8 km/h" is
                # just noise. Drop the min/max arrows and render the
                # value once.
                stats_text = f"{_fmt(mn)} {unit}".strip()
            else:
                mn_str = f"{_fmt(mn)} {unit}".strip()
                mx_str = f"{_fmt(mx)} {unit}".strip()
                stats_text = f"↓ {mn_str}   ↑ {mx_str}"
            stats_lbl = Gtk.Label(label=stats_text, xalign=1.0)
            stats_lbl.set_halign(Gtk.Align.END)
            stats_lbl.set_hexpand(True)
            stats_lbl.add_css_class("caption")
            box.append(stats_lbl)
        else:
            box.append(value_lbl)

        return row
