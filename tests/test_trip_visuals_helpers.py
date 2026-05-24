"""Tests for the pure helpers in cars_trip_visuals: the speed→colour gradient
and the per-metric chart-data builder. These run on every trip detail open
and on every saved-tour replay — visible regressions if either drifts."""
from __future__ import annotations

import math

import pytest

from drivepulse_app.cars.trip_visuals import build_trip_metric_data, speed_to_rgb


# ─── speed_to_rgb ────────────────────────────────────────────────────────────

def test_speed_to_rgb_none_returns_neutral_fallback():
    # Samples without speed get the cool baseline colour rather than black.
    r, g, b = speed_to_rgb(None, vmax=100.0)
    assert (r, g, b) == (0.4, 0.6, 0.9)


def test_speed_to_rgb_zero_vmax_returns_fallback():
    # vmax = 0 is the empty-trip case — same neutral fallback.
    assert speed_to_rgb(50.0, vmax=0.0) == (0.4, 0.6, 0.9)


def test_speed_to_rgb_zero_speed_is_blue_end():
    r, g, b = speed_to_rgb(0.0, vmax=100.0)
    # Cool end of the ramp: low red, mid green, high blue.
    assert b > r and b > g


def test_speed_to_rgb_max_speed_is_red_end():
    r, g, b = speed_to_rgb(100.0, vmax=100.0)
    # Hot end of the ramp: high red, low blue.
    assert r > b
    # Specifically: r = 0.2 + 0.7*1 = 0.9, b = 0.9 - 0.8*1 = 0.1.
    assert r == pytest.approx(0.9)
    assert b == pytest.approx(0.1)


def test_speed_to_rgb_clamps_above_vmax():
    # Sample faster than vmax — clamped to t=1.
    above = speed_to_rgb(150.0, vmax=100.0)
    at_max = speed_to_rgb(100.0, vmax=100.0)
    assert above == at_max


def test_speed_to_rgb_mid_speed_has_green_peak():
    # Green channel peaks at t=0.5 → g = 0.5 + 0.4 = 0.9.
    r, g, b = speed_to_rgb(50.0, vmax=100.0)
    assert g == pytest.approx(0.9, abs=0.01)


def test_speed_to_rgb_components_in_unit_range():
    # Every output channel stays within [0, 1] across the speed band.
    for spd in (0.0, 20.0, 50.0, 80.0, 100.0, 200.0):
        r, g, b = speed_to_rgb(spd, vmax=100.0)
        for c in (r, g, b):
            assert 0.0 <= c <= 1.0


# ─── build_trip_metric_data ──────────────────────────────────────────────────


def _sample(ts: float, lat: float | None = 50.0, lon: float | None = 8.0, **metrics):
    """Build a sample row that mimics a sqlite3.Row dict (for s["k"] access)."""
    base = {
        "ts": ts, "lat": lat, "lon": lon,
        "speed_kmh": None, "rpm": None, "coolant_c": None, "intake_c": None,
        "throttle_pct": None, "engine_load": None, "maf_gps": None,
        "voltage_v": None, "accel_g": None, "fuel_pct": None,
    }
    base.update(metrics)
    return base


def test_build_trip_metric_data_empty_samples_returns_nothing():
    metric_data, avail = build_trip_metric_data([], language="en")
    assert metric_data == {}
    assert avail == []


def test_build_trip_metric_data_skips_samples_without_gps():
    # Samples without lat/lon are silently dropped — the chart needs GPS
    # to position the cursor marker on the map.
    samples = [
        _sample(ts=1.0, lat=None, lon=None, speed_kmh=50),
        _sample(ts=2.0, lat=None, lon=None, speed_kmh=60),
    ]
    metric_data, avail = build_trip_metric_data(samples, language="en")
    assert metric_data == {}
    assert avail == []


def test_build_trip_metric_data_filters_metrics_below_30pct_coverage():
    # 10 samples; speed_kmh present in 5 (50%), rpm present in 2 (20%).
    # speed_kmh should pass, rpm shouldn't (below 30% of 10 = 3).
    samples = [
        _sample(ts=i, speed_kmh=50 if i < 5 else None, rpm=2000 if i < 2 else None)
        for i in range(10)
    ]
    metric_data, _avail = build_trip_metric_data(samples, language="en")
    assert "speed_kmh" in metric_data
    assert "rpm" not in metric_data


def test_build_trip_metric_data_returns_tuples_in_ts_lat_lon_order():
    samples = [
        _sample(ts=10.0, lat=51.1, lon=7.7, speed_kmh=30),
        _sample(ts=20.0, lat=51.2, lon=7.8, speed_kmh=60),
        _sample(ts=30.0, lat=51.3, lon=7.9, speed_kmh=90),
    ]
    metric_data, _avail = build_trip_metric_data(samples, language="en")
    pts = metric_data["speed_kmh"]
    # Schema is (ts, value, lat, lon).
    assert pts[0] == (10.0, 30, 51.1, 7.7)
    assert pts[-1] == (30.0, 90, 51.3, 7.9)


def test_build_trip_metric_data_treats_inf_as_missing():
    # Inf / NaN values are blanked but the row stays in the sample list.
    samples = [
        _sample(ts=i, speed_kmh=(math.inf if i == 1 else 50.0))
        for i in range(10)
    ]
    metric_data, _avail = build_trip_metric_data(samples, language="en")
    pts = metric_data["speed_kmh"]
    inf_pt = next(p for p in pts if p[0] == 1)
    assert inf_pt[1] is None


def test_build_trip_metric_data_adds_elapsed_km_when_gps_advances():
    # Two GPS samples ~111 km apart (one degree of latitude) → elapsed_km
    # should be > 100.
    samples = [
        _sample(ts=0.0, lat=50.0, lon=8.0),
        _sample(ts=3600.0, lat=51.0, lon=8.0),
    ]
    metric_data, avail = build_trip_metric_data(samples, language="en")
    assert "elapsed_km" in metric_data
    last_km = metric_data["elapsed_km"][-1][1]
    assert 100.0 < last_km < 120.0
    # The elapsed_km tuple appears in the avail list with the right unit.
    elapsed_entry = next(e for e in avail if e[0] == "elapsed_km")
    assert elapsed_entry[2] == "km"


def test_build_trip_metric_data_skips_elapsed_km_when_distance_is_zero():
    # Stationary samples (same coord) — no useful elapsed-km line.
    samples = [
        _sample(ts=0.0, lat=50.0, lon=8.0),
        _sample(ts=60.0, lat=50.0, lon=8.0),
        _sample(ts=120.0, lat=50.0, lon=8.0),
    ]
    metric_data, _avail = build_trip_metric_data(samples, language="en")
    assert "elapsed_km" not in metric_data


def test_build_trip_metric_data_avail_entries_have_translated_label():
    samples = [_sample(ts=i, speed_kmh=50) for i in range(10)]
    _metric_data, avail = build_trip_metric_data(samples, language="en")
    speed_entry = next(e for e in avail if e[0] == "speed_kmh")
    key, label, unit, color, fmt = speed_entry
    # Label must come from the translation table (not the raw key).
    assert label != "cars.metric.speed_kmh"
    assert unit == "km/h"
    assert isinstance(color, tuple) and len(color) == 3
    assert fmt == "{:.0f}"
