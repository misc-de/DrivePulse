"""Detail data and value rendering for the Cars page."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from .common import _translate
from .cars_metadata import (
    CATEGORIES,
    LIVE_KEY_TO_PID,
    VIN_DATA_SPECIAL_KEYS,
    _SPECIAL_CAL,
    _SPECIAL_CVN,
    _SPECIAL_DTC,
    _SPECIAL_PENDING,
    _SPECIAL_PROTO,
    _SPECIAL_SCAN_DATE,
    _SPECIAL_VIN,
    _extract_inner_string,
    _format_value_unit,
    _parse_profile_pid_key,
    _unit_display,
    _wmi_to_brand,
)

_PID_TO_LIVE_KEY: dict[str, str] = {pid: key for key, pid in LIVE_KEY_TO_PID.items()}
from .cars_scan_widgets import _format_scan_date, _format_scan_date_stack, _dtc_parts
from .scan_chart_page import ScanChartContent


def _format_dtc(entry: Any) -> str:
    code, desc = _dtc_parts(entry)
    return f"{code}: {desc}" if desc else code


class CarsDetailRenderMixin:
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
                    return self._flatten_profile(data), label
            except Exception:
                pass
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
        none_text = _translate(self.language, "cars.dtc.none")
        out[_SPECIAL_DTC] = none_text if not dtcs else "  ".join(_format_dtc(d) for d in dtcs)
        pending = data.get("pending_dtcs") or []
        out[_SPECIAL_PENDING] = none_text if not pending else "  ".join(_format_dtc(d) for d in pending)
        # Convenience flag for the sidebar highlight: yellow-tint the
        # diagnostics row when the loaded scan actually has DTCs.
        out["__has_dtc__"] = bool(dtcs) or bool(pending)
        for field_key, special_key in VIN_DATA_SPECIAL_KEYS.items():
            val = (data.get("vin_data") or {}).get(field_key)
            if val:
                out[special_key] = str(val)
        return out

    def _format_entry(self, pid_key: str, raw: Any) -> tuple[str, bool]:
        if pid_key == _SPECIAL_VIN and raw:
            vin = _extract_inner_string(raw)
            brand = _wmi_to_brand(vin)
            return (f"{vin}  ({brand})" if brand else vin, False)
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
        for pid_key, label_key in items:
            raw = data.get(pid_key)
            value_text, is_unknown = self._format_entry(pid_key, raw)
            if is_unknown and pid_key in _vin_data_keys:
                continue
            label = _translate(self.language, label_key)
            if not pid_key.startswith("__"):
                if is_live:
                    live_key = _PID_TO_LIVE_KEY.get(pid_key)
                    stats = self._live_session_stats.get(live_key) if live_key else None
                    on_click = None
                else:
                    stats = self._scan_pid_stats.get(pid_key)
                    if stats and "avg" in stats:
                        avg = stats["avg"]
                        unit = _unit_display(stats.get("unit", ""), _lang)
                        if abs(avg) >= 100:
                            avg_str = f"{avg:.0f}"
                        elif abs(avg) >= 10:
                            avg_str = f"{avg:.1f}"
                        else:
                            avg_str = f"{avg:.2f}"
                        value_text = f"⌀ {avg_str} {unit}".strip()
                        is_unknown = False
                    if stats and len(stats.get("values") or []) > 1:
                        def _make_cb(
                            lbl: str,
                            pk: str,
                            all_s: dict,
                            plabels: dict,
                            lang: str,
                        ) -> "callable":
                            def _open() -> None:
                                content = ScanChartContent(
                                    pk, all_s,
                                    getattr(self, "_profiles", []),
                                    getattr(self, "db", None),
                                    plabels, lang,
                                )
                                scroll = Gtk.ScrolledWindow()
                                scroll.set_policy(
                                    Gtk.PolicyType.NEVER,
                                    Gtk.PolicyType.AUTOMATIC,
                                )
                                scroll.set_vexpand(True)
                                scroll.set_hexpand(True)
                                scroll.set_child(content)
                                page = Adw.NavigationPage(
                                    child=self._wrap_sub_page(scroll, lbl),
                                    title=lbl,
                                )
                                page.set_tag(f"scan-chart-{pk}")
                                self.nav_view.push(page)
                            return _open
                        on_click = _make_cb(
                            label, pid_key,
                            self._scan_pid_stats, _pid_labels, _lang,
                        )
                    else:
                        on_click = None
                row = self._make_live_stats_row(label, value_text, stats, is_unknown, on_click)
            else:
                row = self._make_stacked_row(label, value_text, is_unknown)
            self.value_list.append(row)

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

    def _make_live_stats_row(
        self,
        label: str,
        value_text: str,
        stats: "dict | None",
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
        box.append(value_lbl)

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

            mn_str = f"{_fmt(mn)} {unit}".strip()
            mx_str = f"{_fmt(mx)} {unit}".strip()
            stats_text = f"↓ {mn_str}   ↑ {mx_str}"
            stats_lbl = Gtk.Label(label=stats_text, xalign=1.0)
            stats_lbl.set_halign(Gtk.Align.END)
            stats_lbl.set_hexpand(True)
            stats_lbl.add_css_class("dim-label")
            stats_lbl.add_css_class("caption")
            box.append(stats_lbl)

        return row
