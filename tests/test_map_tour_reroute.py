"""Unit tests for MapTourMixin auto-reroute logic.

Focus: the bypass heuristic in ``_trigger_reroute`` that may drop an
intermediate waypoint when the driver appears to have already passed it.
A regression here previously caused the rerouter to skip the next stop
and head straight to the final destination as soon as the driver took
a different street — even if that stop was still kilometres ahead.
"""
from __future__ import annotations

import pytest

from drivepulse_app.map import tour as tour_mod
from drivepulse_app.map.tour import MapTourMixin


class _FakeThread:
    """Stand-in for threading.Thread that captures target+args without running."""

    instances: list = []

    def __init__(self, target=None, args=(), daemon=False, **_):
        self.target = target
        self.args = args
        self.daemon = daemon
        _FakeThread.instances.append(self)

    def start(self) -> None:
        # Intentionally do nothing — we only want to inspect what would have run.
        pass


@pytest.fixture(autouse=True)
def _no_threads(monkeypatch):
    _FakeThread.instances = []
    monkeypatch.setattr(tour_mod.threading, "Thread", _FakeThread)
    yield


def _make_inst(
    *,
    gps_lat=50.0,
    gps_lon=8.0,
    heading=0.0,
    heading_valid=True,
    remaining=None,
    waypoints=None,
):
    inst = object.__new__(MapTourMixin)
    inst._gps_lat = gps_lat
    inst._gps_lon = gps_lon
    inst._gps_heading = heading
    inst._gps_heading_valid = heading_valid
    inst._tour_waypoints = list(waypoints or [])
    inst._remaining_dest_wps = list(remaining or [])
    inst._last_reroute_time = 0.0
    inst._off_route_since = 99.0  # arbitrary non-zero — must be reset to 0
    return inst


def _captured_points() -> list[tuple[float, float]]:
    assert len(_FakeThread.instances) == 1, "exactly one reroute thread expected"
    target, args = _FakeThread.instances[0].target, _FakeThread.instances[0].args
    # target is the bound method _fetch_reroute_bg, args = (all_points,)
    assert args and isinstance(args[0], list)
    return args[0]


# ── Bug-2 regression: far-ahead intermediate WP must not be dropped ──────────

def test_far_intermediate_wp_is_kept_even_when_behind_heading():
    """Driver heading north, intermediate WP ~1.1 km south of current
    position (bearing 180°, diff 180° > 110°). Because the WP is FAR
    (> 250 m), it must remain in the recalculated route — the rerouter
    should turn us around, not skip the stop."""
    intermediate = (49.99, 8.0)   # ~1112 m south of (50, 8)
    final = (50.01, 8.0)          # ~1112 m north of (50, 8)
    inst = _make_inst(
        heading=0.0,              # facing north
        heading_valid=True,
        remaining=[intermediate, final],
    )

    inst._trigger_reroute()

    assert inst._remaining_dest_wps == [intermediate, final]
    points = _captured_points()
    assert points[0] == (50.0, 8.0)
    assert points[1] == intermediate
    assert points[-1] == final


# ── Close-and-behind WP: legitimate bypass, drop it ──────────────────────────

def test_close_intermediate_wp_behind_heading_is_dropped():
    """Intermediate ~55 m south of position, driver heading north.
    Both bearing diff (>110°) and proximity (<250 m) gates are satisfied,
    so the WP was genuinely passed and should be skipped."""
    close_behind = (49.9995, 8.0)  # ~55 m south
    final = (50.01, 8.0)
    inst = _make_inst(
        heading=0.0,
        heading_valid=True,
        remaining=[close_behind, final],
    )

    inst._trigger_reroute()

    assert inst._remaining_dest_wps == [final]
    points = _captured_points()
    assert points == [(50.0, 8.0), final]


# ── Heading invalid (low speed / no fix): never skip ─────────────────────────

def test_intermediate_wp_kept_when_heading_invalid():
    """Without a reliable heading (stationary, GPS noise), the bypass
    heuristic must not engage at all — even a close-and-behind WP stays."""
    close_behind = (49.9995, 8.0)
    final = (50.01, 8.0)
    inst = _make_inst(
        heading=0.0,
        heading_valid=False,
        remaining=[close_behind, final],
    )

    inst._trigger_reroute()

    assert inst._remaining_dest_wps == [close_behind, final]
    points = _captured_points()
    assert points == [(50.0, 8.0), close_behind, final]


# ── WP ahead of driver: keep it (sanity check, distance irrelevant) ──────────

def test_intermediate_wp_ahead_is_kept():
    """When the next WP is in front of the driver (small bearing diff),
    the bypass logic must keep it regardless of distance."""
    ahead = (50.001, 8.0)   # ~111 m north of (50, 8), bearing 0°
    final = (50.01, 8.0)
    inst = _make_inst(
        heading=0.0,
        heading_valid=True,
        remaining=[ahead, final],
    )

    inst._trigger_reroute()

    assert inst._remaining_dest_wps == [ahead, final]


# ── Final destination must never be skipped ──────────────────────────────────

def test_final_destination_is_never_dropped():
    """Even if the sole remaining WP is behind and close, it must remain
    because the bypass loop has the ``len(remaining) > 1`` guard."""
    behind_final = (49.9995, 8.0)
    inst = _make_inst(
        heading=0.0,
        heading_valid=True,
        remaining=[behind_final],
    )

    inst._trigger_reroute()

    assert inst._remaining_dest_wps == [behind_final]
    points = _captured_points()
    assert points == [(50.0, 8.0), behind_final]


# ── Off-route detection threshold tightening (bug-1 regression) ──────────────

def test_off_route_thresholds_are_tightened():
    """Bug 1 was that the rerouter reacted too late. The constants must
    stay at or below the tightened values: 30 m / 4 s. Loosening them
    again should require an explicit code-review decision."""
    assert MapTourMixin._OFF_ROUTE_M <= 30.0
    assert MapTourMixin._OFF_ROUTE_CONFIRM_S <= 4.0
