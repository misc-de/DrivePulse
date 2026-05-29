"""Pure route-progress maths for the navigation tour state machine.

These functions carry no GTK or instance state: given route geometry and the
OSRM/Valhalla step list they produce the cumulative-distance tables, speed-zone
breakpoints, maneuver positions and GPS-progress lookups that ``MapTourMixin``
drives the turn-by-turn UI from. Keeping them here makes the heavy navigation
logic unit-testable without a running map.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from drivepulse_app.common import _translate
from drivepulse_app.map._geometry import bearing, haversine
from drivepulse_app.map._speed_zones import mock_speed_kmh


def compute_route_progress_tables(
    coords: Sequence[Sequence[float]],
    steps: Sequence[dict[str, Any]],
) -> tuple[list[float], list[float]]:
    """Precompute distance-along-route tables for fast progress lookups.

    Returns ``(route_cum_m, step_cum_m)`` where:

    - ``route_cum_m[i]`` = metres from the start of the route up to vertex ``i``
    - ``step_cum_m[k]``  = metres from the start of the route up to maneuver
      ``k``, derived from OSRM's per-step ``distance`` so it stays correct even
      when the step's coordinate is slightly offset from the geometry.

    Coordinates are ``[lon, lat]`` pairs.
    """
    route_cum_m: list[float] = []
    step_cum_m: list[float] = []
    if coords:
        route_cum_m.append(0.0)
        for i in range(1, len(coords)):
            a = coords[i - 1]
            b = coords[i]
            seg = haversine(a[1], a[0], b[1], b[0])
            route_cum_m.append(route_cum_m[-1] + seg)
    if steps:
        cum = 0.0
        for step in steps:
            # Maneuver k sits at the START of step k, so its position along the
            # route is the cumulative distance of steps 0..k-1.
            step_cum_m.append(cum)
            cum += float(step.get("distance") or 0.0)
    return route_cum_m, step_cum_m


def build_speed_zones(
    steps: Sequence[dict[str, Any]],
    step_cum_m: Sequence[float],
) -> list[tuple[float, float]]:
    """Build ``(cum_dist_m, speed_kmh)`` breakpoints.

    Prefers Valhalla's real ``speed_limit`` values. Falls back to the ref-tag
    heuristic (A* → 120, B* → 70, urban → 40) so the sign is always shown during
    mock-mode tours where Valhalla data may be absent.
    """
    if not steps or not step_cum_m:
        return []
    zones: list[tuple[float, float]] = []
    prev_speed: float | None = None
    for i, step in enumerate(steps):
        if "speed_limit" in step:
            speed = float(step["speed_limit"])
        else:
            speed = mock_speed_kmh(step.get("ref") or "")
        if speed != prev_speed:
            cum = step_cum_m[i] if i < len(step_cum_m) else 0.0
            zones.append((cum, speed))
            prev_speed = speed
    return zones


def build_maneuver_positions(
    steps: Sequence[dict[str, Any]],
    step_cum_m: Sequence[float],
    skip_types: Iterable[str],
) -> list[float]:
    """Return cumulative distances (m) for each actionable turn maneuver."""
    skip = set(skip_types)
    positions: list[float] = []
    for i, step in enumerate(steps):
        if step.get("type", "") in skip:
            continue
        if i < len(step_cum_m):
            positions.append(step_cum_m[i])
    return positions


def nearest_route_progress(
    coords: Sequence[Sequence[float]],
    route_cum_m: Sequence[float],
    gps_lon: float,
    gps_lat: float,
    start_idx: int,
) -> tuple[int, float]:
    """Find the route vertex nearest the GPS fix at/after ``start_idx``.

    Returns ``(best_idx, cum_m)``. The search only moves forward from
    ``start_idx`` so progress never rewinds along the route. Returns
    ``(start_idx, 0.0)`` when the route tables are empty.
    """
    if not coords or not route_cum_m:
        return start_idx, 0.0
    best_i = start_idx
    best_d = float("inf")
    for i in range(start_idx, len(coords)):
        coord = coords[i]
        dx = coord[0] - gps_lon
        dy = coord[1] - gps_lat
        d = dx * dx + dy * dy
        if d < best_d:
            best_d = d
            best_i = i
    return best_i, route_cum_m[best_i]


def waypoint_is_passed(
    gps_lat: float,
    gps_lon: float,
    gps_heading: float,
    wp_lat: float,
    wp_lon: float,
    max_dist_m: float,
    *,
    bearing_threshold_deg: float = 110.0,
) -> tuple[bool, float, float]:
    """Decide whether an intermediate waypoint has been driven past.

    A waypoint only counts as *passed* (and may be dropped from a reroute) when
    it is both geographically close (``<= max_dist_m``) and clearly *behind* the
    current heading — its bearing differs from the heading by more than
    ``bearing_threshold_deg``. A far-ahead waypoint that is momentarily
    off-heading (mid-turn, parallel street) must stay in the route.

    Returns ``(passed, dist_m, wp_bearing_deg)``; the distance and bearing are
    returned so callers can log them without recomputing.
    """
    dist_m = haversine(gps_lat, gps_lon, wp_lat, wp_lon)
    brng = bearing(gps_lat, gps_lon, wp_lat, wp_lon)
    diff = abs(gps_heading - brng) % 360.0
    if diff > 180.0:
        diff = 360.0 - diff
    passed = dist_m <= max_dist_m and diff > bearing_threshold_deg
    return passed, dist_m, brng


def off_route_decision(
    off_dist_m: float,
    speed_kmh: float,
    off_route_since: float,
    now: float,
    last_reroute_time: float,
    *,
    off_route_m: float,
    min_speed_kmh: float,
    confirm_s: float,
    cooldown_s: float,
) -> tuple[float, bool]:
    """Decide whether an off-route condition should trigger an auto-reroute.

    Returns ``(new_off_route_since, should_reroute)``. The off-route timer only
    starts once the GPS is both far enough off the route *and* moving above the
    minimum speed (so drift while stationary doesn't count); it must then stay
    off-route for ``confirm_s`` seconds, and reroutes are gated by a cooldown so
    they can't fire back-to-back.

    ``new_off_route_since`` is ``0.0`` when the timer is reset, ``now`` when it
    has just started, or the unchanged start time while confirming/cooling down.
    The caller resets the timer itself once the reroute actually fires.
    """
    if off_dist_m <= off_route_m or speed_kmh < min_speed_kmh:
        return 0.0, False
    if off_route_since == 0.0:
        return now, False
    if now - off_route_since < confirm_s:
        return off_route_since, False
    if now - last_reroute_time < cooldown_s:
        return off_route_since, False
    return off_route_since, True


def tts_distance_text(meters: float, lang: str) -> str:
    """Spoken distance phrase: rounded to 10 m below ~1 km, else 0.1 km steps."""
    if meters < 950:
        n = int(round(meters / 10) * 10) or 10
        return _translate(lang, "tts.distance.m").format(n=n)
    km = round(meters / 1000, 1)
    return _translate(lang, "tts.distance.km").format(n=km)
