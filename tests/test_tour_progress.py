"""Unit tests for the pure route-progress maths extracted from MapTourMixin."""

from drivepulse_app.map._tour_progress import (
    annotate_uturns,
    build_maneuver_positions,
    build_speed_zones,
    compute_route_progress_tables,
    maneuver_passed,
    nearest_route_progress,
    next_actionable_step_idx,
    off_route_decision,
    reconcile_passed_waypoints,
    tts_distance_text,
    waypoint_is_passed,
)

_NON_ACTIONABLE = {"new name", "notification", "continue"}

# Shared reroute thresholds mirroring MapTourMixin's class constants.
_REROUTE_KW = dict(off_route_m=30.0, min_speed_kmh=10.0, confirm_s=4.0, cooldown_s=30.0)


def _decide(off_dist_m, speed_kmh, off_route_since, now, last_reroute_time=-999.0):
    return off_route_decision(
        off_dist_m, speed_kmh, off_route_since, now, last_reroute_time, **_REROUTE_KW
    )

# --- compute_route_progress_tables ----------------------------------------

def test_progress_tables_empty_inputs():
    route, steps = compute_route_progress_tables([], [])
    assert route == []
    assert steps == []


def test_route_cum_m_is_monotonic_and_starts_at_zero():
    # A short east-west then north leg; coords are [lon, lat].
    coords = [[8.0, 50.0], [8.001, 50.0], [8.001, 50.001]]
    route, _ = compute_route_progress_tables(coords, [])
    assert route[0] == 0.0
    assert len(route) == len(coords)
    assert route[1] < route[2]
    # Each entry equals the previous plus a positive segment length.
    assert all(route[i] >= route[i - 1] for i in range(1, len(route)))


def test_step_cum_m_offsets_each_maneuver_by_preceding_distances():
    steps = [{"distance": 100.0}, {"distance": 250.0}, {"distance": 0.0}]
    _, step_cum = compute_route_progress_tables([], steps)
    # Maneuver k sits at the cumulative distance of steps 0..k-1.
    assert step_cum == [0.0, 100.0, 350.0]


def test_step_cum_m_treats_missing_distance_as_zero():
    steps = [{}, {"distance": None}, {"distance": 40.0}]
    _, step_cum = compute_route_progress_tables([], steps)
    assert step_cum == [0.0, 0.0, 0.0]


# --- build_speed_zones ------------------------------------------------------

def test_build_speed_zones_empty_without_tables():
    assert build_speed_zones([], []) == []
    assert build_speed_zones([{"speed_limit": 50}], []) == []


def test_build_speed_zones_emits_breakpoint_only_on_change():
    steps = [
        {"speed_limit": 50.0},
        {"speed_limit": 50.0},
        {"speed_limit": 100.0},
        {"speed_limit": 100.0},
    ]
    step_cum = [0.0, 200.0, 500.0, 900.0]
    zones = build_speed_zones(steps, step_cum)
    assert zones == [(0.0, 50.0), (500.0, 100.0)]


def test_build_speed_zones_falls_back_to_ref_heuristic():
    # No explicit speed_limit -> ref-tag heuristic (autobahn A* -> 120).
    steps = [{"ref": "A3"}]
    zones = build_speed_zones(steps, [0.0])
    assert zones[0][0] == 0.0
    assert zones[0][1] == 120.0


# --- build_maneuver_positions ----------------------------------------------

def test_build_maneuver_positions_skips_non_actionable_and_endpoints():
    steps = [
        {"type": "depart"},
        {"type": "turn"},
        {"type": "continue"},
        {"type": "turn"},
        {"type": "arrive"},
    ]
    step_cum = [0.0, 100.0, 150.0, 300.0, 450.0]
    skip = {"depart", "arrive", "continue"}
    positions = build_maneuver_positions(steps, step_cum, skip)
    assert positions == [100.0, 300.0]


def test_build_maneuver_positions_ignores_steps_beyond_cum_table():
    steps = [{"type": "turn"}, {"type": "turn"}]
    positions = build_maneuver_positions(steps, [0.0], skip_types=set())
    # Second step has no cum entry, so it is dropped rather than raising.
    assert positions == [0.0]


# --- nearest_route_progress -------------------------------------------------

def test_nearest_route_progress_empty_tables_returns_start():
    assert nearest_route_progress([], [], 8.0, 50.0, 3) == (3, 0.0)


def test_nearest_route_progress_picks_closest_vertex_from_start_idx():
    coords = [[8.0, 50.0], [8.001, 50.0], [8.002, 50.0], [8.003, 50.0]]
    route_cum = [0.0, 70.0, 140.0, 210.0]
    # GPS sits right on vertex index 2.
    idx, cum = nearest_route_progress(coords, route_cum, 8.002, 50.0, start_idx=0)
    assert idx == 2
    assert cum == 140.0


def test_nearest_route_progress_never_rewinds_before_start_idx():
    coords = [[8.0, 50.0], [8.001, 50.0], [8.002, 50.0]]
    route_cum = [0.0, 70.0, 140.0]
    # Even though vertex 0 is closest, the search starts at idx 1.
    idx, cum = nearest_route_progress(coords, route_cum, 8.0, 50.0, start_idx=1)
    assert idx == 1
    assert cum == 70.0


# --- tts_distance_text ------------------------------------------------------

def test_tts_distance_text_rounds_to_10m_below_threshold():
    assert "120" in tts_distance_text(123.0, "en")


def test_tts_distance_text_never_says_zero_metres():
    # Rounds to 0 -> clamped to a minimum of 10 m so speech isn't "in 0 m".
    assert "10" in tts_distance_text(2.0, "en")


def test_tts_distance_text_switches_to_km_above_threshold():
    text = tts_distance_text(1500.0, "en")
    assert "1.5" in text


# --- off_route_decision -----------------------------------------------------

def test_off_route_resets_timer_when_back_on_route():
    # On-route (within threshold) clears any running timer.
    assert _decide(off_dist_m=5.0, speed_kmh=50.0, off_route_since=100.0, now=120.0) == (0.0, False)


def test_off_route_ignored_below_min_speed():
    # Far off-route but nearly stationary -> treated as drift, timer stays reset.
    assert _decide(off_dist_m=200.0, speed_kmh=2.0, off_route_since=0.0, now=120.0) == (0.0, False)


def test_off_route_starts_timer_on_first_tick():
    # First off-route tick records the start time but does not reroute yet.
    assert _decide(off_dist_m=200.0, speed_kmh=50.0, off_route_since=0.0, now=120.0) == (120.0, False)


def test_off_route_still_confirming_keeps_timer():
    # 2s elapsed < 4s confirm window -> keep waiting, timer unchanged.
    assert _decide(off_dist_m=200.0, speed_kmh=50.0, off_route_since=100.0, now=102.0) == (100.0, False)


def test_off_route_fires_after_confirm_window():
    # 5s elapsed > 4s confirm and well past cooldown -> reroute.
    assert _decide(off_dist_m=200.0, speed_kmh=50.0, off_route_since=100.0, now=105.0) == (100.0, True)


def test_off_route_blocked_by_cooldown():
    # Confirm window passed, but last reroute was 10s ago (< 30s cooldown).
    new_since, should = _decide(
        off_dist_m=200.0, speed_kmh=50.0, off_route_since=100.0, now=105.0, last_reroute_time=95.0
    )
    assert (new_since, should) == (100.0, False)


# --- waypoint_is_passed -----------------------------------------------------

# Reference fix near 50°N 8°E. 0.001° of latitude is ~111 m.
_GPS_LAT, _GPS_LON = 50.0, 8.0
_WP_NORTH = (50.001, 8.0)   # ~111 m due north  -> bearing ~0°
_WP_SOUTH = (49.999, 8.0)   # ~111 m due south  -> bearing ~180°
_WP_FAR_SOUTH = (49.0, 8.0)  # ~111 km due south


def test_waypoint_not_passed_when_straight_ahead():
    # Driving north toward a waypoint due north -> not passed.
    passed, dist, _ = waypoint_is_passed(_GPS_LAT, _GPS_LON, 0.0, *_WP_NORTH, 200.0)
    assert passed is False
    assert dist < 200.0


def test_waypoint_passed_when_behind_and_close():
    # Driving north while the waypoint is due south and close -> passed.
    passed, dist, _ = waypoint_is_passed(_GPS_LAT, _GPS_LON, 0.0, *_WP_SOUTH, 200.0)
    assert passed is True
    assert dist < 200.0


def test_waypoint_passed_when_driving_straight_away_even_if_far():
    # The driver took their own way: the waypoint is now almost directly behind
    # (bearing diff ~180°), far beyond the bypass radius -> dropped so the route
    # doesn't balloon back to it. Regression for the 1.6 km "via-bypass" bug.
    passed, dist, _ = waypoint_is_passed(_GPS_LAT, _GPS_LON, 0.0, *_WP_FAR_SOUTH, 200.0)
    assert passed is True
    assert dist > 200.0


def test_waypoint_kept_when_far_and_only_moderately_off_heading():
    # Far away and behind-ish but below behind_deg (diff 130°) -> keep it, so a
    # mid-turn / parallel-street wobble doesn't skip a real upcoming via.
    passed, dist, _ = waypoint_is_passed(_GPS_LAT, _GPS_LON, 50.0, *_WP_FAR_SOUTH, 200.0)
    assert passed is False
    assert dist > 200.0


def test_waypoint_bearing_wraparound_near_north():
    # Heading 350° toward a waypoint due north (~0°): the true angular
    # difference is 10°, not 350°. Guards the `diff > 180 -> 360 - diff` wrap.
    passed, _, _ = waypoint_is_passed(_GPS_LAT, _GPS_LON, 350.0, *_WP_NORTH, 200.0)
    assert passed is False


def test_waypoint_bearing_threshold_is_configurable():
    # A close waypoint due south (bearing diff 180°) is passed at the default
    # 110° threshold but not when both thresholds are raised above 180° (which
    # disables the drove-past *and* driving-away paths).
    assert waypoint_is_passed(_GPS_LAT, _GPS_LON, 0.0, *_WP_SOUTH, 200.0)[0] is True
    assert waypoint_is_passed(
        _GPS_LAT, _GPS_LON, 0.0, *_WP_SOUTH, 200.0,
        bearing_threshold_deg=181.0, behind_deg=181.0,
    )[0] is False


def test_waypoint_driving_away_threshold_is_configurable():
    # A far waypoint directly behind (diff 180°) is dropped at the default
    # behind_deg, but kept when behind_deg is raised above 180°.
    assert waypoint_is_passed(_GPS_LAT, _GPS_LON, 0.0, *_WP_FAR_SOUTH, 200.0)[0] is True
    assert waypoint_is_passed(
        _GPS_LAT, _GPS_LON, 0.0, *_WP_FAR_SOUTH, 200.0, behind_deg=181.0
    )[0] is False


# --- next_actionable_step_idx ----------------------------------------------

def test_next_actionable_skips_leading_non_actionable_steps():
    steps = [{"type": "continue"}, {"type": "new name"}, {"type": "turn"}, {"type": "arrive"}]
    assert next_actionable_step_idx(steps, 0, _NON_ACTIONABLE) == 2


def test_next_actionable_stays_put_on_actionable_step():
    steps = [{"type": "turn"}, {"type": "continue"}]
    assert next_actionable_step_idx(steps, 0, _NON_ACTIONABLE) == 0


def test_next_actionable_never_advances_past_last_step():
    # All remaining steps are non-actionable: stop on the final index, not beyond.
    steps = [{"type": "continue"}, {"type": "continue"}, {"type": "continue"}]
    assert next_actionable_step_idx(steps, 0, _NON_ACTIONABLE) == 2


def test_next_actionable_does_not_skip_uturn_continue():
    # OSRM tags U-turns type="continue"; the skip set must not swallow them or
    # the driver gets no U-turn instruction.
    steps = [{"type": "continue", "modifier": "uturn"}, {"type": "turn"}]
    assert next_actionable_step_idx(steps, 0, _NON_ACTIONABLE) == 0


def test_next_actionable_stops_on_uturn_after_plain_continue():
    steps = [
        {"type": "continue", "modifier": "straight"},
        {"type": "continue", "modifier": "uturn"},
        {"type": "turn"},
    ]
    assert next_actionable_step_idx(steps, 0, _NON_ACTIONABLE) == 1


def test_build_maneuver_positions_keeps_uturn_despite_skip_type():
    steps = [
        {"type": "turn", "modifier": "left"},
        {"type": "continue", "modifier": "uturn"},
        {"type": "continue", "modifier": "straight"},
    ]
    step_cum = [0.0, 100.0, 200.0]
    # The plain "continue" is dropped; the U-turn survives the skip set.
    assert build_maneuver_positions(steps, step_cum, {"continue"}) == [0.0, 100.0]


# --- annotate_uturns --------------------------------------------------------


def _uturn_geometry():
    """Out-and-back geometry: an east leg, a tight reversal, a west leg ~11 m
    north. Returns ``(coords, apex_idx)`` with coords as ``[lon, lat]`` pairs and
    a clean ~180° reversal at the apex."""
    coords = [[8.0 + k * 0.00012, 50.0] for k in range(9)]
    apex = len(coords) - 1
    coords += [[8.0 + k * 0.00012, 50.0001] for k in range(8, -1, -1)]
    return coords, apex


def test_annotate_uturns_merges_split_turns_into_single_uturn():
    coords, apex = _uturn_geometry()
    # Backend split the reversal into two short same-side turns at the apex.
    steps = [
        {"lat": coords[0][1], "lon": coords[0][0], "type": "depart", "modifier": "", "name": "A", "distance": 80.0},
        {"lat": coords[apex][1], "lon": coords[apex][0], "type": "turn", "modifier": "left", "name": "", "distance": 12.0},
        {"lat": coords[apex + 1][1], "lon": coords[apex + 1][0], "type": "turn", "modifier": "left", "name": "B", "distance": 80.0},
        {"lat": coords[-1][1], "lon": coords[-1][0], "type": "arrive", "modifier": "", "name": "", "distance": 0.0},
    ]
    out = annotate_uturns(coords, steps)
    mods = [s.get("modifier") for s in out]
    assert mods.count("uturn") == 1
    # The two split lefts collapse into one U-turn.
    assert len(out) == len(steps) - 1
    uturn = next(s for s in out if s["modifier"] == "uturn")
    assert uturn["type"] == "turn"
    assert uturn["distance"] == 92.0  # 12 + 80, distance preserved
    assert sum(s["distance"] for s in out) == sum(s["distance"] for s in steps)


def test_annotate_uturns_noop_when_backend_already_labelled():
    coords, apex = _uturn_geometry()
    steps = [
        {"lat": coords[0][1], "lon": coords[0][0], "type": "depart", "modifier": "", "distance": 80.0},
        {"lat": coords[apex][1], "lon": coords[apex][0], "type": "continue", "modifier": "uturn", "name": "B", "distance": 92.0},
        {"lat": coords[-1][1], "lon": coords[-1][0], "type": "arrive", "modifier": "", "distance": 0.0},
    ]
    out = annotate_uturns(coords, steps)
    assert [s.get("modifier") for s in out] == ["", "uturn", ""]
    assert len(out) == len(steps)


def test_annotate_uturns_ignores_ordinary_sharp_turn():
    # L-shaped route: east then north — a 90° corner, no reversal, no doubling back.
    coords = [[8.0 + k * 0.00012, 50.0] for k in range(9)]
    corner = len(coords) - 1
    coords += [[coords[corner][0], 50.0 + k * 0.0001] for k in range(1, 9)]
    steps = [
        {"lat": coords[0][1], "lon": coords[0][0], "type": "depart", "modifier": "", "distance": 80.0},
        {"lat": coords[corner][1], "lon": coords[corner][0], "type": "turn", "modifier": "left", "distance": 80.0},
        {"lat": coords[-1][1], "lon": coords[-1][0], "type": "arrive", "modifier": "", "distance": 0.0},
    ]
    out = annotate_uturns(coords, steps)
    assert all(s.get("modifier") != "uturn" for s in out)
    assert len(out) == len(steps)


def test_annotate_uturns_is_idempotent():
    coords, apex = _uturn_geometry()
    steps = [
        {"lat": coords[0][1], "lon": coords[0][0], "type": "depart", "modifier": "", "name": "A", "distance": 80.0},
        {"lat": coords[apex][1], "lon": coords[apex][0], "type": "turn", "modifier": "left", "name": "", "distance": 12.0},
        {"lat": coords[apex + 1][1], "lon": coords[apex + 1][0], "type": "turn", "modifier": "left", "name": "B", "distance": 80.0},
        {"lat": coords[-1][1], "lon": coords[-1][0], "type": "arrive", "modifier": "", "name": "", "distance": 0.0},
    ]
    once = annotate_uturns(coords, steps)
    twice = annotate_uturns(coords, once)
    assert [s.get("modifier") for s in once] == [s.get("modifier") for s in twice]
    assert len(once) == len(twice)


def test_annotate_uturns_does_not_mutate_input():
    coords, apex = _uturn_geometry()
    steps = [
        {"lat": coords[apex][1], "lon": coords[apex][0], "type": "turn", "modifier": "left", "distance": 12.0},
    ]
    before = [dict(s) for s in steps]
    annotate_uturns(coords, steps)
    assert steps == before


# --- maneuver_passed --------------------------------------------------------

_MANEUVER_KW = dict(closest_m=80.0, pass_growth_m=8.0)


def test_maneuver_not_passed_while_approaching():
    # Got within 80 m (min=40) and still closing -> not passed.
    assert maneuver_passed(40.0, 30.0, progress_m=0.0, step_route_cum_m=None, **_MANEUVER_KW) is False


def test_maneuver_passed_after_closest_approach_then_receding():
    # Was within 80 m (min=20) and distance has grown well past min+8 -> passed.
    assert maneuver_passed(20.0, 50.0, progress_m=0.0, step_route_cum_m=None, **_MANEUVER_KW) is True


def test_maneuver_not_passed_if_never_got_close_enough():
    # Closest approach was 100 m (> 80 m) -> the closest-approach test never arms.
    assert maneuver_passed(100.0, 200.0, progress_m=0.0, step_route_cum_m=None, **_MANEUVER_KW) is False


def test_maneuver_passed_via_route_progress_fallback():
    # Closest-approach test not satisfied, but route progress is > step_cum + 80 m.
    assert maneuver_passed(100.0, 90.0, progress_m=500.0, step_route_cum_m=400.0, **_MANEUVER_KW) is True


def test_maneuver_route_fallback_needs_margin():
    # Progress only just past the step position (not yet + closest_m) -> not passed.
    assert maneuver_passed(100.0, 90.0, progress_m=420.0, step_route_cum_m=400.0, **_MANEUVER_KW) is False


def test_maneuver_passed_handles_none_min_dist():
    # No closest approach recorded yet and no route table -> not passed (no crash).
    assert maneuver_passed(None, 50.0, progress_m=0.0, step_route_cum_m=None, **_MANEUVER_KW) is False


# --- reconcile_passed_waypoints --------------------------------------------

# Straight west->east route at lat 50°N; each 0.002° lon step is ~143 m.
_RC_COORDS = [[8.000, 50.0], [8.002, 50.0], [8.004, 50.0], [8.006, 50.0],
              [8.008, 50.0], [8.010, 50.0]]
_RC_CUM = compute_route_progress_tables(_RC_COORDS, [])[0]
# Waypoints as (lat, lon): two vias then the final destination.
_RC_VIA1 = (50.0, 8.002)
_RC_VIA2 = (50.0, 8.006)
_RC_FINAL = (50.0, 8.010)
_RC_REMAINING = [_RC_VIA1, _RC_VIA2, _RC_FINAL]


def test_reconcile_keeps_all_when_at_start():
    # GPS at the route start -> nothing is behind us yet.
    out = reconcile_passed_waypoints(_RC_COORDS, _RC_CUM, _RC_REMAINING, 50.0, 8.000)
    assert out == _RC_REMAINING


def test_reconcile_drops_one_passed_via():
    # GPS past via1 (lon 8.004) but before via2 -> drop only via1.
    out = reconcile_passed_waypoints(_RC_COORDS, _RC_CUM, _RC_REMAINING, 50.0, 8.004)
    assert out == [_RC_VIA2, _RC_FINAL]


def test_reconcile_drops_both_vias():
    # GPS past via2 (lon 8.0075) -> both vias gone, final stays.
    out = reconcile_passed_waypoints(_RC_COORDS, _RC_CUM, _RC_REMAINING, 50.0, 8.0075)
    assert out == [_RC_FINAL]


def test_reconcile_never_drops_final_destination():
    # GPS at/after the final destination -> the final entry is still kept.
    out = reconcile_passed_waypoints(_RC_COORDS, _RC_CUM, [_RC_VIA2, _RC_FINAL], 50.0, 8.010)
    assert out == [_RC_FINAL]


def test_reconcile_bails_when_far_off_route():
    # GPS ~1.1 km north of the route -> projection untrusted, list unchanged.
    out = reconcile_passed_waypoints(_RC_COORDS, _RC_CUM, _RC_REMAINING, 50.01, 8.004)
    assert out == _RC_REMAINING


def test_reconcile_handles_missing_gps():
    assert reconcile_passed_waypoints(_RC_COORDS, _RC_CUM, _RC_REMAINING, None, None) == _RC_REMAINING


def test_reconcile_single_destination_unchanged():
    # Only the final destination remains -> nothing to reconcile.
    assert reconcile_passed_waypoints(_RC_COORDS, _RC_CUM, [_RC_FINAL], 50.0, 8.004) == [_RC_FINAL]


def test_reconcile_handles_degenerate_route():
    assert reconcile_passed_waypoints([], [], _RC_REMAINING, 50.0, 8.004) == _RC_REMAINING
