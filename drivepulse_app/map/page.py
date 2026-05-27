"""Map page — OpenStreetMap navigation with GPS tracking and routing.

Backend priority:
  1. WebKit (MapLibre GL JS) — 3D vector tiles, pitch, bearing-follow
  2. Shumate (native GTK4)  — 2D raster tiles, offline-friendly
  3. Placeholder             — neither library available

Mixins:
  MapWebKitMixin       (webkit.py)             — WebKit/MapLibre backend
  MapShumateMixin      (shumate.py)            — Shumate raster backend
  MapLayoutMixin       (layout.py)             — Map area, FAB, zoom, maneuver banner
  MapSearchBarMixin    (layout_search.py)      — Waypoint entry rows + DnD reorder
  MapStepsPanelMixin   (layout_steps.py)       — Turn-by-turn steps side panel
  MapTourActionsMixin  (layout_tour_actions.py)— Topnav, saved-tour list, history
  MapReplayMixin       (replay.py)             — Trip-replay info card, chart, polyline + marker
  MapTourMixin         (tour.py)               — Tour state machine, TTS, step detection
  MapTrafficMixin      (traffic.py)            — Autobahn traffic API, filtering, popover
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from drivepulse_app.common import SOURCE_LANGUAGE, _normalize_language, _translate
from drivepulse_app.db import DriveDB
from drivepulse_app.diagnostics import get_logger, write_diagnostic_log
from drivepulse_app.map.gps_filter import MapGpsFilterMixin
from drivepulse_app.map.layout import MapLayoutMixin
from drivepulse_app.map.layout_search import MapSearchBarMixin
from drivepulse_app.map.layout_steps import MapStepsPanelMixin
from drivepulse_app.map.layout_tour_actions import MapTourActionsMixin
from drivepulse_app.map.layout_tour_history import MapTourHistoryMixin
from drivepulse_app.map.layout_tour_saved import MapTourSavedMixin
from drivepulse_app.map.replay import MapReplayMixin
from drivepulse_app.map.route_compute import MapRouteComputeMixin
from drivepulse_app.map.services import (
    MAP_ICONS,
    MAP_LABEL_KEYS,
    MAP_TYPES,
    _remap_speed_to_route,
    route_via_gps_waypoints,
)
from drivepulse_app.map.shumate import MapShumateMixin
from drivepulse_app.map.state_poll import MapStatePollMixin
from drivepulse_app.map.tour import MapTourMixin
from drivepulse_app.map.traffic import MapTrafficMixin
from drivepulse_app.map.webkit import MapWebKitMixin
from drivepulse_app.tts import service as tts_service

log = get_logger(__name__)

# ── MapPage widget ────────────────────────────────────────────────────────────

class MapPage(
    MapWebKitMixin,
    MapShumateMixin,
    MapLayoutMixin,
    MapSearchBarMixin,
    MapStepsPanelMixin,
    MapTourActionsMixin,
    MapTourHistoryMixin,
    MapTourSavedMixin,
    MapReplayMixin,
    MapTourMixin,
    MapTrafficMixin,
    MapGpsFilterMixin,
    MapStatePollMixin,
    MapRouteComputeMixin,
    Gtk.Box,
):
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
        map_layer: str = "map",
        map_heading_up: bool = True,
        on_poi_visible_changed: Callable[[bool], None] | None = None,
        on_traffic_visible_changed: Callable[[bool], None] | None = None,
        on_3d_view_changed: Callable[[bool], None] | None = None,
        on_map_layer_changed: Callable[[str], None] | None = None,
        on_heading_up_changed: Callable[[bool], None] | None = None,
        on_tour_started: Callable[[list[list[float]], list[tuple[float, float]], list[float]], None] | None = None,
        on_tour_stopped: Callable[[], None] | None = None,
        on_tour_resumed: Callable[[], None] | None = None,
        on_tts_enabled_changed: Callable[[bool], None] | None = None,
        db: DriveDB | None = None,
        get_sync_client: Callable | None = None,
        initial_zoom: float | None = None,
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
        self._initial_zoom: float | None = initial_zoom
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
        self._on_map_layer_changed = on_map_layer_changed
        self._on_heading_up_changed = on_heading_up_changed
        self._map_3d_view: bool = bool(map_3d_view)
        self._heading_up: bool = bool(map_heading_up)
        self._heading_up_btn: Gtk.ToggleButton | None = None
        self._3d_btn: Gtk.ToggleButton | None = None
        self._on_tour_started = on_tour_started
        self._on_tour_stopped = on_tour_stopped
        self._on_tour_resumed = on_tour_resumed
        self._on_tts_enabled_changed = on_tts_enabled_changed
        self._tts_btn: Gtk.ToggleButton | None = None

        self._gps_lat: float | None = None
        self._gps_lon: float | None = None
        self._gps_heading: float = 0.0
        self._gps_heading_valid: bool = False
        self._gps_speed_mps: float = 0.0
        self._follow_gps: bool = True
        self._last_map_js: float = 0.0   # throttle: last time mapSetCar was sent
        # Delta gate so identical positions don't trigger a JS eval + repaint
        # every 180 ms while standing still. Heartbeat after _MAP_JS_HEARTBEAT_S
        # keeps the JS side from believing the GPS feed has stalled.
        self._last_map_js_lat: float | None = None
        self._last_map_js_lon: float | None = None
        self._last_map_js_heading: float = 0.0
        # Route coords [[lon, lat], ...] — kept for traffic proximity filtering
        self._route_coords: list[list[float]] = []
        # Restore the remembered layer; fall back to "map" (index 0) if invalid.
        try:
            self._map_type_idx: int = MAP_TYPES.index(map_layer)
        except ValueError:
            self._map_type_idx = 0
        self._start_coord: tuple[float, float] | None = None
        self._end_coord: tuple[float, float] | None = None
        self._tour_active: bool = False
        self._tour_paused: bool = False
        self._tour_completed: bool = False
        self._tour_steps: list[dict] = []
        self._tour_step_idx: int = 0
        self._tour_coords: list[list[float]] = []
        # True when a trip-based route has been matched but not yet drawn on
        # the map — the polyline is pushed at Tour-Start instead of at load
        # time so the map stays clean while the user reviews the steps list.
        self._pending_route_draw: bool = False
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
        # Route-snapped position; set during active/paused tour, None otherwise.
        self._snapped_lat: float | None = None
        self._snapped_lon: float | None = None
        self._snapped_cum_m: float = 0.0
        # Original waypoints for the current tour — used by auto-rerouting to
        # preserve the destination when the driver deviates from the route.
        self._tour_waypoints: list[tuple[float, float]] = []
        # Remaining waypoints to visit (everything after the current position).
        # Index 0 = next intermediate goal, last = final destination.
        # Used by auto-rerouting and intermediate-waypoint proximity checks.
        self._remaining_dest_wps: list[tuple[float, float]] = []
        # True while the car is inside the 200 m approach radius of the next
        # intermediate waypoint — used to detect the departure that signals arrival.
        self._wp_in_radius: bool = False
        # "Nächstes Ziel" button reference (built in layout.py, controlled by tour.py).
        self._next_wp_btn: Gtk.Button | None = None
        # GPS kinematic sanity filter — last accepted fix + one pending "suspect" slot.
        self._gps_filt_lat: float | None = None
        self._gps_filt_lon: float | None = None
        self._gps_filt_heading: float = 0.0
        self._gps_filt_speed_kmh: float = 0.0
        self._gps_filt_time: float = 0.0
        # A "suspect" is a point whose implied speed was too high but whose
        # direction was consistent with the current heading.  It is held for
        # one GPS cycle: if the next point validates it (progression from the
        # suspect is plausible), it is accepted retroactively; otherwise it is
        # silently discarded as GPS noise.
        self._gps_filt_suspect: tuple | None = None  # (lat, lon, hdg, spd, t)
        # Latest OBD vehicle speed — used to cross-validate GPS position fixes.
        self._obd_speed_kmh: float | None = None
        self._obd_speed_time: float = 0.0
        self._dnd_src_idx: int = -1
        # Speed-limit warning beep
        self._speed_warn_enabled: bool = True
        self._speed_warn_btn: Gtk.Button | None = None
        self._speed_warn_fired: bool = False       # reset on each maneuver step
        self._speed_zones_from_overpass: bool = False  # only warn on Overpass data
        # TTS state
        self._tts_enabled: bool = False
        self._tts_language: str = "auto"
        self._tts_voice: str = "female"
        self._tts_quality: str = "high"
        self._tts_last_step_idx: int = -1
        self._tts_spoken_thresholds: set[int] = set()

        # Maneuver overlay widgets
        self._maneuver_overlay: Gtk.Box | None = None
        self._maneuver_icon: Gtk.Image | None = None
        self._maneuver_distance_lbl: Gtk.Label | None = None
        self._maneuver_instr_lbl: Gtk.Label | None = None
        self._lane_row: Gtk.Box | None = None
        self._lane_step_idx: int = -1

        # Speed zone overlay (bottom-left, visible during active/paused tour)
        self._speed_zone_overlay: Gtk.Box | None = None
        self._speed_zone_lbl: Gtk.Label | None = None
        self._speed_zones: list[tuple[float, float]] = []

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
        self._tour_controls_box: Gtk.Grid | None = None
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

        self._map_db: DriveDB | None = db
        self.get_sync_client: Callable | None = get_sync_client

        # Tour top-nav
        self._tour_topnav: Gtk.Box | None = None
        self._tour_plan_btn: Gtk.ToggleButton | None = None
        self._tour_load_btn: Gtk.Button | None = None
        self._tour_save_btn: Gtk.Button | None = None
        self._tour_plan_active: bool = False
        self._tour_listbox: Gtk.ListBox | None = None
        self._loaded_tour_id: int | None = None
        self._loaded_tour_name: str | None = None
        # Trip-replay equivalent of _loaded_tour_id — tracks the recorded
        # trip currently being shown on the map so the Recent-Tours list
        # can mark it with the green emblem just like loaded tours.
        self._loaded_trip_id: int | None = None

        # NavigationView wraps all map content — enables sub-page push/pop
        self._nav_view: Adw.NavigationView = Adw.NavigationView()
        self._nav_view.set_hexpand(True)
        self._nav_view.set_vexpand(True)
        self.append(self._nav_view)

        self._map_content_box: Gtk.Box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._map_content_box.set_hexpand(True)
        self._map_content_box.set_vexpand(True)
        _main_nav_page = Adw.NavigationPage(title="Map")
        _main_nav_page.set_tag("map-main")
        _main_nav_page.set_can_pop(False)
        _main_nav_page.set_child(self._map_content_box)
        self._nav_view.add(_main_nav_page)

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
            heading_up = "true" if self._heading_up else "false"
            self._js(f"mapSetPoiVisible({poi})")
            self._js(f"mapSetTrafficVisible({traffic})")
            self._js(f"mapSet3DView({view3d})")
            self._js(f"mapSetHeadingUp({heading_up})")
            self._js(f"mapSetTrafficLanguage('{self.language}')")
            initial_layer = (
                MAP_TYPES[self._map_type_idx]
                if 0 <= self._map_type_idx < len(MAP_TYPES) else "map"
            )
            if initial_layer != "map":
                self._js(f"mapSetStyle('{initial_layer}')")
        elif self._backend == "shumate":
            self._shumate_set_poi_visible(self._poi_visible)
            self._shumate_set_traffic_visible(self._traffic_visible)
            self._shumate_apply_attribution()
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
    # Delta thresholds: skip the JS push entirely when neither the position
    # nor the heading have meaningfully changed. ~3 m and 2° are below GPS
    # noise on phones but above true-rest jitter, so we only push when the
    # vehicle has actually moved.
    _MAP_JS_MIN_DEG = 3e-5   # ~3.3 m at the equator
    _MAP_JS_MIN_HEADING = 2.0  # degrees
    # Heartbeat so the JS side gets at least one update per second even when
    # parked — guards against any client-side timeout assuming a stalled feed.
    _MAP_JS_HEARTBEAT_S = 1.0

    # ── Follow / viewport ─────────────────────────────────────────────────────

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
            self._shumate_apply_attribution()
        if self._layer_btn is not None:
            self._layer_btn.set_icon_name(MAP_ICONS.get(layer, "map-symbolic"))
            self._layer_btn.set_tooltip_text(_translate(self.language, MAP_LABEL_KEYS[layer]))
        if self._on_map_layer_changed is not None:
            try:
                self._on_map_layer_changed(layer)
            except Exception:
                log.exception("map_layer_changed callback failed")

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

    def _on_heading_up_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._heading_up = btn.get_active()
        self._refresh_heading_up_btn_tooltip()
        if self._backend == "webkit":
            val = "true" if self._heading_up else "false"
            self._js(f"mapSetHeadingUp({val})")
        elif not self._heading_up and self._shumate_map is not None:
            # Reset Shumate's viewport rotation so the map snaps back to north.
            viewport = self._shumate_map.get_viewport()
            if viewport is not None and hasattr(viewport, "set_rotation"):
                try:
                    viewport.set_rotation(0.0)
                except Exception:
                    log.debug("Shumate viewport.set_rotation(0) failed", exc_info=True)
            if self._car_marker is not None:
                child = self._car_marker.get_child()
                if child is not None:
                    child.queue_draw()
        if self._on_heading_up_changed is not None:
            self._on_heading_up_changed(self._heading_up)

    def _refresh_heading_up_btn_tooltip(self) -> None:
        btn = getattr(self, "_heading_up_btn", None)
        if btn is None:
            return
        key = "map.heading_up.on" if btn.get_active() else "map.heading_up.off"
        btn.set_tooltip_text(_translate(self.language, key))

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
        # Wipe any previously rendered route/polyline only now that the user
        # explicitly asked for a new route — toggling "Plan tour" alone keeps
        # the loaded tour visible while waypoints are being edited.
        self._clear_replay_overlays()
        self._status_lbl.set_text(_translate(self.language, "map.routing.searching"))
        self._route_btn.set_sensitive(False)
        # Swap label → spinner so the user sees something happening during the
        # geocode + OSRM round-trip (can take a couple of seconds).
        if getattr(self, "_route_btn_spinner", None) is not None:
            self._route_btn.set_child(self._route_btn_spinner)
            self._route_btn_spinner.start()
        # Centred overlay spinner — also visible when the search bar is hidden
        # (e.g. loading a saved tour without plan-mode active).
        self._set_route_loading(True)
        threading.Thread(
            target=self._compute_route,
            args=(start_text, wp_texts, end_text),
            daemon=True,
        ).start()

    def _restore_route_btn(self) -> None:
        if getattr(self, "_route_btn_spinner", None) is not None:
            self._route_btn_spinner.stop()
        self._route_btn.set_label(_translate(self.language, "map.route"))
        self._route_btn.set_sensitive(True)
        self._set_route_loading(False)

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
        self._route_coords = []
        self._pending_route_draw = False
        self._loaded_tour_id = None
        self._loaded_tour_name = None
        self._abort_tour()
        self._set_tour_controls_visible(False)
        if self._tour_save_btn is not None:
            self._tour_save_btn.set_visible(False)
        if getattr(self, "_loaded_trip_id", None) is not None and hasattr(
            self, "_clear_replay_overlays"
        ):
            self._clear_replay_overlays()
        elif getattr(self, "_replay_info_overlay", None) is not None:
            self._replay_info_overlay.set_visible(False)
        if self._backend == "webkit":
            self._js("mapClearRoute()")
        else:
            self._shumate_clear_route_layers()
        if hasattr(self, "_update_left_chrome_visibility"):
            self._update_left_chrome_visibility()
        # Refresh Recent + Load-Tours lists so the green "loaded" marker
        # disappears once the trash has wiped the loaded tour/trip.
        if hasattr(self, "_rebuild_tour_history_rows"):
            self._rebuild_tour_history_rows()
        if hasattr(self, "_rebuild_tour_list"):
            self._rebuild_tour_list()

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
        if self._backend == "shumate" and hasattr(self, "_shumate_apply_scale_unit"):
            self._shumate_apply_scale_unit(units)

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

    def set_speed_warn_enabled(self, enabled: bool) -> None:
        self._speed_warn_enabled = bool(enabled)
        if self._speed_warn_btn is not None:
            self._speed_warn_btn.handler_block_by_func(self._on_speed_warn_toggled)
            self._speed_warn_btn.set_active(self._speed_warn_enabled)
            self._speed_warn_btn.handler_unblock_by_func(self._on_speed_warn_toggled)
        self._refresh_speed_warn_btn()

    def _refresh_speed_warn_btn(self) -> None:
        if self._speed_warn_btn is None:
            return
        tip = _translate(self.language, "map.speed_warn.on" if self._speed_warn_enabled else "map.speed_warn.off")
        self._speed_warn_btn.set_tooltip_text(tip)

    def _on_speed_warn_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._speed_warn_enabled = btn.get_active()
        self._refresh_speed_warn_btn()
        cb = getattr(self, "_on_speed_warn_changed", None)
        if callable(cb):
            cb(self._speed_warn_enabled)

    def set_tts_language(self, language: str) -> None:
        self._tts_language = language if language in {"auto", "en", "de"} else "auto"

    def set_tts_voice(self, voice: str) -> None:
        self._tts_voice = voice if voice in {"male", "female"} else "female"

    def set_tts_quality(self, quality: str) -> None:
        self._tts_quality = quality if quality in {"low", "medium", "high"} else "high"

    # ── Mock mode / map state ─────────────────────────────────────────────────

    def set_traffic_sources(self, *, bundesweit: bool, nrw: bool) -> None:
        """Change which data sources are used when the traffic button is activated."""
        MapTrafficMixin.set_traffic_sources(self, bundesweit=bundesweit, nrw=nrw)

    def set_mock_mode(self, mock_mode: bool) -> None:
        self.mock_mode = bool(mock_mode)
        self._refresh_map_state_status()


    def load_trip_as_route(
        self,
        coords_lonlat: list[list[float]],
        distance_km: float | None = None,
        duration_s: float | None = None,
        label: str | None = None,
        timestamps: list[float] | None = None,
    ) -> None:
        """Prepare a recorded trip's driven GPS polyline for tour calculation.

        No OSRM/Valhalla call is made here. The recorded points are kept as a
        pending trace and only map-matched after the user clicks Calculate
        tour, so opening a trip never triggers routing work by itself.
        """
        if not coords_lonlat or len(coords_lonlat) < 2:
            return

        nav_view = getattr(self, "_nav_view", None)
        while nav_view is not None and nav_view.get_previous_page(
            nav_view.get_visible_page()
        ) is not None:
            nav_view.pop()

        if self._tour_paused or self._tour_active:
            self._abort_tour()
        self._clear_replay_overlays()

        coords = [[float(c[0]), float(c[1])] for c in coords_lonlat]
        start_lonlat = coords[0]
        end_lonlat = coords[-1]
        start = (start_lonlat[0], start_lonlat[1])
        end = (end_lonlat[0], end_lonlat[1])

        self._tour_steps = []
        self._tour_step_idx = 0
        self._step_min_dist = None
        self._tour_coords = coords
        self._gps_route_idx = 0
        self._snapped_lat = None
        self._snapped_lon = None
        self._snapped_cum_m = 0.0
        self._compute_route_progress_tables()
        self._start_coord = start
        self._end_coord = end
        self._tour_waypoints = [start, end]
        self._loaded_tour_id = None
        self._route_coords = coords

        target = 2
        while len(self._entry_rows) < target:
            self._insert_entry_after(self._entry_rows[-1][0])
        while len(self._entry_rows) > target:
            self._remove_entry(self._entry_rows[-1][0])
        first_text = label or _translate(self.language, "cars.trip.start")
        last_text = _translate(self.language, "cars.trip.end")
        self._entry_rows[0][1].set_text(first_text)
        self._entry_rows[-1][1].set_text(last_text)
        self._update_placeholders()

        self._status_lbl.set_text("")
        self._populate_trip_route_info(label, distance_km, duration_s)

        self._set_tour_controls_visible(True)
        if self._tour_save_btn is not None:
            self._tour_save_btn.set_visible(False)
        if self._steps_panel is not None:
            self._set_steps_panel_visible(False)
        if self._steps_toggle_btn is not None:
            self._steps_toggle_btn.set_active(False)
        if hasattr(self, "_update_left_chrome_visibility"):
            self._update_left_chrome_visibility()

        self._set_follow(False)

        # Clear any previous route from the map but don't draw the new one
        # yet — it will be pushed at Tour-Start after Valhalla has snapped it.
        if self._backend == "webkit":
            self._js("mapClearRoute()")
        elif self._shumate_map is not None:
            if hasattr(self, "_shumate_clear_route_layers"):
                self._shumate_clear_route_layers()

        self._pending_trip_trace_args = (coords, label, distance_km, duration_s, timestamps)
        self._set_tour_button("calculate")

    def _fetch_trip_trace(
        self,
        coords: list[list[float]],
        label: str | None,
        distance_km: float | None,
        duration_s: float | None,
        timestamps: list[float] | None = None,
    ) -> None:
        log.info("trip_trace_route_call pts=%d", len(coords))
        result = route_via_gps_waypoints(coords, timestamps=timestamps)
        if result is not None:
            snapped, dur, dist, steps = result
            log.info(
                "trip_trace_route_ok snapped_pts=%d steps=%d dist_km=%.1f dur_min=%.0f",
                len(snapped), len(steps), dist / 1000.0, dur / 60.0,
            )
        else:
            write_diagnostic_log(
                __name__,
                logging.WARNING,
                "trip_trace_route_failed pts=%d label=%r",
                len(coords),
                label,
            )
        GLib.idle_add(
            self._trip_trace_result, result, coords, label, distance_km, duration_s
        )

    def _trip_trace_result(
        self,
        result: tuple[list[list[float]], float, float, list[dict]] | None,
        orig_coords: list[list[float]],
        label: str | None,
        orig_distance_km: float | None,
        orig_duration_s: float | None,
    ) -> bool:
        self._set_route_loading(False)
        self._set_tour_button("start" if result is not None else "calculate")
        if self._tour_start_btn is not None:
            self._tour_start_btn.set_sensitive(True)
        if result is not None:
            snapped_coords, duration_s, distance_m, steps = result
            self._tour_coords = snapped_coords
            self._tour_steps = steps
            self._tour_step_idx = 0
            self._step_min_dist = None
            self._gps_route_idx = 0
            self._snapped_lat = None
            self._snapped_lon = None
            self._snapped_cum_m = 0.0
            self._compute_route_progress_tables()
            self._populate_trip_route_info(label, distance_m / 1000.0, duration_s)
            try:
                self._prerender_upcoming_steps(0, 2)
            except Exception:
                pass
            if self._steps_toggle_btn is not None:
                self._steps_toggle_btn.set_active(True)
            if self._steps_panel is not None:
                self._rebuild_steps_list()
                self._set_steps_panel_visible(bool(steps))
        if result is None:
            write_diagnostic_log(
                __name__,
                logging.WARNING,
                "trip_trace_calculation_failed pts=%d label=%r "
                "orig_distance_km=%r orig_duration_s=%r retry_kept=True",
                len(orig_coords),
                label,
                orig_distance_km,
                orig_duration_s,
            )
            self._pending_trip_trace_args = (
                orig_coords, label, orig_distance_km, orig_duration_s
            )
            dialog = Adw.AlertDialog(
                heading=_translate(self.language, "map.tour_calculate_failed.heading"),
                body=_translate(self.language, "map.tour_calculate_failed.body"),
            )
            dialog.add_response("ok", "OK")
            dialog.set_default_response("ok")
            dialog.present(self.get_root())
        if result is not None or getattr(self, "_pending_route_draw", False):
            if result is not None:
                if hasattr(self, "_set_replay_info_minimized"):
                    self._set_replay_info_minimized(True)
                if hasattr(self, "_set_replay_chart_minimized"):
                    self._set_replay_chart_minimized(True)
                latlon_speed = getattr(self, "_loaded_trip_latlon_speed", None)
                if latlon_speed and self._tour_coords:
                    remapped = _remap_speed_to_route(self._tour_coords, latlon_speed)
                    if hasattr(self, "_map_show_track"):
                        self._map_show_track(remapped)
            self._push_route_to_map()
        return False

    def _push_route_to_map(self) -> None:
        """Draw the pending Valhalla-matched route on the map and fit bounds."""
        coords = self._tour_coords
        if not coords:
            return
        self._pending_route_draw = False
        lats = [c[1] for c in coords]
        lons = [c[0] for c in coords]
        if self._backend == "webkit":
            self._js(f"mapSetRoute({json.dumps(coords)})")
            pts_js = json.dumps([[p[0], p[1]] for p in self._tour_waypoints])
            self._js(f"mapSetWaypoints({pts_js})")
            min_lat, max_lat = min(lats), max(lats)
            min_lon, max_lon = min(lons), max(lons)
            self._js(f"mapFitBounds({min_lat},{min_lon},{max_lat},{max_lon})")
        elif self._shumate_map is not None:
            self._shumate_show_route(self._tour_waypoints, coords)

    # ── Form factor (mobile vs desktop chrome) ────────────────────────────────

    def set_form_factor(self, form_factor: str) -> None:
        """Adjust map overlays for mobile vs desktop chrome."""
        self._form_factor = form_factor

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
