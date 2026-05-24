"""Tests for the share_conflicts table operations: count/list/get/discard
and resolve. When a sync push collides with locally-modified data, the
server stores the incoming version as a conflict row; the local user
later picks whether to take the incoming side (resolve) or keep their
own (discard)."""
from __future__ import annotations

import json
import time

import pytest

from drivepulse_app.db import DriveDB


@pytest.fixture
def db(tmp_path):
    instance = DriveDB(tmp_path / "drives.sqlite3")
    yield instance
    instance.close()


def _insert_conflict(db, typ: str, local_id: int, payload: dict, car_id: int | None = None) -> int:
    cur = db._conn.execute(
        "INSERT INTO share_conflicts(type, car_id, local_id, incoming_json, received_at)"
        " VALUES(?,?,?,?,?)",
        (typ, car_id, local_id, json.dumps(payload), "2026-05-24T10:00:00+00:00"),
    )
    db._conn.commit()
    return int(cur.lastrowid)


def test_count_share_conflicts_empty_by_default(db):
    assert db.count_share_conflicts() == 0


def test_count_share_conflicts_tracks_inserts(db):
    _insert_conflict(db, "trip", 1, {"foo": "bar"})
    _insert_conflict(db, "scan", 2, {"baz": "qux"})
    assert db.count_share_conflicts() == 2


def test_list_share_conflicts_orders_newest_first(db):
    _insert_conflict(db, "trip", 1, {})
    time.sleep(0.005)
    # Override received_at on the second row so the order is testable.
    db._conn.execute(
        "INSERT INTO share_conflicts(type, car_id, local_id, incoming_json, received_at)"
        " VALUES(?,?,?,?,?)",
        ("scan", None, 2, "{}", "2026-05-25T10:00:00+00:00"),
    )
    db._conn.commit()
    rows = db.list_share_conflicts()
    # 2026-05-25 (scan) before 2026-05-24 (trip).
    assert [r["type"] for r in rows] == ["scan", "trip"]


def test_get_conflict_round_trips(db):
    cid = _insert_conflict(db, "trip", 42, {"label": "Heimfahrt"})
    row = db.get_conflict(cid)
    assert row is not None
    assert row["type"] == "trip"
    assert row["local_id"] == 42
    assert json.loads(row["incoming_json"])["label"] == "Heimfahrt"


def test_get_conflict_returns_none_for_missing(db):
    assert db.get_conflict(9999) is None


def test_discard_conflict_removes_row(db):
    cid = _insert_conflict(db, "scan", 1, {})
    assert db.count_share_conflicts() == 1
    db.discard_conflict(cid)
    assert db.count_share_conflicts() == 0
    assert db.get_conflict(cid) is None


def test_discard_conflict_unknown_id_is_noop(db):
    # Idempotent: discarding a non-existent ID should not raise.
    db.discard_conflict(9999)


# ─── resolve_conflict: apply incoming version to the live row ───────────────

def test_resolve_trip_conflict_updates_local_trip(db):
    car_id = db.upsert_car(vin="VIN-CONFL")
    trip_id = db.start_trip(car_id)
    db.add_sample(trip_id, ts=1.0, speed_kmh=50.0)
    db.end_trip(trip_id)

    incoming = {
        "ended_at": "2026-05-24T12:00:00+00:00",
        "distance_km": 99.9,
        "duration_s": 1800,
        "max_speed_kmh": 150.0,
        "avg_speed_kmh": 75.0,
        "samples_count": 42,
        "label": "Re-imported tour",
    }
    cid = _insert_conflict(db, "trip", trip_id, incoming, car_id=car_id)
    db.resolve_conflict(cid)

    # The trip row carries the incoming values now…
    trip = next(t for t in db.list_trips_for_car(car_id) if t["id"] == trip_id)
    assert trip["distance_km"] == 99.9
    assert trip["duration_s"] == 1800
    assert trip["max_speed_kmh"] == 150.0
    assert trip["avg_speed_kmh"] == 75.0
    assert trip["samples_count"] == 42
    assert trip["label"] == "Re-imported tour"
    # …and the conflict row was deleted.
    assert db.get_conflict(cid) is None


def test_resolve_scan_conflict_replaces_blob_and_counts(db):
    car_id = db.upsert_car(vin="VIN-CONFL-S")
    scan_id = db.add_scan(car_id, {
        "scanned_at": "2026-05-24T08:00:00+00:00",
        "protocol": "old",
        "dtcs": [],
        "supported_pids": [],
    })
    incoming = {
        "protocol": "ISO 15765-4 (CAN 11/500)",
        "dtc_count": 2,
        "pending_dtc_count": 1,
        "pids_count": 8,
        "data_json": json.dumps({"live_data": {"x": 1}}),
    }
    cid = _insert_conflict(db, "scan", scan_id, incoming, car_id=car_id)
    db.resolve_conflict(cid)

    scans = db.list_scans_for_car(car_id)
    scan = next(s for s in scans if s["id"] == scan_id)
    assert scan["protocol"] == "ISO 15765-4 (CAN 11/500)"
    assert scan["dtc_count"] == 2
    assert scan["pending_dtc_count"] == 1
    assert scan["pids_count"] == 8

    data = db.get_scan_data(scan_id)
    assert data == {"live_data": {"x": 1}}


def test_resolve_run_conflict_replaces_json_blobs(db):
    car_id = db.upsert_car(vin="VIN-CONFL-R")
    run_id = db.add_stopwatch_run(car_id, {"old": True}, [{"ts": 0}])
    incoming = {
        "results": {"target_kmh": 100, "elapsed_s": 8.2},
        "samples": [{"ts": 0.0, "speed": 0}, {"ts": 8.2, "speed": 100}],
    }
    cid = _insert_conflict(db, "run", run_id, incoming, car_id=car_id)
    db.resolve_conflict(cid)

    run = db.get_stopwatch_run(run_id)
    assert run["results"]["target_kmh"] == 100
    assert run["results"]["elapsed_s"] == 8.2
    assert run["samples"] == incoming["samples"]


def test_resolve_unknown_conflict_id_is_noop(db):
    db.resolve_conflict(9999)


def test_resolve_unknown_type_drops_conflict_without_applying(db):
    car_id = db.upsert_car(vin="VIN-CONFL-X")
    trip_id = db.start_trip(car_id)
    db.add_sample(trip_id, ts=1.0, speed_kmh=50.0)
    db.end_trip(trip_id)

    # An unrecognised "type" string — resolve still deletes the conflict
    # but doesn't touch any table (defensive against schema-version drift).
    cid = _insert_conflict(db, "unknown-type", trip_id, {"foo": "bar"}, car_id=car_id)
    db.resolve_conflict(cid)
    assert db.get_conflict(cid) is None
