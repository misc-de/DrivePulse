"""Autos-Browser: Liste bekannter Fahrzeuge → Detail mit kategorisierten Werten."""
from __future__ import annotations

import concurrent.futures
import io
import json
import math
import os
import re
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from common import PROFILES_DIR, SOURCE_LANGUAGE, _normalize_language, _translate
from db import DriveDB


# ---------------------------------------------------------------------------
# PID-Metadaten: Kategorien + endkundenfreundliche Bezeichnungen
# ---------------------------------------------------------------------------

LIVE_KEY_TO_PID: dict[str, str] = {
    "rpm":                    "010C",
    "speed":                  "010D",
    "coolant_temp":           "0105",
    "throttle_pos":           "0111",
    "engine_load":            "0104",
    "intake_temp":            "010F",
    "maf":                    "0110",
    "fuel_level":             "012F",
    "runtime":                "011F",
    "control_module_voltage": "0142",
}

_SPECIAL_VIN = "__VIN__"
_SPECIAL_CAL = "__CAL__"
_SPECIAL_CVN = "__CVN__"
_SPECIAL_PROTO = "__PROTO__"
_SPECIAL_SCAN_DATE = "__SCAN_DATE__"
_SPECIAL_DTC = "__DTC__"
_SPECIAL_PENDING = "__PENDING_DTC__"
_SPECIAL_ADAPTER_V = "__ATRV__"

# (db_key, display_label, unit, line_color_rgb, value_fmt)
_CHART_METRICS: tuple[tuple, ...] = (
    ("speed_kmh",    "Geschwindigkeit", "km/h", (0.34, 0.62, 0.86), "{:.0f}"),
    ("rpm",          "Drehzahl",        "RPM",  (0.95, 0.60, 0.20), "{:.0f}"),
    ("coolant_c",    "Kühlmitteltemp.", "°C",   (0.90, 0.30, 0.30), "{:.0f}"),
    ("intake_c",     "Ansauglufttemp.", "°C",   (0.95, 0.50, 0.20), "{:.0f}"),
    ("throttle_pct", "Drosselklappe",   "%",    (0.30, 0.80, 0.40), "{:.0f}"),
    ("engine_load",  "Motorlast",       "%",    (0.60, 0.85, 0.30), "{:.0f}"),
    ("maf_gps",      "Luftmasse",       "g/s",  (0.70, 0.40, 0.90), "{:.1f}"),
    ("voltage_v",    "Spannung",        "V",    (0.95, 0.75, 0.10), "{:.2f}"),
    ("accel_g",      "Beschleunigung",  "g",    (0.90, 0.40, 0.20), "{:.2f}"),
    ("altitude_m",   "Höhe",            "m",    (0.20, 0.75, 0.70), "{:.0f}"),
    ("fuel_pct",     "Kraftstoff",      "%",    (0.95, 0.80, 0.10), "{:.0f}"),
)

CATEGORIES: tuple[tuple[str, str, str, tuple[tuple[str, str], ...]], ...] = (
    ("vehicle", "cars.category.vehicle", "info-symbolic", (
        (_SPECIAL_VIN,        "cars.pid.VIN"),
        (_SPECIAL_CAL,        "cars.pid.CAL"),
        (_SPECIAL_CVN,        "cars.pid.CVN"),
        (_SPECIAL_PROTO,      "cars.pid.PROTO"),
        ("011C",              "cars.pid.011C"),
        (_SPECIAL_SCAN_DATE,  "cars.pid.SCAN_DATE"),
    )),
    ("engine", "cars.category.engine", "step_object_LinearMotor-symbolic", (
        ("010C", "cars.pid.010C"),
        ("0104", "cars.pid.0104"),
        ("0143", "cars.pid.0143"),
        ("010E", "cars.pid.010E"),
        ("011F", "cars.pid.011F"),
        ("0142", "cars.pid.0142"),
        (_SPECIAL_ADAPTER_V, "cars.pid.ATRV"),
    )),
    ("drive", "cars.category.drive", "speedometer4-symbolic", (
        ("010D", "cars.pid.010D"),
        ("0131", "cars.pid.0131"),
        ("0121", "cars.pid.0121"),
        ("0130", "cars.pid.0130"),
    )),
    ("temperatures", "cars.category.temperatures", "thermometer-symbolic", (
        ("0105", "cars.pid.0105"),
        ("010F", "cars.pid.010F"),
        ("0146", "cars.pid.0146"),
        ("013C", "cars.pid.013C"),
    )),
    ("throttle", "cars.category.throttle", "emblem-system-symbolic", (
        ("0111", "cars.pid.0111"),
        ("0145", "cars.pid.0145"),
        ("0147", "cars.pid.0147"),
        ("0149", "cars.pid.0149"),
        ("014A", "cars.pid.014A"),
        ("014C", "cars.pid.014C"),
    )),
    ("mixture", "cars.category.mixture", "applications-science-symbolic", (
        ("0103", "cars.pid.0103"),
        ("0106", "cars.pid.0106"),
        ("0107", "cars.pid.0107"),
        ("0156", "cars.pid.0156"),
        ("0134", "cars.pid.0134"),
        ("0144", "cars.pid.0144"),
        ("0115", "cars.pid.0115"),
    )),
    ("fuel", "cars.category.fuel", "weather-windy-symbolic", (
        ("0110", "cars.pid.0110"),
        ("012F", "cars.pid.012F"),
        ("0123", "cars.pid.0123"),
        ("012E", "cars.pid.012E"),
        ("0133", "cars.pid.0133"),
    )),
    ("diagnostics", "cars.category.diagnostics", "dialog-warning-symbolic", (
        (_SPECIAL_DTC,        "cars.pid.DTC"),
        (_SPECIAL_PENDING,    "cars.pid.PENDING_DTC"),
        ("0141", "cars.pid.0141"),
    )),
    # Sonderfall: keine PID-Liste, Inhalt = Fahrten dieses Autos aus der DB
    ("trips", "cars.category.trips", "globe-symbolic", ()),
    # Sonderfall: Scan-Verlauf aus der DB
    ("scans", "cars.category.scans", "folder-saved-search-symbolic", ()),
)


_UNIT_DISPLAY: dict[str, str] = {
    "revolutions_per_minute": "rpm",
    "kilometer_per_hour":     "km/h",
    "kilometer":              "km",
    "degree_Celsius":         "°C",
    "degree":                 "°",
    "percent":                "%",
    "kilopascal":             "kPa",
    "volt":                   "V",
    "second":                 "s",
    "milliampere":            "mA",
    "gps":                    "g/s",
    "ratio":                  "",
    "count":                  "",
    "rpm":                    "rpm",
    "km/h":                   "km/h",
    "degC":                   "°C",
    "deg":                    "°",
    "g":                      "g",
}

_UNIT_DISPLAY_DE: dict[str, str] = {
    "revolutions_per_minute": "U/min",
    "rpm":                    "U/min",
}


def _unit_display(unit: str, language: str = "en") -> str:
    if language == "de":
        return _UNIT_DISPLAY_DE.get(unit) or _UNIT_DISPLAY.get(unit, unit)
    return _UNIT_DISPLAY.get(unit, unit)

_WMI_BRANDS: dict[str, str] = {
    "WAU": "Audi", "TRU": "Audi", "WUA": "Audi",
    "WBA": "BMW", "WBS": "BMW", "WBY": "BMW",
    "WVW": "VW", "WV1": "VW", "WV2": "VW", "WVG": "VW",
    "WP0": "Porsche", "WP1": "Porsche",
    "WDB": "Mercedes-Benz", "WDD": "Mercedes-Benz", "WDC": "Mercedes-Benz",
    "VF1": "Renault", "VF3": "Peugeot", "VF7": "Citroën",
    "ZFA": "Fiat", "ZAR": "Alfa Romeo",
    "JT1": "Toyota", "JT2": "Toyota", "JTD": "Toyota",
    "JN1": "Nissan", "JN8": "Nissan",
    "KMH": "Hyundai", "KNA": "Kia",
    "VS5": "SEAT", "VSS": "SEAT", "TMB": "Škoda",
    "WF0": "Ford", "1FT": "Ford", "1FA": "Ford",
    "YV1": "Volvo", "YV4": "Volvo",
}


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------


def _extract_inner_string(raw: Any) -> str:
    if raw is None:
        return ""
    s = str(raw)
    m = re.search(r"b['\"]([^'\"]+)['\"]", s)
    if m:
        return m.group(1)
    return s.strip()


def _wmi_to_brand(vin: str) -> str:
    return _WMI_BRANDS.get(vin[:3], "")


def _parse_profile_pid_key(key: str) -> str:
    m = re.search(r"b['\"]([0-9A-Fa-f]+)['\"]", key)
    return m.group(1).upper() if m else ""


def _format_status_string(value: str) -> str:
    if value.startswith("(") and "'" in value:
        m = re.match(r"^\(\s*'([^']*)'", value)
        if m:
            return m.group(1)
    return value


def _format_value_unit(payload: Any, language: str = "en") -> str:
    if payload is None:
        return "—"
    if isinstance(payload, dict) and "value" in payload:
        value = payload.get("value")
        unit = payload.get("unit") or ""
        unit_disp = _unit_display(unit, language)
        if value is None:
            return "—"
        try:
            v = float(value)
        except (TypeError, ValueError):
            return f"{value} {unit_disp}".strip()
        if abs(v) >= 100:
            text = f"{v:.0f}"
        elif abs(v) >= 10:
            text = f"{v:.1f}"
        else:
            text = f"{v:.2f}"
        return f"{text} {unit_disp}".strip() if unit_disp else text
    if isinstance(payload, str):
        return _format_status_string(payload) or "—"
    return str(payload)


def _format_scan_date(raw: Any) -> str:
    if not raw:
        return "—"
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(raw)


def _build_trip_detail_widget(language: str, trip: Any, samples: list[Any]) -> Gtk.Widget:
    """Stat-Karte + GPS-Track + Speed-Verlauf für eine einzelne Fahrt."""
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    outer.set_margin_top(14)
    outer.set_margin_bottom(14)
    outer.set_margin_start(14)
    outer.set_margin_end(14)

    # --- Stats ---
    stats = Gtk.ListBox()
    stats.set_selection_mode(Gtk.SelectionMode.NONE)
    stats.add_css_class("boxed-list")
    stats.set_valign(Gtk.Align.START)

    def _add_stat(title: str, value: str) -> None:
        row = Adw.ActionRow()
        row.set_title(GLib.markup_escape_text(title))
        lbl = Gtk.Label(label=value, xalign=1.0)
        lbl.add_css_class("monospace")
        lbl.set_halign(Gtk.Align.END)
        row.add_suffix(lbl)
        stats.append(row)

    started = _safe_ts(trip["started_at"])
    ended = _safe_ts(trip["ended_at"])
    _add_stat(_translate(language, "cars.trip.start"), started.strftime("%d.%m.%Y %H:%M:%S") if started else "—")
    _add_stat(_translate(language, "cars.trip.end"), ended.strftime("%d.%m.%Y %H:%M:%S") if ended else "—")
    dur_s = trip["duration_s"] or 0.0
    if dur_s:
        hrs = int(dur_s // 3600)
        mins = int((dur_s % 3600) // 60)
        secs = int(dur_s % 60)
        dur_text = f"{hrs}:{mins:02d}:{secs:02d}" if hrs else f"{mins}:{secs:02d} min"
    else:
        dur_text = "—"
    _add_stat(_translate(language, "cars.trip.duration"), dur_text)
    _add_stat(_translate(language, "cars.trip.distance"), f"{trip['distance_km']:.2f} km" if trip["distance_km"] else "—")
    _add_stat(_translate(language, "cars.trip.max_speed"), f"{trip['max_speed_kmh']:.0f} km/h" if trip["max_speed_kmh"] else "—")
    _add_stat(_translate(language, "cars.trip.avg_speed"), f"{trip['avg_speed_kmh']:.0f} km/h" if trip["avg_speed_kmh"] else "—")
    _add_stat(_translate(language, "cars.trip.samples"), str(trip["samples_count"] or 0))

    outer.append(stats)

    # --- Build per-metric point lists: (ts, value|None, lat, lon) ---
    # Base: all samples that have GPS coordinates (needed for map cursor sync)
    _base = [s for s in samples if s["lat"] is not None and s["lon"] is not None]

    def _finite(v: Any) -> bool:
        """True only for finite, non-NaN numbers — rejects None, nan, inf, strings."""
        try:
            return math.isfinite(float(v))
        except (TypeError, ValueError):
            return False

    metric_data: dict[str, list] = {}
    for _mk, _ml, _mu, _mc, _mf in _CHART_METRICS:
        _pts = [(s["ts"], s[_mk] if _finite(s[_mk]) else None, s["lat"], s["lon"])
                for s in _base]
        if sum(1 for p in _pts if p[1] is not None) >= 2:
            metric_data[_mk] = _pts

    _avail = [(k, l, u, c, f) for k, l, u, c, f in _CHART_METRICS if k in metric_data]

    _def_key = "speed_kmh" if "speed_kmh" in metric_data else (
        _avail[0][0] if _avail else None
    )
    chart_state: dict[str, Any] = {}
    if _def_key:
        _dm = next(m for m in _CHART_METRICS if m[0] == _def_key)
        chart_state = {
            "pts": metric_data[_def_key],
            "unit": _dm[2],
            "color": _dm[3],
            "fmt": _dm[4],
            "key": _def_key,
        }

    # Shared cursor state: idx = index into chart_state["pts"], -1 = none
    cursor_state: dict[str, Any] = {"idx": -1}
    map_widget_ref: list[Any] = [None]
    chart_area_ref: list[Any] = [None]

    def _on_cursor_change() -> None:
        if map_widget_ref[0]:
            map_widget_ref[0].queue_draw()
        if chart_area_ref[0]:
            chart_area_ref[0].queue_draw()

    # --- GPS-Track / OSM Map ---
    gps_points = [(s["lat"], s["lon"], s["speed_kmh"]) for s in samples
                  if s["lat"] is not None and s["lon"] is not None]
    if gps_points:
        gps_title = Gtk.Label(label=_translate(language, "cars.trip.route"), xalign=0.0)
        gps_title.add_css_class("heading")
        outer.append(gps_title)
        map_widget = _build_osm_map_widget(
            gps_points,
            chart_state=chart_state if chart_state else None,
            cursor_state=cursor_state,
        )
        if map_widget is not None:
            map_widget_ref[0] = map_widget
            outer.append(map_widget)
        else:
            gps_area = Gtk.DrawingArea()
            gps_area.set_content_height(240)
            gps_area.set_hexpand(True)
            gps_area.add_css_class("card")
            gps_area.set_draw_func(lambda area, cr, w, h, pts=gps_points: _draw_gps_track(cr, w, h, pts))
            outer.append(gps_area)

    # --- Datenverlauf ---
    if _avail and chart_state:
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_box.set_hexpand(True)

        chart_title_lbl = Gtk.Label(xalign=0.0)
        chart_title_lbl.add_css_class("heading")
        chart_title_lbl.set_hexpand(True)
        _init_lbl = next((m[1] for m in _avail if m[0] == chart_state["key"]), _avail[0][1])
        chart_title_lbl.set_label(_init_lbl)
        header_box.append(chart_title_lbl)

        if len(_avail) > 1:
            _str_model = Gtk.StringList.new([m[1] for m in _avail])
            _dropdown = Gtk.DropDown.new(_str_model, None)
            _dropdown.set_valign(Gtk.Align.CENTER)
            _init_sel = next((i for i, m in enumerate(_avail) if m[0] == chart_state["key"]), 0)
            _dropdown.set_selected(_init_sel)

            def _on_metric_selected(dd: Gtk.DropDown, _pspec: Any, avail: list = _avail) -> None:
                sel = dd.get_selected()
                if 0 <= sel < len(avail):
                    key, lbl, unit, color, fmt = avail[sel]
                    chart_state["pts"] = metric_data[key]
                    chart_state["unit"] = unit
                    chart_state["color"] = color
                    chart_state["fmt"] = fmt
                    chart_state["key"] = key
                    chart_title_lbl.set_label(lbl)
                    cursor_state["idx"] = -1
                    if chart_area_ref[0]:
                        chart_area_ref[0].queue_draw()
                    if map_widget_ref[0]:
                        map_widget_ref[0].queue_draw()

            _dropdown.connect("notify::selected", _on_metric_selected)
            header_box.append(_dropdown)

        outer.append(header_box)
        sp_area = _build_chart_widget(chart_state, cursor_state, _on_cursor_change)
        chart_area_ref[0] = sp_area
        outer.append(sp_area)

    if not gps_points and not _avail:
        empty = Gtk.Label(label=_translate(language, "cars.trip.no_data"), xalign=0.0)
        empty.add_css_class("dim-label")
        outer.append(empty)

    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_vexpand(True)
    scroll.set_hexpand(True)
    scroll.set_child(outer)
    return scroll


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
            r.add_suffix(lbl)
            lb.append(r)
        return lb

    # --- Summary stats ---
    ts = _safe_ts(scan_meta["scanned_at"])
    dtc = int(scan_meta["dtc_count"] or 0)
    pending = int(scan_meta["pending_dtc_count"] or 0)
    pids = int(scan_meta["pids_count"] or 0)

    if prev_meta is None:
        trend_text = _translate(language, "cars.scan.trend_first")
    else:
        delta = dtc - int(prev_meta["dtc_count"] or 0)
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
        (_translate(language, "cars.scan.dtc_count"),   str(dtc)),
        (_translate(language, "cars.scan.pending_count"), str(pending)),
        (_translate(language, "cars.scan.pids_count"),  str(pids)),
        ("DTC Trend", trend_text),
    ))

    # --- Active fault codes ---
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
            r = Adw.ActionRow()
            r.set_title(GLib.markup_escape_text(str(code)))
            r.add_css_class("error")
            dtc_lb.append(r)
        outer.append(dtc_lb)
    else:
        lbl = Gtk.Label(label=_translate(language, "cars.scan.dtcs_none"), xalign=0.0)
        lbl.add_css_class("dim-label")
        outer.append(lbl)

    # --- Pending fault codes ---
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
            r = Adw.ActionRow()
            r.set_title(GLib.markup_escape_text(str(code)))
            p_lb.append(r)
        outer.append(p_lb)

    # --- PID snapshot ---
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
            r.set_title(GLib.markup_escape_text(str(pid_name)))
            if isinstance(val, dict):
                v = val.get("value")
                u = val.get("unit", "")
                display = f"{v} {u}".strip() if v is not None else str(val.get("error", "—"))
            else:
                display = str(val) if val is not None else "—"
            lbl = Gtk.Label(label=display, xalign=1.0)
            lbl.add_css_class("monospace")
            lbl.set_halign(Gtk.Align.END)
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


def _safe_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def _is_dark() -> bool:
    try:
        return Adw.StyleManager.get_default().get_dark()
    except Exception:
        return True


def _draw_gps_track(cr: Any, width: int, height: int, points: list[tuple[float, float, float | None]]) -> None:
    """Zeichnet die GPS-Spur in den DrawingArea-Bereich. Speed kodiert per Farbe."""
    if not points:
        return
    pad = 12
    iw, ih = max(1, width - 2 * pad), max(1, height - 2 * pad)
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)
    lat_span = max(1e-6, lat_max - lat_min)
    lon_span = max(1e-6, lon_max - lon_min)
    # Längengrade an Breitengrad-Cosinus skalieren, damit es nicht verzerrt
    import math as _m
    cos_lat = _m.cos(_m.radians((lat_min + lat_max) / 2))
    aspect = (lon_span * cos_lat) / lat_span
    if aspect > iw / ih:
        draw_w = iw
        draw_h = iw / aspect
    else:
        draw_h = ih
        draw_w = ih * aspect
    off_x = pad + (iw - draw_w) / 2
    off_y = pad + (ih - draw_h) / 2

    def project(lat: float, lon: float) -> tuple[float, float]:
        x = off_x + ((lon - lon_min) / lon_span) * draw_w
        # y invertiert: hoch = Norden
        y = off_y + draw_h - ((lat - lat_min) / lat_span) * draw_h
        return x, y

    # Pfad farbcodiert nach Geschwindigkeit
    speeds = [s for _, _, s in points if s is not None]
    vmax = max(speeds) if speeds else 0.0
    last_pt = project(points[0][0], points[0][1])
    cr.set_line_width(2.5)
    cr.set_line_cap(1)  # ROUND
    cr.set_line_join(1)
    for i in range(1, len(points)):
        lat, lon, spd = points[i]
        x, y = project(lat, lon)
        # Farbe: blau (langsam) → grün → rot (schnell)
        if spd is None or vmax <= 0:
            cr.set_source_rgb(0.4, 0.6, 0.9)
        else:
            t = min(1.0, spd / max(1.0, vmax))
            r = 0.2 + 0.7 * t
            g = 0.5 + 0.4 * (1 - abs(0.5 - t) * 2)
            b = 0.9 - 0.8 * t
            cr.set_source_rgb(r, g, b)
        cr.move_to(*last_pt)
        cr.line_to(x, y)
        cr.stroke()
        last_pt = (x, y)

    # Start- und End-Marker
    sx, sy = project(points[0][0], points[0][1])
    ex, ey = project(points[-1][0], points[-1][1])
    cr.set_source_rgb(0.20, 0.65, 0.30)
    cr.arc(sx, sy, 5, 0, 6.2832)
    cr.fill()
    cr.set_source_rgb(0.85, 0.30, 0.30)
    cr.arc(ex, ey, 5, 0, 6.2832)
    cr.fill()


def _build_chart_widget(
    chart_state: dict,
    cursor_state: dict,
    on_cursor_change: "Callable",
    height: int = 180,
) -> Gtk.DrawingArea:
    """Generic metric/time chart. chart_state holds current pts, unit, color, fmt.
    pts = list of (ts, value|None, lat|None, lon|None).
    cursor_state['idx'] = active index into pts (-1 = none).
    """
    PAD_L, PAD_R, PAD_T, PAD_B = 40, 12, 10, 24
    area = Gtk.DrawingArea()
    area.set_content_height(height)
    area.set_hexpand(True)
    area.add_css_class("card")

    def _idx_from_px(px: float, w: float) -> int:
        pts = chart_state.get("pts") or []
        if not pts:
            return -1
        iw = max(1.0, w - PAD_L - PAD_R)
        ts0 = pts[0][0]
        t_span = max(1e-6, pts[-1][0] - ts0)
        target = ts0 + max(0.0, min(1.0, (px - PAD_L) / iw)) * t_span
        best, best_d = 0, abs(pts[0][0] - target)
        for i, (ts, *_) in enumerate(pts):
            d = abs(ts - target)
            if d < best_d:
                best_d = d
                best = i
        return best

    def _set_cursor(px: float, w: float) -> None:
        idx = _idx_from_px(px, w)
        if idx != cursor_state.get("idx", -1):
            cursor_state["idx"] = idx
            area.queue_draw()
            on_cursor_change()

    def _clear_cursor() -> None:
        if cursor_state.get("idx", -1) != -1:
            cursor_state["idx"] = -1
            area.queue_draw()
            on_cursor_change()

    def draw_cb(_area: Gtk.DrawingArea, cr: Any, w: int, h: int) -> None:
        pts = chart_state.get("pts") or []
        if len(pts) < 2:
            return
        valid_vals = [p[1] for p in pts if isinstance(p[1], (int, float)) and math.isfinite(p[1])]
        if not valid_vals:
            return

        dark = _is_dark()
        iw = max(1, w - PAD_L - PAD_R)
        ih = max(1, h - PAD_T - PAD_B)
        grid_rgba = (1.0, 1.0, 1.0, 0.18) if dark else (0.0, 0.0, 0.0, 0.15)
        text_rgba = (1.0, 1.0, 1.0, 0.55) if dark else (0.0, 0.0, 0.0, 0.55)
        color = chart_state.get("color", (0.34, 0.62, 0.86))
        fmt = chart_state.get("fmt", "{:.0f}")
        unit = chart_state.get("unit", "")

        ts0 = pts[0][0]
        t_span = max(1e-6, pts[-1][0] - ts0)
        v_min = min(valid_vals)
        v_max = max(valid_vals)
        v_pad = max(1e-6, v_max - v_min) * 0.08
        v_lo = v_min - v_pad
        v_hi = v_max + v_pad
        v_range = max(1e-6, v_hi - v_lo)

        def _vy(v: float) -> float:
            return PAD_T + ih - ((v - v_lo) / v_range) * ih

        def _tx(ts: float) -> float:
            return PAD_L + ((ts - ts0) / t_span) * iw

        # Grid lines
        cr.set_line_width(1.0)
        cr.set_source_rgba(*grid_rgba)
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = PAD_T + ih * (1.0 - frac)
            cr.move_to(PAD_L, y)
            cr.line_to(PAD_L + iw, y)
            cr.stroke()

        # Y-axis labels
        cr.set_source_rgba(*text_rgba)
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(10)
        for frac in (0.0, 0.5, 1.0):
            lbl_val = v_lo + frac * v_range
            if not math.isfinite(lbl_val):
                continue
            lbl = fmt.format(lbl_val)
            y = PAD_T + ih * (1.0 - frac) + 4
            cr.move_to(4, y)
            cr.show_text(lbl)

        # Build draw segments (skip None/NaN gaps)
        segments: list[list[tuple[float, float]]] = []
        seg: list[tuple[float, float]] = []
        for ts, v, *_ in pts:
            if not (isinstance(v, (int, float)) and math.isfinite(v)):
                if seg:
                    segments.append(seg)
                    seg = []
            else:
                seg.append((_tx(ts), _vy(v)))
        if seg:
            segments.append(seg)

        # Fill
        fill_rgba = (*color, 0.22)
        for seg in segments:
            if len(seg) < 2:
                continue
            cr.set_source_rgba(*fill_rgba)
            cr.move_to(seg[0][0], PAD_T + ih)
            for x, y in seg:
                cr.line_to(x, y)
            cr.line_to(seg[-1][0], PAD_T + ih)
            cr.close_path()
            cr.fill()

        # Line
        for seg in segments:
            if len(seg) < 2:
                continue
            cr.set_source_rgb(*color)
            cr.set_line_width(2.0)
            cr.move_to(*seg[0])
            for x, y in seg[1:]:
                cr.line_to(x, y)
            cr.stroke()

        # Cursor
        idx = cursor_state.get("idx", -1)
        if 0 <= idx < len(pts):
            ts_c, v_c, *_ = pts[idx]
            if v_c is not None:
                cx = _tx(ts_c)
                cy_dot = _vy(v_c)

                cr.set_source_rgba(1.0, 0.82, 0.1, 0.9)
                cr.set_line_width(1.5)
                cr.move_to(cx, PAD_T)
                cr.line_to(cx, PAD_T + ih)
                cr.stroke()

                cr.set_source_rgb(1.0, 0.82, 0.1)
                cr.arc(cx, cy_dot, 4, 0, 6.2832)
                cr.fill()

                cursor_lbl = fmt.format(v_c) + (" " + unit if unit else "")
                cr.set_font_size(11)
                te = cr.text_extents(cursor_lbl)
                lx = min(cx + 6, w - te.width - 6)
                ly = max(PAD_T + te.height + 4, cy_dot - 4)
                bg = (0.0, 0.0, 0.0, 0.6) if dark else (1.0, 1.0, 1.0, 0.82)
                cr.set_source_rgba(*bg)
                cr.rectangle(lx - 3, ly - te.height - 1, te.width + 6, te.height + 4)
                cr.fill()
                fg = (1.0, 1.0, 1.0) if dark else (0.0, 0.0, 0.0)
                cr.set_source_rgb(*fg)
                cr.move_to(lx, ly)
                cr.show_text(cursor_lbl)

    area.set_draw_func(draw_cb)

    # Pointer hover (mouse / stylus)
    motion_ctl = Gtk.EventControllerMotion()
    motion_ctl.connect("motion", lambda _c, x, _y: _set_cursor(x, area.get_width()))
    motion_ctl.connect("leave", lambda _c: _clear_cursor())
    area.add_controller(motion_ctl)

    # Touch: tap
    tap_ctl = Gtk.GestureClick()
    tap_ctl.connect("pressed", lambda _g, _n, x, _y: _set_cursor(x, area.get_width()))
    area.add_controller(tap_ctl)

    # Touch: drag / swipe
    drag_ctl = Gtk.GestureDrag()

    def _on_chart_drag_begin(g: Any, x: float, _y: float) -> None:
        # Claim the sequence so horizontal cursor-scrubbing on the chart does
        # not also drive the parent page-swipe / back-swipe gestures.
        g.set_state(Gtk.EventSequenceState.CLAIMED)
        _set_cursor(x, area.get_width())

    def _on_chart_drag_update(g: Any, off_x: float, _off_y: float) -> None:
        ok, sx, _sy = g.get_start_point()
        if ok:
            _set_cursor(sx + off_x, area.get_width())

    # CAPTURE phase: claim the sequence before parent swipe/back gestures.
    drag_ctl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    drag_ctl.connect("drag-begin", _on_chart_drag_begin)
    drag_ctl.connect("drag-update", _on_chart_drag_update)
    area.add_controller(drag_ctl)

    return area


# ---------------------------------------------------------------------------
# OSM tile rendering — pure Python/Cairo, no WebKit needed
# ---------------------------------------------------------------------------

_osm_tile_cache: dict[tuple[int, int, int], bytes] = {}
_osm_tile_lock = threading.Lock()
_OSM_TILE_CACHE_MAX = 1000
_TILE_PX = 256

# Ready-to-paint Cairo surfaces (raw tile + grayscale already applied).
# Caching at this level avoids the per-pixel grayscale loop on every re-open.
_osm_surface_cache: dict[tuple[int, int, int], Any] = {}
_osm_surface_lock = threading.Lock()
_OSM_SURFACE_CACHE_MAX = 256

# Persistent disk cache for OSM PNGs so re-opening a trip survives app restarts
# without re-hitting the tile server. Lives in XDG_CACHE_HOME.
_OSM_DISK_CACHE = Path(
    os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
) / "drivepulse" / "tiles"

# Shared thread pool — concurrent tile fetches massively beat the previous
# strict-serial loop (16 tiles × ~300 ms = ~5 s → ~1 s with 6 workers).
_osm_fetch_executor: concurrent.futures.ThreadPoolExecutor | None = None
_osm_fetch_executor_lock = threading.Lock()


def _osm_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _osm_fetch_executor
    with _osm_fetch_executor_lock:
        if _osm_fetch_executor is None:
            _osm_fetch_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=6, thread_name_prefix="osm-tile"
            )
        return _osm_fetch_executor


def _disk_tile_path(zoom: int, tx: int, ty: int) -> Path:
    return _OSM_DISK_CACHE / str(zoom) / str(tx) / f"{ty}.png"


def _lon_to_tx(lon: float, zoom: int) -> int:
    return int((lon + 180.0) / 360.0 * (1 << zoom))


def _lat_to_ty(lat: float, zoom: int) -> int:
    lat_r = math.radians(lat)
    return int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * (1 << zoom))


def _tx_to_lon(tx: int, zoom: int) -> float:
    return tx / (1 << zoom) * 360.0 - 180.0


def _ty_to_lat(ty: int, zoom: int) -> float:
    n = math.pi - 2.0 * math.pi * ty / (1 << zoom)
    return math.degrees(math.atan(math.sinh(n)))


_OSM_MAX_TILES = 4  # max tiles per axis at any zoom level


def _pick_zoom(lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> int:
    """Highest zoom where the bounding box fits in ≤_OSM_MAX_TILES tiles per axis."""
    for zoom in range(16, 9, -1):
        ntx = _lon_to_tx(lon_max, zoom) - _lon_to_tx(lon_min, zoom) + 1
        nty = _lat_to_ty(lat_min, zoom) - _lat_to_ty(lat_max, zoom) + 1
        if ntx <= _OSM_MAX_TILES and nty <= _OSM_MAX_TILES:
            return zoom
    return 10


def _fetch_osm_tile(zoom: int, tx: int, ty: int) -> bytes | None:
    """Returns raw PNG bytes for an OSM tile. RAM cache → disk cache → network."""
    key = (zoom, tx, ty)
    with _osm_tile_lock:
        cached = _osm_tile_cache.get(key)
    if cached is not None:
        return cached

    disk_path = _disk_tile_path(zoom, tx, ty)
    if disk_path.exists():
        try:
            data = disk_path.read_bytes()
        except OSError:
            data = None
        if data:
            with _osm_tile_lock:
                if len(_osm_tile_cache) >= _OSM_TILE_CACHE_MAX:
                    _osm_tile_cache.pop(next(iter(_osm_tile_cache)), None)
                _osm_tile_cache[key] = data
            return data

    try:
        req = urllib.request.Request(
            f"https://tile.openstreetmap.org/{zoom}/{tx}/{ty}.png",
            headers={"User-Agent": "DrivePulse/1.0 (GTK4 OBD dashboard)"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
    except Exception:
        return None
    with _osm_tile_lock:
        if len(_osm_tile_cache) >= _OSM_TILE_CACHE_MAX:
            _osm_tile_cache.pop(next(iter(_osm_tile_cache)), None)
        _osm_tile_cache[key] = data
    try:
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_bytes(data)
    except OSError:
        pass
    return data


def _get_tile_surface(zoom: int, tx: int, ty: int) -> Any:
    """Returns a draw-ready Cairo surface for the tile. Cached so the slow
    pure-Python grayscale conversion happens at most once per tile per session."""
    key = (zoom, tx, ty)
    with _osm_surface_lock:
        surf = _osm_surface_cache.get(key)
    if surf is not None:
        return surf
    data = _fetch_osm_tile(zoom, tx, ty)
    if not data:
        return None
    try:
        import cairo as _c
        raw = _c.ImageSurface.create_from_png(io.BytesIO(data))
        surf = _tile_to_grayscale(raw)
    except Exception:
        return None
    with _osm_surface_lock:
        if len(_osm_surface_cache) >= _OSM_SURFACE_CACHE_MAX:
            _osm_surface_cache.pop(next(iter(_osm_surface_cache)), None)
        _osm_surface_cache[key] = surf
    return surf


def _tile_to_grayscale(surf: Any) -> Any:
    """Convert OSM tile to grayscale using numpy (fast, << 1 ms per tile)."""
    try:
        import numpy as np
        import cairo as _c
        w, h = surf.get_width(), surf.get_height()
        out = _c.ImageSurface(_c.FORMAT_ARGB32, w, h)
        cr = _c.Context(out)
        cr.set_source_surface(surf, 0, 0)
        cr.paint()
        del cr
        out.flush()
        stride = out.get_stride()
        # writable view over surface pixels; Cairo ARGB32 LE = [B, G, R, A]
        arr = np.frombuffer(out.get_data(), dtype=np.uint8).reshape(h, stride // 4, 4)
        lum = (
            arr[:, :w, 0].astype(np.uint16) * 29    # B
            + arr[:, :w, 1].astype(np.uint16) * 150  # G
            + arr[:, :w, 2].astype(np.uint16) * 77   # R
        ) >> 8
        arr[:, :w, 0] = lum
        arr[:, :w, 1] = lum
        arr[:, :w, 2] = lum
        out.mark_dirty()
        return out
    except Exception:
        return surf  # fallback: show colour tile if numpy unavailable


def _build_osm_map_widget(
    gps_points: list[tuple[float, float, "float | None"]],
    chart_state: "dict | None" = None,
    cursor_state: "dict | None" = None,
    height: int = 300,
) -> "Gtk.DrawingArea | None":
    """Tile-stitched OSM map with pinch-zoom, finger-pan, double-tap reset.

    gps_points:   (lat, lon, speed_kmh) — GPS track
    chart_state:  shared dict with 'pts' = [(ts, val, lat, lon), ...] — for cursor dot
    cursor_state: shared dict with key 'idx' (index into chart_state['pts']); -1 = no cursor
    """
    try:
        import cairo as _cairo  # noqa: F401
    except ImportError:
        return None

    lats = [p[0] for p in gps_points]
    lons = [p[1] for p in gps_points]
    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)
    lat_pad = max((lat_max - lat_min) * 0.15, 0.003)
    lon_pad = max((lon_max - lon_min) * 0.15, 0.005)
    lat_min -= lat_pad; lat_max += lat_pad
    lon_min -= lon_pad; lon_max += lon_pad
    center_lat = (lat_min + lat_max) / 2
    center_lon = (lon_min + lon_max) / 2

    def _make_view(z: int, cx: float, cy: float) -> dict:
        # Use stored display bounds when available (set after the first draw);
        # fall back to the padded route bbox before the widget has been painted.
        # state does not exist yet on the very first call (during state = {...}),
        # so we catch the NameError and use the route bbox as initial fallback.
        try:
            dlat_min = state.get("disp_lat_min", lat_min)
            dlat_max = state.get("disp_lat_max", lat_max)
            dlon_min = state.get("disp_lon_min", lon_min)
            dlon_max = state.get("disp_lon_max", lon_max)
        except NameError:
            dlat_min, dlat_max, dlon_min, dlon_max = lat_min, lat_max, lon_min, lon_max
        ntx = max(3, min(_lon_to_tx(dlon_max, z) - _lon_to_tx(dlon_min, z) + 3, _OSM_MAX_TILES))
        nty = max(3, min(_lat_to_ty(dlat_min, z) - _lat_to_ty(dlat_max, z) + 3, _OSM_MAX_TILES))
        tx0 = int(cx - ntx / 2)
        ty0 = int(cy - nty / 2)
        tx1 = tx0 + ntx - 1
        ty1 = ty0 + nty - 1
        return {
            "n_tx": ntx, "n_ty": nty,
            "tx0": tx0, "ty0": ty0, "tx1": tx1, "ty1": ty1,
            "nw_lon": _tx_to_lon(tx0, z),      "nw_lat": _ty_to_lat(ty0, z),
            "se_lon": _tx_to_lon(tx1 + 1, z),  "se_lat": _ty_to_lat(ty1 + 1, z),
        }

    _init_zoom = _pick_zoom(lat_min, lat_max, lon_min, lon_max)
    _init_cx   = _lon_to_tx(center_lon, _init_zoom) + 0.5
    _init_cy   = _lat_to_ty(center_lat, _init_zoom) + 0.5

    state: dict[str, Any] = {
        "zoom": _init_zoom,
        "cx": _init_cx,
        "cy": _init_cy,
        **_make_view(_init_zoom, _init_cx, _init_cy),
        "surfaces": {},
        "loading": True,
        "pinch_scale": 1.0,
        "zoom_factor": 1.0,
        "pan_x": 0.0,
        "pan_y": 0.0,
    }
    area_holder: list[Gtk.DrawingArea] = []

    def draw_cb(_area: Gtk.DrawingArea, cr: Any, w: int, h: int) -> None:
        dark = _is_dark()
        cr.set_source_rgb(0.10, 0.12, 0.18) if dark else cr.set_source_rgb(0.90, 0.91, 0.93)
        cr.paint()

        z     = state["zoom"]
        pan_x = state["pan_x"]
        pan_y = state["pan_y"]
        # Combined zoom: persistent factor × live pinch gesture
        effective_zoom = state["zoom_factor"] * state["pinch_scale"]

        # ── Viewport bbox ─────────────────────────────────────────────────────
        # Base: padded route bbox, expanded to match widget aspect ratio so the
        # route fills the widget initially. zoom_factor persists across gestures.
        cos_mid = math.cos(math.radians((lat_min + lat_max) / 2))
        half_lat = (lat_max - lat_min) / 2
        half_lon = (lon_max - lon_min) / 2
        widget_ar = w / max(1, h)
        geo_ar = (half_lon * cos_mid) / max(1e-9, half_lat)
        if geo_ar > widget_ar:
            half_lat = (half_lon * cos_mid) / max(1e-9, widget_ar)
        else:
            half_lon = (half_lat * widget_ar) / max(1e-9, cos_mid)

        # Apply zoom
        half_lat /= effective_zoom
        half_lon /= effective_zoom

        # Pixel-to-degree scale factors (used by drag handler too)
        lat_per_px = (half_lat * 2) / max(1, h)
        lon_per_px = (half_lon * 2) / max(1, w)
        state["_lat_per_px"] = lat_per_px
        state["_lon_per_px"] = lon_per_px

        # Live drag offset + accumulated geo pan
        geo_pan_lat = state.get("geo_pan_lat", 0.0) + pan_y * lat_per_px
        geo_pan_lon = state.get("geo_pan_lon", 0.0) - pan_x * lon_per_px

        ctr_lat = (lat_min + lat_max) / 2 + geo_pan_lat
        ctr_lon = (lon_min + lon_max) / 2 + geo_pan_lon

        disp_lat_min = ctr_lat - half_lat
        disp_lat_max = ctr_lat + half_lat
        disp_lon_min = ctr_lon - half_lon
        disp_lon_max = ctr_lon + half_lon

        # Check if the loaded tile grid fully covers the visible viewport.
        # On the first draw (and on widget resize) the tile grid was built from
        # the route bbox which may be narrower than the aspect-ratio-expanded
        # viewport → blank strips at the edges.  Schedule exactly one reload
        # when a coverage gap is detected and no gesture is in progress.
        disp_bounds = (round(disp_lat_min, 4), round(disp_lat_max, 4),
                       round(disp_lon_min, 4), round(disp_lon_max, 4))
        if (state.get("_last_disp_bounds") != disp_bounds
                and pan_x == 0.0 and pan_y == 0.0
                and state["pinch_scale"] == 1.0
                and not state.get("_reload_pending")):
            state["_last_disp_bounds"] = disp_bounds
            need_tx0 = _lon_to_tx(disp_lon_min, z) - 1
            need_tx1 = _lon_to_tx(disp_lon_max, z) + 1
            need_ty0 = _lat_to_ty(disp_lat_max, z) - 1
            need_ty1 = _lat_to_ty(disp_lat_min, z) + 1
            if (need_tx0 < state["tx0"] or need_tx1 > state["tx1"] or
                    need_ty0 < state["ty0"] or need_ty1 > state["ty1"]):
                state["disp_lat_min"] = disp_lat_min
                state["disp_lat_max"] = disp_lat_max
                state["disp_lon_min"] = disp_lon_min
                state["disp_lon_max"] = disp_lon_max
                state["_reload_pending"] = True
                def _do_coverage_reload():
                    state["_reload_pending"] = False
                    _reload(state["zoom"], state["cx"], state["cy"])
                    return False
                GLib.idle_add(_do_coverage_reload)

        def proj(lat: float, lon: float) -> tuple[float, float]:
            fx = (lon - disp_lon_min) / max(1e-9, disp_lon_max - disp_lon_min)
            fy = (disp_lat_max - lat) / max(1e-9, disp_lat_max - disp_lat_min)
            return fx * w, fy * h

        # ── Tiles ─────────────────────────────────────────────────────────────
        # Each tile is placed at its geographic position in the viewport,
        # so tiles and GPS track share the same coordinate space.
        tile_alpha = 0.85 if dark else 0.95
        for (tz, ttx, tty), surf in list(state["surfaces"].items()):
            if tz != z:
                continue
            tnw_lon = _tx_to_lon(ttx, z)
            tse_lon = _tx_to_lon(ttx + 1, z)
            tnw_lat = _ty_to_lat(tty, z)
            tse_lat = _ty_to_lat(tty + 1, z)
            x0, y0 = proj(tnw_lat, tnw_lon)
            x1, y1 = proj(tse_lat, tse_lon)
            tdw, tdh = x1 - x0, y1 - y0
            if tdw < 1 or tdh < 1:
                continue
            cr.save()
            cr.translate(x0, y0)
            cr.scale(tdw / _TILE_PX, tdh / _TILE_PX)
            cr.set_source_surface(surf, 0, 0)
            cr.paint_with_alpha(tile_alpha)
            cr.restore()

        # ── GPS track (metric-colored) ────────────────────────────────────────
        # Use current chart_state pts for value-based coloring; fall back to speed.
        _cstate_pts = (chart_state or {}).get("pts") or []
        if _cstate_pts:
            _track = [(p[2], p[3], p[1]) for p in _cstate_pts]  # (lat, lon, value|None)
        else:
            _track = list(gps_points)  # (lat, lon, speed_kmh)
        _vals = [v for _, _, v in _track if v is not None and not math.isnan(v)]
        _vmin = min(_vals) if _vals else 0.0
        _vmax = max(_vals) if _vals else 0.0
        _vrange = max(1e-6, _vmax - _vmin)

        cr.set_line_cap(1)
        cr.set_line_join(1)

        # Shadow / outline stroke
        cr.set_line_width(5.5)
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.55)
        first_pt = True
        for lat, lon, _ in _track:
            px, py = proj(lat, lon)
            if first_pt:
                cr.move_to(px, py)
                first_pt = False
            else:
                cr.line_to(px, py)
        cr.stroke()

        # Colored segments: blau (niedrig) → grün → rot (hoch)
        cr.set_line_width(3.0)
        prev: tuple[float, float] | None = None
        for lat, lon, val in _track:
            px, py = proj(lat, lon)
            if val is not None and not math.isnan(val):
                t  = min(1.0, max(0.0, (val - _vmin) / _vrange))
                rr = 0.2 + 0.7 * t
                gg = 0.5 + 0.4 * (1 - abs(0.5 - t) * 2)
                bb = 0.9 - 0.8 * t
            else:
                rr, gg, bb = 0.4, 0.6, 0.9
            cr.set_source_rgb(rr, gg, bb)
            if prev is None:
                cr.move_to(px, py)
            else:
                cr.move_to(*prev)
                cr.line_to(px, py)
                cr.stroke()
            prev = (px, py)

        # ── Markers ───────────────────────────────────────────────────────────
        for lat, lon, fill in [
            (gps_points[0][0],  gps_points[0][1],  (0.13, 0.67, 0.27)),
            (gps_points[-1][0], gps_points[-1][1], (0.86, 0.21, 0.27)),
        ]:
            mx, my = proj(lat, lon)
            cr.set_source_rgb(1, 1, 1)
            cr.arc(mx, my, 7, 0, 6.2832)
            cr.fill()
            cr.set_source_rgb(*fill)
            cr.arc(mx, my, 5.5, 0, 6.2832)
            cr.fill()

        # ── Cursor dot ────────────────────────────────────────────────────────
        _cpts = (chart_state or {}).get("pts") or []
        if cursor_state is not None and _cpts:
            idx = cursor_state.get("idx", -1)
            if 0 <= idx < len(_cpts):
                clat = _cpts[idx][2]
                clon = _cpts[idx][3]
                if clat is not None and clon is not None:
                    dot_x, dot_y = proj(clat, clon)
                    cr.set_source_rgb(1.0, 0.9, 0.0)
                    cr.arc(dot_x, dot_y, 7, 0, 6.2832)
                    cr.fill()
                    cr.set_source_rgb(0.0, 0.0, 0.0)
                    cr.set_line_width(2.0)
                    cr.arc(dot_x, dot_y, 7, 0, 6.2832)
                    cr.stroke()

        # ── Loading overlay ───────────────────────────────────────────────────
        if state["loading"] and not state["surfaces"]:
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.8)
            cr.select_font_face("Sans", 0, 0)
            cr.set_font_size(13)
            text = "Loading map…"
            te = cr.text_extents(text)
            cr.move_to(w / 2 - te.width / 2, h / 2 + te.height / 2)
            cr.show_text(text)

    area = Gtk.DrawingArea()
    area.set_content_height(height)
    area.set_hexpand(True)
    area.add_css_class("card")
    area.set_draw_func(draw_cb)
    area_holder.append(area)

    # ── Tile loader ──────────────────────────────────────────────────────────

    def _start_fetch(v: dict[str, Any]) -> None:
        z = v["zoom"]
        coords = [
            (z, tx, ty)
            for ty in range(v["ty0"], v["ty1"] + 1)
            for tx in range(v["tx0"], v["tx1"] + 1)
        ]
        # Try the in-memory surface cache first — that's a synchronous, no-IO
        # path so anything already converted shows up instantly on the very next
        # draw without waiting on the thread pool.
        for coord in list(coords):
            with _osm_surface_lock:
                surf = _osm_surface_cache.get(coord)
            if surf is not None:
                state["surfaces"][coord] = surf
                coords.remove(coord)
        if state["surfaces"] and area_holder:
            GLib.idle_add(area_holder[0].queue_draw)

        if not coords:
            state["loading"] = False
            return

        # Fan the remaining tile loads out across the shared pool. Each worker
        # handles disk-cache lookup → network fetch → surface decode.
        executor = _osm_executor()
        futures = {executor.submit(_get_tile_surface, *c): c for c in coords}
        try:
            for fut in concurrent.futures.as_completed(futures):
                if state["zoom"] != z:
                    for pending in futures:
                        pending.cancel()
                    return
                coord = futures[fut]
                try:
                    surf = fut.result()
                except Exception:
                    surf = None
                if surf is None:
                    continue
                state["surfaces"][coord] = surf
                if area_holder:
                    GLib.idle_add(area_holder[0].queue_draw)
        finally:
            state["loading"] = False
            if area_holder:
                GLib.idle_add(area_holder[0].queue_draw)

    def _reload(z: int, cx: float, cy: float) -> None:
        v = {"zoom": z, "cx": cx, "cy": cy, **_make_view(z, cx, cy)}
        state.update(v)
        state["surfaces"] = {}
        state["loading"]  = True
        if area_holder:
            area_holder[0].queue_draw()
        threading.Thread(target=_start_fetch, args=(dict(state),), daemon=True).start()

    # ── Gestures ─────────────────────────────────────────────────────────────

    zoom_start_z: list[int] = [state["zoom"]]

    def _on_zoom_begin(gest: Any, seq: Any) -> None:
        # Claim the touch sequence so the parent page-switch swipe and the
        # NavigationView back-swipe stop seeing follow-up events on this map.
        gest.set_state(Gtk.EventSequenceState.CLAIMED)
        zoom_start_z[0] = state["zoom"]
        state["pinch_scale"] = 1.0

    def _on_scale_changed(gest: Any, scale: float) -> None:
        state["pinch_scale"] = max(0.25, min(4.0, scale))
        if area_holder:
            area_holder[0].queue_draw()

    def _on_zoom_end(gest: Any, seq: Any) -> None:
        # Commit the pinch factor into the persistent zoom_factor
        state["zoom_factor"] = max(0.1, state["zoom_factor"] * state["pinch_scale"])
        state["pinch_scale"] = 1.0
        delta = round(math.log2(max(0.01, state["zoom_factor"])))
        new_z = max(2, min(18, _init_zoom + delta))
        ctr_lat = (lat_min + lat_max) / 2 + state.get("geo_pan_lat", 0.0)
        ctr_lon = (lon_min + lon_max) / 2 + state.get("geo_pan_lon", 0.0)
        state["cx"] = _lon_to_tx(ctr_lon, new_z) + 0.5
        state["cy"] = _lat_to_ty(ctr_lat, new_z) + 0.5
        if new_z != state["zoom"]:
            state["zoom"] = new_z
            _reload(new_z, state["cx"], state["cy"])
        elif area_holder:
            area_holder[0].queue_draw()

    zoom_gest = Gtk.GestureZoom()
    zoom_gest.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    zoom_gest.connect("begin", _on_zoom_begin)
    zoom_gest.connect("scale-changed", _on_scale_changed)
    zoom_gest.connect("end", _on_zoom_end)
    area.add_controller(zoom_gest)

    def _on_drag_begin(gest: Any, x: float, y: float) -> None:
        # Claim the touch sequence: blocks the parent ViewStack horizontal
        # page-swipe and the Adw.NavigationView back-swipe while the user is
        # panning the map. Without this, dragging the map would also flip pages.
        gest.set_state(Gtk.EventSequenceState.CLAIMED)
        state["pan_x"] = 0.0
        state["pan_y"] = 0.0

    def _on_drag_update(gest: Any, off_x: float, off_y: float) -> None:
        state["pan_x"] = off_x
        state["pan_y"] = off_y
        if area_holder:
            area_holder[0].queue_draw()

    def _on_drag_end(gest: Any, off_x: float, off_y: float) -> None:
        state["geo_pan_lat"] = state.get("geo_pan_lat", 0.0) + off_y * state.get("_lat_per_px", 0.0)
        state["geo_pan_lon"] = state.get("geo_pan_lon", 0.0) - off_x * state.get("_lon_per_px", 0.0)
        state["pan_x"] = 0.0
        state["pan_y"] = 0.0
        ctr_lat = (lat_min + lat_max) / 2 + state["geo_pan_lat"]
        ctr_lon = (lon_min + lon_max) / 2 + state["geo_pan_lon"]
        z = state["zoom"]
        state["cx"] = _lon_to_tx(ctr_lon, z) + 0.5
        state["cy"] = _lat_to_ty(ctr_lat, z) + 0.5
        _reload(z, state["cx"], state["cy"])

    drag_gest = Gtk.GestureDrag()
    # CAPTURE phase: this controller sees the touch sequence before parent
    # gestures (ViewStack page-swipe, NavigationView back-swipe), so its
    # drag-begin can claim the sequence before any of them lock on.
    drag_gest.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    drag_gest.connect("drag-begin",  _on_drag_begin)
    drag_gest.connect("drag-update", _on_drag_update)
    drag_gest.connect("drag-end",    _on_drag_end)
    drag_gest.group(zoom_gest)   # cooperate: 2-finger zoom cancels 1-finger pan
    area.add_controller(drag_gest)

    def _on_tap(gest: Any, n_press: int, x: float, y: float) -> None:
        if n_press == 2:
            state["geo_pan_lat"] = 0.0
            state["geo_pan_lon"] = 0.0
            state["zoom_factor"] = 1.0
            state["pinch_scale"] = 1.0
            state["cx"]    = _init_cx
            state["cy"]    = _init_cy
            state["pan_x"] = 0.0
            state["pan_y"] = 0.0
            _reload(_init_zoom, _init_cx, _init_cy)

    tap_gest = Gtk.GestureClick()
    tap_gest.connect("pressed", _on_tap)
    area.add_controller(tap_gest)

    threading.Thread(target=_start_fetch, args=(dict(state),), daemon=True).start()
    return area


def _load_profiles(db: DriveDB | None = None) -> list[dict[str, Any]]:
    """Liefert alle bekannten Autos: aus JSON-Profilen + aus der DB, per VIN gemerged."""
    entries: list[dict[str, Any]] = []
    seen_vins: set[str] = set()

    if PROFILES_DIR.exists():
        for path in sorted(PROFILES_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            vin = _extract_inner_string(data.get("vin"))
            brand = _wmi_to_brand(vin)
            try:
                dt = datetime.fromisoformat(str(data.get("scanned_at", "")).replace("Z", "+00:00"))
                scan_label = dt.strftime("%d.%m.%Y")
            except Exception:
                scan_label = ""
            entries.append({
                "path": path,
                "data": data,
                "vin": vin,
                "brand": brand,
                "scan_label": scan_label,
                "car_id": None,
                "trip_count": 0,
                "total_km": 0.0,
            })
            if vin:
                seen_vins.add(vin)

    if db is not None:
        try:
            db_cars = db.list_cars()
        except Exception:
            db_cars = []
        # JSON-Einträge mit DB-Daten anreichern
        for entry in entries:
            if not entry["vin"]:
                continue
            for row in db_cars:
                if (row["vin"] or "") == entry["vin"]:
                    entry["car_id"] = int(row["id"])
                    entry["trip_count"] = int(row["trip_count"] or 0)
                    entry["total_km"] = float(row["total_km"] or 0.0)
                    break
        # DB-Autos ohne passendes JSON-Profil zusätzlich aufnehmen
        for row in db_cars:
            vin = row["vin"] or ""
            if vin and vin in seen_vins:
                continue
            entries.append({
                "path": None,
                "data": {
                    "vehicle_info": {
                        "VIN": vin or None,
                        "CALIBRATION_ID": row["cal_id"],
                        "CVN": row["cvn"],
                    },
                    "protocol": row["protocol"],
                    "scanned_at": row["first_seen"],
                    "live_data": {},
                },
                "vin": vin,
                "brand": row["brand"] or _wmi_to_brand(vin),
                "scan_label": "",
                "car_id": int(row["id"]),
                "trip_count": int(row["trip_count"] or 0),
                "total_km": float(row["total_km"] or 0.0),
            })
    return entries


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class CarsPage(Gtk.Box):
    """Zweistufige Navigation: Fahrzeug-Liste → Werte-Detail."""

    __gtype_name__ = "CarsPage"

    LIVE_ID = "__live__"
    LIVE_DETAIL_RENDER_INTERVAL_S = 0.25

    def __init__(self, language: str = SOURCE_LANGUAGE, db: DriveDB | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.language = _normalize_language(language)
        self.db = db
        self._latest_live: dict[str, Any] = {}
        self._live_identity: dict[str, str] = {}
        self._obd_connected = False
        self._profiles: list[dict[str, Any]] = []
        self._selected_source: str = self.LIVE_ID
        self._selected_car_id: int | None = None
        self._selected_category: str = CATEGORIES[0][0]
        self._detail_pushed = False
        self.set_header_trash_fn: Any = None
        self._trip_detail_pushed = False
        self._trip_detail_page: Adw.NavigationPage | None = None
        self._scan_detail_pushed = False
        self._scan_detail_page: Adw.NavigationPage | None = None
        self._scan_id_shown: int | None = None
        self._live_row: Adw.ActionRow | None = None
        self._last_live_detail_render = -self.LIVE_DETAIL_RENDER_INTERVAL_S
        self._narrow = False
        self._cat_rows: list[Gtk.ListBoxRow] = []
        self._trip_select_mode: bool = False
        self._trip_selected_ids: set[int] = set()
        # Wird vom DashboardWindow gesetzt: Callback, wenn der Anwender auf der
        # Wurzel (Auto-Liste) nach rechts wischt, um zum vorherigen Tab zurückzukehren.
        self.on_back_swipe: Callable[[], None] | None = None
        self._drag_claimed = False

        self.nav_view = Adw.NavigationView()
        self.nav_view.set_hexpand(True)
        self.nav_view.set_vexpand(True)
        self.nav_view.connect("popped", self._on_popped)
        self.append(self.nav_view)

        self._build_list_page()
        self._build_detail_page()
        self.refresh_profiles()

        # Horizontaler Drag in CAPTURE-Phase: greift Wisch-Gesten ab, bevor
        # Adw.NavigationView sie zu fassen bekommt. So funktioniert „nach rechts
        # zurück zum vorherigen Tab" auch auf der Auto-Liste, wo Adw selbst
        # nichts poppen würde.
        drag = Gtk.GestureDrag()
        drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

    # ---------------------------------------------------- Wisch-Gesten

    def _on_drag_begin(self, _gesture: Gtk.GestureDrag, _x: float, _y: float) -> None:
        self._drag_claimed = False

    def _on_drag_update(self, gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        if self._drag_claimed:
            return
        # Wenn Detail offen ist: Adw.NavigationView soll selbst zurückpoppen.
        if self._detail_pushed:
            return
        # Eindeutig horizontale Geste (mind. 20 px, klar dominanter X-Anteil)
        if abs(offset_x) > 20 and abs(offset_x) > abs(offset_y) * 1.5:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._drag_claimed = True

    def _on_drag_end(self, _gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        if not self._drag_claimed:
            return
        self._drag_claimed = False
        # Nur Wisch nach rechts (offset_x > 0) löst „zurück zum vorherigen Tab" aus.
        if offset_x > 60 and self.on_back_swipe is not None:
            self.on_back_swipe()

    # ---------------------------------------------------- List-Aufbau

    def _build_list_page(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_margin_top(16)
        outer.set_margin_bottom(16)
        outer.set_margin_start(16)
        outer.set_margin_end(16)

        self._list_intro = Gtk.Label(xalign=0.0)
        self._list_intro.add_css_class("dim-label")
        self._list_intro.set_wrap(True)
        outer.append(self._list_intro)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.set_valign(Gtk.Align.START)
        outer.append(self._list_box)

        self._empty_label = Gtk.Label(xalign=0.0)
        self._empty_label.add_css_class("dim-label")
        self._empty_label.set_wrap(True)
        self._empty_label.set_visible(False)
        outer.append(self._empty_label)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_hexpand(True)
        scroll.set_child(outer)

        self._list_page = Adw.NavigationPage(
            child=scroll,
            title=_translate(self.language, "nav.cars"),
        )
        self._list_page.set_tag("list")
        self.nav_view.add(self._list_page)
        self._refresh_list_texts()

    def _refresh_list_texts(self) -> None:
        self._list_intro.set_text(_translate(self.language, "cars.list.intro"))
        self._empty_label.set_text(_translate(self.language, "cars.empty"))

    # ---------------------------------------------------- Detail-Aufbau

    def _build_detail_page(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.set_margin_start(8)
        head.set_margin_top(8)
        head.set_margin_end(12)
        head.set_margin_bottom(4)
        self._detail_back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        self._detail_back_btn.add_css_class("flat")
        self._detail_back_btn.set_tooltip_text(_translate(self.language, "cars.back"))
        self._detail_back_btn.connect("clicked", lambda _b: self.nav_view.pop())
        self._detail_title = Gtk.Label(xalign=0.0)
        self._detail_title.add_css_class("title-3")
        self._detail_title.set_hexpand(True)

        head.append(self._detail_back_btn)
        head.append(self._detail_title)
        outer.append(head)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.set_hexpand(True)
        body.set_vexpand(True)

        self._sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._sidebar.set_margin_top(12)
        self._sidebar.set_margin_bottom(12)
        self._sidebar.set_margin_start(8)
        self._sidebar.set_margin_end(4)

        self._categories_label = Gtk.Label(xalign=0.0)
        self._categories_label.add_css_class("heading")
        self._sidebar.append(self._categories_label)

        self.category_list = Gtk.ListBox()
        self.category_list.set_selection_mode(Gtk.SelectionMode.BROWSE)
        self.category_list.add_css_class("navigation-sidebar")
        self.category_list.connect("row-selected", self._on_category_selected)
        for cat_key, cat_name_key, icon_name, _items in CATEGORIES:
            row = Gtk.ListBoxRow()
            row.cat_key = cat_key  # type: ignore[attr-defined]
            row.cat_label_key = cat_name_key  # type: ignore[attr-defined]

            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            hbox.set_margin_top(8)
            hbox.set_margin_bottom(8)
            hbox.set_margin_start(8)
            hbox.set_margin_end(8)

            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.set_pixel_size(18)
            hbox.append(icon)

            lbl = Gtk.Label(label=_translate(self.language, cat_name_key), xalign=0.0)
            lbl.set_hexpand(True)
            hbox.append(lbl)

            row.cat_label_widget = lbl  # type: ignore[attr-defined]
            row.cat_icon_widget = icon  # type: ignore[attr-defined]
            row.cat_hbox = hbox  # type: ignore[attr-defined]
            row.set_tooltip_text(_translate(self.language, cat_name_key))

            row.set_child(hbox)
            self.category_list.append(row)
            self._cat_rows.append(row)

        cat_scroll = Gtk.ScrolledWindow()
        cat_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        cat_scroll.set_vexpand(True)
        cat_scroll.set_child(self.category_list)
        self._sidebar.append(cat_scroll)
        body.append(self._sidebar)
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        self._apply_narrow_to_sidebar()

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        content.set_hexpand(True)
        content.set_vexpand(True)

        self.content_title = Gtk.Label(xalign=0.0)
        self.content_title.add_css_class("title-2")
        self.content_title.set_margin_top(12)
        self.content_title.set_margin_start(16)
        content.append(self.content_title)

        self.content_subtitle = Gtk.Label(xalign=0.0)
        self.content_subtitle.add_css_class("dim-label")
        self.content_subtitle.set_margin_start(16)
        self.content_subtitle.set_margin_bottom(8)
        content.append(self.content_subtitle)

        self.value_list = Gtk.ListBox()
        self.value_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.value_list.add_css_class("boxed-list")
        self.value_list.set_margin_start(16)
        self.value_list.set_margin_end(16)
        self.value_list.set_margin_bottom(16)
        self.value_list.set_valign(Gtk.Align.START)

        value_scroll = Gtk.ScrolledWindow()
        value_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        value_scroll.set_vexpand(True)
        value_scroll.set_hexpand(True)
        value_scroll.set_child(self.value_list)
        content.append(value_scroll)

        # Selection action bar (trips multi-select mode)
        self._select_count_lbl = Gtk.Label(xalign=0.0)
        self._select_count_lbl.set_hexpand(True)

        self._select_delete_btn = Gtk.Button()
        self._select_delete_btn.add_css_class("destructive-action")
        self._select_delete_btn.connect("clicked", lambda _b: self._confirm_delete_selected_trips())

        _sel_cancel_btn = Gtk.Button(label="")
        _sel_cancel_btn.add_css_class("flat")
        _sel_cancel_btn.connect("clicked", lambda _b: self._exit_trip_select_mode())

        self._select_cancel_btn = _sel_cancel_btn

        sel_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sel_bar.set_margin_start(16)
        sel_bar.set_margin_end(16)
        sel_bar.set_margin_top(8)
        sel_bar.set_margin_bottom(8)
        sel_bar.append(self._select_count_lbl)
        sel_bar.append(self._select_delete_btn)
        sel_bar.append(_sel_cancel_btn)

        self._select_revealer = Gtk.Revealer()
        self._select_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self._select_revealer.set_reveal_child(False)
        self._select_revealer.set_child(sel_bar)
        content.append(self._select_revealer)

        body.append(content)
        outer.append(body)

        self._categories_label.set_text(_translate(self.language, "cars.categories"))

        self._detail_page = Adw.NavigationPage(child=outer, title="")
        self._detail_page.set_tag("detail")

        first_row = self.category_list.get_row_at_index(0)
        if first_row is not None:
            self.category_list.select_row(first_row)

    # ---------------------------------------------------- öffentliche API

    def is_detail_open(self) -> bool:
        """True, solange die Detail-Seite im NavigationView gepusht ist."""
        return self._detail_pushed

    def set_narrow(self, narrow: bool) -> None:
        """Auf Smartphone-Breiten: Labels ausblenden, nur Icons zeigen."""
        if narrow == self._narrow:
            return
        self._narrow = narrow
        self._apply_narrow_to_sidebar()

    def _apply_narrow_to_sidebar(self) -> None:
        narrow = self._narrow
        # Sidebar-Breite umstellen
        self._sidebar.set_size_request(56 if narrow else 220, -1)
        # Überschrift „Kategorien" ausblenden, wenn schmal
        self._categories_label.set_visible(not narrow)
        for row in self._cat_rows:
            lbl = getattr(row, "cat_label_widget", None)
            hbox = getattr(row, "cat_hbox", None)
            if lbl is not None:
                lbl.set_visible(not narrow)
            if hbox is not None:
                hbox.set_halign(Gtk.Align.CENTER if narrow else Gtk.Align.FILL)

    def set_language(self, language: str) -> None:
        self.language = _normalize_language(language)
        self._list_page.set_title(_translate(self.language, "nav.cars"))
        self._categories_label.set_text(_translate(self.language, "cars.categories"))
        self._detail_back_btn.set_tooltip_text(_translate(self.language, "cars.back"))
        self._refresh_list_texts()
        self._rebuild_list()
        for row in self._cat_rows:
            key = getattr(row, "cat_label_key", None)
            lbl = getattr(row, "cat_label_widget", None)
            if key and lbl:
                translated = _translate(self.language, key)
                lbl.set_text(translated)
                row.set_tooltip_text(translated)
        if self._detail_pushed:
            self._render_detail()

    def refresh_profiles(self) -> None:
        self._profiles = _load_profiles(self.db)
        self._rebuild_list()

    def update_live(self, payload: dict[str, Any]) -> None:
        if not payload:
            return
        source = payload.get("source", "")
        if source in ("obd", "mock"):
            for k, v in payload.items():
                if k.startswith("_") or k in ("source", "timestamp", "connection_status", "mock_reason"):
                    continue
                self._latest_live[k] = v
            self._obd_connected = source == "obd"
            self._update_live_row_subtitle()
        if self._selected_source == self.LIVE_ID and self._detail_pushed and self._live_detail_render_due():
            self._render_detail()

    def set_live_identity(self, identity: dict[str, str]) -> None:
        self._live_identity = dict(identity)
        self._update_live_row_subtitle()
        if self._selected_source == self.LIVE_ID and self._detail_pushed:
            self._last_live_detail_render = time.monotonic()
            self._render_detail()

    def _live_detail_render_due(self) -> bool:
        now = time.monotonic()
        if now - self._last_live_detail_render < self.LIVE_DETAIL_RENDER_INTERVAL_S:
            return False
        self._last_live_detail_render = now
        return True

    # ---------------------------------------------------- Listen-Render

    def _rebuild_list(self) -> None:
        while True:
            child = self._list_box.get_first_child()
            if child is None:
                break
            self._list_box.remove(child)

        # Live-Zeile immer oben
        self._live_row = Adw.ActionRow()
        self._live_row.set_title(_translate(self.language, "cars.live.title"))
        self._live_row.set_activatable(True)
        live_icon = Gtk.Image.new_from_icon_name("network-wireless-symbolic")
        self._live_row.add_prefix(live_icon)
        chevron = Gtk.Image.new_from_icon_name("go-next-symbolic")
        self._live_row.add_suffix(chevron)
        self._live_row.connect("activated", lambda _r: self._open_detail(self.LIVE_ID))
        self._list_box.append(self._live_row)
        self._update_live_row_subtitle()

        for entry in self._profiles:
            row = Adw.ActionRow()
            vin = entry.get("vin", "")
            brand = entry.get("brand") or ""
            title = brand if brand else (f"VIN …{vin[-5:]}" if vin else _translate(self.language, "cars.unknown"))
            row.set_title(GLib.markup_escape_text(title))
            sub_parts: list[str] = []
            if vin:
                sub_parts.append(f"VIN …{vin[-5:]}")
            cal = _extract_inner_string((entry["data"].get("vehicle_info") or {}).get("CALIBRATION_ID"))
            if cal:
                sub_parts.append(f"Cal {cal}")
            if entry.get("scan_label"):
                sub_parts.append(entry["scan_label"])
            row.set_subtitle(GLib.markup_escape_text(" · ".join(sub_parts)) if sub_parts else "—")
            row.set_activatable(True)
            icon = Gtk.Image.new_from_icon_name("preferences-system-symbolic")
            row.add_prefix(icon)
            chev = Gtk.Image.new_from_icon_name("go-next-symbolic")
            row.add_suffix(chev)
            row.connect("activated", lambda _r, p=str(entry["path"]): self._open_detail(p))
            self._list_box.append(row)

        self._empty_label.set_visible(not self._profiles)

    def _update_live_row_subtitle(self) -> None:
        row = self._live_row
        if row is None:
            return
        vin = _extract_inner_string(self._live_identity.get("VIN"))
        if vin:
            brand = _wmi_to_brand(vin)
            sub = f"{brand} · …{vin[-5:]}" if brand else f"…{vin[-5:]}"
        elif self._obd_connected:
            sub = _translate(self.language, "cars.live.connected")
        else:
            sub = _translate(self.language, "cars.live.subtitle")
        row.set_subtitle(GLib.markup_escape_text(sub))

    # ---------------------------------------------------- Detail-Navigation

    def _open_detail(self, source: str) -> None:
        self._selected_source = source
        self._selected_car_id = None
        if source == self.LIVE_ID:
            title = _translate(self.language, "cars.live.title")
        else:
            entry = next((e for e in self._profiles if str(e.get("path")) == source), None)
            if entry:
                vin = entry.get("vin", "")
                brand = entry.get("brand") or ""
                title = brand if brand else _translate(self.language, "cars.unknown")
                if vin:
                    title = f"{title} · …{vin[-5:]}"
                self._selected_car_id = entry.get("car_id")
            else:
                title = _translate(self.language, "cars.unknown")
        self._detail_page.set_title(title)
        self._detail_title.set_text(title)
        # Show trash only for real vehicles, not the live view
        if source != self.LIVE_ID and self._selected_car_id is not None:
            self._set_trash(self._confirm_delete_vehicle)
        else:
            self._set_trash(None)
        self._render_detail()
        if not self._detail_pushed:
            self.nav_view.push(self._detail_page)
            self._detail_pushed = True

    def _on_popped(self, _view: Adw.NavigationView, page: Adw.NavigationPage) -> None:
        if page is self._detail_page:
            self._detail_pushed = False
            self._trip_select_mode = False
            self._trip_selected_ids = set()
            self._select_revealer.set_reveal_child(False)
            self._set_trash(None)
        if page is self._trip_detail_page:
            self._trip_detail_pushed = False
            self._trip_detail_page = None
            if self._detail_pushed and self._selected_category == "trips":
                self._render_detail()
            # Restore vehicle delete action when returning to vehicle detail
            if self._detail_pushed and self._selected_car_id is not None:
                self._set_trash(self._confirm_delete_vehicle)
        if page is self._scan_detail_page:
            self._scan_detail_pushed = False
            self._scan_detail_page = None
            self._scan_id_shown = None
            if self._detail_pushed and self._selected_category == "scans":
                self._render_detail()
            if self._detail_pushed and self._selected_car_id is not None:
                self._set_trash(self._confirm_delete_vehicle)

    def _set_trash(self, action_fn: Any) -> None:
        if self.set_header_trash_fn is not None:
            self.set_header_trash_fn(action_fn)

    def _on_category_selected(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        new_cat = getattr(row, "cat_key", CATEGORIES[0][0])
        if self._trip_select_mode and new_cat != "trips":
            self._trip_select_mode = False
            self._trip_selected_ids = set()
            self._select_revealer.set_reveal_child(False)
        self._selected_category = new_cat
        if self._detail_pushed:
            self._render_detail()

    # ---------------------------------------------------- Daten + Detail-Render

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
        for entry in self._profiles:
            if str(entry["path"]) == self._selected_source:
                vin = entry.get("vin", "")
                brand = entry.get("brand") or ""
                label = brand if brand else _translate(self.language, "cars.unknown")
                if vin:
                    label = f"{label} · …{vin[-5:]}"
                return self._flatten_profile(entry["data"]), label
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
        dtcs = data.get("dtcs") or []
        none_text = _translate(self.language, "cars.dtc.none")
        out[_SPECIAL_DTC] = none_text if not dtcs else "  ".join(str(d) for d in dtcs)
        pending = data.get("pending_dtcs") or []
        out[_SPECIAL_PENDING] = none_text if not pending else "  ".join(str(d) for d in pending)
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
        while True:
            child = self.value_list.get_first_child()
            if child is None:
                break
            self.value_list.remove(child)

        cat_meta = next((c for c in CATEGORIES if c[0] == self._selected_category), CATEGORIES[0])
        cat_key, cat_name_key, _icon_name, items = cat_meta
        self.content_title.set_text(_translate(self.language, cat_name_key))

        data, source_label = self._current_data()
        self.content_subtitle.set_text("")

        if cat_key == "trips":
            self._render_trips_into_value_list()
            return

        if cat_key == "scans":
            self._render_scans_into_value_list()
            return

        for pid_key, label_key in items:
            raw = data.get(pid_key)
            value_text, is_unknown = self._format_entry(pid_key, raw)
            label = _translate(self.language, label_key)
            self.value_list.append(self._make_stacked_row(label, value_text, is_unknown))

    def _make_inline_row(self, pid_key: str, label: str, value_text: str, is_unknown: bool) -> Adw.ActionRow:
        row = Adw.ActionRow()
        row.set_title(GLib.markup_escape_text(label))
        row.set_subtitle(GLib.markup_escape_text(pid_key) if not pid_key.startswith("__") else "")

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

    def _render_trips_into_value_list(self) -> None:
        if self.db is None or self._selected_car_id is None:
            self.value_list.append(self._info_row(_translate(self.language, "cars.trips.empty")))
            return
        try:
            trips = self.db.list_trips_for_car(self._selected_car_id)
        except Exception:
            trips = []
        if not trips:
            self.value_list.append(self._info_row(_translate(self.language, "cars.trips.empty")))
            return
        for trip in trips:
            self.value_list.append(self._make_trip_row(trip))

    def _info_row(self, text: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        lbl = Gtk.Label(label=text, xalign=0.0)
        lbl.add_css_class("dim-label")
        lbl.set_wrap(True)
        lbl.set_margin_top(10)
        lbl.set_margin_bottom(10)
        lbl.set_margin_start(14)
        lbl.set_margin_end(14)
        row.set_child(lbl)
        return row

    def _make_trip_row(self, trip: Any) -> Adw.ActionRow:
        row = Adw.ActionRow()
        trip_id = int(trip["id"])
        started = self._parse_ts(trip["started_at"])
        title = started.strftime("%d.%m.%Y · %H:%M") if started else _translate(self.language, "cars.trip.title", id=trip_id)
        row.set_title(GLib.markup_escape_text(title))

        parts: list[str] = []
        dur = trip["duration_s"]
        if dur:
            mins = int(dur // 60)
            secs = int(dur % 60)
            parts.append(f"{mins} min {secs:02d} s" if mins else f"{secs} s")
        km = trip["distance_km"]
        if km is not None:
            parts.append(f"{km:.1f} km")
        vmax = trip["max_speed_kmh"]
        if vmax is not None:
            parts.append(f"max {vmax:.0f} km/h")
        n = trip["samples_count"] or 0
        parts.append(f"{n} {_translate(self.language, 'cars.trip.samples')}")
        if trip["ended_at"] is None:
            parts.append(f"⏺ {_translate(self.language, 'cars.trip.ongoing')}")
        row.set_subtitle(GLib.markup_escape_text(" · ".join(parts)))

        if self._trip_select_mode:
            chk = Gtk.CheckButton()
            chk.set_active(trip_id in self._trip_selected_ids)
            chk.set_valign(Gtk.Align.CENTER)
            chk.connect("toggled", lambda c, tid=trip_id: self._on_trip_checkbox_toggled(tid, c.get_active()))
            row.add_prefix(chk)
            row.set_activatable(False)
        else:
            icon = Gtk.Image.new_from_icon_name("mark-location-symbolic")
            row.add_prefix(icon)
            chev = Gtk.Image.new_from_icon_name("go-next-symbolic")
            row.add_suffix(chev)
            row.set_activatable(True)
            row.connect("activated", lambda _r, tid=trip_id: self._open_trip_detail(tid))
            lp = Gtk.GestureLongPress()
            lp.connect("pressed", lambda _g, _x, _y, tid=trip_id: self._enter_trip_select_mode(tid))
            row.add_controller(lp)

        return row

    def _parse_ts(self, raw: Any) -> datetime | None:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            return None

    # ---------------------------------------------------- Fahrt-Detail-Page

    def _open_trip_detail(self, trip_id: int) -> None:
        if self.db is None:
            return
        try:
            samples = list(self.db.samples_for_trip(trip_id))
            trips = self.db.list_trips_for_car(self._selected_car_id) if self._selected_car_id else []
            trip = next((t for t in trips if int(t["id"]) == trip_id), None)
        except Exception:
            samples, trip = [], None
        if trip is None:
            return

        page_content = _build_trip_detail_widget(self.language, trip, samples)
        title = self._trip_detail_title(trip)

        self._set_trash(lambda: self._confirm_delete_trip(trip_id))

        page = Adw.NavigationPage(child=page_content, title=title)
        page.set_tag(f"trip-{trip_id}")
        self._trip_detail_page = page
        self._trip_detail_pushed = True
        self.nav_view.push(page)

    def _make_delete_dialog(self, heading_key: str, body_key: str) -> Adw.AlertDialog:
        """Create a destructive AlertDialog with a red heading."""
        try:
            dark = Adw.StyleManager.get_default().get_dark()
        except Exception:
            dark = True
        color = "#ff7b63" if dark else "#e01b24"
        heading = _translate(self.language, heading_key)
        body = _translate(self.language, body_key)
        dialog = Adw.AlertDialog()
        dialog.set_heading_use_markup(True)
        dialog.set_heading(f'<span foreground="{color}"><b>{GLib.markup_escape_text(heading)}</b></span>')
        dialog.set_body(body)
        dialog.add_response("cancel", _translate(self.language, "cars.trip.delete_cancel"))
        dialog.add_response("delete", _translate(self.language, "cars.trip.delete_confirm"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        return dialog

    def _confirm_delete_trip(self, trip_id: int) -> None:
        dialog = self._make_delete_dialog("cars.trip.delete_title", "cars.trip.delete_body")
        dialog.connect("response", lambda _d, resp: self._delete_trip(trip_id) if resp == "delete" else None)
        dialog.present(self)

    def _delete_trip(self, trip_id: int) -> None:
        if self.db is None:
            return
        try:
            self.db.delete_trip(trip_id)
        except Exception:
            return
        if self._trip_detail_page is not None:
            self.nav_view.pop()

    # ---------------------------------------------------- Scan löschen

    def _confirm_delete_scan(self, scan_id: int) -> None:
        dialog = self._make_delete_dialog("cars.scan.delete_title", "cars.scan.delete_body")
        dialog.connect("response", lambda _d, r: self._delete_scan(scan_id) if r == "delete" else None)
        dialog.present(self)

    def _delete_scan(self, scan_id: int) -> None:
        if self.db is None:
            return
        try:
            self.db.delete_scan(scan_id)
        except Exception:
            return
        if self._scan_detail_page is not None:
            self.nav_view.pop()
        self._scan_id_shown = None
        self._render_detail()

    # ---------------------------------------------------- Fahrzeug löschen

    def _confirm_delete_vehicle(self) -> None:
        dialog = self._make_delete_dialog("cars.vehicle.delete_title", "cars.vehicle.delete_body")
        dialog.connect("response", lambda _d, r: self._delete_vehicle() if r == "delete" else None)
        dialog.present(self)

    def _delete_vehicle(self) -> None:
        if self.db and self._selected_car_id:
            try:
                self.db.delete_car(self._selected_car_id)
            except Exception:
                pass
        entry = next(
            (e for e in self._profiles if e.get("path") and str(e["path"]) == self._selected_source),
            None,
        )
        if entry and entry.get("path"):
            try:
                Path(entry["path"]).unlink(missing_ok=True)
            except Exception:
                pass
        if self._detail_pushed:
            self.nav_view.pop()
        GLib.idle_add(self.refresh_profiles)

    # ---------------------------------------------------- Fahrten Multi-Auswahl

    def _enter_trip_select_mode(self, trip_id: int) -> None:
        self._trip_select_mode = True
        self._trip_selected_ids = {trip_id}
        self._render_detail()
        self._update_select_bar()
        self._select_revealer.set_reveal_child(True)

    def _exit_trip_select_mode(self) -> None:
        self._trip_select_mode = False
        self._trip_selected_ids = set()
        self._select_revealer.set_reveal_child(False)
        self._render_detail()

    def _on_trip_checkbox_toggled(self, trip_id: int, active: bool) -> None:
        if active:
            self._trip_selected_ids.add(trip_id)
        else:
            self._trip_selected_ids.discard(trip_id)
        self._update_select_bar()

    def _update_select_bar(self) -> None:
        n = len(self._trip_selected_ids)
        self._select_count_lbl.set_text(
            _translate(self.language, "cars.trip.selected_count", n=n)
        )
        self._select_delete_btn.set_label(_translate(self.language, "cars.trip.delete_confirm"))
        self._select_delete_btn.set_sensitive(n > 0)
        self._select_cancel_btn.set_label(_translate(self.language, "cars.trip.delete_cancel"))

    def _confirm_delete_selected_trips(self) -> None:
        n = len(self._trip_selected_ids)
        if n == 0:
            return
        dialog = self._make_delete_dialog("cars.trip.delete_title", "cars.trip.delete_title")
        # Override body with dynamic count text
        dialog.set_body(_translate(self.language, "cars.trip.delete_multi_body", n=n))
        dialog.connect("response", lambda _d, r: self._delete_selected_trips() if r == "delete" else None)
        dialog.present(self)

    def _delete_selected_trips(self) -> None:
        if self.db is None:
            return
        for tid in list(self._trip_selected_ids):
            try:
                self.db.delete_trip(tid)
            except Exception:
                pass
        self._exit_trip_select_mode()

    def _trip_detail_title(self, trip: Any) -> str:
        started = self._parse_ts(trip["started_at"])
        if started is None:
            return _translate(self.language, "cars.trip.title", id=int(trip["id"]))
        return started.strftime("%d.%m.%Y %H:%M")

    # ---------------------------------------------------- Scan-Liste & Detail

    def _render_scans_into_value_list(self) -> None:
        if self.db is None or self._selected_car_id is None:
            self.value_list.append(self._info_row(_translate(self.language, "cars.scans.empty")))
            return
        try:
            scans = self.db.list_scans_for_car(self._selected_car_id)
        except Exception:
            scans = []
        if not scans:
            self.value_list.append(self._info_row(_translate(self.language, "cars.scans.empty")))
            return
        for i, scan in enumerate(scans):
            prev = scans[i + 1] if i + 1 < len(scans) else None
            self.value_list.append(self._make_scan_row(scan, prev))

    def _make_scan_row(self, scan: Any, prev_scan: Any | None) -> Adw.ActionRow:
        row = Adw.ActionRow()
        ts = self._parse_ts(scan["scanned_at"])
        title = ts.strftime("%d.%m.%Y · %H:%M") if ts else str(scan["id"])
        row.set_title(GLib.markup_escape_text(title))

        dtc = int(scan["dtc_count"] or 0)
        pending = int(scan["pending_dtc_count"] or 0)
        pids = int(scan["pids_count"] or 0)

        # DTC trend vs. previous scan
        if prev_scan is None:
            trend = _translate(self.language, "cars.scan.trend_first")
        else:
            delta = dtc - int(prev_scan["dtc_count"] or 0)
            if delta > 0:
                trend = _translate(self.language, "cars.scan.trend_up", delta=delta)
            elif delta < 0:
                trend = _translate(self.language, "cars.scan.trend_down", delta=abs(delta))
            else:
                trend = _translate(self.language, "cars.scan.trend_same")

        parts = [
            f"{dtc} {_translate(self.language, 'cars.scan.dtc_count')}",
            f"{pending} {_translate(self.language, 'cars.scan.pending_count')}",
            f"{pids} {_translate(self.language, 'cars.scan.pids_count')}",
            trend,
        ]
        row.set_subtitle(GLib.markup_escape_text(" · ".join(parts)))
        row.set_activatable(True)

        # Colour-code the DTC count badge
        badge = Gtk.Label(label=str(dtc))
        badge.add_css_class("pill" if dtc == 0 else "error")
        badge.add_css_class("caption")
        badge.set_halign(Gtk.Align.END)
        row.add_suffix(badge)
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row.connect("activated", lambda _r, sid=int(scan["id"]): self._open_scan_detail(sid))
        return row

    def _open_scan_detail(self, scan_id: int) -> None:
        if self.db is None:
            return
        try:
            data = self.db.get_scan_data(scan_id)
            scans = self.db.list_scans_for_car(self._selected_car_id) if self._selected_car_id else []
            scan_meta = next((s for s in scans if int(s["id"]) == scan_id), None)
            # Previous scan for trend context
            idx = next((i for i, s in enumerate(scans) if int(s["id"]) == scan_id), None)
            prev_meta = scans[idx + 1] if idx is not None and idx + 1 < len(scans) else None
        except Exception:
            return
        if scan_meta is None:
            return

        page_content = _build_scan_detail_widget(self.language, scan_meta, prev_meta, data)
        ts = self._parse_ts(scan_meta["scanned_at"])
        title = _translate(self.language, "cars.scan.title",
                           date=ts.strftime("%d.%m.%Y %H:%M") if ts else str(scan_id))

        self._set_trash(lambda: self._confirm_delete_scan(scan_id))

        page = Adw.NavigationPage(child=page_content, title=title)
        page.set_tag(f"scan-{scan_id}")
        self._scan_detail_page = page
        self._scan_detail_pushed = True
        self._scan_id_shown = scan_id
        self.nav_view.push(page)

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
