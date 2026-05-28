"""Tests for the mock tour simulator's pure-logic kernel.

``MockTourSimulator`` walks an OSRM polyline at speed-zone-aware speeds
and emits synthetic GPS payloads. The GLib timer + payload dispatch is
side-effectful, but ``_speed_at``, ``_advance``, ``_current_position``
and ``_current_heading`` are pure functions of the simulator's internal
state — exercised here via direct attribute seeding so the tests don't
have to start the GLib main loop.
"""
from __future__ import annotations


def _make_sim():
    """Return a MockTourSimulator with no payload callback wired up."""
    from drivepulse_app.mock.tour import MockTourSimulator

    return MockTourSimulator(on_payload=lambda _p: None, target_kmh=50.0)


# ── _speed_at: fallback, zones, maneuvers ─────────────────────────────────────


def test_speed_at_returns_default_when_no_zones():
    # With no speed zones at all the simulator falls back to its constructor
    # default — confirms the bisect-empty short-circuit doesn't crash.
    sim = _make_sim()
    assert sim._speed_at(123.0) == 50.0


def test_speed_at_picks_active_zone_segment():
    # Three zones: 0m..300m → 40 km/h, 300m..1000m → 70, beyond → 120.
    sim = _make_sim()
    sim._speed_zones = [(0.0, 40.0), (300.0, 70.0), (1000.0, 120.0)]
    sim._zone_starts = [0.0, 300.0, 1000.0]

    assert sim._speed_at(150.0) == 40.0   # inside first zone
    assert sim._speed_at(500.0) == 70.0   # inside second
    assert sim._speed_at(2_000.0) == 120.0  # inside third


def test_speed_at_boundary_includes_left_edge():
    # bisect_right semantics: at the exact boundary the *new* zone takes
    # over (the breakpoint *is* the start of its zone).
    sim = _make_sim()
    sim._speed_zones = [(0.0, 40.0), (300.0, 70.0)]
    sim._zone_starts = [0.0, 300.0]
    assert sim._speed_at(300.0) == 70.0
    assert sim._speed_at(299.999) == 40.0


def test_speed_at_drops_to_turn_kmh_in_approach_window():
    # Maneuver at 500 m: approach starts at 500 - 60 = 440 m, clears at 525 m.
    # Inside that window speed must drop to _TURN_KMH (20 km/h) regardless
    # of the zone speed.
    sim = _make_sim()
    sim._speed_zones = [(0.0, 70.0)]
    sim._zone_starts = [0.0]
    sim._maneuver_m = [500.0]

    assert sim._speed_at(439.0) == 70.0    # just before window
    assert sim._speed_at(440.0) == 20.0    # window start
    assert sim._speed_at(500.0) == 20.0    # exactly at turn
    assert sim._speed_at(525.0) == 20.0    # window end
    assert sim._speed_at(525.1) == 70.0    # past window


# ── _advance: polyline walking + arrival ──────────────────────────────────────


def test_advance_within_first_segment():
    # 100 m segment, advance 30 m → stay in segment, progress updated.
    sim = _make_sim()
    # ~111 m at the equator for 0.001° lat
    sim._coords = [(0.0, 0.0), (0.001, 0.0)]
    sim._seg_idx = 0
    sim._seg_progress_m = 0.0
    sim._cum_dist_m = 0.0

    arrived = sim._advance(30.0)

    assert arrived is False
    assert sim._seg_idx == 0
    assert sim._seg_progress_m == 30.0
    assert sim._cum_dist_m == 30.0


def test_advance_across_segments_consumes_remaining_distance():
    # Three short segments; advance enough to land in the third.
    sim = _make_sim()
    # Each segment ≈ 111 m
    sim._coords = [
        (0.0, 0.0),
        (0.001, 0.0),
        (0.002, 0.0),
        (0.003, 0.0),
    ]
    sim._seg_idx = 0
    sim._seg_progress_m = 0.0
    sim._cum_dist_m = 0.0

    arrived = sim._advance(150.0)

    # The first ~111 m segment is consumed; ~39 m remains in segment 1.
    assert arrived is False
    assert sim._seg_idx == 1
    # cum_dist_m must equal 150 m to within float tolerance.
    assert abs(sim._cum_dist_m - 150.0) < 1e-3


def test_advance_arrival_returns_true_and_caps_index():
    # Advance far past the end of the polyline.
    sim = _make_sim()
    sim._coords = [(0.0, 0.0), (0.001, 0.0)]
    sim._seg_idx = 0
    sim._seg_progress_m = 0.0
    sim._cum_dist_m = 0.0

    arrived = sim._advance(10_000.0)

    assert arrived is True
    assert sim._seg_idx >= len(sim._coords) - 1


# ── _current_position: linear interpolation along segment ─────────────────────


def test_current_position_at_segment_start_returns_segment_origin():
    sim = _make_sim()
    sim._coords = [(50.0, 7.0), (50.001, 7.001)]
    sim._seg_idx = 0
    sim._seg_progress_m = 0.0

    lat, lon = sim._current_position()
    assert (lat, lon) == (50.0, 7.0)


def test_current_position_interpolates_at_midpoint():
    # 50% along a segment → halfway in both lat and lon.
    sim = _make_sim()
    sim._coords = [(50.0, 7.0), (50.001, 7.001)]
    sim._seg_idx = 0
    # Segment is ~135 m; half ≈ 67.5 m → progress = 50% of segment length.
    from drivepulse_app.map._geometry import haversine
    seg_len = haversine(50.0, 7.0, 50.001, 7.001)
    sim._seg_progress_m = seg_len / 2.0

    lat, lon = sim._current_position()
    # Halfway → midpoint of both coordinates.
    assert abs(lat - 50.0005) < 1e-9
    assert abs(lon - 7.0005) < 1e-9


def test_current_position_at_end_returns_last_coord():
    # After arrival the simulator pins to the final coordinate so
    # consumers always see a real (lat, lon) — never an off-end index.
    sim = _make_sim()
    sim._coords = [(50.0, 7.0), (50.001, 7.001), (50.002, 7.002)]
    sim._seg_idx = 99  # past end

    assert sim._current_position() == (50.002, 7.002)


# ── _current_heading: forward bearing through current segment ─────────────────


def test_current_heading_points_eastward_for_lon_increase():
    sim = _make_sim()
    sim._coords = [(50.0, 7.0), (50.0, 7.001)]
    sim._seg_idx = 0

    # Bearing east is ~90° at this latitude.
    heading = sim._current_heading()
    assert 88.0 < heading < 92.0


def test_current_heading_at_end_uses_last_segment():
    # After arrival there is no "next" coord, but the heading must still
    # be defined (consumers chain it into payloads).  Falls back to the
    # bearing of the final segment.
    sim = _make_sim()
    sim._coords = [(50.0, 7.0), (50.001, 7.0)]   # straight north
    sim._seg_idx = 99

    heading = sim._current_heading()
    # Bearing north is ~0° (or 360°); allow a small wraparound tolerance.
    assert heading < 1.0 or heading > 359.0


def test_current_heading_with_too_few_points_returns_zero():
    # Pathological case: only one coordinate (shouldn't happen in practice
    # but the helper must not crash).
    sim = _make_sim()
    sim._coords = [(50.0, 7.0)]
    sim._seg_idx = 0

    assert sim._current_heading() == 0.0
