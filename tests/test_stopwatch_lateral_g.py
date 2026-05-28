"""Tests for the lateral-G estimator in StopWatchProcessingMixin.

The estimator turns GPS heading-change-over-time into a centripetal
acceleration ``a = v · ω``, low-pass filtered to suppress GPS jitter.
It drives the X-axis of the G-force ball and the lateral-G readout, so
a regression here would either render the readout permanently stuck or
fill the display with noise.

The mixin reads/writes a handful of state attributes; we instantiate it
via ``__new__`` and seed those attributes by hand so the test is
independent of the StopWatchPage GTK class composition.
"""
from __future__ import annotations

import itertools
import math

import pytest


def _make_processor():
    """Return a StopWatchProcessingMixin instance with cleared state."""
    from drivepulse_app.stopwatch.processing import StopWatchProcessingMixin

    p = StopWatchProcessingMixin.__new__(StopWatchProcessingMixin)
    p._last_heading_deg = None
    p._last_heading_time = None
    p._lateral_g = 0.0
    return p


# ── Below-threshold / no-input → decay path ───────────────────────────────────


def test_lateral_g_below_10kmh_decays_toward_zero():
    # GPS heading is unreliable at low speeds, so the estimator must decay
    # whatever lateral G it currently holds rather than pick up noise.
    p = _make_processor()
    p._lateral_g = 1.0

    p._update_lateral_g(heading_deg=180.0, speed_kmh=5.0, now=100.0)

    # 0.6 multiplier per call.
    assert p._lateral_g == 0.6


def test_lateral_g_none_heading_decays():
    p = _make_processor()
    p._lateral_g = 0.5

    p._update_lateral_g(heading_deg=None, speed_kmh=50.0, now=100.0)

    assert p._lateral_g == 0.3


def test_lateral_g_none_speed_decays():
    p = _make_processor()
    p._lateral_g = 0.8

    p._update_lateral_g(heading_deg=180.0, speed_kmh=None, now=100.0)

    assert p._lateral_g == 0.48


def test_lateral_g_decay_stores_heading_for_next_call():
    # Even on the decay path the stored heading/time gets refreshed —
    # otherwise the first valid sample after the slow-speed window would
    # be paired with a stale timestamp and produce a huge bogus omega.
    p = _make_processor()
    p._update_lateral_g(heading_deg=200.0, speed_kmh=5.0, now=42.0)
    assert p._last_heading_deg == 200.0
    assert p._last_heading_time == 42.0


# ── First sample seeds state ──────────────────────────────────────────────────


def test_lateral_g_first_sample_seeds_state_without_computing():
    # Need a previous heading + time to compute a delta. The first valid
    # sample must populate them and emit lateral_g unchanged.
    p = _make_processor()
    p._lateral_g = 0.3  # arbitrary previous value

    p._update_lateral_g(heading_deg=90.0, speed_kmh=30.0, now=10.0)

    assert p._last_heading_deg == 90.0
    assert p._last_heading_time == 10.0
    assert p._lateral_g == 0.3  # not touched on the seed call


# ── Centripetal math ──────────────────────────────────────────────────────────


def test_lateral_g_right_turn_produces_positive():
    # 30° right turn over 1s at 36 km/h (10 m/s):
    #   omega = radians(30°) ≈ 0.5236 rad/s
    #   a_lat = 10 m/s · 0.5236 ≈ 5.236 m/s²
    #   target_g = 5.236 / 9.80665 ≈ 0.534
    #   filtered = 0 + (0.534 - 0) * 0.35 ≈ 0.187
    p = _make_processor()
    p._last_heading_deg = 90.0
    p._last_heading_time = 10.0

    p._update_lateral_g(heading_deg=120.0, speed_kmh=36.0, now=11.0)

    expected = (10.0 * math.radians(30.0) / 1.0) / 9.80665 * 0.35
    assert p._lateral_g == expected


def test_lateral_g_left_turn_produces_negative():
    # Mirror of the right-turn test — heading goes 90° → 60° over 1s.
    p = _make_processor()
    p._last_heading_deg = 90.0
    p._last_heading_time = 10.0

    p._update_lateral_g(heading_deg=60.0, speed_kmh=36.0, now=11.0)

    assert p._lateral_g < 0


def test_lateral_g_handles_360_wraparound_correctly():
    # Heading wraps from 350° to 10° — that's a +20° right turn, not -340°.
    p = _make_processor()
    p._last_heading_deg = 350.0
    p._last_heading_time = 10.0
    p._update_lateral_g(heading_deg=10.0, speed_kmh=50.0, now=11.0)

    # The wrap-correction must place this in the positive lateral_g range,
    # i.e. a normal-magnitude right turn rather than a ~17× larger anti-turn.
    assert 0 < p._lateral_g < 0.5


def test_lateral_g_low_pass_filter_attenuates():
    # The filter multiplies the new target by 0.35; verify the smoothing
    # behaves as a single-pole LPF — repeated identical targets converge.
    p = _make_processor()
    p._last_heading_deg = 0.0
    p._last_heading_time = 0.0

    # Same per-tick stimulus: 10° turn over 1s at 50 km/h.
    targets = []
    for i in range(1, 8):
        p._update_lateral_g(heading_deg=i * 10.0, speed_kmh=50.0, now=float(i))
        targets.append(p._lateral_g)

    # Strictly monotonically converging (each call moves 35% closer to
    # the steady-state target, never past it).
    for a, b in itertools.pairwise(targets):
        assert b > a


def test_lateral_g_clamps_short_dt_to_50ms():
    # dt is floored at 0.05 to prevent a /0 explosion when two readings
    # arrive in the same tick. Same heading change with dt=0 must produce
    # the same result as dt=0.05.
    p1 = _make_processor()
    p1._last_heading_deg = 90.0
    p1._last_heading_time = 10.0
    p1._update_lateral_g(heading_deg=100.0, speed_kmh=50.0, now=10.0)

    p2 = _make_processor()
    p2._last_heading_deg = 90.0
    p2._last_heading_time = 10.0
    p2._update_lateral_g(heading_deg=100.0, speed_kmh=50.0, now=10.05)

    # Tolerance: dt enters the computation twice (in delta-rate AND in the
    # filter step downstream), and floating-point shuffles by a few ULPs.
    assert p1._lateral_g == pytest.approx(p2._lateral_g, rel=1e-9)
