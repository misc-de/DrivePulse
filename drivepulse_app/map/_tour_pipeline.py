"""GPS-trace → road-snapped tour reconstruction pipeline.

``route_via_gps_waypoints`` is the public entry point. Internally:

1. Try Valhalla map-matching (``valhalla_trace_route`` per leg).
2. If the match diverges from the GPS trace or visits orphan
   territory, fall back to the waypoint-extraction path.
3. The fallback path cleans the GPS trace, extracts turn waypoints,
   snaps them to roads with directional hints, prunes spurious
   detours, removes U-turn artefacts (with several artefact-detection
   heuristics) and finally asks ``compute_route`` to render an
   actual road-following polyline through the surviving waypoints.

The pipeline depends on ``_geometry`` (math helpers), ``_routing``
(``compute_route`` and ``valhalla_trace_route``) and the shared HTTP
client. It is the largest cohesive block extracted from services.py.
"""
from __future__ import annotations

import concurrent.futures
import logging
import math
from collections.abc import Callable
from typing import Any

from drivepulse_app.diagnostics import get_logger, write_diagnostic_log
from drivepulse_app.http_client import http_get
from drivepulse_app.map._geometry import bearing, haversine
from drivepulse_app.map._routing import compute_route, valhalla_trace_route

log = get_logger(__name__)

HttpGet = Callable[[str], Any]


def _clean_gps_trace(
    coords: list[list[float]],
    timestamps: list[float] | None = None,
    spike_min_m: float = 30.0,
    cluster_radius_m: float = 15.0,
    stop_gap_s: float = 60.0,
) -> tuple[list[list[float]], list[int]]:
    """Remove GPS artefacts before waypoint extraction.

    Returns ``(cleaned_coords, stop_indices)`` where *stop_indices* is a list
    of indices in *cleaned_coords* at which a new leg begins (the first point
    after a stop gap).  Callers should split the trace at these indices so
    that each leg is routed independently and the stop boundary is guaranteed
    to appear as a waypoint.

    Pass 1 – spikes: a point that jumps far from both neighbours while the
    neighbours remain close to each other (classic multipath bounce).
    Condition: d(prev→curr) > spike_min_m AND d(curr→next) > spike_min_m
               AND d(prev→next) < 0.5 * max(d(prev→curr), d(curr→next)).

    Pass 2 – clusters: consecutive points within *cluster_radius_m* of the
    last committed point (e.g. maneuvering on a forecourt) are collapsed to
    just the cluster entry and exit.  When *timestamps* are supplied a gap
    of more than *stop_gap_s* seconds within a cluster is treated as a
    deliberate stop (e.g. engine off for refuelling): both the last point
    before the gap and the first point after are kept and the post-gap index
    is added to *stop_indices*.
    """
    if len(coords) < 3:
        return list(coords), []

    ts = timestamps  # parallel array; may be None

    # Pass 1: spike removal — carry timestamps in a parallel array
    no_spikes: list[list[float]] = [coords[0]]
    no_spikes_ts: list[float] = [ts[0] if ts else 0.0]
    i = 1
    while i < len(coords) - 1:
        prev = no_spikes[-1]
        curr = coords[i]
        nxt = coords[i + 1]
        d_prev = haversine(prev[1], prev[0], curr[1], curr[0])
        d_next = haversine(curr[1], curr[0], nxt[1], nxt[0])
        d_skip = haversine(prev[1], prev[0], nxt[1], nxt[0])
        if (d_prev > spike_min_m and d_next > spike_min_m
                and d_skip < max(d_prev, d_next) * 0.5):
            i += 1
            continue
        no_spikes.append(curr)
        no_spikes_ts.append(ts[i] if ts else 0.0)
        i += 1
    no_spikes.append(coords[-1])
    no_spikes_ts.append(ts[-1] if ts else 0.0)

    if len(no_spikes) < 2:
        return no_spikes, []

    # Pass 2: cluster collapse with stop-gap detection
    result: list[list[float]] = [no_spikes[0]]
    stop_indices: list[int] = []
    last_skipped: list[float] | None = None
    last_skipped_ts: float = 0.0

    for i in range(1, len(no_spikes)):
        pt = no_spikes[i]
        pt_ts = no_spikes_ts[i]

        # Stop gap within a cluster: end-of-leg / start-of-leg boundary
        if (last_skipped is not None and ts is not None
                and pt_ts - last_skipped_ts > stop_gap_s):
            result.append(last_skipped)      # last point before engine-off
            result.append(pt)               # first point after engine-on
            stop_indices.append(len(result) - 1)
            last_skipped = None
            continue

        anchor = result[-1]
        d = haversine(anchor[1], anchor[0], pt[1], pt[0])
        if d < cluster_radius_m:
            last_skipped = pt
            last_skipped_ts = pt_ts
        else:
            if last_skipped is not None:
                result.append(last_skipped)
                last_skipped = None
            result.append(pt)

    if last_skipped is not None:
        result.append(last_skipped)

    return result, stop_indices


def extract_turn_waypoints(
    coords_lonlat: list[list[float]],
    min_turn_deg: float = 30.0,
    min_segment_m: float = 150.0,
    max_waypoints: int = 25,
) -> list[tuple[float, float]]:
    """Extract start, significant turns, and end as (lat, lon) waypoints.

    Accumulates GPS points into segments of at least *min_segment_m* before
    computing each segment bearing — this filters per-point GPS noise before
    turn detection.  A waypoint is added when adjacent segment bearings differ
    by at least *min_turn_deg* degrees.
    """
    if len(coords_lonlat) < 2:
        return [(c[1], c[0]) for c in coords_lonlat]

    waypoints: list[tuple[float, float]] = [(coords_lonlat[0][1], coords_lonlat[0][0])]
    seg_start_idx = 0
    seg_dist = 0.0
    prev_seg_bearing: float | None = None

    for i in range(1, len(coords_lonlat)):
        a, b = coords_lonlat[i - 1], coords_lonlat[i]
        seg_dist += haversine(a[1], a[0], b[1], b[0])
        if seg_dist < min_segment_m:
            continue

        s = coords_lonlat[seg_start_idx]
        curr_bearing = bearing(s[1], s[0], b[1], b[0])

        if prev_seg_bearing is not None:
            diff = abs(curr_bearing - prev_seg_bearing) % 360.0
            if diff > 180.0:
                diff = 360.0 - diff
            if diff >= min_turn_deg:
                waypoints.append((b[1], b[0]))

        prev_seg_bearing = curr_bearing
        seg_start_idx = i
        seg_dist = 0.0

    last = (coords_lonlat[-1][1], coords_lonlat[-1][0])
    if waypoints[-1] != last:
        waypoints.append(last)

    if len(waypoints) > max_waypoints:
        step = (len(waypoints) - 1) / (max_waypoints - 1)
        sampled = [waypoints[round(i * step)] for i in range(max_waypoints - 1)]
        waypoints = [*sampled, last]

    return waypoints


def _waypoint_bearings_from_track(
    waypoints: list[tuple[float, float]],
    cleaned_track: list[list[float]],
    lookahead_m: float = 40.0,
) -> list[float | None]:
    """Compute the direction of travel (0-360°) at each waypoint.

    Finds the nearest GPS point in *cleaned_track* to each waypoint, then
    looks ahead *lookahead_m* metres to get a stable bearing.  Returns None
    when the track is too short to compute a bearing.
    """
    def _bearing_at(idx: int) -> float | None:
        cumulative = 0.0
        for j in range(idx + 1, len(cleaned_track)):
            prev, curr = cleaned_track[j - 1], cleaned_track[j]
            cumulative += haversine(prev[1], prev[0], curr[1], curr[0])
            if cumulative >= lookahead_m:
                return bearing(
                    cleaned_track[idx][1], cleaned_track[idx][0],
                    curr[1], curr[0],
                )
        # Not enough track ahead — try looking back instead
        cumulative = 0.0
        for j in range(idx - 1, -1, -1):
            prev, curr = cleaned_track[j], cleaned_track[j + 1]
            cumulative += haversine(prev[1], prev[0], curr[1], curr[0])
            if cumulative >= lookahead_m:
                return bearing(prev[1], prev[0], cleaned_track[idx][1], cleaned_track[idx][0])
        return None

    result: list[float | None] = []
    for wp in waypoints:
        best_i = min(
            range(len(cleaned_track)),
            key=lambda i: haversine(wp[0], wp[1], cleaned_track[i][1], cleaned_track[i][0]),
        )
        result.append(_bearing_at(best_i))
    return result


def _snap_waypoints_to_road(
    waypoints: list[tuple[float, float]],
    bearings: list[float | None] | None = None,
    bearing_range: int = 30,
    http_get_fn: HttpGet = http_get,
) -> list[tuple[float, float]]:
    """Snap each (lat, lon) waypoint to the nearest driveable road.

    Uses OSRM /nearest.  When *bearings* is provided (same length as
    *waypoints*), each non-None bearing is passed as a heading constraint so
    OSRM snaps to the road segment that matches the direction of travel —
    preventing wrong-lane snapping on one-way streets.  Falls back to the
    original coordinate if the request fails.  Calls are issued in parallel.
    """
    def _snap_one(args: tuple[int, tuple[float, float]]) -> tuple[float, float]:
        i, wp = args
        lat, lon = wp
        url = f"https://router.project-osrm.org/nearest/v1/driving/{lon},{lat}"
        b_val = bearings[i] if bearings and i < len(bearings) else None
        if b_val is not None:
            b = round(b_val) % 360
            url += f"?bearings={b},{bearing_range}"
        try:
            data = http_get_fn(url)
            loc = data["waypoints"][0]["location"]  # [lon, lat]
            return (loc[1], loc[0])
        except (KeyError, IndexError, TypeError, AttributeError):
            return wp

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(_snap_one, enumerate(waypoints)))


def _subsample_cleaned_track(
    coords_lonlat: list[list[float]],
    interval_m: float = 50.0,
) -> list[tuple[float, float]]:
    """Sample a cleaned GPS track at ~interval_m distance intervals.

    Returns (lat, lon) tuples.  Always includes the first and last point.
    """
    if not coords_lonlat:
        return []
    result: list[tuple[float, float]] = [(coords_lonlat[0][1], coords_lonlat[0][0])]
    dist_acc = 0.0
    for i in range(1, len(coords_lonlat)):
        a, b = coords_lonlat[i - 1], coords_lonlat[i]
        dist_acc += haversine(a[1], a[0], b[1], b[0])
        if dist_acc >= interval_m:
            result.append((b[1], b[0]))
            dist_acc = 0.0
    last = (coords_lonlat[-1][1], coords_lonlat[-1][0])
    if result[-1] != last:
        result.append(last)
    return result


def _prune_bad_waypoints(
    waypoints: list[tuple[float, float]],
    protected: set[int] | None = None,
    save_threshold_m: float = 200.0,
    max_segment_m: float = float("inf"),
    http_get_fn: HttpGet = http_get,
) -> list[tuple[float, float]]:
    """Remove inner waypoints that force excessive routing detours.

    For each inner waypoint WP[i], compares the combined routing distance
    WP[i-1]→WP[i] + WP[i]→WP[i+1]  against the direct segment
    WP[i-1]→WP[i+1].  If skipping WP[i] saves more than *save_threshold_m*
    metres AND the resulting segment would stay under *max_segment_m*, the
    waypoint is removed.

    The *max_segment_m* guard prevents the pruner from creating long,
    guidance-free jumps that let the router pick arbitrary wrong streets.

    *protected* is a set of waypoint indices that must never be removed —
    use this to preserve stop-gap boundary points (e.g. the gas-station
    turnaround) that are genuine visited locations, not routing artefacts.

    Iterates until no further savings above the threshold are found.
    """
    wps = list(waypoints)
    prot: list[bool] = [False] * len(wps)
    prot[0] = True
    prot[-1] = True
    if protected:
        for idx in protected:
            if 0 <= idx < len(prot):
                prot[idx] = True

    changed = True
    while changed and len(wps) > 2:
        changed = False
        best_i, best_save = -1, 0.0
        for i in range(1, len(wps) - 1):
            if prot[i]:
                continue
            r_prev = compute_route([wps[i - 1], wps[i]], http_get_fn=http_get_fn)
            r_next = compute_route([wps[i], wps[i + 1]], http_get_fn=http_get_fn)
            r_skip = compute_route([wps[i - 1], wps[i + 1]], http_get_fn=http_get_fn)
            if r_prev is None or r_next is None or r_skip is None:
                continue
            if r_skip[2] > max_segment_m:
                continue  # removing this WP would create a too-long guidance gap
            save = (r_prev[2] + r_next[2]) - r_skip[2]
            if save > best_save:
                best_save = save
                best_i = i
        if best_i >= 0 and best_save > save_threshold_m:
            write_diagnostic_log(
                __name__, logging.INFO,
                "_prune_bad_waypoints removed WP%d (%.1f,%.1f) saved=%.0fm",
                best_i, wps[best_i][0], wps[best_i][1], best_save,
            )
            wps.pop(best_i)
            prot.pop(best_i)
            changed = True
    return wps


def _sample_waypoints(
    coords_lonlat: list[list[float]],
    min_segment_m: float = 50.0,
    max_waypoints: int = 60,
    sample_interval_m: float = 100.0,
) -> list[tuple[float, float]]:
    """Combine turn-apex waypoints with evenly spaced GPS samples.

    Extracts turn waypoints (bearing change ≥ 30°) and additionally adds one
    GPS point per *sample_interval_m* of accumulated distance.  This prevents
    long straight stretches or gradual curves from having no routing anchors,
    which would let the router choose wrong streets freely.

    The combined list is deduplicated (points within 10 m of an already-kept
    point are skipped) and capped at *max_waypoints*.
    """
    if len(coords_lonlat) < 2:
        return [(c[1], c[0]) for c in coords_lonlat]

    turn_wps = set(extract_turn_waypoints(
        coords_lonlat, min_segment_m=min_segment_m, max_waypoints=max_waypoints
    ))

    # Walk the trace; emit a sample every sample_interval_m AND all turn points.
    result: list[tuple[float, float]] = []
    acc = 0.0
    next_sample = 0.0

    def _add(lat: float, lon: float) -> None:
        if not result or haversine(result[-1][0], result[-1][1], lat, lon) > 10.0:
            result.append((lat, lon))

    for i, c in enumerate(coords_lonlat):
        lat, lon = c[1], c[0]
        is_turn = (lat, lon) in turn_wps
        if i > 0:
            prev = coords_lonlat[i - 1]
            acc += haversine(prev[1], prev[0], lat, lon)
        if is_turn or acc >= next_sample:
            _add(lat, lon)
            if acc >= next_sample:
                next_sample = acc + sample_interval_m

    last = (coords_lonlat[-1][1], coords_lonlat[-1][0])
    _add(*last)

    if len(result) > max_waypoints:
        step = (len(result) - 1) / (max_waypoints - 1)
        sampled = [result[round(i * step)] for i in range(max_waypoints - 1)]
        result = [*sampled, last]

    return result


def _deduplicate_close_waypoints(
    waypoints: list[tuple[float, float]],
    min_dist_m: float = 20.0,
) -> list[tuple[float, float]]:
    """Drop consecutive inner waypoints within min_dist_m of the previous one.

    Always preserves the first and last waypoint.  Removes duplicated
    stop-boundary pairs (e.g. last-before-stop and first-after-stop that
    are both on the same gas-station forecourt 13 m apart).
    """
    if len(waypoints) <= 2:
        return waypoints
    result = [waypoints[0]]
    for wp in waypoints[1:-1]:
        if haversine(result[-1][0], result[-1][1], wp[0], wp[1]) >= min_dist_m:
            result.append(wp)
    result.append(waypoints[-1])
    return result


def _is_dead_end_uturn(steps: list[dict], uturn_idx: int, short_m: float = 100.0) -> bool:
    """Detect the ``50m into a dead-end and back`` artifact pattern.

    A real U-turn (e.g. gas-station exit) has at least one long adjacent leg —
    the car drove a meaningful distance before reversing.  A GPS-noise U-turn
    looks like ``short forward → uturn → short reverse``: the OSRM router was
    forced to dart into a side street to honour a misplaced waypoint, then
    immediately back out again.
    """
    if uturn_idx <= 0 or uturn_idx >= len(steps) - 1:
        return False
    prev_d = float(steps[uturn_idx - 1].get("distance", 0) or 0)
    next_d = float(steps[uturn_idx + 1].get("distance", 0) or 0)
    return prev_d < short_m and next_d < short_m


def _uturn_physically_impossible(
    raw_coords: list[list[float]],
    raw_timestamps: list[float],
    step: dict,
    speed_threshold_kmh: float = 20.0,
    position_threshold_m: float = 150.0,
    dense_radius_m: float = 25.0,
    dense_count_threshold: int = 5,
    speed_window: int = 7,
) -> bool:
    """Decide whether an OSRM U-turn step could not correspond to a real maneuver.

    Three checks, applied in priority:

    1. **Dense GPS coverage** (≥ *dense_count_threshold* points within
       *dense_radius_m*): the driver demonstrably drove right here — even if
       the windowed speed seems high, the U-turn is real.  This is the
       "GPS-data-insists-it-happened" rule.
    2. **Phantom position** (nearest GPS > *position_threshold_m*): no GPS
       evidence at all → artifact.
    3. **No slowdown** (minimum windowed speed > *speed_threshold_kmh*): a
       real U-turn requires braking; if the GPS never dropped below the
       threshold around the U-turn position, the car kept driving and the
       OSRM U-turn is its routing interpretation.
    """
    if not raw_coords or not raw_timestamps or len(raw_coords) != len(raw_timestamps):
        return False
    target_lat, target_lon = step.get("lat", 0.0), step.get("lon", 0.0)
    dense_count = 0
    best_i, best_d = 0, float("inf")
    for i, c in enumerate(raw_coords):
        d = haversine(target_lat, target_lon, c[1], c[0])
        if d < dense_radius_m:
            dense_count += 1
        if d < best_d:
            best_d = d
            best_i = i
    if dense_count >= dense_count_threshold:
        return False  # the car physically drove through this location
    if best_d > position_threshold_m:
        return True  # phantom location — no real GPS evidence here
    lo = max(1, best_i - speed_window)
    hi = min(len(raw_coords) - 1, best_i + speed_window)
    if hi <= lo:
        return False
    min_speed_kmh = float("inf")
    for j in range(lo, hi + 1):
        dt = raw_timestamps[j] - raw_timestamps[j - 1]
        if dt <= 0:
            continue
        seg = haversine(
            raw_coords[j - 1][1], raw_coords[j - 1][0],
            raw_coords[j][1], raw_coords[j][0],
        )
        kmh = (seg / dt) * 3.6
        min_speed_kmh = min(min_speed_kmh, kmh)
    if min_speed_kmh == float("inf"):
        return False
    return min_speed_kmh > speed_threshold_kmh


def _remove_uturn_waypoints(
    waypoints: list[tuple[float, float]],
    protected_coords: set[tuple[float, float]] | None = None,
    raw_coords: list[list[float]] | None = None,
    raw_timestamps: list[float] | None = None,
    max_iters: int = 5,
    protection_radius_m: float = 200.0,
    max_saving_pct: float = 0.15,
    http_get_fn: HttpGet = http_get,
) -> list[tuple[float, float]]:
    """Iteratively remove waypoints that force spurious U-turns in the routed result.

    After each routing call, finds waypoints nearest to U-turn steps and considers
    removing them.  Guards prevent over-removal:

    1. *protected_coords* (stop-gap positions): waypoints within *protection_radius_m*
       of a motor-off stop are preserved — they mark genuine direction reversals.
    2. *max_saving_pct*: if removing the bad set would shorten the route by more than
       this fraction of the total distance, the removal is skipped — a large saving
       means the waypoints are on a real detour (e.g. gas-station approach), not GPS
       noise on a one-way street.

    Two classes of artifact bypass the % guard and are removed unconditionally:

    * Dead-end pattern (short→uturn→short): unmistakable noise artifact.
    * Physically impossible U-turn (requires *raw_coords* + *raw_timestamps*):
      GPS shows the car going too fast through the U-turn position, or the
      position is far from any real GPS point.
    """
    def _find_bad_wp_index(wps: list[tuple[float, float]], step: dict) -> int | None:
        best_i, best_d = None, float("inf")
        for i, wp in enumerate(wps[1:-1], 1):  # never touch first/last
            d = haversine(step["lat"], step["lon"], wp[0], wp[1])
            if d < best_d:
                best_d = d
                best_i = i
        if best_i is None or best_d >= protection_radius_m:
            return None
        if protected_coords:
            wp = wps[best_i]
            if any(
                haversine(pc[0], pc[1], wp[0], wp[1]) < protection_radius_m
                for pc in protected_coords
            ):
                return None
        return best_i

    wps = list(waypoints)
    for _ in range(max_iters):
        if len(wps) <= 2:
            break
        r = compute_route(wps, http_get_fn=http_get_fn)
        if r is None:
            break
        _rc, _dur, current_dist, steps = r
        uturn_indices = [
            i for i, s in enumerate(steps)
            if "uturn" in (s.get("modifier", "") + s.get("type", "")).lower()
        ]
        if not uturn_indices:
            break

        def _total_uturn_dist(route_steps: list[dict]) -> float:
            return sum(
                float(s.get("distance", 0) or 0)
                for s in route_steps
                if "uturn" in (s.get("modifier", "") + s.get("type", "")).lower()
            )

        current_uturn_dist = _total_uturn_dist(steps)

        # Classify each U-turn locally — dead-end / physically-impossible are
        # GPS-noise artifacts; everything else needs the % guard.  Keep them in
        # a stable order so removal attempts are deterministic.
        artifact_candidates: list[int] = []
        normal_candidates: list[int] = []
        seen: set[int] = set()
        has_phantom_uturn = False
        for ui in uturn_indices:
            is_artifact = _is_dead_end_uturn(steps, ui) or _uturn_physically_impossible(
                raw_coords or [], raw_timestamps or [], steps[ui]
            )
            bi = _find_bad_wp_index(wps, steps[ui])
            if bi is None:
                # No waypoint within protection_radius_m of this U-turn.  When the
                # U-turn is a phantom (far from any GPS) the offending waypoint can
                # be on the *other side* of the U-turn — we fall back to trying
                # every non-protected waypoint individually further below.
                if is_artifact:
                    has_phantom_uturn = True
                continue
            if bi in seen:
                continue
            seen.add(bi)
            if is_artifact:
                artifact_candidates.append(bi)
            else:
                normal_candidates.append(bi)

        # Phantom U-turn fallback: add every non-protected interior waypoint as
        # an artifact candidate.  do-no-harm validation below will reject any
        # removal that does not actually shorten the total U-turn distance.
        if has_phantom_uturn:
            for i in range(1, len(wps) - 1):
                if i in seen:
                    continue
                wp = wps[i]
                if protected_coords and any(
                    haversine(pc[0], pc[1], wp[0], wp[1]) < protection_radius_m
                    for pc in protected_coords
                ):
                    continue
                seen.add(i)
                artifact_candidates.append(i)

        # Try each artifact removal individually, in order.  Accept the first one
        # that does not increase the total U-turn distance — otherwise we would
        # trade a known short artifact for a longer reroute-induced U-turn
        # elsewhere (this happened on Trip 5's 21m + 17m pair when removed
        # together).  Re-iterate after each accepted removal so the next pass
        # sees the new routing reality.
        accepted = False
        for bi in artifact_candidates:
            candidate = [wps[i] for i in range(len(wps)) if i != bi]
            r_candidate = compute_route(candidate, http_get_fn=http_get_fn)
            if r_candidate is None:
                continue
            if _total_uturn_dist(r_candidate[3]) <= current_uturn_dist:
                wps = candidate
                accepted = True
                break
        if accepted:
            continue

        # Pass 2: ordinary U-turn waypoints — only remove if the % saving guard
        # allows it (otherwise it is probably a genuine detour like a gas-station
        # approach).  Also try individually to avoid coupled regressions.
        for bi in normal_candidates:
            candidate = [wps[i] for i in range(len(wps)) if i != bi]
            r_candidate = compute_route(candidate, http_get_fn=http_get_fn)
            if r_candidate is None:
                continue
            new_dist = r_candidate[2]
            if current_dist > 0 and (current_dist - new_dist) / current_dist > max_saving_pct:
                continue
            if _total_uturn_dist(r_candidate[3]) <= current_uturn_dist:
                wps = candidate
                accepted = True
                break
        if not accepted:
            break
    return wps


def _expand_turn_waypoints(
    turn_waypoints: list[tuple[float, float]],
    seg_coords: list[list[float]],
    window: int = 2,
) -> list[tuple[float, float]]:
    """Widen each turn apex to a ±window band of neighbouring GPS points.

    A single GPS apex at a turn may land on the wrong side of a one-way
    street, forcing a large routing detour.  Adding the neighbouring GPS
    points gives the router directional context so it can pick the correct
    approach road.  Points within 5 m of each other are deduplicated.
    """
    if window <= 0 or not turn_waypoints or not seg_coords:
        return turn_waypoints

    n = len(seg_coords)
    added: set[int] = set()
    result: list[tuple[int, tuple[float, float]]] = []

    for wp_lat, wp_lon in turn_waypoints:
        best_i, best_d = 0, float("inf")
        for j, c in enumerate(seg_coords):
            d = haversine(wp_lat, wp_lon, c[1], c[0])
            if d < best_d:
                best_d = d
                best_i = j
        for j in range(max(0, best_i - window), min(n, best_i + window + 1)):
            if j not in added:
                added.add(j)
                c = seg_coords[j]
                result.append((j, (c[1], c[0])))

    result.sort(key=lambda x: x[0])
    out: list[tuple[float, float]] = []
    for _, wp in result:
        if not out or haversine(out[-1][0], out[-1][1], wp[0], wp[1]) > 5.0:
            out.append(wp)
    return out


def route_via_gps_waypoints(
    coords_lonlat: list[list[float]],
    timestamps: list[float] | None = None,
    http_get_fn: HttpGet = http_get,
) -> tuple[list[list[float]], float, float, list[dict]] | None:
    """Compute a road-snapped route from a noisy GPS trace.

    Tries Valhalla map-matching (trace_route) first — it sends the full
    GPS shape and returns the exact roads driven.  Falls back to the
    waypoint-extraction approach (turn detection + routing) if
    map-matching is unavailable or fails.
    """
    if len(coords_lonlat) < 2:
        return None

    # Primary: map-matching via Valhalla trace_route, one leg at a time.
    # The full GPS trace often fails map-matching because stationary GPS
    # jitter (standing still = random drift in place) looks physically
    # impossible for a car.  We therefore:
    #   1. Clean the trace (spikes + cluster-collapse removes jitter).
    #   2. Split at stop-gaps so each driving leg is matched independently.
    #   3. Merge the per-leg results into a single route.
    cleaned_legs, stop_indices_legs = _clean_gps_trace(
        coords_lonlat, timestamps=timestamps
    )
    leg_boundaries = [0, *stop_indices_legs, len(cleaned_legs)]
    leg_results: list[tuple[list[list[float]], float, float, list[dict]]] = []
    match_ok = True
    for k in range(len(leg_boundaries) - 1):
        leg = cleaned_legs[leg_boundaries[k]:leg_boundaries[k + 1]]
        if len(leg) < 2:
            match_ok = False
            break
        lr = valhalla_trace_route(leg)
        if lr is None:
            match_ok = False
            break
        leg_results.append(lr)

    if match_ok and leg_results:
        merged_coords: list[list[float]] = []
        merged_dur = 0.0
        merged_dist = 0.0
        merged_steps: list[dict] = []
        for coords_r, dur_r, dist_r, steps_r in leg_results:
            leg_coords = coords_r[1:] if merged_coords and coords_r and merged_coords[-1] == coords_r[0] else coords_r
            merged_coords.extend(leg_coords)
            merged_dur += dur_r
            merged_dist += dist_r
            merged_steps.extend(steps_r)

        # Validate: Valhalla map-matching can silently "shortcut" loops or
        # detours when GPS points are sparse, returning a plausible-looking
        # route that bypasses streets the driver actually used.  Reject the
        # match if the GPS trace diverges from the route, or if the route
        # passes through territory with no GPS support.
        deviations = _gps_route_deviations(coords_lonlat, merged_coords)
        orphans = _route_orphan_corrections(merged_coords, coords_lonlat)
        if deviations or orphans:
            write_diagnostic_log(
                __name__, logging.INFO,
                "route_via_gps_waypoints map_match_diverged "
                "deviations=%d orphans=%d dist_km=%.1f fallback_to_waypoints",
                len(deviations), len(orphans), merged_dist / 1000.0,
            )
        else:
            write_diagnostic_log(
                __name__, logging.INFO,
                "route_via_gps_waypoints map_match_ok legs=%d dist_km=%.1f",
                len(leg_results), merged_dist / 1000.0,
            )
            return merged_coords, merged_dur, merged_dist, merged_steps
    else:
        write_diagnostic_log(
            __name__, logging.INFO,
            "route_via_gps_waypoints map_match_failed fallback_to_waypoints pts=%d",
            len(coords_lonlat),
        )

    # Fallback: waypoint extraction + routing + U-turn correction.
    cleaned, stop_indices = _clean_gps_trace(coords_lonlat, timestamps=timestamps)

    # Collect GPS positions of motor-off stop gaps (e.g. tank stops).  These are
    # genuine direction reversals and must not be removed by the U-turn loop.
    stop_gap_coords: set[tuple[float, float]] = set()
    for si in stop_indices:
        if si < len(cleaned):
            c = cleaned[si]
            stop_gap_coords.add((c[1], c[0]))  # (lat, lon)

    wps = extract_turn_waypoints(cleaned, min_segment_m=30.0, max_waypoints=60)
    all_waypoints: list[tuple[float, float]] = list(wps)
    protected_wp_indices: set[int] = {0, len(all_waypoints) - 1}
    # Also protect pruner indices near stop gaps so the tank-stop waypoints
    # survive the distance-based pruner as well.
    for i, wp in enumerate(all_waypoints):
        if any(haversine(sc[0], sc[1], wp[0], wp[1]) < 200.0 for sc in stop_gap_coords):
            protected_wp_indices.add(i)

    write_diagnostic_log(
        __name__, logging.INFO,
        "route_via_gps_waypoints pts=%d cleaned=%d waypoints=%d protected=%d",
        len(coords_lonlat), len(cleaned),
        len(all_waypoints), len(protected_wp_indices),
    )
    all_waypoints = _prune_bad_waypoints(
        all_waypoints, protected=protected_wp_indices,
        save_threshold_m=100.0, http_get_fn=http_get_fn
    )
    # Compute direction-of-travel bearings after pruning so we snap each
    # surviving waypoint to the road segment matching the actual travel direction.
    # This prevents wrong-lane snapping on one-way streets.
    # Start and end are kept unconstrained — they must snap to the nearest road
    # regardless of heading (parking spot might face any direction).
    wp_bearings = _waypoint_bearings_from_track(all_waypoints, cleaned)
    if wp_bearings:
        wp_bearings[0] = None
        wp_bearings[-1] = None
    all_waypoints = _snap_waypoints_to_road(
        all_waypoints, bearings=wp_bearings, http_get_fn=http_get_fn
    )
    # 30m (not 50m): on 4 km traces with mostly straight segments the 50m
    # threshold can collapse 16 surviving waypoints down to 10, leaving
    # ~1 km gaps where compute_route invents its own preferred road.
    # Trip 25 specifically went from -4.9% to +0.7% with 30m; the other
    # 13 reference trips are unaffected (their waypoint spacing was
    # already above 30m before this loop ran).
    all_waypoints = _deduplicate_close_waypoints(all_waypoints, min_dist_m=30.0)
    all_waypoints = _remove_uturn_waypoints(
        all_waypoints,
        protected_coords=stop_gap_coords,
        raw_coords=coords_lonlat,
        raw_timestamps=timestamps,
        http_get_fn=http_get_fn,
    )
    return compute_route(all_waypoints, http_get_fn=http_get_fn)


def _gps_route_deviations(
    gps_coords: list[list[float]],
    route_coords: list[list[float]],
    threshold_m: float = 50.0,
    min_streak: int = 3,
) -> list[tuple[int, tuple[float, float]]]:
    """Find GPS points that deviate significantly from the computed route.

    Returns a list of ``(gps_index, (lat, lon))`` — one entry per consecutive
    run of *min_streak* or more GPS points that are all farther than
    *threshold_m* from the route.  The returned point is the one with the
    maximum deviation within each run.
    """
    if len(route_coords) < 2 or not gps_coords:
        return []

    cos_lat = math.cos(math.radians(
        sum(c[1] for c in gps_coords) / len(gps_coords)
    ))

    def _dist_to_route(lon: float, lat: float) -> float:
        best_d2 = float("inf")
        px, py = lon * cos_lat, lat
        for i in range(len(route_coords) - 1):
            ax = route_coords[i][0] * cos_lat
            ay = route_coords[i][1]
            bx = route_coords[i + 1][0] * cos_lat
            by = route_coords[i + 1][1]
            abx, aby = bx - ax, by - ay
            ab2 = abx * abx + aby * aby
            if ab2 > 0.0:
                t = max(0.0, min(1.0, ((px - ax) * abx + (py - ay) * aby) / ab2))
            else:
                t = 0.0
            dx = px - (ax + t * abx)
            dy = py - (ay + t * aby)
            d2 = dx * dx + dy * dy
            best_d2 = min(best_d2, d2)
        return math.sqrt(best_d2) * 111_000.0

    corrections: list[tuple[int, tuple[float, float]]] = []
    streak_start: int | None = None
    worst_idx: int | None = None
    worst_dist = 0.0

    def _flush(end: int) -> None:
        nonlocal streak_start, worst_idx, worst_dist
        if streak_start is not None and worst_idx is not None and end - streak_start >= min_streak:
            c = gps_coords[worst_idx]
            corrections.append((worst_idx, (c[1], c[0])))
        streak_start = None
        worst_idx = None
        worst_dist = 0.0

    for i, coord in enumerate(gps_coords):
        d = _dist_to_route(coord[0], coord[1])
        if d > threshold_m:
            if streak_start is None:
                streak_start = i
            if d > worst_dist:
                worst_idx = i
                worst_dist = d
        else:
            _flush(i)

    _flush(len(gps_coords))
    return corrections


def _route_orphan_corrections(
    route_coords: list[list[float]],
    gps_coords: list[list[float]],
    sample_m: float = 30.0,
    threshold_m: float = 40.0,
    min_orphan_m: float = 80.0,
) -> list[tuple[int, tuple[float, float]]]:
    """Find GPS correction points for route segments with no GPS support.

    Walks the route in *sample_m* steps.  Where a consecutive run of
    sample points all have no GPS point within *threshold_m*, the route
    passes through territory the car never visited.  For each such orphan
    segment longer than *min_orphan_m*, the nearest GPS point to the
    segment midpoint is returned as a correction waypoint.

    Return format matches _gps_route_deviations: list of
    ``(gps_index, (lat, lon))``.
    """
    if len(route_coords) < 2 or not gps_coords:
        return []

    cos_lat = math.cos(math.radians(
        sum(c[1] for c in route_coords) / len(route_coords)
    ))

    # Sample the route polyline at sample_m intervals
    samples: list[tuple[float, float, float]] = []  # (cum_m, lat, lon)
    cum = 0.0
    remainder = 0.0
    for i in range(len(route_coords) - 1):
        a, b = route_coords[i], route_coords[i + 1]
        seg_m = haversine(a[1], a[0], b[1], b[0])
        t = remainder / seg_m if seg_m > 0 else 0.0
        while t <= 1.0:
            lat = a[1] + t * (b[1] - a[1])
            lon = a[0] + t * (b[0] - a[0])
            samples.append((cum + t * seg_m, lat, lon))
            t += sample_m / seg_m if seg_m > 0 else 1.0
        remainder = (t - 1.0) * seg_m if seg_m > 0 else 0.0
        cum += seg_m

    # For each sample, find distance to nearest GPS point
    corrections: list[tuple[int, tuple[float, float]]] = []
    orphan_start_cum: float | None = None
    orphan_samples: list[tuple[float, float, float]] = []

    def _flush_orphan() -> None:
        if orphan_start_cum is None or not orphan_samples:
            return
        orphan_end_cum = orphan_samples[-1][0]
        if orphan_end_cum - orphan_start_cum < min_orphan_m:
            return
        mid_idx = len(orphan_samples) // 2
        _, mid_lat, mid_lon = orphan_samples[mid_idx]
        best_gi, best_d = 0, float("inf")
        for gi, g in enumerate(gps_coords):
            d = haversine(mid_lat, mid_lon, g[1], g[0])
            if d < best_d:
                best_d = d
                best_gi = gi
        gc = gps_coords[best_gi]
        corrections.append((best_gi, (gc[1], gc[0])))

    for cum_m, lat, lon in samples:
        px, py = lon * cos_lat, lat
        best_d2 = float("inf")
        for g in gps_coords:
            gx, gy = g[0] * cos_lat, g[1]
            d2 = (px - gx) ** 2 + (py - gy) ** 2
            best_d2 = min(best_d2, d2)
        dist_m = math.sqrt(best_d2) * 111_000.0
        if dist_m > threshold_m:
            if orphan_start_cum is None:
                orphan_start_cum = cum_m
            orphan_samples.append((cum_m, lat, lon))
        else:
            _flush_orphan()
            orphan_start_cum = None
            orphan_samples = []

    _flush_orphan()
    return corrections


def _insert_correction_waypoints(
    waypoints: list[tuple[float, float]],
    corrections: list[tuple[int, tuple[float, float]]],
    gps_coords: list[list[float]],
) -> list[tuple[float, float]]:
    """Merge GPS correction points into an ordered waypoint list.

    Each existing waypoint is mapped to its nearest GPS index; the corrections
    (also GPS indices) are then interleaved so the combined list stays in
    route order.  Points closer than 5 m to an already-present waypoint are
    dropped to avoid duplicates.
    """
    if not corrections:
        return waypoints

    # Map each waypoint to the nearest GPS point index
    wp_gps: list[tuple[int, tuple[float, float]]] = []
    for wp_lat, wp_lon in waypoints:
        best_i, best_d = 0, float("inf")
        for i, c in enumerate(gps_coords):
            d = haversine(wp_lat, wp_lon, c[1], c[0])
            if d < best_d:
                best_d = d
                best_i = i
        wp_gps.append((best_i, (wp_lat, wp_lon)))

    combined = wp_gps + corrections
    combined.sort(key=lambda x: x[0])

    result: list[tuple[float, float]] = []
    for _, wp in combined:
        if not result or haversine(result[-1][0], result[-1][1], wp[0], wp[1]) > 5.0:
            result.append(wp)

    return result
