"""Cross-page hand-offs from the Cars tab to Map and StopWatch:
swipe wraparound on the first tab and "open this trip / run elsewhere"
shortcuts."""
from __future__ import annotations

import time

from gi.repository import GLib


class DashboardNavRoutingMixin:
    def _on_cars_back_swipe(self) -> None:
        """From the Cars tab (overview) — a right-swipe lands on StopWatch.

        Cars is the first tab; a right-swipe would otherwise have nowhere to
        go. Instead of the ViewSwitcher's endless-loop behaviour we jump
        straight to the opposite end (StopWatch) so the gesture is not lost.
        """
        if self.view_stack.get_visible_child_name() == self.PAGE_CARS:
            self.view_stack.set_visible_child_name(self.PAGE_STOPWATCH)
            self._last_swipe_time = time.monotonic()

    def _on_cars_forward_swipe(self) -> None:
        """From the Cars tab (list) — a left-swipe lands on the Map."""
        if self.view_stack.get_visible_child_name() == self.PAGE_CARS:
            self.view_stack.set_visible_child_name(self.PAGE_MAP)
            self._last_swipe_time = time.monotonic()

    def _open_trip_as_route_on_map(
        self,
        coords_lonlat: list[list[float]],
        distance_km: float | None,
        duration_s: float | None,
        label: str | None,
    ) -> None:
        """Switch to the Map tab and draw a recorded trip's polyline as the route."""
        if not coords_lonlat:
            return
        self._cancel_map_unload()
        self._ensure_map_page()
        self.view_stack.set_visible_child_name(self.PAGE_MAP)

        def _load_and_remove() -> bool:
            if self.map_page is not None:
                self.map_page.load_trip_as_route(
                    coords_lonlat, distance_km, duration_s, label
                )
            return False

        GLib.idle_add(_load_and_remove)

    def _show_trip_replay_on_map_from_cars(self, trip_id: int, meta: dict) -> None:
        """Switch to the Map tab and reuse the map's own replay machinery
        (speed-coloured polyline, info card, speed/RPM chart) for the
        trip the user picked in the Cars page."""
        if not hasattr(self, "map_page"):
            return
        self.view_stack.set_visible_child_name(self.PAGE_MAP)

        def _replay_and_remove() -> bool:
            try:
                self.map_page._show_trip_replay(meta)
            except Exception:
                # Fall back to the polyline-only path so the user at
                # least sees the track if the full replay errors out.
                try:
                    samples = list(self.db.samples_for_trip(trip_id)) if self.db else []
                except Exception:
                    samples = []
                coords = [
                    [float(s["lon"]), float(s["lat"])]
                    for s in samples
                    if s["lat"] is not None and s["lon"] is not None
                ]
                if len(coords) >= 2:
                    self.map_page.load_trip_as_route(
                        coords, meta.get("distance_km"), meta.get("duration_s"),
                        meta.get("trip_label"),
                    )
            return False

        GLib.idle_add(_replay_and_remove)

    def _load_persisted_run_into_stopwatch(self, data: dict) -> None:
        """Hand off a saved stopwatch run, switch to the StopWatch tab, replay."""
        sw = getattr(self, "stopwatch_page", None)
        if sw is None or not hasattr(sw, "load_persisted_run"):
            return
        if not sw.load_persisted_run(data):
            return
        self.view_stack.set_visible_child_name(self.PAGE_STOPWATCH)
        if hasattr(sw, "replay_measurement"):
            GLib.idle_add(lambda: (sw.replay_measurement(), False)[1])
