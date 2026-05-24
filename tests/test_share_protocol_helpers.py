"""Tests for share_protocol pure helpers: VIN anonymisation/hashing and the
trip-comparison logic that prevents redundant DB writes during sync."""
from __future__ import annotations

from drivepulse_app.share.protocol import (
    _round2,
    _trips_identical,
    make_anon_vin,
    make_vin_hash,
)

# ─── make_vin_hash ────────────────────────────────────────────────────────────

def test_make_vin_hash_is_stable():
    # Same VIN must hash identically across calls — used as a sync key.
    h1 = make_vin_hash("WAUZZZ8KZBA000000")
    h2 = make_vin_hash("WAUZZZ8KZBA000000")
    assert h1 == h2


def test_make_vin_hash_changes_with_input():
    assert make_vin_hash("WAUZZZ8KZBA000000") != make_vin_hash("WBAVA31030NL00000")


def test_make_vin_hash_is_64_char_sha256_hex():
    h = make_vin_hash("X" * 17)
    assert len(h) == 64
    int(h, 16)  # raises if not hex


# ─── make_anon_vin ────────────────────────────────────────────────────────────

def test_make_anon_vin_keeps_prefix_and_suffix():
    # First 4 (WMI + first digit) and last 4 (serial tail) survive; the
    # rest is masked with "0" so the brand region is recognisable but the
    # specific vehicle is not.
    out = make_anon_vin("WAUZZZ8KZBA000099")
    assert out.startswith("WAUZ")
    assert out.endswith("0099")
    assert len(out) == len("WAUZZZ8KZBA000099")


def test_make_anon_vin_short_input_passes_through():
    # Strings under 8 chars don't have enough fixed-region info to anonymise
    # — return as-is rather than mangling.
    assert make_anon_vin("ABC") == "ABC"
    assert make_anon_vin("1234567") == "1234567"


# ─── _round2 ─────────────────────────────────────────────────────────────────

def test_round2_rounds_to_two_decimals():
    assert _round2(1.2345) == 1.23
    assert _round2(0.005) in (0.0, 0.01)  # banker's rounding tolerance


def test_round2_handles_none():
    assert _round2(None) is None


def test_round2_handles_non_numeric():
    # Defensive: incoming sync payloads may carry strings or junk.
    assert _round2("not a number") is None
    assert _round2([1, 2, 3]) is None


def test_round2_accepts_numeric_string():
    assert _round2("3.14159") == 3.14


# ─── _trips_identical ────────────────────────────────────────────────────────

def _trip_row(distance, duration, mx, avg, samples=10):
    """Build a sqlite3.Row-like dict; only the keys we test are needed."""
    return {
        "distance_km":    distance,
        "duration_s":     duration,
        "max_speed_kmh":  mx,
        "avg_speed_kmh":  avg,
        "samples_count":  samples,
    }


def test_trips_identical_matching_values():
    existing = _trip_row(12.34, 600, 100.0, 50.5)
    incoming = {
        "distance_km":    12.34,
        "duration_s":     600,
        "max_speed_kmh":  100.0,
        "avg_speed_kmh":  50.5,
        "samples_count":  10,
    }
    assert _trips_identical(existing, incoming) is True


def test_trips_identical_tolerates_rounding_in_third_decimal():
    # Sync payload may carry slightly different rounding from the wire.
    # _round2 normalises to 2 decimals before comparing.
    existing = _trip_row(12.345, 600, 100.001, 50.5)
    incoming = {
        "distance_km":    12.347,
        "duration_s":     600,
        "max_speed_kmh":  100.004,
        "avg_speed_kmh":  50.50,
        "samples_count":  10,
    }
    assert _trips_identical(existing, incoming) is True


def test_trips_identical_detects_distance_drift():
    # 0.1 km drift survives _round2 and should mark trips as different,
    # forcing a re-import.
    existing = _trip_row(12.3, 600, 100.0, 50.5)
    incoming = {
        "distance_km":    12.4,
        "duration_s":     600,
        "max_speed_kmh":  100.0,
        "avg_speed_kmh":  50.5,
        "samples_count":  10,
    }
    assert _trips_identical(existing, incoming) is False


def test_trips_identical_detects_sample_count_mismatch():
    existing = _trip_row(12.34, 600, 100.0, 50.5, samples=10)
    incoming = {
        "distance_km":    12.34,
        "duration_s":     600,
        "max_speed_kmh":  100.0,
        "avg_speed_kmh":  50.5,
        "samples_count":  25,
    }
    assert _trips_identical(existing, incoming) is False


def test_trips_identical_treats_missing_incoming_fields_as_none():
    # Older clients may omit fields; the comparison should not match an
    # existing row that has real values.
    existing = _trip_row(12.34, 600, 100.0, 50.5)
    assert _trips_identical(existing, {}) is False
