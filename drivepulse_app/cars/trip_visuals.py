"""Trip charts, GPS track drawing and OSM map rendering for the Cars page."""
from __future__ import annotations

import concurrent.futures
import io
import math
import os
import threading
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

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


def _build_chart_widget(
    chart_state: dict,
    cursor_state: dict,
    on_cursor_change: Callable,
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

    def draw_cb(area: Gtk.DrawingArea, cr: Any, w: int, h: int) -> None:
        pts = chart_state.get("pts") or []
        if len(pts) < 2:
            return
        valid_vals = [p[1] for p in pts if isinstance(p[1], (int, float)) and math.isfinite(p[1])]
        if not valid_vals:
            return

        dark = _is_dark()
        iw = max(1, w - PAD_L - PAD_R)
        ih = max(1, h - PAD_T - PAD_B)
        grid_rgba = (1.0, 1.0, 1.0, 0.45) if dark else (0.0, 0.0, 0.0, 0.45)
        text_rgba = (1.0, 1.0, 1.0, 0.95) if dark else (0.0, 0.0, 0.0, 0.95)
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
                extra_fn = chart_state.get("cursor_extra_fn")
                extra_lbl = extra_fn(ts_c) if extra_fn else None
                cr.set_font_size(11)
                te = cr.text_extents(cursor_lbl)
                te2 = cr.text_extents(extra_lbl) if extra_lbl else None
                box_w = (max(te.width, te2.width) if te2 else te.width) + 6
                line_h = te.height + 4
                box_h = line_h + (te2.height + 3 if te2 else 0) + 1
                lx = min(cx + 6, w - box_w - 3)
                ly = max(PAD_T + te.height + 4, cy_dot - 4)
                bg = (0.0, 0.0, 0.0, 0.6) if dark else (1.0, 1.0, 1.0, 0.82)
                cr.set_source_rgba(*bg)
                cr.rectangle(lx - 3, ly - te.height - 1, box_w, box_h)
                cr.fill()
                fg = (1.0, 1.0, 1.0) if dark else (0.0, 0.0, 0.0)
                cr.set_source_rgb(*fg)
                cr.move_to(lx, ly)
                cr.show_text(cursor_lbl)
                if extra_lbl:
                    cr.move_to(lx, ly + line_h)
                    cr.show_text(extra_lbl)

    area.set_draw_func(draw_cb)

    # Pointer hover (mouse / stylus). Touch and pressed-pointer drag go
    # through the scrub gesture below — EventControllerMotion only reliably
    # fires for non-pressed pointer motion (hover).
    _hovering = [False]
    motion_ctl = Gtk.EventControllerMotion()

    def _on_pointer_enter(_c: Any, _x: float, _y: float) -> None:
        _hovering[0] = True

    def _on_pointer_motion(_c: Any, x: float, _y: float) -> None:
        _set_cursor(x, area.get_width())

    def _on_pointer_leave(_c: Any) -> None:
        _hovering[0] = False
        _clear_cursor()

    motion_ctl.connect("enter", _on_pointer_enter)
    motion_ctl.connect("motion", _on_pointer_motion)
    motion_ctl.connect("leave", _on_pointer_leave)
    area.add_controller(motion_ctl)

    # Scrub gesture for tap + drag, touch + pointer. Using the base
    # Gtk.GestureSingle directly gives us begin/update/end signals from
    # Gtk.Gesture that fire for EVERY event of the sequence with NO
    # threshold — unlike Gtk.GestureDrag (8 px) or Gtk.GestureClick (may
    # self-cancel on drag). Claiming the sequence in begin keeps a parent
    # ScrolledWindow's kinetic-scroll gesture from stealing horizontal or
    # vertical drag motion.
    scrub_ctl = Gtk.GestureSingle()
    scrub_ctl.set_button(0)  # any pointer button + touch
    scrub_ctl.set_touch_only(False)
    scrub_ctl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

    def _track(gesture: Any, sequence: Any) -> None:
        ok, x, _y = gesture.get_point(sequence)
        if ok:
            _set_cursor(x, area.get_width())

    def _on_scrub_begin(gesture: Any, sequence: Any) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        _track(gesture, sequence)

    def _on_scrub_update(gesture: Any, sequence: Any) -> None:
        _track(gesture, sequence)

    scrub_ctl.connect("begin", _on_scrub_begin)
    scrub_ctl.connect("update", _on_scrub_update)
    area.add_controller(scrub_ctl)

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


_OSM_INIT_FIT_TILES = 4   # _pick_zoom: highest zoom where route bbox fits in ≤N tiles
_OSM_MAX_VIEW_TILES = 10  # _make_view: cap on the loaded tile grid per axis


def _pick_zoom(lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> int:
    """Highest zoom where the bounding box fits in ≤_OSM_INIT_FIT_TILES tiles per axis."""
    for zoom in range(16, 9, -1):
        ntx = _lon_to_tx(lon_max, zoom) - _lon_to_tx(lon_min, zoom) + 1
        nty = _lat_to_ty(lat_min, zoom) - _lat_to_ty(lat_max, zoom) + 1
        if ntx <= _OSM_INIT_FIT_TILES and nty <= _OSM_INIT_FIT_TILES:
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
    except Exception as exc:
        log.info("Could not fetch OSM tile z=%s x=%s y=%s: %s", zoom, tx, ty, exc)
        return None
    with _osm_tile_lock:
        if len(_osm_tile_cache) >= _OSM_TILE_CACHE_MAX:
            _osm_tile_cache.pop(next(iter(_osm_tile_cache)), None)
        _osm_tile_cache[key] = data
    try:
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_bytes(data)
    except OSError as exc:
        log.info("Could not write OSM tile cache %s: %s", disk_path, exc)
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
    except Exception as exc:
        log.info("Could not build Cairo surface for OSM tile z=%s x=%s y=%s: %s", zoom, tx, ty, exc)
        return None
    with _osm_surface_lock:
        if len(_osm_surface_cache) >= _OSM_SURFACE_CACHE_MAX:
            _osm_surface_cache.pop(next(iter(_osm_surface_cache)), None)
        _osm_surface_cache[key] = surf
    return surf


def _tile_to_grayscale(surf: Any) -> Any:
    """Convert OSM tile to grayscale using numpy (fast, << 1 ms per tile)."""
    try:
        import cairo as _c
        import numpy as np
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
    gps_points: list[tuple[float, float, float | None]],
    chart_state: dict | None = None,
    cursor_state: dict | None = None,
    height: int = 300,
) -> Gtk.DrawingArea | None:
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
    lat_min -= lat_pad
    lat_max += lat_pad
    lon_min -= lon_pad
    lon_max += lon_pad
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
        ntx = max(3, min(_lon_to_tx(dlon_max, z) - _lon_to_tx(dlon_min, z) + 3, _OSM_MAX_VIEW_TILES))
        nty = max(3, min(_lat_to_ty(dlat_min, z) - _lat_to_ty(dlat_max, z) + 3, _OSM_MAX_VIEW_TILES))
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
    area.add_controller(drag_gest)
    drag_gest.group(zoom_gest)   # cooperate: 2-finger zoom cancels 1-finger pan

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

    def center_on(lat: float, lon: float) -> None:
        """Shift the map center to (lat, lon) without changing the zoom level."""
        base_lat = (lat_min + lat_max) / 2
        base_lon = (lon_min + lon_max) / 2
        state["geo_pan_lat"] = lat - base_lat
        state["geo_pan_lon"] = lon - base_lon
        state["pan_x"] = 0.0
        state["pan_y"] = 0.0
        z = state["zoom"]
        state["cx"] = _lon_to_tx(lon, z) + 0.5
        state["cy"] = _lat_to_ty(lat, z) + 0.5
        if area_holder:
            area_holder[0].queue_draw()

    threading.Thread(target=_start_fetch, args=(dict(state),), daemon=True).start()
    return area, center_on
