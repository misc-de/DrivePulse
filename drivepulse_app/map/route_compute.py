"""Route computation: resolve start/waypoints/end via geocoder, call the
routing engine in a worker thread, push the polyline / waypoints / fit-bounds
to the map backend on the GLib main loop."""
from __future__ import annotations

import json

from gi.repository import GLib

from drivepulse_app.common import _translate
from drivepulse_app.diagnostics import get_logger
from drivepulse_app.map.services import compute_route, geocode, resolve_route_points

log = get_logger(__name__)


class MapRouteComputeMixin:
    # Owning class (MapPage) initializes these in __init__. Annotated here so
    # mypy doesn't infer them as non-Optional from the assignments below.
    _start_coord: tuple[float, float] | None
    _end_coord: tuple[float, float] | None
    _step_min_dist: float | None
    _snapped_lat: float | None
    _snapped_lon: float | None

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
            result = compute_route(all_points)
        except Exception:
            log.exception("Could not compute map route")
            GLib.idle_add(self._route_error)
            return
        GLib.idle_add(self._route_result, all_points, result)

    def _route_error(self) -> bool:
        self._status_lbl.set_text(_translate(self.language, "map.routing.error"))
        self._restore_route_btn()
        return False

    def _route_result(
        self,
        all_points: list[tuple[float, float]],
        result: tuple[list[list[float]], float, float, list[dict]] | None,
    ) -> bool:
        self._restore_route_btn()
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
        # Pre-render the first couple of nav prompts so that the moment
        # the driver hits "Start", the very first announcement plays
        # without the usual 1-2 s piper warm-up latency.
        try:
            self._prerender_upcoming_steps(0, 2)
        except Exception:
            log.debug("Could not pre-render initial nav prompts", exc_info=True)
        self._gps_route_idx = 0
        self._snapped_lat = None
        self._snapped_lon = None
        self._snapped_cum_m = 0.0
        self._compute_route_progress_tables()
        self._start_coord = all_points[0]
        self._end_coord = all_points[-1]
        self._tour_waypoints = list(all_points)
        # The duration/distance OSD card is built but kept hidden after route
        # calculation — the user did not want an info bar auto-appearing on the
        # map.  The status label still conveys transient routing-state messages.
        self._status_lbl.set_text("")
        self._hide_route_info()
        self._set_tour_controls_visible(True)
        self._set_tour_button("start")
        if self._tour_save_btn is not None:
            self._tour_save_btn.set_visible(
                getattr(self, "_loaded_tour_id", None) is None
            )
        if hasattr(self, "_update_left_chrome_visibility"):
            self._update_left_chrome_visibility()
        # Do not auto-open the steps panel after computing a route — the user
        # opens it explicitly via the toggle. If it was left open from a previous
        # tour, close it so the stale step list does not stay visible.
        if self._steps_toggle_btn is not None and self._steps_toggle_btn.get_active():
            self._steps_toggle_btn.set_active(False)
        elif self._steps_panel is not None:
            self._set_steps_panel_visible(False)

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
