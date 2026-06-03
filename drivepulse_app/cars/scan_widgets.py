"""Scan history widgets for the Cars page."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from drivepulse_app.common import _translate


def _decode(val: Any) -> str:
    """Convert any value to a clean string, decoding bytes without the b'...' wrapper."""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val) if val is not None else "—"


def _dtc_parts(entry: Any) -> tuple[str, str]:
    """Return (code, description) from a DTC entry.

    Robust against the various shapes we've seen across versions:
      - dict {"code", "description"} — current scanner output
      - bytes — decoded
      - JSON-encoded dict string '{"code": "...", "description": "..."}'
      - tuple/list of (code, description)
      - "CODE: description" or just "CODE"
    """
    if entry is None:
        return "?", ""
    if isinstance(entry, bytes):
        try:
            entry = entry.decode("utf-8", errors="replace")
        except Exception:
            entry = entry.decode("latin-1", errors="replace")
    if isinstance(entry, dict):
        code = entry.get("code") or entry.get("Code") or "?"
        desc = entry.get("description") or entry.get("desc") or ""
        return str(code), str(desc)
    if isinstance(entry, (tuple, list)) and entry:
        code = str(entry[0])
        desc = str(entry[1]) if len(entry) > 1 else ""
        return code, desc
    if isinstance(entry, str):
        s = entry.strip()
        if s.startswith("{") and s.endswith("}"):
            # JSON-ish dict that slipped through as a string.
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    return _dtc_parts(obj)
            except (json.JSONDecodeError, ValueError):
                pass
        # "CODE: Description" — split on the first colon only so descriptions
        # that themselves contain colons survive intact.
        if ":" in s:
            code, _, desc = s.partition(":")
            code = code.strip()
            # Only treat as code/desc when the prefix looks like an OBD code
            # (e.g. P0420, U0100, C1234, B1234) — otherwise the whole string
            # is just a free-form description.
            if re.fullmatch(r"[PCBU][0-9A-F]{4}", code, flags=re.IGNORECASE):
                return code.upper(), desc.strip()
        return s, ""
    return str(entry), ""


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _fmt_num(v: Any) -> str:
    """Compact number: drop a trailing .0, leave everything else as-is."""
    if v is None:
        return "—"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else f"{v:g}"
    return str(v)


def _pretty_monitor(name: str) -> str:
    """'MONITOR_CATALYST_B1' → 'Catalyst B1'."""
    s = str(name)
    if s.startswith("MONITOR_"):
        s = s[len("MONITOR_"):]
    return s.replace("_", " ").title()


def _readiness_label(name: str) -> str:
    """'OXYGEN_SENSOR_HEATER_MONITORING' → 'Oxygen Sensor Heater'."""
    s = str(name)
    for suffix in ("_SYSTEM_MONITORING", "_MONITORING"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s.replace("_", " ").title()


def _format_scan_date(raw: Any) -> str:
    if not raw:
        return "—"
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return str(raw)


def _format_scan_date_stack(raw: Any, language: str = "en") -> str | None:
    """Pango markup for the three-line scan date stamp used in the sidebar.

    Layout: year (lightest) / day-month (subtle) / HH:MM (subtle), centred.
    The middle line follows the language's day/month convention — German
    and other European locales get DD.MM, English (en) gets MM.DD.
    Returns ``None`` when the timestamp can't be parsed.
    """
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    # en → MM.DD (US default); any other language → DD.MM (European).
    md_fmt = "%m.%d" if (language or "").lower().startswith("en") else "%d.%m"
    return (
        f'<span alpha="40%">{dt.strftime("%Y")}</span>\n'
        f'<span alpha="75%">{dt.strftime(md_fmt)}</span>\n'
        f'<span alpha="75%">{dt.strftime("%H:%M")}</span>'
    )


def _safe_scan_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
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

    dtc = _safe_int(scan_meta["dtc_count"])
    pending = _safe_int(scan_meta["pending_dtc_count"])
    pids = _safe_int(scan_meta["pids_count"])

    # Trend annotation only when DTC count actually moved; "unchanged" is
    # noise and a brand-new scan with no predecessor stays unlabeled too.
    trend_text: str | None = None
    if prev_meta is None:
        trend_text = _translate(language, "cars.scan.trend_first")
    else:
        delta = dtc - _safe_int(prev_meta["dtc_count"])
        if delta > 0:
            trend_text = _translate(language, "cars.scan.trend_up", delta=delta)
        elif delta < 0:
            trend_text = _translate(language, "cars.scan.trend_down", delta=abs(delta))

    # Scan date is already shown in the sub-page heading — drop the
    # repeated "Datum" stat row.
    stat_rows: list[tuple[str, str]] = [
        (_translate(language, "cars.scan.protocol"),
         str(scan_meta["protocol"] or "—")),
        (_translate(language, "cars.scan.dtc_count"), str(dtc)),
        (_translate(language, "cars.scan.pending_count"), str(pending)),
        (_translate(language, "cars.scan.pids_count"), str(pids)),
    ]
    if trend_text:
        stat_rows.append(("DTC Trend", trend_text))
    outer.append(_stat_list(*stat_rows))

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

    # Permanent DTCs (Mode 0A) — persist until the fault self-heals; cannot be
    # cleared with Mode 04, so they're the strongest "real history" signal.
    permanent_dtcs = data.get("permanent_dtcs") or []
    if permanent_dtcs:
        pm_title = Gtk.Label(label=_translate(language, "cars.scan.permanent_dtcs"), xalign=0.0)
        pm_title.add_css_class("heading")
        outer.append(pm_title)
        pm_lb = Gtk.ListBox()
        pm_lb.set_selection_mode(Gtk.SelectionMode.NONE)
        pm_lb.add_css_class("boxed-list")
        pm_lb.set_valign(Gtk.Align.START)
        for code in permanent_dtcs:
            dtc_code, dtc_desc = _dtc_parts(code)
            r = Adw.ActionRow()
            r.set_title(GLib.markup_escape_text(dtc_code))
            if dtc_desc:
                r.set_subtitle(GLib.markup_escape_text(dtc_desc))
            r.add_css_class("error")
            pm_lb.append(r)
        outer.append(pm_lb)

    # Readiness monitors (Mode 01 STATUS): MIL state + per-monitor ready/not.
    readiness = data.get("readiness") or {}
    if readiness:
        rd_title = Gtk.Label(label=_translate(language, "cars.scan.readiness"), xalign=0.0)
        rd_title.add_css_class("heading")
        outer.append(rd_title)
        rd_lb = Gtk.ListBox()
        rd_lb.set_selection_mode(Gtk.SelectionMode.NONE)
        rd_lb.add_css_class("boxed-list")
        rd_lb.set_valign(Gtk.Align.START)
        mil_on = bool(readiness.get("MIL"))
        mil_row = Adw.ActionRow()
        mil_row.set_title(GLib.markup_escape_text(_translate(language, "cars.scan.mil")))
        mil_lbl = Gtk.Label(
            label=_translate(language, "cars.scan.mil_on" if mil_on else "cars.scan.mil_off")
        )
        mil_lbl.add_css_class("error" if mil_on else "success")
        mil_lbl.set_halign(Gtk.Align.END)
        mil_lbl.set_valign(Gtk.Align.CENTER)
        mil_row.add_suffix(mil_lbl)
        rd_lb.append(mil_row)
        for mname, mval in sorted((readiness.get("monitors") or {}).items()):
            complete = bool((mval or {}).get("complete"))
            r = Adw.ActionRow()
            r.set_title(GLib.markup_escape_text(_readiness_label(mname)))
            key = "cars.scan.readiness_ready" if complete else "cars.scan.readiness_not_ready"
            mark = ("✓ " if complete else "✗ ") + _translate(language, key)
            lbl = Gtk.Label(label=mark)
            lbl.add_css_class("success" if complete else "warning")
            lbl.set_halign(Gtk.Align.END)
            lbl.set_valign(Gtk.Align.CENTER)
            r.add_suffix(lbl)
            rd_lb.append(r)
        outer.append(rd_lb)

    # On-board monitor tests (Mode 06) — collapsible per monitor.
    monitors = data.get("monitors") or {}
    if monitors:
        mo_title = Gtk.Label(label=_translate(language, "cars.scan.monitors"), xalign=0.0)
        mo_title.add_css_class("heading")
        outer.append(mo_title)
        mo_lb = Gtk.ListBox()
        mo_lb.set_selection_mode(Gtk.SelectionMode.NONE)
        mo_lb.add_css_class("boxed-list")
        mo_lb.set_valign(Gtk.Align.START)
        for mon_name, mon_tests in sorted(monitors.items()):
            tests = mon_tests or []
            exp = Adw.ExpanderRow()
            exp.set_title(GLib.markup_escape_text(_pretty_monitor(mon_name)))
            all_passed = bool(tests) and all(bool(t.get("passed")) for t in tests)
            badge = Gtk.Label(label="✓" if all_passed else "•")
            badge.add_css_class("success" if all_passed else "dim-label")
            badge.set_valign(Gtk.Align.CENTER)
            exp.add_suffix(badge)
            for t in tests:
                cr = Adw.ActionRow()
                tid = t.get("tid")
                tname = t.get("name")
                if (not tname or tname == "Unknown") and isinstance(tid, int):
                    tname = f"TID 0x{tid:02X}"
                cr.set_title(GLib.markup_escape_text(str(tname or "Test")))
                unit = t.get("unit") or ""
                lo, hi = t.get("min"), t.get("max")
                if lo is not None or hi is not None:
                    cr.set_subtitle(
                        GLib.markup_escape_text(f"{_fmt_num(lo)}–{_fmt_num(hi)} {unit}".strip())
                    )
                lbl = Gtk.Label(label=f"{_fmt_num(t.get('value'))} {unit}".strip())
                lbl.add_css_class("monospace")
                lbl.set_halign(Gtk.Align.END)
                lbl.set_valign(Gtk.Align.CENTER)
                cr.add_suffix(lbl)
                exp.add_row(cr)
            mo_lb.append(exp)
        outer.append(mo_lb)

    # IUMPR (Mode 09 PID 08) drive-cycle / monitor completion counters per ECU.
    iumpr = data.get("iumpr") or {}
    if iumpr:
        iu_title = Gtk.Label(label=_translate(language, "cars.scan.iumpr"), xalign=0.0)
        iu_title.add_css_class("heading")
        outer.append(iu_title)
        iu_lb = Gtk.ListBox()
        iu_lb.set_selection_mode(Gtk.SelectionMode.NONE)
        iu_lb.add_css_class("boxed-list")
        iu_lb.set_valign(Gtk.Align.START)
        for _ecu, payload in sorted(iumpr.items()):
            for name, num in (payload or {}).get("values", {}).items():
                r = Adw.ActionRow()
                r.set_title(GLib.markup_escape_text(str(name)))
                lbl = Gtk.Label(label=str(num))
                lbl.add_css_class("monospace")
                lbl.set_halign(Gtk.Align.END)
                lbl.set_valign(Gtk.Align.CENTER)
                r.add_suffix(lbl)
                iu_lb.append(r)
        outer.append(iu_lb)

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
