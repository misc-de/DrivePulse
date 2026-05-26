"""OSM tile rendering — pure Python/Cairo, no WebKit needed.

Tile coordinate math, RAM/disk/threadpool tile cache, grayscale-on-paint
conversion, and the ``_build_osm_map_widget`` factory that returns a
DrawingArea with pinch-zoom, drag-pan and double-tap reset gestures.
"""
from __future__ import annotations

import concurrent.futures
import io
import math
import os
import threading
import urllib.request
from pathlib import Path
from typing import Any

from gi.repository import Adw, GLib, Gtk

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)


def _is_dark() -> bool:
    try:
        return Adw.StyleManager.get_default().get_dark()
    except Exception:
        log.debug("Adw.StyleManager.get_dark failed, defaulting to dark", exc_info=True)
        return True

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
