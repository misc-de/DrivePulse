"""Tests for the pure-logic helpers in cars/trip_visuals.

``speed_to_rgb`` is the shared speed → colour ramp used by both the
trip GPS-track painter and the chart legend.  ``build_trip_metric_data``
turns raw GPS+OBD samples into per-metric time series ready for the
chart widget, dropping metrics whose live data is sparser than 30%.

Both are pure functions of their inputs (no GTK, no DB) — the chart
widget and the painter that consume them are kept out of these tests.
"""
from __future__ import annotations

import pytest

from drivepulse_app.cars.trip_visuals import build_trip_metric_data, speed_to_rgb

# ── speed_to_rgb ──────────────────────────────────────────────────────────────


def test_speed_to_rgb_none_returns_neutral_blue():
    # No-data colour is the muted blue used for samples without a speed reading.
    assert speed_to_rgb(None, vmax=100) == (0.4, 0.6, 0.9)


def test_speed_to_rgb_zero_vmax_returns_neutral_blue():
    # vmax==0 means we never saw any speed > 0; everything maps to the
    # neutral colour rather than blowing up on the division.
    assert speed_to_rgb(50.0, vmax=0.0) == (0.4, 0.6, 0.9)


def test_speed_to_rgb_at_low_end_is_blue_dominant():
    r, g, b = speed_to_rgb(0.0, vmax=100.0)
    # At t=0 the formula gives r=0.2, g=0.5, b=0.9 → blue dominates by design.
    assert r == pytest.approx(0.2)
    assert g == pytest.approx(0.5)
    assert b == pytest.approx(0.9)


def test_speed_to_rgb_at_mid_is_green_peak():
    r, g, b = speed_to_rgb(50.0, vmax=100.0)
    # At t=0.5 the green channel peaks (formula peaks at t=0.5) — blue/red
    # cross over here. Confirms the ramp uses green as the midpoint.
    assert g == pytest.approx(0.9)
    assert r == pytest.approx(0.55)
    assert b == pytest.approx(0.5)


def test_speed_to_rgb_at_high_end_is_red_dominant():
    r, g, b = speed_to_rgb(100.0, vmax=100.0)
    # At t=1 the formula gives r=0.9, g=0.5, b≈0.1 → red dominates.
    assert r == pytest.approx(0.9)
    assert g == pytest.approx(0.5)
    assert b == pytest.approx(0.1, abs=1e-9)


def test_speed_to_rgb_clamps_above_vmax():
    # Speed > vmax must still map to the saturated red end (no overshoot
    # into negative blue).
    above = speed_to_rgb(200.0, vmax=100.0)
    at_max = speed_to_rgb(100.0, vmax=100.0)
    assert above == at_max


# ── build_trip_metric_data ────────────────────────────────────────────────────


def _make_samples(n: int = 10, *, with_obd: bool = True) -> list[dict]:
    """Build *n* synthetic GPS samples — moving from ~Köln Hbf eastwards."""
    samples = []
    for i in range(n):
        s = {
            "ts": float(i),
            "lat": 50.9429 + i * 0.0001,
            "lon": 6.9583 + i * 0.0001,
            "speed_kmh": 50.0 + i if with_obd else None,
            "rpm": 1500.0 + i * 10 if with_obd else None,
            "coolant_c": None,
            "intake_c": None,
            "throttle_pct": None,
            "engine_load": None,
            "maf_gps": None,
            "voltage_v": None,
            "accel_g": None,
            "fuel_pct": None,
        }
        samples.append(s)
    return samples


def test_build_trip_metric_data_emits_metrics_with_enough_coverage():
    samples = _make_samples(n=10)
    metric_data, avail = build_trip_metric_data(samples, language="en")

    # speed_kmh + rpm both have 100% valid coverage → present in metric_data.
    assert "speed_kmh" in metric_data
    assert "rpm" in metric_data
    # coolant + the rest are 0% covered → dropped.
    assert "coolant_c" not in metric_data
    # avail tuple shape: (key, label, unit, color, fmt).
    keys = [a[0] for a in avail]
    assert "speed_kmh" in keys
    assert "rpm" in keys


def test_build_trip_metric_data_skips_samples_without_gps():
    samples = _make_samples(n=4)
    samples.append({**samples[-1], "lat": None, "lon": None})  # no GPS
    samples.append({**samples[0], "lat": None, "lon": None})

    metric_data, _avail = build_trip_metric_data(samples, language="en")

    # Two samples got dropped (lat or lon None) — speed_kmh series must have
    # the same length as the original 4 GPS-bearing samples.
    assert len(metric_data["speed_kmh"]) == 4


def test_build_trip_metric_data_drops_metric_below_30_percent_coverage():
    # 10 samples; only 2 have a coolant reading (20% coverage) → below
    # the 30% threshold → coolant_c must not appear in metric_data.
    samples = _make_samples(n=10)
    samples[0]["coolant_c"] = 80.0
    samples[1]["coolant_c"] = 81.0

    metric_data, _avail = build_trip_metric_data(samples, language="en")

    assert "coolant_c" not in metric_data


def test_build_trip_metric_data_keeps_metric_at_30_percent_coverage():
    # 10 samples; 3 have a coolant reading (30% coverage) → exactly at the
    # threshold → coolant_c must be kept (the check is `>= min_valid`).
    samples = _make_samples(n=10)
    for i in range(3):
        samples[i]["coolant_c"] = 80.0 + i

    metric_data, _avail = build_trip_metric_data(samples, language="en")

    assert "coolant_c" in metric_data
    # Other 7 entries must show up as None (not omitted), so the chart can
    # still align the x-axis with the speed series.
    coolant_values = [v for _ts, v, _lat, _lon in metric_data["coolant_c"]]
    assert sum(1 for v in coolant_values if v is not None) == 3
    assert sum(1 for v in coolant_values if v is None) == 7


def test_build_trip_metric_data_computes_elapsed_km_when_distance_significant():
    # Build samples that physically span ~5 km (0.05° in lat at 50.9 ≈ 5.6 km).
    samples = []
    for i in range(10):
        samples.append({
            "ts": float(i),
            "lat": 50.9429 + i * 0.005,
            "lon": 6.9583,
            "speed_kmh": 50.0,
            "rpm": None, "coolant_c": None, "intake_c": None,
            "throttle_pct": None, "engine_load": None, "maf_gps": None,
            "voltage_v": None, "accel_g": None, "fuel_pct": None,
        })

    metric_data, avail = build_trip_metric_data(samples, language="en")

    assert "elapsed_km" in metric_data
    # Last sample's cumulative distance should be the largest in the series.
    cum_values = [v for _ts, v, _lat, _lon in metric_data["elapsed_km"]]
    assert cum_values[-1] > cum_values[0]
    # elapsed_km also has to appear in the avail tuple list.
    assert any(a[0] == "elapsed_km" for a in avail)


def test_build_trip_metric_data_skips_elapsed_km_for_stationary_trip():
    # All samples at the same location → cum_km stays ~0 → elapsed_km
    # series must not be emitted (the cumulative-km check is > 0.01).
    samples = []
    for i in range(5):
        samples.append({
            "ts": float(i),
            "lat": 50.9429,
            "lon": 6.9583,
            "speed_kmh": 0.0,
            "rpm": None, "coolant_c": None, "intake_c": None,
            "throttle_pct": None, "engine_load": None, "maf_gps": None,
            "voltage_v": None, "accel_g": None, "fuel_pct": None,
        })

    metric_data, _avail = build_trip_metric_data(samples, language="en")

    assert "elapsed_km" not in metric_data


def test_build_trip_metric_data_handles_nan_as_missing():
    # NaN and ±inf must be treated as "no reading" — the _finite guard
    # rejects them before they're plotted (would crash the chart).
    samples = _make_samples(n=10)
    samples[0]["speed_kmh"] = float("nan")
    samples[1]["speed_kmh"] = float("inf")

    metric_data, _avail = build_trip_metric_data(samples, language="en")

    # The first two values must be None — the rest are still valid.
    assert metric_data["speed_kmh"][0][1] is None
    assert metric_data["speed_kmh"][1][1] is None
    assert metric_data["speed_kmh"][2][1] is not None
