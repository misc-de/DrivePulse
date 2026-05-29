"""Unit tests for the pure route-progress maths extracted from MapTourMixin."""

from drivepulse_app.map._tour_progress import (
    build_maneuver_positions,
    build_speed_zones,
    compute_route_progress_tables,
    nearest_route_progress,
    off_route_decision,
    tts_distance_text,
    waypoint_is_passed,
)

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


def test_waypoint_not_passed_when_behind_but_far():
    # Behind the heading but well beyond the bypass radius -> keep it in the route.
    passed, dist, _ = waypoint_is_passed(_GPS_LAT, _GPS_LON, 0.0, *_WP_FAR_SOUTH, 200.0)
    assert passed is False
    assert dist > 200.0


def test_waypoint_bearing_wraparound_near_north():
    # Heading 350° toward a waypoint due north (~0°): the true angular
    # difference is 10°, not 350°. Guards the `diff > 180 -> 360 - diff` wrap.
    passed, _, _ = waypoint_is_passed(_GPS_LAT, _GPS_LON, 350.0, *_WP_NORTH, 200.0)
    assert passed is False


def test_waypoint_bearing_threshold_is_configurable():
    # A waypoint due south (bearing diff 180°) is passed at the default 110°
    # threshold but not when the threshold is raised above 180°.
    assert waypoint_is_passed(_GPS_LAT, _GPS_LON, 0.0, *_WP_SOUTH, 200.0)[0] is True
    assert waypoint_is_passed(
        _GPS_LAT, _GPS_LON, 0.0, *_WP_SOUTH, 200.0, bearing_threshold_deg=181.0
    )[0] is False
