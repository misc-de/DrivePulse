"""Reverse a calculated (not yet started) tour.

A reversed tour needs its own turn-by-turn steps, so the route is calculated
again with the waypoints in opposite order instead of just flipping the line:

* planned / saved tours (``_route_result``): waypoints reversed, routed again;
* tours rebuilt from a recorded trip (``_trip_trace_result``): the GPS trace
  is mirrored — points reversed, timestamps mirrored so the intervals (and
  thus stop detection in the trace pipeline) stay intact.

Both result handlers store what they were built from in ``_reverse_source``;
the "Umkehren" button next to "Tour starten" is visible only while it is set
and the tour waits to be started (see ``_set_tour_button``).
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from gi.repository import GLib

from drivepulse_app.diagnostics import get_logger
from drivepulse_app.map.services import compute_route

log = get_logger(__name__)


def reverse_trace(
    coords: list[list[float]], timestamps: list[float] | None
) -> tuple[list[list[float]], list[float] | None]:
    """Reverse a recorded GPS trace, mirroring timestamps so they keep
    increasing with the original gaps between samples."""
    rev_coords = [list(c) for c in reversed(coords)]
    if not timestamps or len(timestamps) != len(coords):
        return rev_coords, None
    t_sum = timestamps[0] + timestamps[-1]
    return rev_coords, [t_sum - t for t in reversed(timestamps)]


class MapTourReverseMixin:
    # Concrete MapPage state surfaced to this mixin. See project_mixin_typing.md.
    _reverse_source: tuple[str, Any] | None
    _tour_active: bool
    _tour_paused: bool
    _tour_start_btn: Any
    _tour_reverse_btn: Any
    _entry_rows: list[Any]

    _set_route_loading: Callable[[bool], None]
    _fetch_trip_trace: Callable[..., None]
    _route_result: Callable[..., bool]
    _update_placeholders: Callable[[], None]

    def _on_tour_reverse_clicked(self, _btn: object) -> None:
        source = getattr(self, "_reverse_source", None)
        if source is None or self._tour_active or self._tour_paused:
            return
        kind, data = source
        self._set_reverse_busy(True)
        if kind == "trace":
            coords, label, distance_km, duration_s, timestamps = data
            rev_coords, rev_ts = reverse_trace(coords, timestamps)
            log.info("tour_reverse trace pts=%d", len(rev_coords))
            self._swap_entry_texts()
            threading.Thread(
                target=self._fetch_trip_trace,
                args=(rev_coords, label, distance_km, duration_s, rev_ts),
                daemon=True,
            ).start()
        else:
            points = list(reversed(data))
            log.info("tour_reverse plan wps=%d", len(points))
            self._swap_entry_texts()
            threading.Thread(target=self._compute_reversed_plan, args=(points,), daemon=True).start()

    def _compute_reversed_plan(self, points: list[tuple[float, float]]) -> None:
        try:
            result = compute_route(points)
        except Exception:
            log.exception("Could not compute reversed route")
            result = None
        GLib.idle_add(self._reversed_plan_result, points, result)

    def _reversed_plan_result(self, points: list[tuple[float, float]], result: Any) -> bool:
        self._set_reverse_busy(False)
        if result is None:
            # Keep the current (forward) tour; undo the form swap.
            self._swap_entry_texts()
        self._route_result(points, result)
        return False

    def _set_reverse_busy(self, busy: bool) -> None:
        self._set_route_loading(busy)
        for btn in (self._tour_start_btn, getattr(self, "_tour_reverse_btn", None)):
            if btn is not None:
                btn.set_sensitive(not busy)

    def _swap_entry_texts(self) -> None:
        """Mirror the Von/Nach(/via) form so it matches the reversed tour."""
        rows = getattr(self, "_entry_rows", None) or []
        texts = [row[1].get_text() for row in rows]
        for row, text in zip(rows, reversed(texts), strict=True):
            row[1].set_text(text)
        if hasattr(self, "_update_placeholders"):
            self._update_placeholders()
