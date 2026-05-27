"""Map tour/navigation mixin — state machine, TTS, step detection."""
from __future__ import annotations

import json
import threading
import time
from typing import ClassVar

from gi.repository import GLib
from gi.repository import Gtk as _Gtk

from drivepulse_app.common import _translate
from drivepulse_app.diagnostics import get_logger
from drivepulse_app.map.services import (
    bearing,
    compute_route,
    fetch_overpass_speed_zones,
    format_distance,
    haversine,
    maneuver_icon,
    maneuver_text_key,
    mock_speed_kmh,
    osrm_route,
)
from drivepulse_app.tts import service as tts_service

log = get_logger(__name__)


class MapTourMixin:
    """Tour/navigation state machine, TTS and step detection."""

    # Concrete MapPage initializes these as Optional[(float, float)].
    _start_coord: tuple[float, float] | None
    _end_coord: tuple[float, float] | None

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

    # OSRM step types that don't represent an actionable maneuver.
    # "continue" = road continues with minor direction/name change — not worth showing.
    _NON_ACTIONABLE_STEP_TYPES = frozenset({
        "new name", "notification", "continue",
    })

    _TTS_THRESHOLDS = (300, 80)

    # Off-route detection: reroute automatically when the perpendicular distance
    # from the GPS to the snapped route position exceeds this threshold for a
    # sustained period. Speed gate prevents rerouting while nearly stationary
    # (GPS drift, waiting at traffic lights).
    _OFF_ROUTE_M = 30.0          # metres off-route to start the timer
    _OFF_ROUTE_CONFIRM_S = 4.0   # seconds off-route before rerouting fires
    _REROUTE_COOLDOWN_S = 30.0   # minimum gap between successive auto-reroutes
    _REROUTE_MIN_SPEED_KMH = 10.0  # don't reroute below this speed
    _BYPASS_MAX_DIST_M = 250.0   # only drop a behind-heading WP when within this radius

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
            self._js(f"mapStartTour({lat}, {lon})")
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
        self._wp_in_radius = False
        self._set_next_wp_btn_visible(False)
        self._tts_last_step_idx = -1
        self._tts_spoken_thresholds = set()
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
            self._js(f"mapSetGuideToStart({json.dumps(coords)})")
        elif self._shumate_map is not None and self._guide_path_layer is not None:
            self._shumate_set_guide(coords)
        return False

    def _skip_non_actionable_steps(self) -> None:
        while (
            self._tour_step_idx < len(self._tour_steps) - 1
            and self._tour_steps[self._tour_step_idx].get("type")
            in self._NON_ACTIONABLE_STEP_TYPES
        ):
            self._tour_step_idx += 1
            self._step_min_dist = None

    def _compute_route_progress_tables(self) -> None:
        """Precompute distance-along-route tables for fast progress lookups.

        - `_route_cum_m[i]` = metres from start of route up to vertex i
        - `_step_cum_m[k]`  = metres from start of route up to maneuver k,
          derived from OSRM's per-step `distance` so it stays correct even
          when the step's coordinate is slightly offset from the geometry.
        """
        self._route_cum_m = []
        self._step_cum_m = []
        if self._tour_coords:
            self._route_cum_m.append(0.0)
            for i in range(1, len(self._tour_coords)):
                a = self._tour_coords[i - 1]
                b = self._tour_coords[i]
                # coords are [lon, lat]
                seg = haversine(a[1], a[0], b[1], b[0])
                self._route_cum_m.append(self._route_cum_m[-1] + seg)
        if self._tour_steps:
            cum = 0.0
            for step in self._tour_steps:
                # Maneuver k sits at the START of step k.  So its position
                # along the route is the cumulative distance of steps 0..k-1.
                self._step_cum_m.append(cum)
                cum += float(step.get("distance") or 0.0)

    def _build_speed_zones(self) -> list[tuple[float, float]]:
        """Build (cum_dist_m, speed_kmh) breakpoints.

        Prefers Valhalla's real ``speed_limit`` values.  Falls back to the
        ref-tag heuristic (A* → 120, B* → 70, urban → 40) so the sign is
        always shown during mock-mode tours where Valhalla data may be absent.
        """
        if not self._tour_steps or not self._step_cum_m:
            return []
        zones: list[tuple[float, float]] = []
        prev_speed: float | None = None
        for i, step in enumerate(self._tour_steps):
            if "speed_limit" in step:
                speed = float(step["speed_limit"])
            else:
                speed = mock_speed_kmh(step.get("ref") or "")
            if speed != prev_speed:
                cum = self._step_cum_m[i] if i < len(self._step_cum_m) else 0.0
                zones.append((cum, speed))
                prev_speed = speed
        return zones

    def _start_overpass_speed_fetch(self) -> None:
        """Kick off a background thread that pre-fetches per-segment speed limits."""
        coords = list(self._tour_coords) if self._tour_coords else []
        if not coords:
            return
        gen = self._route_gen
        t = threading.Thread(
            target=self._overpass_speed_bg,
            args=(coords, gen),
            daemon=True,
        )
        t.start()

    def _overpass_speed_bg(
        self, coords: list[list[float]], gen: int
    ) -> None:
        try:
            zones = fetch_overpass_speed_zones(coords)
        except Exception:
            log.exception("Overpass speed fetch failed")
            zones = []
        GLib.idle_add(self._apply_overpass_speed_zones, zones, gen)

    def _apply_overpass_speed_zones(
        self, zones: list[tuple[float, float]], gen: int
    ) -> bool:
        if gen != self._route_gen or not self._tour_active:
            return False
        if zones:
            self._speed_zones = zones
            self._speed_zones_from_overpass = True
            log.debug("Overpass speed zones: %d breakpoints loaded", len(zones))
        return False

    def _build_maneuver_positions(self) -> list[float]:
        """Return cumulative distances (m) for each actionable turn maneuver."""
        skip = self._NON_ACTIONABLE_STEP_TYPES | {"depart", "arrive"}
        positions: list[float] = []
        for i, step in enumerate(self._tour_steps):
            if step.get("type", "") in skip:
                continue
            if i < len(self._step_cum_m):
                positions.append(self._step_cum_m[i])
        return positions

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
        best_i = self._gps_route_idx
        best_d = float("inf")
        for i in range(self._gps_route_idx, len(self._tour_coords)):
            coord = self._tour_coords[i]
            dx = coord[0] - self._gps_lon
            dy = coord[1] - self._gps_lat
            d = dx * dx + dy * dy
            if d < best_d:
                best_d = d
                best_i = i
        self._gps_route_idx = best_i
        return self._route_cum_m[best_i]

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

            # "Passed" when we've gotten within _MANEUVER_CLOSEST_M and the
            # distance has grown back past minimum + noise-guard.
            passed = (
                self._step_min_dist <= self._MANEUVER_CLOSEST_M
                and distance_m > self._step_min_dist + self._MANEUVER_PASS_GROWTH_M
            )

            # Route-progress fallback: the maneuver is behind us when GPS
            # progress is at least MANEUVER_CLOSEST_M past the step's own
            # route position.  We deliberately do NOT anchor on the next
            # step's start — doing so caused premature step advancement at
            # close-together maneuvers (roundabouts, complex intersections),
            # which desynced the visual overlay and TTS announcements.
            if not passed and self._step_cum_m:
                curr_idx = self._tour_step_idx
                if curr_idx < len(self._step_cum_m):
                    maneuver_behind_m = self._step_cum_m[curr_idx] + self._MANEUVER_CLOSEST_M
                    if progress_m > maneuver_behind_m:
                        passed = True

            if not passed:
                break  # still approaching — show this step

            # Step passed — last step means route complete.
            if self._tour_step_idx >= len(self._tour_steps) - 1:
                self._tour_completed = True
                self._maneuver_overlay.set_visible(False)
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
        self._update_speed_zone_overlay()
        self._update_lane_guidance(step)

    def _update_tts(self, step: dict, distance_m: float) -> None:
        if not self._tts_enabled:
            return
        current_idx = self._tour_step_idx
        if current_idx != self._tts_last_step_idx:
            self._tts_last_step_idx = current_idx
            self._tts_spoken_thresholds = set()
            # Don't announce immediately — the threshold loop below will fire on
            # the very next tick (or this one) at the appropriate distance.

        # Look-ahead: fire threshold early enough to compensate for TTS latency.
        # At 50 km/h and 1s latency the car travels ~14m — audible instructions
        # would otherwise describe a maneuver the driver has already reached.
        look_ahead_m = self._gps_speed_mps * tts_service.get_latency_s()
        trigger_dist = distance_m + look_ahead_m

        for threshold in self._TTS_THRESHOLDS:
            if threshold in self._tts_spoken_thresholds:
                continue
            if trigger_dist <= threshold:
                self._tts_announce(step, distance_m)
                self._tts_spoken_thresholds.add(threshold)
                break

    def _tts_effective_language(self) -> str:
        if self._tts_language != "auto":
            return self._tts_language
        return self.language if self.language in {"en", "de"} else "en"

    def _tts_distance_text(self, meters: float, lang: str) -> str:
        if meters < 950:
            n = int(round(meters / 10) * 10) or 10
            return _translate(lang, "tts.distance.m").format(n=n)
        km = round(meters / 1000, 1)
        return _translate(lang, "tts.distance.km").format(n=km)

    def _update_speed_zone_overlay(self) -> None:
        if self._speed_zone_overlay is None or self._speed_zone_lbl is None:
            return
        if not self._speed_zones:
            self._speed_zone_overlay.set_visible(False)
            return
        progress_m = self._gps_progress_m()
        speed: float | None = None
        for cum_m, spd in self._speed_zones:
            if cum_m <= progress_m:
                speed = spd
            else:
                break
        if speed is None:
            self._speed_zone_overlay.set_visible(False)
            return
        self._speed_zone_lbl.set_text(str(int(speed)))
        self._speed_zone_overlay.set_visible(True)

        # Speed-limit warning beep — only with Overpass data, only once per step.
        if (
            getattr(self, "_speed_warn_enabled", True)
            and getattr(self, "_speed_zones_from_overpass", False)
            and not getattr(self, "_speed_warn_fired", False)
            and self._tour_active
        ):
            import time as _time
            _now = _time.monotonic()
            _gps_age = _now - getattr(self, "_gps_filt_time", 0.0)
            if _gps_age < self._GPS_MAX_STALE_S:
                vehicle_kmh = getattr(self, "_gps_filt_speed_kmh", 0.0) or 0.0
            else:
                _obd_age = _now - getattr(self, "_obd_speed_time", 0.0)
                vehicle_kmh = (
                    getattr(self, "_obd_speed_kmh", None) or 0.0
                    if _obd_age < self._OBD_SPEED_STALE_S
                    else 0.0
                )
            if vehicle_kmh >= speed * 1.30:
                self._speed_warn_fired = True
                self._play_speed_beep(long_double=True)
            elif vehicle_kmh >= speed * 1.15:
                self._speed_warn_fired = True
                self._play_speed_beep(long_double=False)

    def _play_speed_beep(self, long_double: bool) -> None:
        import io
        import math
        import struct
        import subprocess
        import wave

        def _do() -> None:
            rate = 22050
            freq = 880.0
            volume = 0.65
            # short: 160 ms single tone  |  long-double: 280 ms + 120 ms gap + 280 ms
            segments = (
                [(280, True), (120, False), (280, True)] if long_double else [(160, True)]
            )
            frames: list[bytes] = []
            for ms, on in segments:
                n = int(rate * ms / 1000)
                for i in range(n):
                    v = int(32767 * volume * math.sin(2 * math.pi * freq * i / rate)) if on else 0
                    frames.append(struct.pack("<h", v))
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(b"".join(frames))
            try:
                proc = subprocess.Popen(
                    ["aplay", "-q"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                proc.communicate(input=buf.getvalue(), timeout=3)
            except (OSError, subprocess.SubprocessError):
                log.debug("aplay maneuver beep failed", exc_info=True)

        threading.Thread(target=_do, daemon=True).start()

    def _prerender_upcoming_steps(self, from_idx: int, count: int = 5) -> None:
        """Pre-render TTS audio for the next *count* steps starting at *from_idx*.

        Uses threshold distances (300 m, 80 m) to approximate the spoken text.
        At typical speeds the 80 m threshold collapses to maneuver-text-only
        (heard_dist < 60 m), so that variant always matches exactly.
        """
        if not self._tts_enabled:
            return
        lang = self._tts_effective_language()
        gender = self._tts_voice
        for i in range(from_idx, min(from_idx + count, len(self._tour_steps))):
            step = self._tour_steps[i]
            if step.get("type") in {"depart", "arrive"}:
                continue
            maneuver_text = _translate(
                lang, maneuver_text_key(step.get("type", ""), step.get("modifier", ""))
            )
            for threshold_m in self._TTS_THRESHOLDS:
                heard_dist = float(threshold_m)
                if heard_dist > 60:
                    dist_text = self._tts_distance_text(heard_dist, lang)
                    text = _translate(lang, "tts.in_distance").format(distance=dist_text) + " " + maneuver_text
                else:
                    text = maneuver_text
                tts_service.prerender(text, lang, gender, quality=self._tts_quality)

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

    # ── Auto-rerouting ────────────────────────────────────────────────────────

    def _check_off_route(self, off_dist_m: float, now: float) -> None:
        """Called each GPS tick with the perpendicular distance to the route."""
        speed_kmh = self._gps_speed_mps * 3.6
        if off_dist_m <= self._OFF_ROUTE_M or speed_kmh < self._REROUTE_MIN_SPEED_KMH:
            self._off_route_since = 0.0
            return
        if self._off_route_since == 0.0:
            self._off_route_since = now
            return
        if now - self._off_route_since < self._OFF_ROUTE_CONFIRM_S:
            return
        if now - self._last_reroute_time < self._REROUTE_COOLDOWN_S:
            return
        self._trigger_reroute()

    def _trigger_reroute(self) -> None:
        if self._gps_lat is None or self._gps_lon is None:
            return
        # Use the tracked remaining waypoints so already-visited intermediates
        # are not included in the recalculated route.
        remaining = list(getattr(self, "_remaining_dest_wps", None) or [])
        if not remaining:
            wps = getattr(self, "_tour_waypoints", None) or []
            remaining = list(wps[1:])
        if not remaining:
            return

        # Only drop an intermediate waypoint when the driver has clearly
        # *passed* it: WP must be both behind (bearing > 110° off heading) AND
        # geographically close (≤ _BYPASS_MAX_DIST_M). A far-ahead WP that is
        # momentarily off-heading (e.g. mid-turn or on a parallel street) must
        # stay in the route so the rerouter brings us back to it instead of
        # cutting straight to the final destination. The final destination
        # (last entry) is never skipped.
        if getattr(self, "_gps_heading_valid", False) and len(remaining) > 1:
            while len(remaining) > 1:
                wp = remaining[0]
                wp_dist = haversine(self._gps_lat, self._gps_lon, wp[0], wp[1])
                if wp_dist > self._BYPASS_MAX_DIST_M:
                    break
                brng = bearing(self._gps_lat, self._gps_lon, wp[0], wp[1])
                diff = abs(self._gps_heading - brng) % 360.0
                if diff > 180.0:
                    diff = 360.0 - diff
                if diff > 110.0:
                    log.info(
                        "Reroute: skipping passed waypoint (%.5f, %.5f) "
                        "— dist=%.0fm, heading=%.0f°, wp_bearing=%.0f°",
                        wp[0], wp[1], wp_dist, self._gps_heading, brng,
                    )
                    remaining.pop(0)
                    self._remaining_dest_wps = list(remaining)
                else:
                    break

        new_points = [(self._gps_lat, self._gps_lon), *remaining]
        self._last_reroute_time = time.monotonic()
        self._off_route_since = 0.0
        log.info("Off-route: recalculating route from current GPS position")
        threading.Thread(
            target=self._fetch_reroute_bg,
            args=(new_points,),
            daemon=True,
        ).start()

    def _fetch_reroute_bg(self, all_points: list[tuple[float, float]]) -> None:
        try:
            result = compute_route(all_points)
        except Exception:
            log.exception("Auto-reroute fetch failed")
            result = None
        GLib.idle_add(self._apply_rerouted_route, all_points, result)

    def _apply_rerouted_route(
        self,
        all_points: list[tuple[float, float]],
        result: tuple[list[list[float]], float, float, list[dict]] | None,
    ) -> bool:
        if result is None or not self._tour_active:
            return False
        coords, duration_s, distance_m, steps = result
        if not steps or not coords:
            return False

        self._tour_steps = steps
        self._tour_step_idx = 0
        self._step_min_dist = None
        self._tour_coords = list(coords)
        self._gps_route_idx = 0
        self._snapped_lat = None
        self._snapped_lon = None
        self._snapped_cum_m = 0.0
        self._compute_route_progress_tables()
        self._start_coord = all_points[0]
        self._end_coord = all_points[-1]
        self._tour_waypoints = list(all_points)
        self._route_coords = coords
        self._tts_last_step_idx = -1
        self._tts_spoken_thresholds = set()
        self._tts_prerender_step_idx = -1
        self._lane_step_idx = -1
        self._speed_zones = self._build_speed_zones()
        self._speed_zones_from_overpass = False
        self._speed_warn_fired = False
        self._route_gen += 1
        self._start_overpass_speed_fetch()
        self._prerender_upcoming_steps(0, 5)
        self._skip_non_actionable_steps()
        self._update_maneuver_overlay()
        self._highlight_active_step()

        if self._status_lbl is not None:
            self._status_lbl.set_text("")
        self._show_route_info(duration_s, distance_m)

        if self._backend == "webkit":
            self._js(f"mapSetRoute({json.dumps(coords)})")
            pts_js = json.dumps([[p[0], p[1]] for p in all_points])
            self._js(f"mapSetWaypoints({pts_js})")
        elif self._shumate_map is not None:
            self._shumate_set_path(self._path_layer, coords)
            if self._wp_layer is not None:
                self._wp_layer.remove_all()
                for i, pt in enumerate(all_points):
                    role = "start" if i == 0 else ("end" if i == len(all_points) - 1 else "via")
                    self._wp_layer.add_marker(self._make_wp_marker(pt[0], pt[1], role))

        if (
            self._steps_toggle_btn is not None
            and self._steps_toggle_btn.get_active()
            and self._steps_listbox is not None
        ):
            self._rebuild_steps_list()
            if self._steps_panel is not None:
                self._set_steps_panel_visible(bool(self._tour_steps))

        if self._on_tour_resumed is not None:
            self._on_tour_resumed()

        log.info("Route recalculated: %.1f km, %d steps", distance_m / 1000, len(steps))
        return False

    # ── Intermediate waypoint tracking ───────────────────────────────────────

    def _check_waypoint_proximity(self) -> None:
        """Called every GPS tick — shows/hides the 'Next waypoint' button and
        automatically marks a waypoint as reached when the driver departs the
        200 m approach radius after having entered it."""
        if not self._tour_active:
            return
        remaining = getattr(self, "_remaining_dest_wps", [])
        # len >= 2 means there is at least one intermediate waypoint before the
        # final destination.  len == 1 means we're heading straight to the end.
        if len(remaining) < 2:
            self._set_next_wp_btn_visible(False)
            return
        next_wp = remaining[0]
        pos_lat = self._snapped_lat if self._snapped_lat is not None else self._gps_lat
        pos_lon = self._snapped_lon if self._snapped_lon is not None else self._gps_lon
        if pos_lat is None or pos_lon is None:
            return
        dist = haversine(pos_lat, pos_lon, next_wp[0], next_wp[1])
        if dist <= 200.0:
            self._wp_in_radius = True
            self._set_next_wp_btn_visible(True)
        elif self._wp_in_radius:
            # Driver has left the 200 m radius → waypoint considered reached.
            self._wp_in_radius = False
            self._set_next_wp_btn_visible(False)
            self._on_waypoint_reached()

    def _on_waypoint_reached(self) -> None:
        """Mark the current intermediate waypoint as done, advance the list, and
        immediately recalculate the route so old segments are removed from the
        display and the navigation points reflect only the remaining legs."""
        remaining = getattr(self, "_remaining_dest_wps", [])
        if len(remaining) < 2:
            return
        wp = remaining[0]
        log.info("Intermediate waypoint reached: (%.5f, %.5f)", wp[0], wp[1])
        self._remaining_dest_wps = remaining[1:]
        log.info(
            "Remaining destination waypoints: %d", len(self._remaining_dest_wps)
        )
        # Trigger an immediate route recalculation from current GPS to the
        # remaining waypoints.  This removes old route segments (the part
        # leading to the now-completed intermediate goal) from both the map
        # and the turn-by-turn step list.
        if (
            self._tour_active
            and self._gps_lat is not None
            and self._gps_lon is not None
            and self._remaining_dest_wps
        ):
            new_points = [(self._gps_lat, self._gps_lon), *self._remaining_dest_wps]
            self._last_reroute_time = time.monotonic()
            self._off_route_since = 0.0
            log.info("Recalculating route after waypoint reached")
            threading.Thread(
                target=self._fetch_reroute_bg,
                args=(new_points,),
                daemon=True,
            ).start()

    def _on_next_wp_clicked(self, _btn: object) -> None:
        """User taps 'Next waypoint' to manually advance past the current
        intermediate waypoint; the route is recalculated inside
        _on_waypoint_reached so old segments disappear immediately."""
        self._wp_in_radius = False
        self._set_next_wp_btn_visible(False)
        self._on_waypoint_reached()

    def _set_next_wp_btn_visible(self, visible: bool) -> None:
        btn = getattr(self, "_next_wp_btn", None)
        if btn is not None:
            btn.set_visible(visible)

    # ── TTS ──────────────────────────────────────────────────────────────────

    def _tts_announce(self, step: dict, distance_m: float) -> None:
        if not self._tts_enabled:
            return
        if step.get("type") == "depart":
            return
        lang = self._tts_effective_language()
        maneuver_text = _translate(lang, maneuver_text_key(step.get("type", ""), step.get("modifier", "")))
        # Subtract look-ahead so the spoken distance matches reality at the
        # moment the driver hears the announcement, not when it was triggered.
        look_ahead_m = self._gps_speed_mps * tts_service.get_latency_s()
        heard_dist = max(0.0, distance_m - look_ahead_m)
        if heard_dist > 60:
            dist_text = self._tts_distance_text(heard_dist, lang)
            text = _translate(lang, "tts.in_distance").format(distance=dist_text) + " " + maneuver_text
        else:
            text = maneuver_text
        tts_service.speak(text, lang, self._tts_voice, quality=self._tts_quality)
