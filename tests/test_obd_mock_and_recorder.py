"""Tests for the OBD mock simulator's acceleration curve and the
recorder's payload-to-float helper.

``MockObdSimulator._accel_g_for_speed`` is a piecewise function used to
shape the synthetic 0→230 km/h run. ``ObdRecorder._to_float`` normalises
the python-OBD response shapes (bare numbers and ``{value, unit}``
dicts) into a single float pipeline. Both are pure and easy to test;
the IO-heavy parts of those modules (subprocess, DB queue, threads)
stay out of these tests.
"""
from __future__ import annotations

import itertools

# ── MockObdSimulator._accel_g_for_speed ───────────────────────────────────────


def test_accel_g_launches_around_one_g():
    from drivepulse_app.obd.mock import MockObdSimulator

    # 0 km/h is the launch point — should be at the peak of the curve (~1 g).
    g0 = MockObdSimulator._accel_g_for_speed(0.0)
    assert 0.95 <= g0 <= 1.05


def test_accel_g_tapers_through_low_band():
    from drivepulse_app.obd.mock import MockObdSimulator

    # The 0→100 km/h band tapers linearly from ~1.0 g to ~0.45 g.  Verify
    # the curve is strictly monotone decreasing through several samples.
    samples = [MockObdSimulator._accel_g_for_speed(s) for s in (0, 25, 50, 75, 99)]
    for a, b in itertools.pairwise(samples):
        assert b < a
    assert samples[-1] < 0.5  # ~0.45 g near 100 km/h


def test_accel_g_band_continuity_at_100_kmh():
    from drivepulse_app.obd.mock import MockObdSimulator

    # The 0–100 and 100–200 bands are designed to meet at ~0.45 g.  The
    # implementation chooses the 100–200 branch at exactly 100 km/h.
    below = MockObdSimulator._accel_g_for_speed(99.0)
    at = MockObdSimulator._accel_g_for_speed(100.0)
    # They should be within a few hundredths of a g — no big jump.
    assert abs(below - at) < 0.05


def test_accel_g_mid_band_decreases_toward_high_band():
    from drivepulse_app.obd.mock import MockObdSimulator

    # 100→200 km/h: ~0.45 g down to ~0.10 g.
    a = MockObdSimulator._accel_g_for_speed(100.0)
    b = MockObdSimulator._accel_g_for_speed(150.0)
    c = MockObdSimulator._accel_g_for_speed(199.0)
    assert a > b > c
    assert c < 0.15


def test_accel_g_top_band_floors_at_002():
    from drivepulse_app.obd.mock import MockObdSimulator

    # 200→230 km/h band floors at 0.02 g — never goes negative within it.
    for s in (200.0, 215.0, 229.0):
        g = MockObdSimulator._accel_g_for_speed(s)
        assert g >= 0.02
        assert g <= 0.10


def test_accel_g_above_230_signals_engine_drag():
    from drivepulse_app.obd.mock import MockObdSimulator

    # Past the top of the curve the simulator emits a slight negative
    # acceleration so the speed walks back down → terminates the run.
    g = MockObdSimulator._accel_g_for_speed(231.0)
    assert g < 0


# ── ObdRecorder._to_float ─────────────────────────────────────────────────────


def test_to_float_extracts_value_from_python_obd_dict():
    from drivepulse_app.obd.recorder import ObdRecorder

    # python-OBD responses come as {"value": N, "unit": "..."} — must
    # unwrap to the float.
    assert ObdRecorder._to_float({"value": 1500, "unit": "rpm"}) == 1500.0


def test_to_float_accepts_bare_numbers():
    from drivepulse_app.obd.recorder import ObdRecorder

    # GPS samples are emitted as bare floats — the helper must accept
    # both shapes without the caller having to disambiguate.
    assert ObdRecorder._to_float(42.5) == 42.5
    assert ObdRecorder._to_float(7) == 7.0


def test_to_float_returns_none_for_none_or_missing_value():
    from drivepulse_app.obd.recorder import ObdRecorder

    assert ObdRecorder._to_float(None) is None
    assert ObdRecorder._to_float({"value": None}) is None
    assert ObdRecorder._to_float({}) is None


def test_to_float_returns_none_for_non_numeric():
    from drivepulse_app.obd.recorder import ObdRecorder

    # "stalled" / "n/a" strings come back from python-OBD for sensors that
    # aren't responding — must be discarded, not cast to crash.
    assert ObdRecorder._to_float("stalled") is None
    assert ObdRecorder._to_float({"value": "n/a"}) is None
    assert ObdRecorder._to_float([1, 2]) is None
