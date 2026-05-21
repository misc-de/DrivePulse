"""Map page — OpenStreetMap navigation with GPS tracking and routing.

Backend priority:
  1. WebKit (MapLibre GL JS) — 3D vector tiles, pitch, bearing-follow
  2. Shumate (native GTK4)  — 2D raster tiles, offline-friendly
  3. Placeholder             — neither library available

Mixins:
  MapWebKitMixin   (map_webkit.py)  — WebKit/MapLibre backend
  MapShumateMixin  (map_shumate.py) — Shumate raster backend
  MapLayoutMixin   (map_layout.py)  — UI construction (_build_* methods, CSS, step list)
  MapTourMixin     (map_tour.py)    — Tour state machine, TTS, step detection
  MapTrafficMixin  (map_traffic.py) — Autobahn traffic API, filtering, popover
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk  # noqa: E402

from .common import SOURCE_LANGUAGE, _normalize_language, _translate
from .diagnostics import get_logger
from . import tts_service
from .map_shumate import SHUMATE_OK, MapShumateMixin
from .map_webkit import WEBKIT_OK, MapWebKitMixin
from .map_layout import MapLayoutMixin
from .map_tour import MapTourMixin
from .map_traffic import MapTrafficMixin
from .map_services import (
    MAP_ICONS,
    MAP_LABEL_KEYS,
    MAP_TYPES,
    format_distance,
    format_duration,
    geocode,
    haversine,
    maneuver_icon,
    maneuver_text_key,
    osrm_route,
    resolve_route_points,
)

log = get_logger(__name__)

# ── MapPage widget ────────────────────────────────────────────────────────────

class MapPage(MapWebKitMixin, MapShumateMixin, MapLayoutMixin, MapTourMixin, MapTrafficMixin, Gtk.Box):
    """OpenStreetMap navigation page — WebKit/MapLibre (3D) or Shumate (2D)."""
    __gtype_name__ = "MapPage"

    def __init__(
        self,
        language: str = SOURCE_LANGUAGE,
        force_webkit: bool = False,
        units: str = "metric",
        mock_mode: bool = False,
        poi_visible: bool = False,
        traffic_visible: bool = False,
        traffic_bundesweit: bool = True,
        traffic_nrw: bool = False,
        map_3d_view: bool = True,
        on_poi_visible_changed: Callable[[bool], None] | None = None,
        on_traffic_visible_changed: Callable[[bool], None] | None = None,
        on_3d_view_changed: Callable[[bool], None] | None = None,
        on_tour_started: Callable[[list[list[float]]], None] | None = None,
        on_tour_stopped: Callable[[], None] | None = None,
        on_tour_resumed: Callable[[], None] | None = None,
        on_tts_enabled_changed: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_halign(Gtk.Align.FILL)
        self.set_valign(Gtk.Align.FILL)
        self.language = _normalize_language(language)
        self.force_webkit = force_webkit
        self.units = units if units in {"metric", "imperial"} else "metric"
        self.mock_mode = bool(mock_mode)
        # Latest map view state pushed from the JS side (zoom/pitch/bearing).
        # Rendered into the status row above the map AND a bottom-left overlay
        # whenever mock_mode is on.
        self._map_zoom: float | None = None
        self._map_pitch: float | None = None
        self._map_bearing: float | None = None
        self._map_state_overlay: Gtk.Box | None = None
        self._map_state_lbl: Gtk.Label | None = None
        self._map_state_poll_id: int | None = None
        self._on_poi_visible_changed = on_poi_visible_changed
        self._on_traffic_visible_changed = on_traffic_visible_changed
        self._on_3d_view_changed = on_3d_view_changed
        self._map_3d_view: bool = bool(map_3d_view)
        self._3d_btn: Gtk.ToggleButton | None = None
        self._on_tour_started = on_tour_started
        self._on_tour_stopped = on_tour_stopped
        self._on_tour_resumed = on_tour_resumed
        self._on_tts_enabled_changed = on_tts_enabled_changed
        self._tts_btn: Gtk.ToggleButton | None = None

        self._gps_lat: float | None = None
        self._gps_lon: float | None = None
        self._gps_heading: float = 0.0
        self._gps_speed_mps: float = 0.0
        self._follow_gps: bool = True
        self._last_map_js: float = 0.0   # throttle: last time mapSetCar was sent
        # Route coords [[lon, lat], ...] — kept for traffic proximity filtering
        self._route_coords: list[list[float]] = []
        self._map_type_idx: int = 0
        self._routing_mode: str = "car"
        self._start_coord: tuple[float, float] | None = None
        self._end_coord: tuple[float, float] | None = None
        self._tour_active: bool = False
        self._tour_paused: bool = False
        self._tour_completed: bool = False
        self._tour_steps: list[dict] = []
        self._tour_step_idx: int = 0
        self._tour_coords: list[list[float]] = []
        # Minimum (closest) distance seen for the current step's maneuver point.
        # Detecting "passed" via minimum-distance + growth is more reliable than
        # tracking only the last distance: sparse GPS can jump from 60 m to 80 m
        # without ever registering an approach, which the old logic missed.
        self._step_min_dist: float | None = None
        # Cumulative metres along the route for each maneuver step.  Computed
        # from OSRM's per-step `distance` field, so it doesn't depend on the
        # maneuver coordinates lining up with the route geometry — that's the
        # weak link in the haversine-only check when OSRM places the maneuver
        # marker slightly off the road.
        self._step_cum_m: list[float] = []
        # Cumulative metres from the start of the route up to each geometry
        # vertex.  Used together with _gps_route_idx to know how far the
        # driver has progressed along the route in absolute terms.
        self._route_cum_m: list[float] = []
        # Latest projected route-vertex index for the GPS position; rises
        # monotonically along the route so a single backwards GPS jitter
        # doesn't undo progress.
        self._gps_route_idx: int = 0
        self._dnd_src_idx: int = -1
        # TTS state
        self._tts_enabled: bool = False
        self._tts_language: str = "auto"
        self._tts_voice: str = "female"
        self._tts_last_step_idx: int = -1
        self._tts_spoken_thresholds: set[int] = set()

        # Maneuver overlay widgets
        self._maneuver_overlay: Gtk.Box | None = None
        self._maneuver_icon: Gtk.Image | None = None
        self._maneuver_distance_lbl: Gtk.Label | None = None
        self._maneuver_instr_lbl: Gtk.Label | None = None

        # Backend: "webkit" | "shumate" | "none"
        self._backend: str = "none"

        # WebKit state
        self._webview: Any = None

        # Shumate state
        self._shumate_map: Any = None
        self._inner_map: Any = None
        self._car_marker: Any = None
        self._marker_layer: Any = None
        self._path_layer: Any = None
        self._guide_path_layer: Any = None
        self._wp_layer: Any = None
        self._sources: dict[str, Any] = {}
        self._setting_pos: bool = False

        # FAB buttons (None when backend unavailable)
        self._follow_btn: Gtk.ToggleButton | None = None
        self._center_btn: Gtk.Button | None = None
        self._layer_btn: Gtk.Button | None = None
        self._traffic_btn: Gtk.ToggleButton | None = None
        self._zoom_in_btn: Gtk.Button | None = None
        self._zoom_out_btn: Gtk.Button | None = None
        self._tour_start_btn: Gtk.Button | None = None
        self._tour_start_lbl: Gtk.Label | None = None
        self._tour_btn_icon: Gtk.Image | None = None
        self._tour_controls_box: Gtk.Box | None = None
        self._steps_toggle_btn: Gtk.ToggleButton | None = None
        self._steps_panel: Gtk.Box | None = None
        self._steps_listbox: Gtk.ListBox | None = None
        self._steps_scrolled: Gtk.ScrolledWindow | None = None
        self._steps_row_widgets: list[Gtk.Widget] = []
        self._steps_row_listbox_rows: list[Gtk.ListBoxRow] = []

        # Traffic layer (Shumate only)
        self._traffic_layer: Any = None
        self._traffic_loaded: bool = False
        self._traffic_bundesweit: bool = bool(traffic_bundesweit)
        self._traffic_nrw: bool = bool(traffic_nrw)

        # POI layer
        self._poi_btn: Gtk.ToggleButton | None = None
        self._poi_layer: Any = None
        self._poi_visible: bool = bool(poi_visible)
        self._traffic_visible: bool = bool(traffic_visible)

        # Entry rows: flat list of (row_box, entry, remove_btn)
        self._entry_rows: list[tuple[Gtk.Box, Gtk.Entry, Gtk.Button]] = []
        self._entries_container: Gtk.Box | None = None
        self._search_bar: Gtk.Box | None = None

        # Tour top-nav
        self._tour_topnav: Gtk.Box | None = None
        self._tour_plan_btn: Gtk.ToggleButton | None = None
        self._tour_load_btn: Gtk.Button | None = None
        self._tour_save_btn: Gtk.Button | None = None
        self._tour_plan_active: bool = False

        self._build_tour_topnav()
        self._build_search_bar()
        self._build_map()

        self.connect("map", self._on_mapped)

        if self.mock_mode:
            self._ensure_map_state_poll()

    def _on_mapped(self, _widget: Any) -> None:
        GLib.idle_add(self._drop_focus)
        if self._backend == "shumate":
            GLib.timeout_add(200, self._shumate_initial_render)

    def _apply_initial_overlay_state(self) -> None:
        """Sync POI/traffic visibility + 3D preference from settings."""
        if self._backend == "webkit":
            poi = "true" if self._poi_visible else "false"
            traffic = "true" if self._traffic_visible else "false"
            view3d = "true" if self._map_3d_view else "false"
            self._js(f"mapSetPoiVisible({poi})")
            self._js(f"mapSetTrafficVisible({traffic})")
            self._js(f"mapSet3DView({view3d})")
            self._js(f"mapSetTrafficLanguage('{self.language}')")
        elif self._backend == "shumate":
            self._shumate_set_poi_visible(self._poi_visible)
            self._shumate_set_traffic_visible(self._traffic_visible)
        if self._traffic_visible and not self._traffic_loaded:
            self._traffic_loaded = True
            self._status_lbl.set_text(_translate(self.language, "map.traffic.loading"))
            threading.Thread(target=self._load_traffic_thread, daemon=True).start()

    def _on_webview_load_changed(self, wv: Any, load_event: Any) -> None:
        super()._on_webview_load_changed(wv, load_event)
        if int(load_event) == 3:
            GLib.timeout_add(200, self._apply_initial_overlay_state_after_load)

    def _apply_initial_overlay_state_after_load(self) -> bool:
        self._apply_initial_overlay_state()
        return False

    def _drop_focus(self) -> bool:
        root = self.get_root()
        if root is not None:
            root.set_focus(None)
        return False

    # ── GPS position updates ──────────────────────────────────────────────────

    # Minimum interval between mapSetCar JS pushes. Slightly below the mock
    # tour's 250 ms tick so a tick that arrives a few ms early isn't dropped —
    # dropped ticks were the main source of the arrow's "step-pause-step" feel.
    _MAP_JS_INTERVAL = 0.18  # ≈ 5.5 Hz cap

    def update_gps(
        self,
        lat: float | None,
        lon: float | None,
        heading: float | None,
        speed_kmh: float | None = None,
    ) -> None:
        if lat is None or lon is None:
            return
        self._gps_lat = lat
        self._gps_lon = lon
        self._gps_heading = heading or 0.0
        self._gps_speed_mps = (speed_kmh / 3.6) if speed_kmh is not None else self._gps_speed_mps

        # During an active tour always re-engage follow so the map tracks the driver.
        if self._tour_active and not self._follow_gps:
            self._set_follow(True)

        if self._backend == "webkit":
            now = time.monotonic()
            if now - self._last_map_js >= self._MAP_JS_INTERVAL:
                self._last_map_js = now
                self._js(f"mapSetCar({lat}, {lon}, {self._gps_heading})")
        elif self._backend == "shumate" and self._shumate_map is not None:
            self._update_shumate_gps(lat, lon)
            if self._follow_gps:
                self._goto(lat, lon)

        if self._tour_active or self._tour_paused:
            self._update_maneuver_overlay()

    def _goto(self, lat: float, lon: float) -> None:
        if self._backend == "webkit":
            self._js(f"mapSetCar({lat}, {lon}, {self._gps_heading})")
        elif self._shumate_map is not None:
            self._setting_pos = True
            self._shumate_map.get_viewport().set_location(lat, lon)
            self._setting_pos = False

    # ── Follow / viewport ─────────────────────────────────────────────────────

    def _on_viewport_moved(self, _viewport: Any, _pspec: Any) -> None:
        if not self._setting_pos and self._follow_gps:
            self._set_follow(False)

    def _set_follow(self, active: bool) -> bool:
        self._follow_gps = active
        if self._follow_btn is not None:
            self._follow_btn.handler_block_by_func(self._on_follow_toggled)
            self._follow_btn.set_active(active)
            self._follow_btn.handler_unblock_by_func(self._on_follow_toggled)
        if self._backend == "webkit":
            val = "true" if active else "false"
            self._js(f"mapSetFollow({val})")
        return False

    def _on_follow_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._follow_gps = btn.get_active()
        if self._backend == "webkit":
            val = "true" if self._follow_gps else "false"
            self._js(f"mapSetFollow({val})")
        if self._follow_gps and self._gps_lat is not None and self._gps_lon is not None:
            self._goto(self._gps_lat, self._gps_lon)

    def _on_center_clicked(self, _btn: Gtk.Button) -> None:
        if self._gps_lat is None:
            return
        self._set_follow(True)
        if self._backend == "webkit":
            self._js(f"mapGoTo({self._gps_lat}, {self._gps_lon}, 17)")
        elif self._shumate_map is not None:
            viewport = self._shumate_map.get_viewport()
            self._setting_pos = True
            viewport.set_location(self._gps_lat, self._gps_lon)
            viewport.set_zoom_level(17.0)
            self._setting_pos = False

    def _on_layer_clicked(self, _btn: Gtk.Button) -> None:
        self._map_type_idx = (self._map_type_idx + 1) % len(MAP_TYPES)
        layer = MAP_TYPES[self._map_type_idx]
        if self._backend == "webkit":
            self._js(f"mapSetStyle('{layer}')")
        elif self._shumate_map is not None:
            self._shumate_map.set_map_source(self._sources[layer])
        if self._layer_btn is not None:
            self._layer_btn.set_icon_name(MAP_ICONS.get(layer, "map-symbolic"))
            self._layer_btn.set_tooltip_text(_translate(self.language, MAP_LABEL_KEYS[layer]))

    # ── POI layer (Overpass API) ──────────────────────────────────────────────

    def _on_poi_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._poi_visible = btn.get_active()
        if self._backend == "webkit":
            val = "true" if self._poi_visible else "false"
            self._js(f"mapSetPoiVisible({val})")
        else:
            self._shumate_set_poi_visible(self._poi_visible)
        if self._on_poi_visible_changed is not None:
            self._on_poi_visible_changed(self._poi_visible)

    # ── 3D view toggle ────────────────────────────────────────────────────────

    def _on_3d_clicked(self, _btn: Gtk.Button) -> None:
        active = not self._map_3d_view
        self._map_3d_view = active
        self._refresh_3d_btn()
        if self._backend == "webkit":
            self._js("mapSet3DView(true)" if active else "mapSet3DView(false)")
        if self._on_3d_view_changed is not None:
            self._on_3d_view_changed(active)

    def _zoom_step(self, delta: int) -> None:
        if self._backend == "webkit":
            self._js("mapZoomIn()" if delta > 0 else "mapZoomOut()")
        elif self._shumate_map is not None:
            viewport = self._shumate_map.get_viewport()
            current = viewport.get_zoom_level()
            self._setting_pos = True
            viewport.set_zoom_level(max(1.0, min(self._shumate_max_zoom(), current + delta)))
            self._setting_pos = False

    # ── Route ─────────────────────────────────────────────────────────────────

    def _on_route_clicked(self, _widget: Any) -> None:
        texts = [e.get_text().strip() for _, e, __ in self._entry_rows]
        if not texts[-1]:
            return
        start_text = texts[0]
        wp_texts = texts[1:-1]
        end_text = texts[-1]
        self._status_lbl.set_text(_translate(self.language, "map.routing.searching"))
        self._route_btn.set_sensitive(False)
        threading.Thread(
            target=self._compute_route,
            args=(start_text, wp_texts, end_text),
            daemon=True,
        ).start()

    def _on_clear_clicked(self, _btn: Gtk.Button) -> None:
        # Remove all rows except first two (start + end)
        while len(self._entry_rows) > 2:
            row, _, __ = self._entry_rows.pop()
            if self._entries_container is not None:
                self._entries_container.remove(row)
        # Clear text in remaining entries
        for _, entry, __ in self._entry_rows:
            entry.set_text("")
        self._update_placeholders()
        self._update_remove_sensitivity()
        self._status_lbl.set_text("")
        self._start_coord = None
        self._end_coord = None
        self._tour_steps = []
        self._tour_step_idx = 0
        self._tour_coords = []
        self._abort_tour()
        self._set_tour_controls_visible(False)
        if self._backend == "webkit":
            self._js("mapClearRoute()")
        else:
            self._shumate_clear_route_layers()

    def set_nav_visible(self, visible: bool) -> None:
        if self._tour_topnav is not None:
            self._tour_topnav.set_visible(visible)
        if self._search_bar is not None:
            self._search_bar.set_visible(visible and self._tour_plan_active)
        # Showing/hiding the search bar changes the map widget's allocated
        # height — MapLibre's WebGL canvas doesn't notice on its own, and
        # Shumate's viewport also needs a queue_draw to repaint the freshly
        # exposed area.  Defer until after GTK has allocated the new layout.
        GLib.idle_add(self._nudge_map_resize)

    def _nudge_map_resize(self) -> bool:
        if self._backend == "webkit":
            # mapResize() calls map.resize() inside MapLibre, which recomputes
            # the canvas size and triggers a re-render at the new dimensions.
            self._do_map_resize()
            # A second nudge after the GTK layout has fully settled catches
            # the case where the first call ran before the search bar's
            # disappearance had propagated through the size cycle.
            GLib.timeout_add(150, self._do_map_resize)
        elif self._backend == "shumate" and self._shumate_map is not None:
            self._shumate_map.queue_resize()
            self._shumate_map.queue_draw()
        return False

    def set_units(self, units: str) -> None:
        units = units if units in {"metric", "imperial"} else "metric"
        if units == self.units:
            return
        self.units = units
        # Re-render whatever's currently on screen using the new unit system.
        if self._tour_active:
            self._update_maneuver_overlay()

    # ── TTS ───────────────────────────────────────────────────────────────────

    def _refresh_tts_btn(self) -> None:
        if self._tts_btn is None:
            return
        icon = "audio-volume-high-symbolic" if self._tts_enabled else "audio-volume-muted-symbolic"
        self._tts_btn.set_icon_name(icon)

    def _on_tts_btn_toggled(self, btn: Gtk.ToggleButton) -> None:
        enabled = btn.get_active()
        self._tts_enabled = enabled
        if not enabled:
            tts_service.stop()
        self._refresh_tts_btn()
        if self._on_tts_enabled_changed:
            self._on_tts_enabled_changed(enabled)

    def set_tts_enabled(self, enabled: bool) -> None:
        self._tts_enabled = bool(enabled)
        if not enabled:
            tts_service.stop()
        if self._tts_btn is not None:
            self._tts_btn.handler_block_by_func(self._on_tts_btn_toggled)
            self._tts_btn.set_active(self._tts_enabled)
            self._tts_btn.handler_unblock_by_func(self._on_tts_btn_toggled)
        self._refresh_tts_btn()

    def set_tts_language(self, language: str) -> None:
        self._tts_language = language if language in {"auto", "en", "de"} else "auto"

    def set_tts_voice(self, voice: str) -> None:
        self._tts_voice = voice if voice in {"male", "female"} else "female"

    # ── Mock mode / map state ─────────────────────────────────────────────────

    def set_traffic_sources(self, *, bundesweit: bool, nrw: bool) -> None:
        """Change which data sources are used when the traffic button is activated."""
        MapTrafficMixin.set_traffic_sources(self, bundesweit=bundesweit, nrw=nrw)

    def set_mock_mode(self, mock_mode: bool) -> None:
        self.mock_mode = bool(mock_mode)
        if self.mock_mode:
            self._ensure_map_state_poll()
        self._refresh_map_state_status()

    def _ensure_map_state_poll(self) -> None:
        if self._map_state_poll_id is not None:
            return
        # 1 s tick — independent of the JS bridge, so it works for Shumate too.
        self._map_state_poll_id = GLib.timeout_add(1000, self._poll_map_state)

    def _poll_map_state(self) -> bool:
        if not self.mock_mode:
            self._map_state_poll_id = None
            return False
        # Shumate: read directly from the viewport (2D, no pitch/bearing).
        if self._backend == "shumate" and self._shumate_map is not None:
            try:
                viewport = self._shumate_map.get_viewport()
                self._map_zoom = float(viewport.get_zoom_level())
                self._map_pitch = None
                self._map_bearing = None
            except Exception:
                pass
        # WebKit: query via evaluate_javascript — the script-message-handler
        # bridge proved unreliable in our deployment, so we just RPC the values
        # out directly. The callback updates the cached fields.
        elif self._backend == "webkit" and self._webview is not None:
            self._evaluate_webkit_state()
        self._refresh_map_state_status()
        return True

    def _evaluate_webkit_state(self) -> None:
        script = (
            "(function(){try{if(typeof map==='undefined'||!map)return null;"
            "return JSON.stringify([map.getZoom(),map.getPitch(),map.getBearing()]);"
            "}catch(e){return null;}})()"
        )
        try:
            if hasattr(self._webview, "evaluate_javascript"):
                # WebKit 6: 7 args incl. callback
                self._webview.evaluate_javascript(
                    script, -1, None, None, None,
                    self._on_webkit_state_eval, None,
                )
            else:
                # WebKit2: run_javascript(script, cancellable, callback, user_data)
                self._webview.run_javascript(
                    script, None, self._on_webkit_state_eval, None,
                )
        except Exception:
            log.debug("evaluate_javascript failed", exc_info=True)

    def _on_webkit_state_eval(self, webview: Any, result: Any, _user: Any) -> None:
        try:
            if hasattr(webview, "evaluate_javascript_finish"):
                js_val = webview.evaluate_javascript_finish(result)
            else:
                js_val = webview.run_javascript_finish(result).get_js_value()
            raw = js_val.to_string() if js_val is not None else None
            if not raw or raw == "null":
                return
            import json as _json
            try:
                z, p, b = _json.loads(raw)
            except Exception:
                return
            self._map_zoom = float(z)
            self._map_pitch = float(p)
            self._map_bearing = float(b)
            self._refresh_map_state_status()
        except Exception:
            log.debug("evaluate_javascript_finish failed", exc_info=True)

    def _refresh_map_state_status(self) -> None:
        """In mock mode, render live map view state in the status row and a
        dedicated bottom-left overlay on the map."""
        if not self.mock_mode:
            if self._map_state_overlay is not None:
                self._map_state_overlay.set_visible(False)
            return
        # Tag with the active backend so it's obvious which path is feeding
        # the readout (e.g. "shumate" never has pitch).
        parts: list[str] = [self._backend or "none"]
        if self._map_zoom is not None:
            parts.append(f"zoom {self._map_zoom:.1f}")
        if self._map_pitch is not None:
            parts.append(f"pitch {self._map_pitch:.0f}°")
        if self._map_bearing is not None:
            parts.append(f"bearing {self._map_bearing:.0f}°")
        text = "  ".join(parts)
        # Status row (Duration / Distance) stays free for routing info even in
        # mock mode now that the bottom-left overlay is working.
        if self._map_state_lbl is not None:
            self._map_state_lbl.set_text(text)
        if self._map_state_overlay is not None:
            self._map_state_overlay.set_visible(True)

    def _on_js_map_state(self, zoom: float, pitch: float, bearing: float) -> None:
        self._map_zoom = zoom
        self._map_pitch = pitch
        self._map_bearing = bearing
        self._refresh_map_state_status()

    def _compute_route(self, start_text: str, wp_texts: list[str], end_text: str) -> None:
        try:
            gps = (
                (self._gps_lat, self._gps_lon)
                if self._gps_lat is not None and self._gps_lon is not None
                else None
            )
            all_points = resolve_route_points(start_text, wp_texts, end_text, gps, geocode)
            if all_points is None:
                GLib.idle_add(self._route_error)
                return
            result = osrm_route(all_points, self._routing_mode)
        except Exception:
            log.exception("Could not compute map route")
            GLib.idle_add(self._route_error)
            return
        GLib.idle_add(self._route_result, all_points, result)

    def _route_error(self) -> bool:
        self._status_lbl.set_text(_translate(self.language, "map.routing.error"))
        self._route_btn.set_sensitive(True)
        return False

    def _route_result(
        self,
        all_points: list[tuple[float, float]],
        result: tuple[list[list[float]], float, float, list[dict]] | None,
    ) -> bool:
        self._route_btn.set_sensitive(True)
        if result is None:
            self._status_lbl.set_text(_translate(self.language, "map.routing.error"))
            return False

        coords, duration_s, distance_m, steps = result
        # New route invalidates any paused tour state from a prior route.
        if self._tour_paused or self._tour_active:
            self._abort_tour()
        self._tour_steps = steps
        self._tour_step_idx = 0
        self._step_min_dist = None
        self._tour_coords = list(coords) if coords else []
        self._gps_route_idx = 0
        self._compute_route_progress_tables()
        self._start_coord = all_points[0]
        self._end_coord = all_points[-1]
        prefix = _translate(self.language, "map.duration_prefix")
        distance_prefix = _translate(self.language, "map.distance_prefix")
        self._status_lbl.set_text(
            f"{prefix}{format_duration(duration_s)} / "
            f"{distance_prefix}{format_distance(distance_m, self.units)}"
        )
        self._set_tour_controls_visible(True)
        if self._steps_toggle_btn is not None and self._steps_toggle_btn.get_active():
            self._rebuild_steps_list()
            if self._steps_panel is not None:
                self._steps_panel.set_visible(bool(self._tour_steps))

        if coords:
            lats = [c[1] for c in coords]
            lons = [c[0] for c in coords]
            self._set_follow(False)

        self._route_coords = coords  # store for traffic proximity filter

        if self._backend == "webkit":
            self._js(f"mapSetRoute({json.dumps(coords)})")
            pts_js = json.dumps([[p[0], p[1]] for p in all_points])
            self._js(f"mapSetWaypoints({pts_js})")
            if coords:
                min_lat, max_lat = min(lats), max(lats)
                min_lon, max_lon = min(lons), max(lons)
                self._js(f"mapFitBounds({min_lat},{min_lon},{max_lat},{max_lon})")
        elif self._shumate_map is not None:
            self._shumate_show_route(all_points, coords)

        return False

    # ── Language ──────────────────────────────────────────────────────────────

    def set_language(self, language: str) -> None:
        self.language = _normalize_language(language)
        self._update_placeholders()
        if self._backend == "webkit":
            self._js(f"mapSetTrafficLanguage('{self.language}')")
        self._route_btn.set_label(_translate(self.language, "map.route"))
        layer = MAP_TYPES[self._map_type_idx]
        if self._layer_btn is not None:
            self._layer_btn.set_tooltip_text(_translate(self.language, MAP_LABEL_KEYS[layer]))
        if self._follow_btn is not None:
            self._follow_btn.set_tooltip_text(_translate(self.language, "map.follow"))
        if self._center_btn is not None:
            self._center_btn.set_tooltip_text(_translate(self.language, "map.center"))
        if self._traffic_btn is not None:
            self._traffic_btn.set_tooltip_text(_translate(self.language, "map.traffic"))
        if self._zoom_in_btn is not None:
            self._zoom_in_btn.set_tooltip_text(_translate(self.language, "map.zoom_in"))
        if self._zoom_out_btn is not None:
            self._zoom_out_btn.set_tooltip_text(_translate(self.language, "map.zoom_out"))
        if self._tour_start_lbl is not None:
            if self._tour_active:
                self._set_tour_button("stop")
            elif self._tour_paused:
                self._set_tour_button("resume")
            else:
                self._set_tour_button("start")
        if self._steps_toggle_btn is not None:
            self._steps_toggle_btn.set_tooltip_text(
                _translate(self.language, "map.steps.toggle")
            )
        if self._tour_steps and self._steps_listbox is not None:
            self._rebuild_steps_list()
