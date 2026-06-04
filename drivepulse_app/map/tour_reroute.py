"""Automatic rerouting and intermediate-waypoint tracking for the map tour.

Split out of ``tour.py``: off-route detection with a sustained-distance +
speed gate, the reroute recalculation (dropping only clearly-passed
intermediate waypoints), and the "next waypoint" proximity logic that advances
multi-stop routes. Driven from :class:`MapTourMixin` via ``self`` on the
composed ``MapPage``.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from gi.repository import GLib

from drivepulse_app.diagnostics import get_logger
from drivepulse_app.map._jsbridge import js_call
from drivepulse_app.map._tour_progress import (
    annotate_uturns,
    off_route_decision,
    waypoint_is_passed,
)
from drivepulse_app.map.services import bearing, compute_route, haversine

log = get_logger(__name__)


class MapTourRerouteMixin:
    """Off-route detection, reroute recalculation and waypoint advancement."""

    # Concrete MapPage state surfaced to this mixin. See project_mixin_typing.md.
    _start_coord: tuple[float, float] | None
    _end_coord: tuple[float, float] | None
    _gps_lat: float | None
    _gps_lon: float | None
    _gps_heading: float
    _gps_speed_mps: float
    _snapped_lat: float | None
    _snapped_lon: float | None
    _step_min_dist: float | None
    _tour_active: bool
    _route_gen: int
    _off_route_since: float
    _last_reroute_time: float
    _tts_spoken_thresholds: set[int]
    _backend: str
    _status_lbl: Any
    _shumate_map: Any
    _path_layer: Any
    _guide_path_layer: Any
    _wp_layer: Any
    _steps_toggle_btn: Any
    _steps_listbox: Any
    _steps_panel: Any

    # Methods defined in sibling mixins, called here via ``self``.
    _compute_route_progress_tables: Callable[[], None]
    _build_speed_zones: Callable[[], list[tuple[float, float]]]
    _start_overpass_speed_fetch: Callable[[], None]
    _prerender_upcoming_steps: Callable[..., None]
    _skip_non_actionable_steps: Callable[[], None]
    _update_maneuver_overlay: Callable[[], None]
    _highlight_active_step: Callable[..., Any]
    _show_route_info: Callable[..., Any]
    _rebuild_steps_list: Callable[..., Any]
    _set_steps_panel_visible: Callable[[bool], None]
    _shumate_set_path: Callable[..., Any]
    _make_wp_marker: Callable[..., Any]
    _js: Callable[[str], None]
    _on_tour_resumed: Callable[..., Any] | None
    _persist_active_tour: Callable[[], None]

    # Off-route detection: reroute automatically when the perpendicular distance
    # from the GPS to the snapped route position exceeds this threshold for a
    # sustained period. Speed gate prevents rerouting while nearly stationary
    # (GPS drift, waiting at traffic lights).
    _OFF_ROUTE_M = 30.0          # metres off-route to start the timer
    _OFF_ROUTE_CONFIRM_S = 4.0   # seconds off-route before rerouting fires
    _REROUTE_COOLDOWN_S = 30.0   # minimum gap between successive auto-reroutes
    _REROUTE_MIN_SPEED_KMH = 10.0  # don't reroute below this speed
    _BYPASS_MAX_DIST_M = 250.0   # only drop a behind-heading WP when within this radius

    # Wrong-way / U-turn reroute: a U-turn keeps you *on* the route line (~0 m
    # off), so the distance test above never fires. Detect it instead by the GPS
    # heading running opposite to the route's own direction at the snapped
    # segment. This deliberately overrides the cooldown — a reversal is a clear,
    # intentional signal — but keeps a short min-gap so it can't thrash.
    _UTURN_REROUTE_DEG = 120.0   # heading vs route direction beyond this = wrong-way
    _UTURN_CONFIRM_S = 3.0       # sustained wrong-way for this long before rerouting
    _UTURN_MIN_GAP_S = 8.0       # minimum gap after any reroute before a U-turn one
    _wrong_way_since: float = 0.0

    # ── Auto-rerouting ────────────────────────────────────────────────────────

    def _check_off_route(self, off_dist_m: float, now: float) -> None:
        """Called each GPS tick with the perpendicular distance to the route."""
        speed_kmh = self._gps_speed_mps * 3.6
        self._off_route_since, should_reroute = off_route_decision(
            off_dist_m, speed_kmh, self._off_route_since, now, self._last_reroute_time,
            off_route_m=self._OFF_ROUTE_M,
            min_speed_kmh=self._REROUTE_MIN_SPEED_KMH,
            confirm_s=self._OFF_ROUTE_CONFIRM_S,
            cooldown_s=self._REROUTE_COOLDOWN_S,
        )
        if should_reroute:
            self._wrong_way_since = 0.0
            self._trigger_reroute()
            return
        # A U-turn keeps you on the route line, so the distance test never fires
        # — catch it by heading instead (and let it override the cooldown).
        if self._wrong_way_reroute(speed_kmh, now):
            self._trigger_reroute()

    def _route_bearing_at_idx(self, idx: int) -> float | None:
        """Bearing (deg) of the route's travel direction at vertex *idx*, or None.

        ``_tour_coords`` are ``[lon, lat]`` pairs; ``idx`` is the snapped segment's
        start vertex (``self._gps_route_idx``)."""
        coords = self._tour_coords
        if not coords or idx < 0 or idx >= len(coords) - 1:
            return None
        a, b = coords[idx], coords[idx + 1]
        return bearing(a[1], a[0], b[1], b[0])

    def _wrong_way_reroute(self, speed_kmh: float, now: float) -> bool:
        """True once the driver has been heading opposite the route long enough
        to warrant a reroute — a U-turn the perpendicular-distance test misses."""
        if speed_kmh < self._REROUTE_MIN_SPEED_KMH or not getattr(
            self, "_gps_heading_valid", False
        ):
            self._wrong_way_since = 0.0
            return False
        route_brng = self._route_bearing_at_idx(self._gps_route_idx)
        if route_brng is None:
            self._wrong_way_since = 0.0
            return False
        # Don't fight a U-turn the route itself prescribes here.
        idx = self._tour_step_idx
        step = self._tour_steps[idx] if 0 <= idx < len(self._tour_steps) else {}
        if step.get("modifier") == "uturn":
            self._wrong_way_since = 0.0
            return False
        diff = abs(self._gps_heading - route_brng) % 360.0
        if diff > 180.0:
            diff = 360.0 - diff
        if diff < self._UTURN_REROUTE_DEG:
            self._wrong_way_since = 0.0
            return False
        if self._wrong_way_since == 0.0:
            self._wrong_way_since = now
            return False
        if now - self._wrong_way_since < self._UTURN_CONFIRM_S:
            return False
        if now - self._last_reroute_time < self._UTURN_MIN_GAP_S:
            return False
        log.info(
            "Wrong-way/U-turn: heading=%.0f vs route=%.0f (diff=%.0f) — rerouting",
            self._gps_heading, route_brng, diff,
        )
        self._wrong_way_since = 0.0
        return True

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

        # Only drop an intermediate waypoint when the driver has clearly left
        # it behind — either by driving right past it (close + behind) or by
        # heading almost straight away from it (see waypoint_is_passed). A
        # far-ahead WP that is only momentarily off-heading (mid-turn, parallel
        # street) stays in the route so the rerouter brings us back to it
        # instead of cutting straight to the final destination. The final
        # destination (last entry) is never skipped.
        if getattr(self, "_gps_heading_valid", False) and len(remaining) > 1:
            while len(remaining) > 1:
                wp = remaining[0]
                passed, wp_dist, brng = waypoint_is_passed(
                    self._gps_lat, self._gps_lon, self._gps_heading,
                    wp[0], wp[1], self._BYPASS_MAX_DIST_M,
                )
                if not passed:
                    break
                log.info(
                    "Reroute: skipping passed waypoint (%.5f, %.5f) "
                    "— dist=%.0fm, heading=%.0f°, wp_bearing=%.0f°",
                    wp[0], wp[1], wp_dist, self._gps_heading, brng,
                )
                remaining.pop(0)
                self._remaining_dest_wps = list(remaining)

        # A bypassed via may have been dropped above — persist the new progress
        # so an app restart resumes from the legs that are genuinely left.
        self._persist_active_tour()
        new_points = [(self._gps_lat, self._gps_lon), *remaining]
        self._last_reroute_time = time.monotonic()
        self._off_route_since = 0.0
        self._wrong_way_since = 0.0
        # Reroute diagnostics: capture the bypass-decision inputs for the next
        # remaining waypoint so a drive where the route balloons back to a
        # deliberately-bypassed via can be reconstructed from the log.
        wp0 = remaining[0]
        passed0, wp0_dist, wp0_brng = waypoint_is_passed(
            self._gps_lat, self._gps_lon, self._gps_heading,
            wp0[0], wp0[1], self._BYPASS_MAX_DIST_M,
        )
        hdiff = abs(self._gps_heading - wp0_brng) % 360.0
        if hdiff > 180.0:
            hdiff = 360.0 - hdiff
        log.info(
            "Off-route: recalculating route from current GPS position "
            "(gps=(%.5f,%.5f) heading=%.0f hvalid=%s remaining=%d "
            "next_wp=(%.5f,%.5f) wp_dist=%.0fm bearing_diff=%.0f passed=%s)",
            self._gps_lat, self._gps_lon, self._gps_heading,
            getattr(self, "_gps_heading_valid", False), len(remaining),
            wp0[0], wp0[1], wp0_dist, hdiff, passed0,
        )
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

        # Some backends (e.g. OSRM) emit U-turns as type="continue" or split
        # them into ordinary turns; relabel them so the driver is actually told
        # to make the U-turn instead of silently skipping it.
        steps = annotate_uturns(coords, steps)
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
        # A reroute restarts the step list at the "depart" maneuver ("Start
        # tour"), which is meaningless mid-drive — skip straight to the first
        # real turn so the card never shows "Start tour / 0 m" while moving.
        if (
            self._tour_step_idx == 0
            and len(self._tour_steps) > 1
            and self._tour_steps[0].get("type") == "depart"
        ):
            self._tour_step_idx = 1
            self._step_min_dist = None
        self._update_maneuver_overlay()
        self._highlight_active_step()

        if self._status_lbl is not None:
            self._status_lbl.set_text("")
        self._show_route_info(duration_s, distance_m)

        if self._backend == "webkit":
            self._js(js_call("mapSetRoute", coords))
            self._js(js_call("mapSetWaypoints", [[p[0], p[1]] for p in all_points]))
            self._js("mapClearGuideToStart()")
        elif self._shumate_map is not None:
            self._shumate_set_path(self._path_layer, coords)
            if hasattr(self, "_shumate_set_route_muted"):
                self._shumate_set_route_muted(False)  # new route = valid again
            # We're navigating the real route now — drop any leftover
            # guide-to-start (green) line so it can't linger to the destination.
            if self._guide_path_layer is not None:
                self._guide_path_layer.remove_all()
            if self._wp_layer is not None:
                self._wp_layer.remove_all()
                for i, pt in enumerate(all_points):
                    role = "start" if i == 0 else ("end" if i == len(all_points) - 1 else "via")
                    # A reroute always happens mid-drive — never re-add the start
                    # arrow (it would just pin a car-lookalike at the reroute spot).
                    if role == "start":
                        continue
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

        log.info(
            "Route recalculated: %.1f km, %d steps, waypoints=%s",
            distance_m / 1000, len(steps),
            [(round(p[0], 5), round(p[1], 5)) for p in all_points],
        )
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
        self._persist_active_tour()
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
