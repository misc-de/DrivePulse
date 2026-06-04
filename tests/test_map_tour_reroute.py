"""Unit tests for MapTourRerouteMixin auto-reroute logic.

Focus: the bypass heuristic in ``_trigger_reroute`` that may drop an
intermediate waypoint when the driver appears to have already passed it.
A regression here previously caused the rerouter to skip the next stop
and head straight to the final destination as soon as the driver took
a different street — even if that stop was still kilometres ahead.
"""
from __future__ import annotations

from typing import ClassVar

import pytest

from drivepulse_app.map import tour_reroute as tour_mod
from drivepulse_app.map.tour_reroute import MapTourRerouteMixin


class _FakeThread:
    """Stand-in for threading.Thread that captures target+args without running."""

    instances: ClassVar[list] = []

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
    inst = object.__new__(MapTourRerouteMixin)
    inst._gps_lat = gps_lat
    inst._gps_lon = gps_lon
    inst._gps_heading = heading
    inst._gps_heading_valid = heading_valid
    inst._tour_waypoints = list(waypoints or [])
    inst._remaining_dest_wps = list(remaining or [])
    inst._last_reroute_time = 0.0
    inst._off_route_since = 99.0  # arbitrary non-zero — must be reset to 0
    inst._persist_active_tour = lambda: None  # provided by MapTourMixin in the app
    return inst


def _captured_points() -> list[tuple[float, float]]:
    assert len(_FakeThread.instances) == 1, "exactly one reroute thread expected"
    # The thread's target is the bound _fetch_reroute_bg; args = (all_points,)
    args = _FakeThread.instances[0].args
    assert args and isinstance(args[0], list)
    return args[0]


# ── Via-bypass fix: far via the driver heads straight away from is dropped ────

def test_far_intermediate_wp_dropped_when_driving_straight_away():
    """Driver heading north, intermediate WP ~1.1 km *south* (bearing 180°,
    diff 180°): the driver has taken their own way and is driving straight
    away from the via, so it is dropped and the route goes to the final
    destination instead of ballooning >1 km back to the bypassed via.
    Real-world repro: round-trip with the via 1.6 km behind while heading 89°."""
    intermediate = (49.99, 8.0)   # ~1112 m south of (50, 8)
    final = (50.01, 8.0)          # ~1112 m north of (50, 8)
    inst = _make_inst(
        heading=0.0,              # facing north, directly away from the via
        heading_valid=True,
        remaining=[intermediate, final],
    )

    inst._trigger_reroute()

    assert inst._remaining_dest_wps == [final]
    points = _captured_points()
    assert points == [(50.0, 8.0), final]


def test_far_intermediate_wp_kept_when_only_moderately_off_heading():
    """A far via that is behind-ish but not straight-behind (diff 125°, below
    behind_deg) is kept — a mid-turn or parallel-street wobble must not skip a
    stop that is still kilometres ahead on the planned route."""
    intermediate = (49.99, 8.0)   # ~1112 m south, bearing 180°
    final = (50.01, 8.0)
    inst = _make_inst(
        heading=55.0,             # diff 125° -> behind but not driving straight away
        heading_valid=True,
        remaining=[intermediate, final],
    )

    inst._trigger_reroute()

    assert inst._remaining_dest_wps == [intermediate, final]
    points = _captured_points()
    assert points == [(50.0, 8.0), intermediate, final]


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
    assert MapTourRerouteMixin._OFF_ROUTE_M <= 30.0
    assert MapTourRerouteMixin._OFF_ROUTE_CONFIRM_S <= 4.0


# ── Wrong-way / U-turn reroute (heading-based; the distance test can't see it) ─

def _make_wrongway_inst(
    *,
    heading,
    route_coords=None,
    step_modifier="",
    last_reroute=0.0,
    wrong_way_since=0.0,
    heading_valid=True,
):
    inst = object.__new__(MapTourRerouteMixin)
    inst._gps_heading = heading
    inst._gps_heading_valid = heading_valid
    # Default route heads due north → bearing at segment 0 is 0°.
    inst._tour_coords = route_coords or [[8.0, 50.0], [8.0, 50.02]]
    inst._gps_route_idx = 0
    inst._tour_steps = [{"type": "turn", "modifier": step_modifier}]
    inst._tour_step_idx = 0
    inst._last_reroute_time = last_reroute
    inst._wrong_way_since = wrong_way_since
    return inst


def test_wrong_way_triggers_after_confirm():
    """Heading south (180°) on a north-bound route (0°): diff 180° > 120°.
    Fires only after the confirm window, overriding the normal cooldown."""
    inst = _make_wrongway_inst(heading=180.0)
    assert inst._wrong_way_reroute(speed_kmh=40.0, now=100.0) is False  # starts timer
    assert inst._wrong_way_since == 100.0
    assert inst._wrong_way_reroute(speed_kmh=40.0, now=102.0) is False  # still confirming
    assert inst._wrong_way_reroute(speed_kmh=40.0, now=104.0) is True   # >3s + >8s gap
    assert inst._wrong_way_since == 0.0


def test_wrong_way_resets_when_back_on_heading():
    inst = _make_wrongway_inst(heading=180.0)
    assert inst._wrong_way_reroute(speed_kmh=40.0, now=100.0) is False
    inst._gps_heading = 5.0  # turned back onto the route direction → timer resets
    assert inst._wrong_way_reroute(speed_kmh=40.0, now=101.0) is False
    assert inst._wrong_way_since == 0.0


def test_wrong_way_ignored_for_prescribed_uturn():
    """When the route itself prescribes a U-turn here, never fight it."""
    inst = _make_wrongway_inst(heading=180.0, step_modifier="uturn")
    assert inst._wrong_way_reroute(speed_kmh=40.0, now=100.0) is False
    assert inst._wrong_way_reroute(speed_kmh=40.0, now=104.0) is False


def test_wrong_way_ignored_below_min_speed():
    inst = _make_wrongway_inst(heading=180.0)
    assert inst._wrong_way_reroute(speed_kmh=5.0, now=100.0) is False
    assert inst._wrong_way_since == 0.0


def test_wrong_way_respects_min_gap_after_reroute():
    """Sustained wrong-way, but a reroute fired 4 s ago (< 8 s min-gap): hold."""
    inst = _make_wrongway_inst(heading=180.0, last_reroute=100.0, wrong_way_since=100.0)
    assert inst._wrong_way_reroute(speed_kmh=40.0, now=104.0) is False
