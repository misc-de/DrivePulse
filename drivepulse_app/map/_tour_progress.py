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
    """Return cumulative distances (m) for each actionable turn maneuver.

    A U-turn is always actionable, even when the backend tags it with an
    otherwise-skipped type (OSRM emits U-turns as ``type="continue"``), so a
    ``uturn`` modifier overrides the skip set.
    """
    skip = set(skip_types)
    positions: list[float] = []
    for i, step in enumerate(steps):
        if step.get("type", "") in skip and step.get("modifier") != "uturn":
            continue
        if i < len(step_cum_m):
            positions.append(step_cum_m[i])
    return positions


# Geometry-based U-turn detection (see annotate_uturns).
_UTURN_REVERSAL_DEG = 150.0       # min heading reversal to count as a U-turn
_UTURN_LEG_WINDOW_M = 30.0        # bearing measured over this distance each side
_UTURN_RETURN_PROXIMITY_M = 35.0  # the two legs must end this close (U-turn signature)
_UTURN_RELABEL_DIST_M = 30.0      # apex must be within this of a step to relabel it
_UTURN_MERGE_CONNECTOR_M = 40.0   # fold an adjacent same-side turn shorter than this

# Step types whose maneuver may be relabelled to a U-turn. Roundabouts, ramps,
# merges, depart and arrive are deliberately excluded — a U-turn must not
# override them even if the geometry briefly reverses.
_UTURN_RELABELABLE_TYPES = frozenset({
    "turn", "continue", "new name", "end of road", "fork",
})


def _uturn_leg_point(
    coords: Sequence[Sequence[float]], idx: int, window_m: float, *, forward: bool
) -> Sequence[float] | None:
    """Return the route vertex ~``window_m`` from ``idx`` (or None if too short)."""
    n = len(coords)
    d = 0.0
    j = idx
    if forward:
        while j < n - 1 and d < window_m:
            d += haversine(coords[j][1], coords[j][0], coords[j + 1][1], coords[j + 1][0])
            j += 1
    else:
        while j > 0 and d < window_m:
            d += haversine(coords[j - 1][1], coords[j - 1][0], coords[j][1], coords[j][0])
            j -= 1
    if d < window_m * 0.5:
        return None
    return coords[j]


def _detect_uturn_apexes(
    coords: Sequence[Sequence[float]], route_cum_m: Sequence[float]
) -> list[int]:
    """Find route vertices where travel direction reverses ~180° over a short span.

    A genuine U-turn has two signatures: the heading reverses by at least
    ``_UTURN_REVERSAL_DEG`` AND the points one leg-window before and after end up
    within ``_UTURN_RETURN_PROXIMITY_M`` of each other (you come back beside where
    you were). The proximity test is what separates a U-turn from an ordinary
    sharp junction turn or a wide loop, neither of which doubles back on itself.

    Returns vertex indices, one per U-turn (consecutive hits collapse to the
    sharpest).
    """
    hits: list[tuple[int, float]] = []
    for i in range(len(coords)):
        before = _uturn_leg_point(coords, i, _UTURN_LEG_WINDOW_M, forward=False)
        after = _uturn_leg_point(coords, i, _UTURN_LEG_WINDOW_M, forward=True)
        if before is None or after is None:
            continue
        in_b = bearing(before[1], before[0], coords[i][1], coords[i][0])
        out_b = bearing(coords[i][1], coords[i][0], after[1], after[0])
        diff = abs(in_b - out_b) % 360.0
        if diff > 180.0:
            diff = 360.0 - diff
        if diff < _UTURN_REVERSAL_DEG:
            continue
        if haversine(before[1], before[0], after[1], after[0]) > _UTURN_RETURN_PROXIMITY_M:
            continue
        if hits and route_cum_m[i] - route_cum_m[hits[-1][0]] < 20.0:
            if diff > hits[-1][1]:
                hits[-1] = (i, diff)
            continue
        hits.append((i, diff))
    return [i for i, _ in hits]


def _same_turn_side(mod_a: str, mod_b: str) -> bool:
    return ("left" in mod_a and "left" in mod_b) or ("right" in mod_a and "right" in mod_b)


def _relabel_step_as_uturn(
    steps: list[dict[str, Any]], step_cum: list[float], k: int
) -> None:
    """Mark step ``k`` as a U-turn, folding short same-side connector turns in.

    Backends split a reversal into two adjacent same-side turns over a short
    connector, and the apex may snap to either of them. Any immediately
    adjacent same-side ``turn`` reachable over a connector shorter than
    ``_UTURN_MERGE_CONNECTOR_M`` is absorbed (on both sides), so the pair reads
    as one U-turn anchored at the earliest maneuver (announced in time) with the
    distances summed. With no such neighbour, step ``k`` is relabelled in place.
    Total step distance is preserved either way so the progress tables stay
    consistent.
    """
    side = steps[k].get("modifier", "")
    lo = hi = k
    if (
        k - 1 >= 0
        and steps[k - 1].get("type") == "turn"
        and (step_cum[k] - step_cum[k - 1]) < _UTURN_MERGE_CONNECTOR_M
        and _same_turn_side(steps[k - 1].get("modifier", ""), side)
    ):
        lo = k - 1
    if (
        k + 1 < len(steps)
        and steps[k + 1].get("type") == "turn"
        and (step_cum[k + 1] - step_cum[k]) < _UTURN_MERGE_CONNECTOR_M
        and _same_turn_side(steps[k + 1].get("modifier", ""), side)
    ):
        hi = k + 1

    if lo == hi:
        steps[k]["type"] = "turn"
        steps[k]["modifier"] = "uturn"
        return

    merged = dict(steps[lo])
    merged["type"] = "turn"
    merged["modifier"] = "uturn"
    merged["distance"] = sum(float(steps[j].get("distance") or 0.0) for j in range(lo, hi + 1))
    merged["name"] = next(
        (steps[j].get("name") for j in range(lo, hi + 1) if steps[j].get("name")), ""
    )
    steps[lo : hi + 1] = [merged]
    step_cum[lo : hi + 1] = [step_cum[lo]]


def annotate_uturns(
    coords: Sequence[Sequence[float]],
    steps: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Label real U-turns the routing backend left unmarked.

    Backends sometimes represent a physical U-turn as one or two ordinary turn
    maneuvers (Valhalla splits a tight reversal into two same-side turns) rather
    than tagging it ``uturn``. The driver then gets no U-turn instruction — the
    reversal vanishes into "a long straight, then a turn". This pass scans the
    route geometry for ~180° reversals (:func:`_detect_uturn_apexes`) and, for
    each one not already marked, relabels the nearest maneuver to ``uturn``,
    folding a short same-side connector turn into it so it reads as a single
    instruction.

    Returns a new step list (inputs are not mutated); total step distance is
    preserved so downstream progress tables stay consistent. ``coords`` are
    ``[lon, lat]`` pairs.
    """
    out = [dict(s) for s in steps]
    if len(coords) < 3 or not out:
        return out

    route_cum_m: list[float] = [0.0]
    for i in range(1, len(coords)):
        route_cum_m.append(
            route_cum_m[-1]
            + haversine(coords[i - 1][1], coords[i - 1][0], coords[i][1], coords[i][0])
        )

    apex_indices = _detect_uturn_apexes(coords, route_cum_m)
    if not apex_indices:
        return out

    # Cumulative route position of each step, found by snapping its maneuver
    # coordinate to the geometry with a forward-only search so the outbound and
    # return legs of a U-turn (which sit close together) cannot cross-match.
    step_cum: list[float] = []
    search = 0
    for s in out:
        best_i, best_d = search, float("inf")
        for i in range(search, len(coords)):
            d = haversine(s["lat"], s["lon"], coords[i][1], coords[i][0])
            if d < best_d:
                best_d, best_i = d, i
        step_cum.append(route_cum_m[best_i])
        search = best_i

    # Apply from the last apex to the first so earlier indices stay valid as
    # merges shrink the list.
    for ai in sorted(apex_indices, reverse=True):
        apex_cum = route_cum_m[ai]
        k = min(range(len(out)), key=lambda j: abs(step_cum[j] - apex_cum))
        if out[k].get("modifier") == "uturn":
            continue  # backend already marked this reversal
        if abs(step_cum[k] - apex_cum) > _UTURN_RELABEL_DIST_M:
            continue  # no maneuver sits at the reversal — leave the route as-is
        if out[k].get("type") not in _UTURN_RELABELABLE_TYPES:
            continue  # don't override roundabouts, ramps, merges, arrive…
        _relabel_step_as_uturn(out, step_cum, k)
    return out


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


def next_actionable_step_idx(
    steps: Sequence[dict[str, Any]],
    idx: int,
    non_actionable_types: Iterable[str],
) -> int:
    """Advance ``idx`` past consecutive non-actionable steps (but never past the last).

    A ``uturn`` maneuver is never skipped even when its type is in the skip set
    (OSRM labels U-turns ``type="continue"``) — otherwise the driver gets no
    U-turn instruction and the reversal silently collapses into the prior step.
    """
    skip = set(non_actionable_types)
    while (
        idx < len(steps) - 1
        and steps[idx].get("type") in skip
        and steps[idx].get("modifier") != "uturn"
    ):
        idx += 1
    return idx


def maneuver_passed(
    step_min_dist: float | None,
    distance_m: float,
    progress_m: float,
    step_route_cum_m: float | None,
    *,
    closest_m: float,
    pass_growth_m: float,
) -> bool:
    """Decide whether the active turn maneuver has been driven past.

    Primary signal: the car got within ``closest_m`` of the maneuver and the
    distance has since grown back past ``step_min_dist + pass_growth_m`` (a
    closest-approach test that tolerates GPS noise). Route-progress fallback:
    the maneuver is behind us once cumulative route progress passes the
    maneuver's own route position by ``closest_m`` — used when the
    closest-approach test hasn't fired yet. ``step_route_cum_m`` is ``None`` when
    no route table entry is available, disabling the fallback.
    """
    passed = (
        step_min_dist is not None
        and step_min_dist <= closest_m
        and distance_m > step_min_dist + pass_growth_m
    )
    if not passed and step_route_cum_m is not None and progress_m > step_route_cum_m + closest_m:
        passed = True
    return passed


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
