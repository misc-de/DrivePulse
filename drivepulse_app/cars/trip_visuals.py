"""Trip charts, GPS-track drawing and OSM map rendering for the Cars page.

The heavyweight widgets live in private sibling modules:
- ``_trip_chart._build_chart_widget`` — interactive metric/time chart
- ``_osm_map._build_osm_map_widget`` — tile-stitched OSM map

They're re-exported at the bottom of this module so existing
``from drivepulse_app.cars.trip_visuals import …`` imports keep working.
"""
from __future__ import annotations

import math
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from drivepulse_app.cars.metadata import _CHART_METRICS
from drivepulse_app.common import _translate
from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)


def lift_dropdown_popover(dropdown: Gtk.DropDown, y_offset_px: int = -10) -> None:
    """Shift a Gtk.DropDown's popover by y_offset_px when it opens.

    Negative values move the popover up; default -10 lifts it 10 px so the
    options sit a little above their default position.
    """
    child = dropdown.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Popover):
            child.set_offset(0, y_offset_px)
            return
        child = child.get_next_sibling()


def speed_to_rgb(spd: float | None, vmax: float) -> tuple[float, float, float]:
    """Map a sample speed to (r, g, b) on the blue→green→red speed gradient.

    Same colour ramp as the Fahrtenbuch's _draw_gps_track so all trip
    visualisations share one palette.
    """
    if spd is None or vmax <= 0:
        return (0.4, 0.6, 0.9)
    t = min(1.0, spd / max(1.0, vmax))
    r = 0.2 + 0.7 * t
    g = 0.5 + 0.4 * (1 - abs(0.5 - t) * 2)
    b = 0.9 - 0.8 * t
    return (r, g, b)


def build_trip_metric_data(
    samples: list, language: str
) -> tuple[dict[str, list], list[tuple[str, str, str, tuple[float, float, float], str]]]:
    """Build per-metric point lists for the trip chart.

    Returns ``(metric_data, avail)`` where ``metric_data[key]`` is a list of
    ``(ts, value|None, lat, lon)`` tuples (one per GPS sample) and ``avail``
    is the list of ``(key, label, unit, color, fmt)`` tuples for metrics
    that have enough valid samples to be worth plotting.
    """
    base = [s for s in samples if s["lat"] is not None and s["lon"] is not None]

    def _finite(v: Any) -> bool:
        try:
            return math.isfinite(float(v))
        except (TypeError, ValueError):
            return False

    min_valid = max(2, int(len(base) * 0.30))
    metric_data: dict[str, list] = {}
    for mk, _ml, _mu, _mc, _mf in _CHART_METRICS:
        pts = [
            (s["ts"], s[mk] if _finite(s[mk]) else None, s["lat"], s["lon"])
            for s in base
        ]
        if sum(1 for p in pts if p[1] is not None) >= min_valid:
            metric_data[mk] = pts

    if len(base) >= 2:
        cum_km = 0.0
        elapsed_pts: list = []
        prev_s = None
        for s in base:
            if prev_s is not None:
                dlat = math.radians(s["lat"] - prev_s["lat"])
                dlon = math.radians(s["lon"] - prev_s["lon"])
                a = (
                    math.sin(dlat / 2) ** 2
                    + math.cos(math.radians(prev_s["lat"]))
                    * math.cos(math.radians(s["lat"]))
                    * math.sin(dlon / 2) ** 2
                )
                cum_km += 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
            elapsed_pts.append((s["ts"], cum_km, s["lat"], s["lon"]))
            prev_s = s
        if cum_km > 0.01:
            metric_data["elapsed_km"] = elapsed_pts

    avail: list = [
        (k, _translate(language, lbl), u, c, f)
        for k, lbl, u, c, f in _CHART_METRICS
        if k in metric_data
    ]
    if "elapsed_km" in metric_data:
        avail.append(
            (
                "elapsed_km",
                _translate(language, "cars.metric.elapsed_km"),
                "km",
                (0.20, 0.75, 0.60),
                "{:.2f}",
            )
        )
    return metric_data, avail


def _is_dark() -> bool:
    try:
        return Adw.StyleManager.get_default().get_dark()
    except Exception:
        log.debug("Adw.StyleManager.get_dark failed, defaulting to dark", exc_info=True)
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
    draw_w: float
    draw_h: float
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


# Re-exports for callers that still import these via trip_visuals (tests +
# trip_widgets + map.replay). Done after the helpers above are defined so a
# future from-import in the private modules wouldn't trigger a circular load.
from drivepulse_app.cars._osm_map import _build_osm_map_widget  # noqa: E402
from drivepulse_app.cars._trip_chart import _build_chart_widget  # noqa: E402

__all__ = [
    "_build_chart_widget",
    "_build_osm_map_widget",
    "_draw_gps_track",
    "_is_dark",
    "build_trip_metric_data",
    "lift_dropdown_popover",
    "speed_to_rgb",
]

