"""Autos-Browser: Liste bekannter Fahrzeuge → Detail mit kategorisierten Werten."""
from __future__ import annotations

import http.server
import json
import math
import re
import socketserver
import threading
from datetime import datetime
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

CATEGORIES: tuple[tuple[str, str, str, tuple[tuple[str, str], ...]], ...] = (
    ("vehicle", "cars.category.vehicle", "dialog-information-symbolic", (
        (_SPECIAL_VIN,        "cars.pid.VIN"),
        (_SPECIAL_CAL,        "cars.pid.CAL"),
        (_SPECIAL_CVN,        "cars.pid.CVN"),
        (_SPECIAL_PROTO,      "cars.pid.PROTO"),
        ("011C",              "cars.pid.011C"),
        (_SPECIAL_SCAN_DATE,  "cars.pid.SCAN_DATE"),
    )),
    ("engine", "cars.category.engine", "applications-engineering-symbolic", (
        ("010C", "cars.pid.010C"),
        ("0104", "cars.pid.0104"),
        ("0143", "cars.pid.0143"),
        ("010E", "cars.pid.010E"),
        ("011F", "cars.pid.011F"),
        ("0142", "cars.pid.0142"),
        (_SPECIAL_ADAPTER_V, "cars.pid.ATRV"),
    )),
    ("drive", "cars.category.drive", "media-seek-forward-symbolic", (
        ("010D", "cars.pid.010D"),
        ("0131", "cars.pid.0131"),
        ("0121", "cars.pid.0121"),
        ("0130", "cars.pid.0130"),
    )),
    ("temperatures", "cars.category.temperatures", "weather-clear-symbolic", (
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
    ("trips", "cars.category.trips", "document-open-recent-symbolic", ()),
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

    # --- GPS-Track / OSM Map ---
    gps_points = [(s["lat"], s["lon"], s["speed_kmh"]) for s in samples
                  if s["lat"] is not None and s["lon"] is not None]
    if gps_points:
        gps_title = Gtk.Label(label=_translate(language, "cars.trip.route"), xalign=0.0)
        gps_title.add_css_class("heading")
        outer.append(gps_title)
        map_widget = _build_osm_map_widget(gps_points)
        if map_widget is not None:
            outer.append(map_widget)
        else:
            gps_area = Gtk.DrawingArea()
            gps_area.set_content_height(240)
            gps_area.set_hexpand(True)
            gps_area.add_css_class("card")
            gps_area.set_draw_func(lambda area, cr, w, h, pts=gps_points: _draw_gps_track(cr, w, h, pts))
            outer.append(gps_area)

    # --- Geschwindigkeitsverlauf ---
    speed_series = [(s["ts"], s["speed_kmh"]) for s in samples if s["speed_kmh"] is not None]
    if speed_series:
        sp_title = Gtk.Label(label=_translate(language, "cars.trip.speed_chart"), xalign=0.0)
        sp_title.add_css_class("heading")
        outer.append(sp_title)
        sp_area = Gtk.DrawingArea()
        sp_area.set_content_height(180)
        sp_area.set_hexpand(True)
        sp_area.add_css_class("card")
        sp_area.set_draw_func(lambda area, cr, w, h, s=speed_series: _draw_speed_series(cr, w, h, s))
        outer.append(sp_area)

    if not gps_points and not speed_series:
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


def _draw_speed_series(cr: Any, width: int, height: int, series: list[tuple[float, float]]) -> None:
    if len(series) < 2:
        return
    pad_l, pad_r, pad_t, pad_b = 36, 12, 10, 22
    iw = max(1, width - pad_l - pad_r)
    ih = max(1, height - pad_t - pad_b)
    ts0 = series[0][0]
    ts1 = series[-1][0]
    t_span = max(1e-6, ts1 - ts0)
    v_max = max(s[1] for s in series)
    v_max_disp = max(20.0, math.ceil(v_max / 20.0) * 20.0)

    # Achsen
    cr.set_source_rgba(1, 1, 1, 0.20)
    cr.set_line_width(1.0)
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = pad_t + ih - frac * ih
        cr.move_to(pad_l, y)
        cr.line_to(pad_l + iw, y)
        cr.stroke()
    cr.set_source_rgba(1, 1, 1, 0.55)
    cr.select_font_face("Sans")
    cr.set_font_size(10)
    for frac in (0.0, 0.5, 1.0):
        label = f"{int(v_max_disp * frac)}"
        y = pad_t + ih - frac * ih + 4
        cr.move_to(4, y)
        cr.show_text(label)

    # Polyline + leicht gefüllte Fläche
    cr.set_line_width(2.0)
    cr.set_source_rgba(0.34, 0.62, 0.86, 0.25)
    cr.move_to(pad_l, pad_t + ih)
    for ts, v in series:
        x = pad_l + ((ts - ts0) / t_span) * iw
        y = pad_t + ih - (min(v, v_max_disp) / v_max_disp) * ih
        cr.line_to(x, y)
    cr.line_to(pad_l + iw, pad_t + ih)
    cr.close_path()
    cr.fill()

    cr.set_source_rgb(0.34, 0.62, 0.86)
    first = True
    for ts, v in series:
        x = pad_l + ((ts - ts0) / t_span) * iw
        y = pad_t + ih - (min(v, v_max_disp) / v_max_disp) * ih
        if first:
            cr.move_to(x, y)
            first = False
        else:
            cr.line_to(x, y)
    cr.stroke()


class _TileProxyHandler(http.server.BaseHTTPRequestHandler):
    """Proxies OSM tile requests from WebKit to tile.openstreetmap.org.

    WebKit's content-process sandbox can silently block tile images even when
    script loading works.  Routing through localhost bypasses this because
    WebKit always allows loopback connections.
    """

    _cache: dict[str, bytes] = {}
    _cache_lock = threading.Lock()
    _MAX_TILES = 800  # ~20–40 MB depending on tile size

    def do_GET(self) -> None:
        # self.path is e.g. "/15/17602/11365.png"
        import urllib.request as _ur
        cache_key = self.path
        with self._cache_lock:
            data = self._cache.get(cache_key)
        if data is None:
            try:
                req = _ur.Request(
                    "https://tile.openstreetmap.org" + self.path,
                    headers={"User-Agent": "DrivePulse/1.0 (GTK4 OBD dashboard)"},
                )
                with _ur.urlopen(req, timeout=10) as resp:
                    data = resp.read()
                with self._cache_lock:
                    if len(self._cache) >= self._MAX_TILES:
                        oldest = next(iter(self._cache))
                        del self._cache[oldest]
                    self._cache[cache_key] = data
            except Exception:
                self.send_error(502)
                return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args: object) -> None:
        pass  # suppress access log noise


_tile_proxy_port: int | None = None
_tile_proxy_lock = threading.Lock()


def _get_tile_proxy_port() -> int:
    """Start the tile proxy on first call and return its port (lazy singleton)."""
    global _tile_proxy_port
    with _tile_proxy_lock:
        if _tile_proxy_port is None:
            server = socketserver.TCPServer(("127.0.0.1", 0), _TileProxyHandler)
            server.allow_reuse_address = True
            _tile_proxy_port = server.server_address[1]
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
    return _tile_proxy_port


def _build_osm_map_widget(
    gps_points: list[tuple[float, float, float | None]],
    height: int = 300,
) -> "Gtk.Widget | None":
    """Return a WebKit WebView showing the GPS track on OpenStreetMap.

    Returns None when WebKit is not available so callers can fall back to the
    Cairo drawing.
    """
    try:
        import gi as _gi
        try:
            _gi.require_version("WebKit", "6.0")
            from gi.repository import WebKit as _WebKit  # type: ignore[attr-defined]
            print("[OSM] using WebKit 6.0")
        except (ValueError, ImportError):
            _gi.require_version("WebKit2", "4.1")
            from gi.repository import WebKit2 as _WebKit  # type: ignore[attr-defined,no-redef]
            print("[OSM] using WebKit2 4.1")
    except Exception as e:
        print(f"[OSM] WebKit not available: {e} — falling back to Cairo")
        return None

    pts_data = [
        [lat, lon, spd if spd is not None else -1.0]
        for lat, lon, spd in gps_points
    ]
    speeds = [s for _, _, s in gps_points if s is not None]
    max_speed = max(speeds) if speeds else 1.0
    pts_json = json.dumps(pts_data)

    # Route tiles through a local Python proxy so WebKit's content-process
    # sandbox cannot block them.  Leaflet still loads from the CDN (scripts are
    # not affected by the sandbox), but tile images go via 127.0.0.1.
    try:
        port = _get_tile_proxy_port()
    except Exception:
        port = None

    if port is not None:
        tile_url = f"http://127.0.0.1:{port}/{{z}}/{{x}}/{{y}}.png"
        tile_opts = "maxZoom:19,attribution:'\\u00a9 <a href=\"https://www.openstreetmap.org/copyright\">OpenStreetMap</a> contributors'"
    else:
        # Fallback: direct OSM (may not work in sandboxed WebKit)
        tile_url = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        tile_opts = "maxZoom:19,attribution:'\\u00a9 OpenStreetMap contributors'"

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
html,body{{width:100%;height:{height}px;overflow:hidden;background:#1a1a2e;}}
#map{{width:100%;height:{height}px;}}
#msg{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
      color:#aaa;font:14px sans-serif;text-align:center;pointer-events:none;}}
</style>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
</head>
<body>
<div id="map"></div>
<div id="msg">Loading map…</div>
<script>
window.addEventListener('load', function() {{
  var msg = document.getElementById('msg');
  if (typeof L === 'undefined') {{
    msg.textContent = 'Map unavailable — no internet?';
    return;
  }}
  msg.style.display = 'none';
  var pts = {pts_json};
  var maxSpd = {max_speed:.2f};
  var map = L.map('map', {{attributionControl:true, zoomControl:true}});
  L.tileLayer('{tile_url}', {{{tile_opts}}}).addTo(map);
  function spColor(spd) {{
    if (spd < 0 || maxSpd <= 0) return '#6699ee';
    var t = Math.min(1.0, spd / Math.max(1.0, maxSpd));
    return 'rgb(' + Math.round(51+178*t) + ',' + Math.round(128+102*(1-Math.abs(0.5-t)*2)) + ',' + Math.round(230-204*t) + ')';
  }}
  for (var i = 1; i < pts.length; i++) {{
    L.polyline([[pts[i-1][0],pts[i-1][1]],[pts[i][0],pts[i][1]]], {{
      color: spColor(pts[i][2]), weight: 3.5, opacity: 0.9
    }}).addTo(map);
  }}
  if (pts.length > 0) {{
    L.circleMarker([pts[0][0],pts[0][1]], {{radius:7,color:'#fff',weight:2,fillColor:'#22aa44',fillOpacity:1}}).addTo(map);
    L.circleMarker([pts[pts.length-1][0],pts[pts.length-1][1]], {{radius:7,color:'#fff',weight:2,fillColor:'#dd3333',fillOpacity:1}}).addTo(map);
  }}
  if (pts.length > 1) {{
    var lats = pts.map(function(p){{return p[0];}});
    var lons = pts.map(function(p){{return p[1];}});
    map.fitBounds([[Math.min.apply(null,lats),Math.min.apply(null,lons)],
                   [Math.max.apply(null,lats),Math.max.apply(null,lons)]], {{padding:[20,20]}});
  }} else {{
    map.setView([pts[0][0],pts[0][1]], 15);
  }}
}});
</script>
</body>
</html>"""

    try:
        settings = _WebKit.Settings()
        settings.set_enable_javascript(True)
        view = _WebKit.WebView()
        view.set_settings(settings)
        view.set_size_request(-1, height)
        view.set_hexpand(True)
        view.load_html(html, None)
        print(f"[OSM] WebView created, tile proxy port={port}, {len(gps_points)} GPS points")
        return view
    except Exception as e:
        print(f"[OSM] WebView creation failed: {e} — falling back to Cairo")
        return None


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
        self._trip_detail_pushed = False
        self._trip_detail_page: Adw.NavigationPage | None = None
        self._scan_detail_pushed = False
        self._scan_detail_page: Adw.NavigationPage | None = None
        self._live_row: Adw.ActionRow | None = None
        self._narrow = False
        self._cat_rows: list[Gtk.ListBoxRow] = []
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
        if self._selected_source == self.LIVE_ID and self._detail_pushed:
            self._render_detail()

    def set_live_identity(self, identity: dict[str, str]) -> None:
        self._live_identity = dict(identity)
        self._update_live_row_subtitle()
        if self._selected_source == self.LIVE_ID and self._detail_pushed:
            self._render_detail()

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
        self._render_detail()
        if not self._detail_pushed:
            self.nav_view.push(self._detail_page)
            self._detail_pushed = True

    def _on_popped(self, _view: Adw.NavigationView, page: Adw.NavigationPage) -> None:
        if page is self._detail_page:
            self._detail_pushed = False
        if page is self._trip_detail_page:
            self._trip_detail_pushed = False
            self._trip_detail_page = None
            if self._detail_pushed and self._selected_category == "trips":
                self._render_detail()
        if page is self._scan_detail_page:
            self._scan_detail_pushed = False
            self._scan_detail_page = None
            if self._detail_pushed and self._selected_category == "scans":
                self._render_detail()

    def _on_category_selected(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        self._selected_category = getattr(row, "cat_key", CATEGORIES[0][0])
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
        self.content_subtitle.set_text(
            _translate(self.language, "cars.source.label", source=source_label)
        )

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

        row.set_activatable(True)
        icon = Gtk.Image.new_from_icon_name("mark-location-symbolic")
        row.add_prefix(icon)
        chev = Gtk.Image.new_from_icon_name("go-next-symbolic")
        row.add_suffix(chev)
        row.connect("activated", lambda _r, tid=trip_id: self._open_trip_detail(tid))
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
        page = Adw.NavigationPage(child=page_content, title=title)
        page.set_tag(f"trip-{trip_id}")
        self._trip_detail_page = page
        self._trip_detail_pushed = True
        self.nav_view.push(page)

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
        page = Adw.NavigationPage(child=page_content, title=title)
        page.set_tag(f"scan-{scan_id}")
        self._scan_detail_page = page
        self._scan_detail_pushed = True
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
