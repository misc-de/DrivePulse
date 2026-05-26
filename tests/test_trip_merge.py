"""Tests for DriveDB.merge_trips — combine consecutive trips into one
entry. The pause between trips is logged as standstill (zero speed) and
is intentionally NOT counted as drive time, so distance / duration stay
honest for a Fahrtenbuch."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from drivepulse_app.db import DriveDB


@pytest.fixture
def db(tmp_path):
    instance = DriveDB(tmp_path / "drives.sqlite3")
    yield instance
    instance.close()


def _add_trip(db, car_id, started_at, ended_at, samples):
    """Create a trip, fill with samples, end it. db.end_trip() recomputes
    the aggregate columns from the samples table, matching what the
    real recorder would have produced."""
    trip_id = db.start_trip(car_id, started_at=started_at)
    for s in samples:
        db.add_sample(trip_id, s["ts"], **{k: v for k, v in s.items() if k != "ts"})
    db.end_trip(trip_id)
    # Force ended_at to a deterministic value (end_trip uses datetime.now()).
    db._conn.execute("UPDATE trips SET ended_at=? WHERE id=?",
                     (ended_at.isoformat(), trip_id))
    db._conn.commit()
    return trip_id


def test_merge_trips_rejects_single_trip(db):
    car = db.upsert_car(vin="VINTMERGE1")
    t = _add_trip(db, car,
                  datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
                  datetime(2026, 1, 1, 8, 5, tzinfo=UTC),
                  [{"ts": 0.0, "speed_kmh": 50.0}])
    with pytest.raises(ValueError, match="too_few"):
        db.merge_trips([t])


def test_merge_trips_rejects_different_cars(db):
    a = db.upsert_car(vin="VINTMERGEA")
    b = db.upsert_car(vin="VINTMERGEB")
    ta = _add_trip(db, a,
                   datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
                   datetime(2026, 1, 1, 8, 5, tzinfo=UTC),
                   [{"ts": 0.0, "speed_kmh": 50.0}])
    tb = _add_trip(db, b,
                   datetime(2026, 1, 1, 8, 6, tzinfo=UTC),
                   datetime(2026, 1, 1, 8, 10, tzinfo=UTC),
                   [{"ts": 100.0, "speed_kmh": 50.0}])
    with pytest.raises(ValueError, match="different_cars"):
        db.merge_trips([ta, tb])


def test_merge_trips_rejects_large_gap(db):
    # Reject when the gap between trip1.ended_at and trip2.started_at
    # exceeds 30 min (the user's mental model: pause for fuel, not a
    # day apart).
    car = db.upsert_car(vin="VINTMERGEG")
    t0 = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    t1 = _add_trip(db, car,
                   t0, t0 + timedelta(minutes=5),
                   [{"ts": 0.0, "speed_kmh": 50.0}])
    t2_start = t0 + timedelta(minutes=50)
    t2 = _add_trip(db, car,
                   t2_start, t2_start + timedelta(minutes=5),
                   [{"ts": 1000.0, "speed_kmh": 60.0}])
    with pytest.raises(ValueError, match="gap_too_large"):
        db.merge_trips([t1, t2])


def test_merge_trips_keeps_earliest_and_sums_drive_metrics(db):
    car = db.upsert_car(vin="VINTMERGES")
    t0 = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    ts0 = t0.timestamp()
    # Trip 1: 5 min @ 60 km/h ≈ 5 km
    t1 = _add_trip(db, car,
                   t0, t0 + timedelta(minutes=5),
                   [{"ts": ts0 + i, "speed_kmh": 60.0} for i in range(0, 301, 30)])
    # Pause: 10 min. Trip 2: 5 min @ 80 km/h.
    t2_start = t0 + timedelta(minutes=15)
    ts2 = t2_start.timestamp()
    t2 = _add_trip(db, car,
                   t2_start, t2_start + timedelta(minutes=5),
                   [{"ts": ts2 + i, "speed_kmh": 80.0} for i in range(0, 301, 30)])

    pre = {r["id"]: dict(r) for r in db.list_trips_for_car(car)}
    expected_distance = (pre[t1]["distance_km"] or 0) + (pre[t2]["distance_km"] or 0)
    expected_duration = (pre[t1]["duration_s"] or 0) + (pre[t2]["duration_s"] or 0)
    expected_max = max(pre[t1]["max_speed_kmh"] or 0, pre[t2]["max_speed_kmh"] or 0)

    survivor = db.merge_trips([t2, t1])  # order irrelevant
    assert survivor == t1  # earliest wins

    trips_after = db.list_trips_for_car(car)
    assert len(trips_after) == 1
    row = trips_after[0]
    assert row["id"] == t1
    # Drive metrics sum the source trips; the 10-min pause does NOT count.
    assert abs((row["distance_km"] or 0) - expected_distance) < 1e-6
    assert abs((row["duration_s"] or 0) - expected_duration) < 1e-3
    assert row["max_speed_kmh"] == expected_max


def test_merge_trips_fills_pause_with_zero_speed_samples(db):
    car = db.upsert_car(vin="VINTMERGEP")
    t0 = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    ts0 = t0.timestamp()
    t1 = _add_trip(db, car,
                   t0, t0 + timedelta(seconds=10),
                   [{"ts": ts0 + i, "speed_kmh": 50.0} for i in range(11)])
    # 60-second pause.
    t2_start = t0 + timedelta(seconds=70)
    ts2 = t2_start.timestamp()
    t2 = _add_trip(db, car,
                   t2_start, t2_start + timedelta(seconds=10),
                   [{"ts": ts2 + i, "speed_kmh": 60.0} for i in range(11)])
    db.merge_trips([t1, t2])
    samples = list(db.samples_for_trip(t1))
    # Pause is bridged by many zero-speed samples.
    zero_speed = sum(1 for s in samples if (s["speed_kmh"] or 0) == 0.0)
    assert zero_speed > 30, f"expected zero-fill samples for the pause, got {zero_speed}"
    # lat / lon stay NULL on fill rows so the map doesn't jump to (0, 0).
    fill_rows = [s for s in samples if (s["speed_kmh"] or 0) == 0.0]
    assert all(s["lat"] is None for s in fill_rows)
    assert all(s["lon"] is None for s in fill_rows)
