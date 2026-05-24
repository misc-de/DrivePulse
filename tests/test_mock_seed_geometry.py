"""Tests for mock_seed.py geometry helpers: haversine, bearing, polyline
interpolation/resampling. These run in the offline path that builds the
demo data set when OSRM is unreachable. Bad math here = unrealistic
routes on the map."""
from __future__ import annotations

import math

import pytest

from drivepulse_app.mock.seed import (
    _bearing_deg,
    _haversine_km,
    _interp_polyline,
    _resample_polyline,
)


# ─── _haversine_km ───────────────────────────────────────────────────────────

def test_haversine_km_zero_when_same_point():
    assert _haversine_km((50.0, 8.0), (50.0, 8.0)) == 0.0


def test_haversine_km_one_degree_latitude_is_about_111_km():
    d = _haversine_km((50.0, 8.0), (51.0, 8.0))
    # 1° latitude ≈ 111.195 km regardless of longitude.
    assert 110.5 < d < 111.5


def test_haversine_km_symmetric():
    a = _haversine_km((50.0, 8.0), (52.5, 13.4))  # Frankfurt → Berlin-ish
    b = _haversine_km((52.5, 13.4), (50.0, 8.0))
    assert a == pytest.approx(b, rel=1e-9)


def test_haversine_km_returns_finite_for_antipode():
    d = _haversine_km((0.0, 0.0), (0.0, 180.0))
    assert math.isfinite(d)
    assert 19_900 < d < 20_100  # ~20 015 km


# ─── _bearing_deg ────────────────────────────────────────────────────────────

def test_bearing_due_north():
    # Move north along the same longitude → bearing 0°.
    assert _bearing_deg((50.0, 8.0), (51.0, 8.0)) == pytest.approx(0.0, abs=0.01)


def test_bearing_due_south_is_180():
    assert _bearing_deg((51.0, 8.0), (50.0, 8.0)) == pytest.approx(180.0, abs=0.01)


def test_bearing_due_east_is_90():
    # At the equator, due-east bearing is exactly 90.
    assert _bearing_deg((0.0, 0.0), (0.0, 1.0)) == pytest.approx(90.0, abs=0.01)


def test_bearing_due_west_is_270():
    assert _bearing_deg((0.0, 0.0), (0.0, -1.0)) == pytest.approx(270.0, abs=0.01)


def test_bearing_always_in_zero_three_sixty_range():
    # Sweep a few directions and ensure bearing stays bounded.
    for dlat, dlon in [(0.1, 0.1), (-0.1, 0.1), (-0.1, -0.1), (0.1, -0.1)]:
        b = _bearing_deg((50.0, 8.0), (50.0 + dlat, 8.0 + dlon))
        assert 0.0 <= b < 360.0


# ─── _interp_polyline ───────────────────────────────────────────────────────

def test_interp_polyline_includes_anchor_endpoints():
    anchors = [(50.0, 8.0), (50.01, 8.01)]
    pts = _interp_polyline(anchors, step_km=0.5)
    # Start and end must be exact anchors.
    assert pts[0] == (50.0, 8.0)
    assert pts[-1] == (50.01, 8.01)


def test_interp_polyline_spacing_is_about_step_km():
    # Long leg, fine step → many intermediate samples.
    anchors = [(50.0, 0.0), (50.0, 0.5)]  # ~36 km east at lat 50
    pts = _interp_polyline(anchors, step_km=2.0)
    # We get many points; consecutive ones should be roughly 2 km apart.
    deltas = [_haversine_km(pts[i - 1], pts[i]) for i in range(1, len(pts))]
    # Allow generous slack — linear interpolation isn't great-circle, and
    # the last segment may be shorter to terminate at the anchor.
    assert max(deltas) < 5.0


def test_interp_polyline_multi_segment_visits_all_anchors():
    anchors = [(50.0, 8.0), (50.1, 8.1), (50.2, 8.0)]
    pts = _interp_polyline(anchors, step_km=1.0)
    # Both endpoints + the middle anchor should be very close to some
    # point in pts.
    for anchor in anchors:
        closest = min(_haversine_km(anchor, p) for p in pts)
        assert closest < 1.5  # within step distance


def test_interp_polyline_single_pair_minimum_two_points():
    pts = _interp_polyline([(50.0, 8.0), (50.0, 8.0001)], step_km=10.0)
    assert pts[0] == (50.0, 8.0)
    assert pts[-1] == (50.0, 8.0001)


# ─── _resample_polyline ──────────────────────────────────────────────────────

def test_resample_polyline_returns_input_when_already_small_enough():
    pts = [(0.0, 0.0), (0.1, 0.1)]
    assert _resample_polyline(pts, target_count=10) == pts


def test_resample_polyline_returns_input_for_invalid_target():
    pts = [(0.0, 0.0), (0.1, 0.1), (0.2, 0.2)]
    # target_count ≤ 2 is treated as "give back what you have" — caller
    # asked for nonsense, don't drop data.
    assert _resample_polyline(pts, target_count=0) == pts
    assert _resample_polyline(pts, target_count=2) == pts


def test_resample_polyline_starts_and_ends_with_anchors():
    pts = [(50.0 + i * 0.001, 8.0) for i in range(50)]
    out = _resample_polyline(pts, target_count=10)
    assert out[0] == pts[0]
    assert out[-1] == pts[-1]


def test_resample_polyline_returns_target_count_points():
    pts = [(50.0 + i * 0.001, 8.0) for i in range(100)]
    out = _resample_polyline(pts, target_count=12)
    assert len(out) == 12


def test_resample_polyline_handles_zero_length_route():
    # All points identical → cumulative distance is 0 → fallback to
    # just first+last so the caller still has both anchors.
    pts = [(50.0, 8.0)] * 20
    out = _resample_polyline(pts, target_count=5)
    assert out == [pts[0], pts[-1]]
