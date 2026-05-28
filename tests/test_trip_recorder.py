"""Tests for TripRecorder: the in-memory state machine that merges OBD
and GPS samples into per-trip rows. Pairs with a real DriveDB so the
sample-write contract is exercised end to end."""
from __future__ import annotations

import math
import time

import pytest

from drivepulse_app.db import DriveDB
from drivepulse_app.trip_recorder import (
    TripRecorder,
    _haversine_m,
    filter_gps_samples,
)

# ─── _haversine_m: geodesic distance ─────────────────────────────────────────

def test_haversine_zero_when_same_point():
    assert _haversine_m(50.0, 8.0, 50.0, 8.0) == 0.0


def test_haversine_one_degree_latitude_is_about_111km():
    # 1° of latitude ≈ 111.195 km regardless of longitude.
    d = _haversine_m(50.0, 8.0, 51.0, 8.0)
    assert 110_500 < d < 111_500


def test_haversine_symmetric_in_argument_order():
    a = _haversine_m(50.1, 8.6, 48.1, 11.6)  # Frankfurt → München (rough)
    b = _haversine_m(48.1, 11.6, 50.1, 8.6)
    assert a == pytest.approx(b, rel=1e-9)


def test_haversine_clamps_acos_argument_to_avoid_nan():
    # Antipode is 20 015 km ≈ half the earth — should not blow up.
    d = _haversine_m(0.0, 0.0, 0.0, 180.0)
    assert 19_900_000 < d < 20_100_000
    assert math.isfinite(d)


# ─── TripRecorder: state transitions + sample merging ────────────────────────

@pytest.fixture
def db(tmp_path):
    instance = DriveDB(tmp_path / "drives.sqlite3")
    yield instance
    instance.close()


def test_recorder_drops_obd_until_car_is_set(db):
    rec = TripRecorder(db)
    rec.record_obd(ts=1.0, speed_kmh=50.0)  # no car_id yet
    assert rec.trip_id is None
    (count,) = db._conn.execute("SELECT COUNT(*) FROM samples").fetchone()
    assert count == 0


def test_recorder_starts_trip_on_first_sample_after_set_car(db):
    rec = TripRecorder(db)
    rec.set_car(vin="VIN-TR")
    rec.record_obd(ts=1.0, speed_kmh=50.0, rpm=2000)
    assert rec.trip_id is not None
    samples = list(db.samples_for_trip(rec.trip_id))
    assert len(samples) == 1
    assert samples[0]["speed_kmh"] == 50.0
    assert samples[0]["rpm"] == 2000


def test_recorder_merges_last_known_gps_into_obd_sample(db):
    rec = TripRecorder(db)
    rec.set_car(vin="VIN-TR2")
    # GPS arrives first, then OBD — sample should carry both.
    rec.update_gps(lat=50.1, lon=8.6, altitude_m=120.0, heading_deg=180.0, gps_speed_kmh=85.0)
    rec.record_obd(ts=10.0, speed_kmh=80.0)
    sample = next(iter(db.samples_for_trip(rec.trip_id)))
    assert sample["lat"] == 50.1
    assert sample["lon"] == 8.6
    assert sample["altitude_m"] == 120.0
    assert sample["heading_deg"] == 180.0
    assert sample["gps_speed_kmh"] == 85.0


def test_recorder_obd_field_overrides_gps_cache_when_both_present(db):
    """A field passed explicitly to record_obd must win over the cached
    GPS value (e.g. OBD-side speed overrides GPS speed)."""
    rec = TripRecorder(db)
    rec.set_car(vin="VIN-TR3")
    rec.update_gps(gps_speed_kmh=85.0, lat=50.0, lon=8.0)
    rec.record_obd(ts=10.0, gps_speed_kmh=99.9)  # explicit value
    sample = next(iter(db.samples_for_trip(rec.trip_id)))
    assert sample["gps_speed_kmh"] == 99.9


def test_recorder_rejects_gps_outlier_implying_impossible_speed(db):
    """If two GPS fixes are seconds apart but km in distance, the second
    fix is rejected — likely a multi-path glitch or warm-start cold-fix."""
    rec = TripRecorder(db)
    rec.set_car(vin="VIN-OUT")
    rec.update_gps(lat=50.0, lon=8.0)
    # Without sleeping, _last_gps_ts is just-now. Immediately jump to a
    # point 10 km away — that's ~10000 m / ~0.01s, way above the 100 m/s cap.
    rec.update_gps(lat=50.1, lon=8.1)  # ~13 km away
    # Outlier should not overwrite the first fix.
    assert rec._last_gps["lat"] == 50.0
    assert rec._last_gps["lon"] == 8.0


def test_recorder_freezes_gps_drift_while_stationary(db):
    """At a standstill (OBD speed ~0) GPS still drifts a few metres per fix.
    That drift must not overwrite the cached position, or it would be recorded
    as movement and inflate the trip distance."""
    rec = TripRecorder(db)
    rec.set_car(vin="VIN-STAND")
    # Vehicle is parked: OBD reports 0 km/h.
    rec.record_obd(ts=1.0, speed_kmh=0.0)
    rec.update_gps(lat=50.0, lon=8.0)
    # A drifting GPS fix arrives while still stationary.
    rec.update_gps(lat=50.0002, lon=8.0002)  # ~28 m of drift
    assert rec._last_gps["lat"] == 50.0
    assert rec._last_gps["lon"] == 8.0


def test_recorder_accepts_gps_movement_once_obd_reports_motion(db, monkeypatch):
    """When OBD confirms the car is moving, GPS position updates flow through —
    the stationary clamp must not freeze a real drive. Time is advanced between
    fixes so the kinematic outlier filter sees a realistic 1 s gap."""
    clock = [1000.0]
    monkeypatch.setattr("drivepulse_app.trip_recorder.time.monotonic", lambda: clock[0])
    rec = TripRecorder(db)
    rec.set_car(vin="VIN-MOVE")
    rec.record_obd(ts=1.0, speed_kmh=0.0)
    rec.update_gps(lat=50.0, lon=8.0)
    # Car starts moving: OBD speed rises, GPS should now track.
    clock[0] += 1.0
    rec.record_obd(ts=2.0, speed_kmh=40.0)
    rec.update_gps(lat=50.0002, lon=8.0002)  # ~28 m in 1 s ≈ 100 km/h
    assert rec._last_gps["lat"] == 50.0002
    assert rec._last_gps["lon"] == 8.0002


def test_recorder_stationary_clamp_falls_back_to_gps_speed(db):
    """Without fresh OBD speed, GPS speed decides standstill. A near-zero GPS
    speed freezes position; a clear moving speed lets it through."""
    rec = TripRecorder(db)
    rec.set_car(vin="VIN-GPSONLY")
    rec.update_gps(lat=50.0, lon=8.0, gps_speed_kmh=0.5)
    rec.update_gps(lat=50.0002, lon=8.0002, gps_speed_kmh=0.5)
    assert rec._last_gps["lat"] == 50.0  # frozen


def test_recorder_end_trip_clears_trip_id(db):
    rec = TripRecorder(db)
    rec.set_car(vin="VIN-END")
    rec.record_obd(ts=1.0, speed_kmh=50.0)
    rec.record_obd(ts=2.0, speed_kmh=55.0)
    assert rec.trip_id is not None
    rec.end_trip()
    assert rec.trip_id is None
    assert rec._last_obd_ts == 0.0


def test_recorder_maybe_end_idle_trip_fires_after_timeout(db):
    rec = TripRecorder(db)
    rec.set_car(vin="VIN-IDLE")
    rec.record_obd(ts=1000.0, speed_kmh=50.0)
    # Below timeout → no change.
    fired = rec.maybe_end_idle_trip(now=1000.0 + TripRecorder.IDLE_TIMEOUT_S - 1)
    assert fired is False
    assert rec.trip_id is not None
    # Past timeout → trip ends.
    fired = rec.maybe_end_idle_trip(now=1000.0 + TripRecorder.IDLE_TIMEOUT_S + 1)
    assert fired is True
    assert rec.trip_id is None


def test_recorder_maybe_end_idle_returns_false_when_no_trip(db):
    rec = TripRecorder(db)
    rec.set_car(vin="VIN-NOIDLE")
    # No samples recorded yet → no active trip → noop.
    assert rec.maybe_end_idle_trip(now=time.monotonic()) is False


def test_recorder_set_car_ends_active_trip_when_car_changes(db):
    rec = TripRecorder(db)
    rec.set_car(vin="VIN-A")
    rec.record_obd(ts=1.0, speed_kmh=50.0)
    rec.record_obd(ts=2.0, speed_kmh=55.0)
    first_trip = rec.trip_id
    # Switching to a different car ends the old trip and resets state.
    rec.set_car(vin="VIN-B")
    assert rec.trip_id is None
    # The old trip should be persisted (ended_at set).
    rows = list(db._conn.execute(
        "SELECT ended_at FROM trips WHERE id=?", (first_trip,)
    ))
    assert rows and rows[0][0] is not None


def test_recorder_set_car_keeps_trip_when_same_car(db):
    rec = TripRecorder(db)
    rec.set_car(vin="VIN-SAME")
    rec.record_obd(ts=1.0, speed_kmh=50.0)
    active = rec.trip_id
    rec.set_car(vin="VIN-SAME", brand="now-known")
    # Same car → same trip.
    assert rec.trip_id == active


# ─── filter_gps_samples: read-time smoothing of stored trips ─────────────────

def _sample(ts, lat, lon, speed_kmh=None, gps_speed_kmh=None, rpm=None):
    return {
        "ts": ts, "lat": lat, "lon": lon,
        "speed_kmh": speed_kmh, "gps_speed_kmh": gps_speed_kmh, "rpm": rpm,
    }


def test_filter_gps_samples_freezes_standstill_drift():
    """A parked vehicle (OBD 0 km/h) whose GPS drifts must have its position
    frozen to the last fix on read-back."""
    rows = [
        _sample(1.0, 50.0, 8.0, speed_kmh=0.0),
        _sample(2.0, 50.0002, 8.0002, speed_kmh=0.0),  # ~28 m drift while parked
        _sample(3.0, 50.0001, 7.9999, speed_kmh=0.0),
    ]
    out = filter_gps_samples(rows)
    assert [(r["lat"], r["lon"]) for r in out] == [
        (50.0, 8.0), (50.0, 8.0), (50.0, 8.0),
    ]


def test_filter_gps_samples_keeps_real_movement():
    """Genuine driving (OBD reports speed, ~1 s between fixes) passes through."""
    rows = [
        _sample(1.0, 50.0, 8.0, speed_kmh=40.0),
        _sample(2.0, 50.0002, 8.0002, speed_kmh=40.0),
        _sample(3.0, 50.0004, 8.0004, speed_kmh=40.0),
    ]
    out = filter_gps_samples(rows)
    assert [(r["lat"], r["lon"]) for r in out] == [
        (50.0, 8.0), (50.0002, 8.0002), (50.0004, 8.0004),
    ]


def test_filter_gps_samples_clamps_kinematic_outlier():
    """A fix implying > 250 km/h over a realistic gap is clamped to the last
    valid position, not recorded as a jump."""
    rows = [
        _sample(1.0, 50.0, 8.0, speed_kmh=50.0),
        _sample(2.0, 50.1, 8.1, speed_kmh=50.0),  # ~13 km in 1 s → outlier
        _sample(3.0, 50.0009, 8.0, speed_kmh=50.0),  # back near reality
    ]
    out = filter_gps_samples(rows)
    assert (out[1]["lat"], out[1]["lon"]) == (50.0, 8.0)  # clamped


def test_filter_gps_samples_passes_through_non_gps_rows():
    """Rows without a fix and all non-position fields are left untouched."""
    rows = [
        _sample(1.0, None, None, rpm=900.0),
        _sample(2.0, 50.0, 8.0, speed_kmh=30.0, rpm=2000.0),
    ]
    out = filter_gps_samples(rows)
    assert out[0]["lat"] is None and out[0]["rpm"] == 900.0
    assert out[1]["rpm"] == 2000.0


def test_filter_gps_samples_does_not_mutate_input():
    rows = [
        _sample(1.0, 50.0, 8.0, speed_kmh=0.0),
        _sample(2.0, 50.0002, 8.0002, speed_kmh=0.0),
    ]
    filter_gps_samples(rows)
    assert rows[1]["lat"] == 50.0002  # original untouched
