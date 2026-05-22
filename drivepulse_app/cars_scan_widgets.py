"""Scan history widgets for the Cars page."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .common import _translate


def _decode(val: Any) -> str:
    """Convert any value to a clean string, decoding bytes without the b'...' wrapper."""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val) if val is not None else "—"


def _dtc_parts(entry: Any) -> tuple[str, str]:
    """Return (code, description) from a DTC entry — supports dict (new) and plain string (legacy)."""
    if isinstance(entry, dict):
        return entry.get("code", "?"), entry.get("description", "")
    return str(entry) if entry is not None else "?", ""


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _format_scan_date(raw: Any) -> str:
    if not raw:
        return "—"
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(raw)


def _safe_scan_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def _build_scan_detail_widget(
    language: str,
    scan_meta: Any,
    prev_meta: Any | None,
    data: dict[str, Any],
) -> Gtk.Widget:
    """Detail view for a single OBD scan: stats, DTC trend, fault codes, PID snapshot."""
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    outer.set_margin_top(14)
    outer.set_margin_bottom(14)
    outer.set_margin_start(14)
    outer.set_margin_end(14)

    def _stat_list(*rows: tuple[str, str]) -> Gtk.ListBox:
        lb = Gtk.ListBox()
        lb.set_selection_mode(Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        lb.set_valign(Gtk.Align.START)
        for title_text, value_text in rows:
            r = Adw.ActionRow()
            r.set_title(GLib.markup_escape_text(title_text))
            lbl = Gtk.Label(label=value_text, xalign=1.0)
            lbl.add_css_class("monospace")
            lbl.set_halign(Gtk.Align.END)
            lbl.set_valign(Gtk.Align.CENTER)
            lbl.set_wrap(True)
            lbl.set_max_width_chars(28)
            r.add_suffix(lbl)
            lb.append(r)
        return lb

    ts = _safe_scan_ts(scan_meta["scanned_at"])
    dtc = _safe_int(scan_meta["dtc_count"])
    pending = _safe_int(scan_meta["pending_dtc_count"])
    pids = _safe_int(scan_meta["pids_count"])

    if prev_meta is None:
        trend_text = _translate(language, "cars.scan.trend_first")
    else:
        delta = dtc - _safe_int(prev_meta["dtc_count"])
        if delta > 0:
            trend_text = _translate(language, "cars.scan.trend_up", delta=delta)
        elif delta < 0:
            trend_text = _translate(language, "cars.scan.trend_down", delta=abs(delta))
        else:
            trend_text = _translate(language, "cars.scan.trend_same")

    outer.append(_stat_list(
        (_translate(language, "cars.scan.date"),
         ts.strftime("%d.%m.%Y %H:%M:%S") if ts else "—"),
        (_translate(language, "cars.scan.protocol"),
         str(scan_meta["protocol"] or "—")),
        (_translate(language, "cars.scan.dtc_count"), str(dtc)),
        (_translate(language, "cars.scan.pending_count"), str(pending)),
        (_translate(language, "cars.scan.pids_count"), str(pids)),
        ("DTC Trend", trend_text),
    ))

    dtcs = data.get("dtcs") or []
    dtc_title = Gtk.Label(label=_translate(language, "cars.scan.dtcs"), xalign=0.0)
    dtc_title.add_css_class("heading")
    outer.append(dtc_title)
    if dtcs:
        dtc_lb = Gtk.ListBox()
        dtc_lb.set_selection_mode(Gtk.SelectionMode.NONE)
        dtc_lb.add_css_class("boxed-list")
        dtc_lb.set_valign(Gtk.Align.START)
        for code in dtcs:
            dtc_code, dtc_desc = _dtc_parts(code)
            r = Adw.ActionRow()
            r.set_title(GLib.markup_escape_text(dtc_code))
            if dtc_desc:
                r.set_subtitle(GLib.markup_escape_text(dtc_desc))
            r.add_css_class("error")
            dtc_lb.append(r)
        outer.append(dtc_lb)
    else:
        lbl = Gtk.Label(label=_translate(language, "cars.scan.dtcs_none"), xalign=0.0)
        lbl.add_css_class("dim-label")
        outer.append(lbl)

    pending_dtcs = data.get("pending_dtcs") or []
    if pending_dtcs:
        p_title = Gtk.Label(label=_translate(language, "cars.scan.pending_dtcs"), xalign=0.0)
        p_title.add_css_class("heading")
        outer.append(p_title)
        p_lb = Gtk.ListBox()
        p_lb.set_selection_mode(Gtk.SelectionMode.NONE)
        p_lb.add_css_class("boxed-list")
        p_lb.set_valign(Gtk.Align.START)
        for code in pending_dtcs:
            dtc_code, dtc_desc = _dtc_parts(code)
            r = Adw.ActionRow()
            r.set_title(GLib.markup_escape_text(dtc_code))
            if dtc_desc:
                r.set_subtitle(GLib.markup_escape_text(dtc_desc))
            p_lb.append(r)
        outer.append(p_lb)

    live = data.get("live_data") or {}
    if live:
        pid_title = Gtk.Label(label="PID Snapshot", xalign=0.0)
        pid_title.add_css_class("heading")
        outer.append(pid_title)
        pid_lb = Gtk.ListBox()
        pid_lb.set_selection_mode(Gtk.SelectionMode.NONE)
        pid_lb.add_css_class("boxed-list")
        pid_lb.set_valign(Gtk.Align.START)
        for pid_name, val in sorted(live.items()):
            r = Adw.ActionRow()
            r.set_title(GLib.markup_escape_text(_decode(pid_name)))
            if isinstance(val, dict):
                v = val.get("value")
                u = val.get("unit", "")
                display = f"{_decode(v)} {_decode(u)}".strip() if v is not None else _decode(val.get("error", "—"))
            else:
                display = _decode(val)
            lbl = Gtk.Label(label=display, xalign=1.0)
            lbl.add_css_class("monospace")
            lbl.set_halign(Gtk.Align.END)
            lbl.set_valign(Gtk.Align.CENTER)
            lbl.set_wrap(True)
            lbl.set_max_width_chars(28)
            lbl.set_selectable(True)
            r.add_suffix(lbl)
            pid_lb.append(r)
        outer.append(pid_lb)

    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_vexpand(True)
    scroll.set_hexpand(True)
    scroll.set_child(outer)
    return scroll
