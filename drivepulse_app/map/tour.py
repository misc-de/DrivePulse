"""Map tour/navigation mixin — state machine, maneuver overlay, lane guidance.

The tour feature is split across four cooperating mixins, all composed into
``MapPage`` so calls resolve via ``self``:

* :class:`MapTourMixin` (here)      — lifecycle (start/pause/resume/abort),
  progress tracking and the turn-by-turn maneuver overlay + lane guidance.
* :class:`~drivepulse_app.map.tour_tts.MapTourTtsMixin`        — voice guidance.
* :class:`~drivepulse_app.map.tour_speed.MapTourSpeedMixin`    — speed-limit
  zones and the over-speed warning.
* :class:`~drivepulse_app.map.tour_reroute.MapTourRerouteMixin`— auto-reroute
  and intermediate-waypoint tracking.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, ClassVar

from gi.repository import GLib
from gi.repository import Gtk as _Gtk

from drivepulse_app.common import _translate
from drivepulse_app.diagnostics import get_logger
from drivepulse_app.map import _tour_state
from drivepulse_app.map._jsbridge import js_call
from drivepulse_app.map._tour_progress import (
    build_maneuver_positions,
    compute_route_progress_tables,
    maneuver_passed,
    nearest_route_progress,
    next_actionable_step_idx,
    reconcile_passed_waypoints,
    waypoint_is_passed,
)
from drivepulse_app.map.services import (
    format_distance,
    haversine,
    maneuver_icon,
    maneuver_text_key,
    osrm_route,
)
from drivepulse_app.tts import service as tts_service

log = get_logger(__name__)


class MapTourMixin:
    """Tour lifecycle, route-progress tracking and the maneuver overlay."""

    # Concrete MapPage initializes these as Optional[(float, float)].
    _start_coord: tuple[float, float] | None
    # Trip-trace render args buffered until the user confirms (or None when idle).
    _pending_trip_trace_args: (
        tuple[list[list[float]], str | None, float | None, float | None, list[float] | None] | None
    )

    # Concrete MapPage state surfaced to this mixin. See project_mixin_typing.md.
    language: str
    units: str
    _backend: str
    _gps_lat: float | None
    _gps_lon: float | None
    _gps_heading: float
    _tour_coords: list[list[float]]
    _tour_steps: list[dict]
    _tour_waypoints: list[tuple[float, float]]
    _shumate_map: Any
    _maneuver_overlay: Any
    _maneuver_icon: Any
    _maneuver_instr_lbl: Any
    _maneuver_distance_lbl: Any
    _lane_row: Any
    _speed_zone_overlay: Any
    _tour_start_btn: Any
    _tour_start_lbl: Any
    _tour_btn_icon: Any
    _tour_controls_box: Any
    _steps_panel: Any
    _steps_toggle_btn: Any
    _zoom_in_btn: Any
    _zoom_out_btn: Any
    _guide_path_layer: Any

    _on_tour_started: Callable[..., Any] | None
    _on_tour_stopped: Callable[..., Any] | None
    _on_tour_resumed: Callable[..., Any] | None

    _js: Callable[[str], None]
    _set_follow: Callable[[bool], bool]
    _set_steps_panel_visible: Callable[[bool], None]
    _set_route_loading: Callable[..., Any]
    _highlight_active_step: Callable[..., Any]
    _fetch_trip_trace: Callable[..., Any]
    _shumate_max_zoom: Callable[..., Any]
    _shumate_set_guide: Callable[..., Any]
    _viewport_lock: Callable[..., Any]

    # Methods defined in the sibling tour mixins, called here via ``self``.
    _build_speed_zones: Callable[[], list[tuple[float, float]]]
    _start_overpass_speed_fetch: Callable[[], None]
    _update_speed_zone_overlay: Callable[[], None]
    _prerender_upcoming_steps: Callable[..., None]
    _update_tts: Callable[..., None]
    _set_next_wp_btn_visible: Callable[[bool], None]

    # Comfortable street-level zoom for tour following; max zoom (22) was
    # too close to be useful for navigation.
    _TOUR_ZOOM = 18.0

    # Minimum interval between mapSetCar JS pushes. Slightly below the mock
    # tour's 250 ms tick so a tick that arrives a few ms early isn't dropped —
    # dropped ticks were the main source of the arrow's "step-pause-step" feel.
    _MAP_JS_INTERVAL = 0.18  # ≈ 5.5 Hz cap

    # A step is considered "passed" when we've gotten within this distance AND
    # then the distance has grown beyond minimum + _MANEUVER_PASS_GROWTH_M.
    # 80 m covers sparse-GPS scenarios (1 Hz at 50 km/h = ~14 m/s, so 80 m
    # allows up to ~5 ticks of travel while still inside the detection window).
    _MANEUVER_CLOSEST_M = 80.0

    # How much the distance must grow from the minimum before we declare a
    # step passed. Large enough to ignore GPS noise (~3–5 m), small enough
    # not to wait until we're halfway to the next maneuver.
    _MANEUVER_PASS_GROWTH_M = 8.0

    # Within this distance of the final destination the tour auto-finishes —
    # the driver stops *at* the destination and never drives "past" the arrive
    # maneuver, so the overshoot test alone would leave navigation hanging.
    _ARRIVAL_RADIUS_M = 25.0

    # OSRM step types that don't represent an actionable maneuver.
    # "continue" = road continues with minor direction/name change — not worth showing.
    _NON_ACTIONABLE_STEP_TYPES = frozenset({
        "new name", "notification", "continue",
    })

    # Shared progress/reroute counters; the sibling speed/reroute mixins read
    # these, but the initial values live here next to the lifecycle that resets
    # them in _begin_tour / _abort_tour.
    _off_route_since: float = 0.0
    _last_reroute_time: float = 0.0
    _route_gen: int = 0

    def _on_tour_start_clicked(self, _btn: object) -> None:
        if self._start_coord is None:
            return
        if self._tour_active:
            self._pause_tour()
            return
        if self._tour_paused:
            self._resume_tour()
            return
        pending = getattr(self, "_pending_trip_trace_args", None)
        if pending is not None:
            self._pending_trip_trace_args = None
            if self._tour_start_btn is not None:
                self._tour_start_btn.set_sensitive(False)
            self._set_route_loading(True)
            coords, label, distance_km, duration_s, *rest = pending
            timestamps = rest[0] if rest else None
            log.info("trip_trace_start coords=%d label=%r", len(coords), label)
            threading.Thread(
                target=self._fetch_trip_trace,
                args=(coords, label, distance_km, duration_s, timestamps),
                daemon=True,
            ).start()
            return
        self._begin_tour()

    def _persist_active_tour(self) -> None:
        """Persist the remaining destination waypoints so an app restart can
        resume the tour from the legs the driver has not yet completed. An
        empty remaining list clears the persisted state."""
        _tour_state.save_active_tour(
            list(getattr(self, "_remaining_dest_wps", []) or []),
            name=getattr(self, "_loaded_tour_name", None),
            tour_id=getattr(self, "_loaded_tour_id", None),
        )

    def _begin_tour(self) -> None:
        if self._start_coord is None:
            return
        if getattr(self, "_pending_route_draw", False) and hasattr(self, "_push_route_to_map"):
            self._push_route_to_map()
        if self._backend == "webkit":
            self._js("mapClearColoredTrack()")
        elif getattr(self, "_shumate_map", None) is not None and hasattr(self, "_shumate_clear_colored_track"):
            self._shumate_clear_colored_track()
        lat, lon = self._start_coord
        self._tour_active = True
        self._tour_paused = False
        self._tour_completed = False
        self._tour_step_idx = 0
        self._tts_prerender_step_idx = -1
        self._step_min_dist: float | None = None
        self._gps_route_idx = 0
        self._snapped_lat: float | None = None
        self._snapped_lon: float | None = None
        self._snapped_cum_m = 0.0
        self._off_route_since = 0.0
        self._last_reroute_time = 0.0
        self._remaining_dest_wps = list(self._tour_waypoints[1:]) if self._tour_waypoints else []
        # Drop intermediate vias the driver is already past (app restart
        # mid-drive, or drove ahead before tapping Start) so the tour resumes
        # where they actually are instead of routing back to a reached stop.
        if len(self._remaining_dest_wps) > 1:
            self._remaining_dest_wps = reconcile_passed_waypoints(
                self._tour_coords, getattr(self, "_route_cum_m", []),
                self._remaining_dest_wps, self._gps_lat, self._gps_lon,
            )
        # reconcile_passed_waypoints only drops vias we drove geometrically
        # *past*. Also drop vias we've clearly turned away from — the same
        # heading-based bypass the live rerouter uses — so an app-restart resume
        # or a reloaded saved tour can't resurrect a deliberately-skipped via and
        # balloon the route into a 1.6 km out-and-back. The destination stays.
        if (
            getattr(self, "_gps_heading_valid", False)
            and self._gps_lat is not None
            and self._gps_lon is not None
        ):
            max_dist = getattr(self, "_BYPASS_MAX_DIST_M", 250.0)
            while len(self._remaining_dest_wps) > 1:
                wp = self._remaining_dest_wps[0]
                passed, _wd, _wb = waypoint_is_passed(
                    self._gps_lat, self._gps_lon, self._gps_heading,
                    wp[0], wp[1], max_dist,
                )
                if not passed:
                    break
                log.info("Begin-tour: dropping bypassed via (%.5f, %.5f)", wp[0], wp[1])
                self._remaining_dest_wps = self._remaining_dest_wps[1:]
        # Persist progress so an app restart can resume from the remaining legs.
        self._persist_active_tour()
        self._wp_in_radius = False
        self._speed_zones = self._build_speed_zones()
        self._speed_zones_from_overpass = False
        self._speed_warn_fired = False
        self._route_gen += 1
        self._start_overpass_speed_fetch()
        self._prerender_upcoming_steps(0, 5)
        self._set_nav_chrome_visible(False)
        self._set_tour_button("stop")
        if hasattr(self, "_update_left_chrome_visibility"):
            self._update_left_chrome_visibility()
        self._update_maneuver_overlay()
        self._highlight_active_step()
        if self._on_tour_started is not None and self._tour_coords:
            self._on_tour_started(
                self._tour_coords,
                self._speed_zones,
                self._build_maneuver_positions(),
            )
        self._set_follow(True)
        if self._backend == "webkit":
            self._js(js_call("mapStartTour", lat, lon))
        elif self._shumate_map is not None:
            viewport = self._shumate_map.get_viewport()
            with self._viewport_lock():
                viewport.set_zoom_level(min(self._TOUR_ZOOM, self._shumate_max_zoom()))
                viewport.set_location(lat, lon)
        if self._gps_lat is not None and self._gps_lon is not None:
            dist = haversine(self._gps_lat, self._gps_lon, lat, lon)
            if dist > 200:
                gps_lat, gps_lon = self._gps_lat, self._gps_lon
                threading.Thread(
                    target=self._fetch_guide_to_start,
                    args=(gps_lat, gps_lon, lat, lon),
                    daemon=True,
                ).start()

    def _pause_tour(self) -> None:
        """Pause an active tour — keep route and progress so it can resume."""
        self._off_route_since = 0.0
        self._tour_active = False
        self._tour_paused = True
        self._set_tour_button("resume")
        if self._backend == "webkit":
            self._js("mapSetTourActive(false)")
        # Maneuver overlay stays visible so the driver can still see the next
        # instruction while paused.
        if self._on_tour_stopped is not None:
            self._on_tour_stopped()
        if hasattr(self, "_update_left_chrome_visibility"):
            self._update_left_chrome_visibility()

    def _resume_tour(self) -> None:
        """Resume a paused tour without recomputing or recentring."""
        self._tour_active = True
        self._tour_paused = False
        self._set_tour_button("stop")
        self._set_follow(True)
        if self._backend == "webkit":
            self._js("mapSetTourActive(true)")
        self._update_maneuver_overlay()
        if self._on_tour_resumed is not None:
            self._on_tour_resumed()
        if hasattr(self, "_update_left_chrome_visibility"):
            self._update_left_chrome_visibility()

    def _abort_tour(self) -> None:
        """Full reset — used when the route is cleared or replaced."""
        was_running = self._tour_active or self._tour_paused
        self._tour_active = False
        self._tour_paused = False
        self._tour_completed = False
        self._step_min_dist = None
        self._gps_route_idx = 0
        self._snapped_lat = None
        self._snapped_lon = None
        self._snapped_cum_m = 0.0
        self._off_route_since = 0.0
        self._last_reroute_time = 0.0
        self._remaining_dest_wps = []
        _tour_state.clear_active_tour()
        self._wp_in_radius = False
        self._set_next_wp_btn_visible(False)
        self._tts_last_step_idx = -1
        self._tts_spoken_thresholds: set[int] = set()
        self._tts_prerender_step_idx = -1
        self._speed_zones = []
        self._lane_step_idx = -1
        tts_service.stop()
        tts_service.clear_audio_cache()
        self._set_nav_chrome_visible(True)
        self._set_tour_button("start")
        if self._backend == "webkit":
            self._js("mapSetTourActive(false)")
            self._js("mapResetView()")
            self._js("mapClearGuideToStart()")
        elif self._guide_path_layer is not None:
            self._guide_path_layer.remove_all()
            # Reset the heading-up rotation that was applied during the tour.
            if self._shumate_map is not None:
                viewport = self._shumate_map.get_viewport()
                if viewport is not None and hasattr(viewport, "set_rotation"):
                    try:
                        viewport.set_rotation(0.0)
                    except Exception:
                        log.debug("Shumate viewport.set_rotation(0) on tour-stop failed", exc_info=True)
        if self._maneuver_overlay is not None:
            self._maneuver_overlay.set_visible(False)
        if self._lane_row is not None:
            self._lane_row.set_visible(False)
        if self._speed_zone_overlay is not None:
            self._speed_zone_overlay.set_visible(False)
        self._highlight_active_step()
        if was_running and self._on_tour_stopped is not None:
            self._on_tour_stopped()
        if hasattr(self, "_update_left_chrome_visibility"):
            self._update_left_chrome_visibility()

    def _complete_tour(self) -> None:
        """Arrived at the final destination: end navigation cleanly.

        Mirrors the teardown of :meth:`_abort_tour` but deliberately does NOT
        stop TTS, so the "you have arrived" announcement (already triggered at
        the <=80 m threshold) plays to the end. Clears all route layers — including
        the green guide-to-start line that otherwise lingered after arrival.
        """
        was_running = self._tour_active or self._tour_paused
        self._tour_completed = True
        self._tour_active = False
        self._tour_paused = False
        self._step_min_dist = None
        self._gps_route_idx = 0
        self._snapped_lat = None
        self._snapped_lon = None
        self._snapped_cum_m = 0.0
        self._off_route_since = 0.0
        self._last_reroute_time = 0.0
        self._remaining_dest_wps = []
        _tour_state.clear_active_tour()
        self._wp_in_radius = False
        self._set_next_wp_btn_visible(False)
        self._tts_last_step_idx = -1
        self._tts_prerender_step_idx = -1
        self._speed_zones = []
        self._lane_step_idx = -1
        self._set_nav_chrome_visible(True)
        self._set_tour_button("start")
        if self._backend == "webkit":
            self._js("mapSetTourActive(false)")
            self._js("mapResetView()")
            self._js("mapClearGuideToStart()")
        elif self._shumate_map is not None:
            if hasattr(self, "_shumate_clear_route_layers"):
                self._shumate_clear_route_layers()
            viewport = self._shumate_map.get_viewport()
            if viewport is not None and hasattr(viewport, "set_rotation"):
                try:
                    viewport.set_rotation(0.0)
                except Exception:
                    log.debug("Shumate viewport.set_rotation(0) on arrival failed", exc_info=True)
        if self._maneuver_overlay is not None:
            self._maneuver_overlay.set_visible(False)
        if self._lane_row is not None:
            self._lane_row.set_visible(False)
        if self._speed_zone_overlay is not None:
            self._speed_zone_overlay.set_visible(False)
        self._highlight_active_step()
        log.info("Tour complete — arrived at destination, navigation ended")
        if was_running and self._on_tour_stopped is not None:
            self._on_tour_stopped()
        if hasattr(self, "_update_left_chrome_visibility"):
            self._update_left_chrome_visibility()

    def _set_tour_button(self, mode: str) -> None:
        """mode: 'start' | 'calculate' | 'stop' | 'resume'."""
        label_key = {
            "start":     "map.tour_start",
            "calculate": "map.tour_calculate",
            "stop":      "map.tour_stop",
            "resume":    "map.tour_resume",
        }.get(mode, "map.tour_start")
        icon_name = "media-playback-stop-symbolic" if mode == "stop" else "media-playback-start-symbolic"
        if self._tour_start_lbl is not None:
            self._tour_start_lbl.set_label(_translate(self.language, label_key))
        if self._tour_btn_icon is not None:
            self._tour_btn_icon.set_from_icon_name(icon_name)
        # Abort button is only meaningful while the tour is paused — i.e.
        # the start button currently reads "Resume tour".
        abort_btn = getattr(self, "_tour_abort_btn", None)
        if abort_btn is not None:
            abort_btn.set_visible(mode == "resume")

    def _set_nav_chrome_visible(self, visible: bool) -> None:
        """Show/hide UI chrome that clutters the screen during active navigation."""
        for btn in (self._zoom_in_btn, self._zoom_out_btn, self._steps_toggle_btn):
            if btn is not None:
                btn.set_visible(visible)
        # Replay-info card + chart card and their restore icons have no role
        # during turn-by-turn navigation. Hide them when a tour starts; the
        # replay panels are tied to a trip-replay session and stay hidden
        # until the user opens a replay again.  The route-info OSD (duration +
        # distance) is also suppressed — the active turn-by-turn maneuver
        # overlay supersedes that summary.
        if not visible:
            for w_name in (
                "_replay_info_overlay",
                "_replay_info_restore_btn",
                "_replay_chart_overlay",
                "_replay_chart_restore_btn",
                "_route_info_overlay",
            ):
                w = getattr(self, w_name, None)
                if w is not None:
                    w.set_visible(False)

    def _set_tour_controls_visible(self, visible: bool) -> None:
        if self._tour_controls_box is not None:
            self._tour_controls_box.set_visible(visible)
        if visible:
            if self._tour_start_btn is not None:
                self._tour_start_btn.set_visible(True)
            if self._steps_toggle_btn is not None:
                self._steps_toggle_btn.set_visible(not self._tour_active)
        else:
            if self._steps_toggle_btn is not None:
                self._steps_toggle_btn.set_active(False)
            if self._steps_panel is not None:
                self._set_steps_panel_visible(False)

    def _fetch_guide_to_start(
        self, gps_lat: float, gps_lon: float, start_lat: float, start_lon: float
    ) -> None:
        result = osrm_route([(gps_lat, gps_lon), (start_lat, start_lon)])
        GLib.idle_add(self._guide_result, result)

    def _guide_result(
        self,
        result: tuple[list[list[float]], float, float, list[dict]] | None,
    ) -> bool:
        if result is None:
            return False
        coords = result[0]
        if self._backend == "webkit":
            self._js(js_call("mapSetGuideToStart", coords))
        elif self._shumate_map is not None and self._guide_path_layer is not None:
            self._shumate_set_guide(coords)
        return False

    def _skip_non_actionable_steps(self) -> None:
        new_idx = next_actionable_step_idx(
            self._tour_steps, self._tour_step_idx, self._NON_ACTIONABLE_STEP_TYPES
        )
        if new_idx != self._tour_step_idx:
            self._tour_step_idx = new_idx
            # Advancing to a new active step invalidates the closest-approach tracker.
            self._step_min_dist = None

    def _compute_route_progress_tables(self) -> None:
        """Precompute distance-along-route tables for fast progress lookups.

        - `_route_cum_m[i]` = metres from start of route up to vertex i
        - `_step_cum_m[k]`  = metres from start of route up to maneuver k,
          derived from OSRM's per-step `distance` so it stays correct even
          when the step's coordinate is slightly offset from the geometry.
        """
        self._route_cum_m, self._step_cum_m = compute_route_progress_tables(
            self._tour_coords, self._tour_steps
        )

    def _build_maneuver_positions(self) -> list[float]:
        """Return cumulative distances (m) for each actionable turn maneuver."""
        skip = self._NON_ACTIONABLE_STEP_TYPES | {"depart", "arrive"}
        return build_maneuver_positions(self._tour_steps, self._step_cum_m, skip)

    def _gps_progress_m(self) -> float:
        """Return how far the GPS fix has progressed along the route, in m.

        When snap_to_route already ran in update_gps(), returns the cached
        fractional-segment cumulative distance — more accurate than the old
        vertex-only search.  Falls back to the vertex search when no snap is
        available (e.g. before the first GPS fix after a tour start).
        """
        if self._snapped_lat is not None:
            return self._snapped_cum_m

        if (
            not self._tour_coords
            or not self._route_cum_m
            or self._gps_lat is None
            or self._gps_lon is None
        ):
            return 0.0
        best_i, cum_m = nearest_route_progress(
            self._tour_coords, self._route_cum_m,
            self._gps_lon, self._gps_lat, self._gps_route_idx,
        )
        self._gps_route_idx = best_i
        return cum_m

    def _update_maneuver_overlay(self) -> None:
        if self._maneuver_overlay is None:
            return
        if (
            not (self._tour_active or self._tour_paused)
            or not self._tour_steps
            or self._tour_completed
            or self._gps_lat is None
            or self._gps_lon is None
        ):
            self._maneuver_overlay.set_visible(False)
            return

        self._skip_non_actionable_steps()

        # Use snapped position for distance calculations when available —
        # eliminates GPS-off-road noise and gives more accurate maneuver distances.
        pos_lat = self._snapped_lat if self._snapped_lat is not None else self._gps_lat
        pos_lon = self._snapped_lon if self._snapped_lon is not None else self._gps_lon

        # How far we've driven along the route, in metres.  This is the
        # primary "have we passed step N yet?" signal because it uses
        # OSRM's own per-step distance values, which stay accurate even
        # when individual maneuver coordinates are slightly offset from
        # the road geometry.
        progress_m = self._gps_progress_m()

        # Multi-step advance loop: if the car passed several close-together
        # maneuvers between GPS ticks (e.g. city-centre roundabout exits) we
        # advance as many steps as the data supports in a single call.
        for _ in range(len(self._tour_steps)):
            step = self._tour_steps[self._tour_step_idx]
            distance_m = haversine(
                pos_lat, pos_lon, step["lat"], step["lon"]
            )

            # Track the closest approach seen so far for this step.
            if self._step_min_dist is None or distance_m < self._step_min_dist:
                self._step_min_dist = distance_m

            # Closest-approach test plus a route-progress fallback. The fallback
            # deliberately anchors on the step's OWN route position (not the next
            # step's start) — anchoring ahead caused premature advancement at
            # close-together maneuvers (roundabouts), desyncing overlay and TTS.
            curr_idx = self._tour_step_idx
            step_cum = self._step_cum_m[curr_idx] if curr_idx < len(self._step_cum_m) else None
            passed = maneuver_passed(
                self._step_min_dist, distance_m, progress_m, step_cum,
                closest_m=self._MANEUVER_CLOSEST_M,
                pass_growth_m=self._MANEUVER_PASS_GROWTH_M,
            )

            if not passed:
                break  # still approaching — show this step

            # Step passed — last step means route complete.
            if self._tour_step_idx >= len(self._tour_steps) - 1:
                self._complete_tour()
                return

            self._tour_step_idx += 1
            self._step_min_dist = None
            self._speed_warn_fired = False
            self._skip_non_actionable_steps()
            # Loop continues to check whether the newly active step is also passed.

        # When the active step changes, pre-render the next batch of announcements.
        if self._tour_step_idx != self._tts_prerender_step_idx:
            self._tts_prerender_step_idx = self._tour_step_idx
            self._prerender_upcoming_steps(self._tour_step_idx, 5)

        step = self._tour_steps[self._tour_step_idx]
        distance_m = haversine(pos_lat, pos_lon, step["lat"], step["lon"])
        m_type = step.get("type", "")
        m_modifier = step.get("modifier", "")
        name = step.get("name", "") or ""

        icon = maneuver_icon(m_type, m_modifier)
        text = _translate(self.language, maneuver_text_key(m_type, m_modifier))
        if name and m_type not in {"arrive", "depart"}:
            text += _translate(self.language, "map.maneuver.on_street").format(name=name)

        if self._maneuver_icon is not None:
            self._maneuver_icon.set_from_icon_name(icon)
        if self._maneuver_distance_lbl is not None:
            self._maneuver_distance_lbl.set_text(format_distance(distance_m, self.units))
        if self._maneuver_instr_lbl is not None:
            self._maneuver_instr_lbl.set_text(text)
        self._maneuver_overlay.set_visible(True)
        self._highlight_active_step()
        self._update_tts(step, distance_m)
        # Reached the final destination — finish the tour. The arrival
        # announcement above has just been triggered; we stop *at* the
        # destination instead of driving past the arrive maneuver, so this
        # distance check (not the overshoot test) is what ends navigation.
        if (
            self._tour_step_idx >= len(self._tour_steps) - 1
            and distance_m <= self._ARRIVAL_RADIUS_M
        ):
            self._complete_tour()
            return
        self._update_speed_zone_overlay()
        self._update_lane_guidance(step)

    # Valhalla lane indication → matching nav icon
    _LANE_ICON: ClassVar[dict[str, str]] = {
        "left":         "dp-nav-left-symbolic",
        "slight_left":  "dp-nav-slight-left-symbolic",
        "straight":     "dp-nav-straight-symbolic",
        "slight_right": "dp-nav-slight-right-symbolic",
        "right":        "dp-nav-right-symbolic",
        "sharp_left":   "dp-nav-sharp-left-symbolic",
        "sharp_right":  "dp-nav-sharp-right-symbolic",
        "uturn":        "dp-nav-uturn-symbolic",
    }

    def _update_lane_guidance(self, step: dict) -> None:
        if self._lane_row is None:
            return
        if self._tour_step_idx == self._lane_step_idx:
            return  # already rendered for this step
        self._lane_step_idx = self._tour_step_idx

        # Remove previous lane widgets.
        child = self._lane_row.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._lane_row.remove(child)
            child = nxt

        lanes: list[dict] = step.get("lanes") or []
        if len(lanes) < 2:
            self._lane_row.set_visible(False)
            return

        for lane in lanes:
            valid = bool(lane.get("valid", False))
            indications: list[str] = lane.get("indications") or ["straight"]
            ind = indications[0] if indications else "straight"
            icon_name = self._LANE_ICON.get(ind, "dp-nav-straight-symbolic")

            box = _Gtk.Box(orientation=_Gtk.Orientation.VERTICAL)
            box.add_css_class("dp-lane")
            if valid:
                box.add_css_class("dp-lane-valid")
            box.set_halign(_Gtk.Align.CENTER)
            box.set_valign(_Gtk.Align.CENTER)

            img = _Gtk.Image.new_from_icon_name(icon_name)
            img.set_pixel_size(26)
            img.set_halign(_Gtk.Align.CENTER)
            img.set_valign(_Gtk.Align.CENTER)
            box.append(img)
            self._lane_row.append(box)

        self._lane_row.set_visible(True)
