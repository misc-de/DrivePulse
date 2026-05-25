from __future__ import annotations

import sqlite3
from datetime import UTC

import pytest


def test_db_returns_empty_scan_data_for_invalid_json(tmp_path):
    from drivepulse_app.db import DriveDB

    db = DriveDB(tmp_path / "drivepulse.sqlite3")
    try:
        car_id = db.upsert_car(vin="TESTVIN123")
        with db._lock:
            cur = db._conn.execute(
                "INSERT INTO scans(car_id, scanned_at, data_json) VALUES(?,?,?)",
                (car_id, "2026-01-01T00:00:00+00:00", "{bad json"),
            )
            db._conn.commit()
            scan_id = int(cur.lastrowid)

        assert db.get_scan_data(scan_id) == {}
    finally:
        db.close()


def test_db_returns_empty_stopwatch_run_for_invalid_json(tmp_path):
    from drivepulse_app.db import DriveDB

    db = DriveDB(tmp_path / "drivepulse.sqlite3")
    try:
        car_id = db.upsert_car(vin="TESTVIN456")
        with db._lock:
            cur = db._conn.execute(
                "INSERT INTO acceleration_runs(car_id, run_at, results_json, samples_json) VALUES(?,?,?,?)",
                (car_id, "2026-01-01T00:00:00+00:00", "{bad json", "[]"),
            )
            db._conn.commit()
            run_id = int(cur.lastrowid)

        assert db.get_stopwatch_run(run_id) == {}
    finally:
        db.close()


def test_db_configures_lock_timeout_and_profile_path_index(tmp_path):
    from drivepulse_app.db import DriveDB

    db = DriveDB(tmp_path / "drivepulse.sqlite3")
    try:
        with db._lock:
            busy_timeout = db._conn.execute("PRAGMA busy_timeout").fetchone()[0]
            indexes = {
                row["name"]
                for row in db._conn.execute("PRAGMA index_list('cars')").fetchall()
            }

        assert busy_timeout == 5000
        assert "idx_cars_profile_path" in indexes
    finally:
        db.close()


def test_db_sets_current_schema_version(tmp_path):
    from drivepulse_app.db import DriveDB

    path = tmp_path / "drivepulse.sqlite3"
    db = DriveDB(path)
    try:
        with db._lock:
            version = db._conn.execute("PRAGMA user_version").fetchone()[0]

        assert version == 1
    finally:
        db.close()


def test_db_migration_upgrades_legacy_version_zero(tmp_path):
    from drivepulse_app.db import DriveDB

    path = tmp_path / "drivepulse.sqlite3"
    first = DriveDB(path)
    with first._lock:
        first._conn.execute("PRAGMA user_version=0")
        first._conn.commit()
    first.close()

    second = DriveDB(path)
    try:
        with second._lock:
            version = second._conn.execute("PRAGMA user_version").fetchone()[0]

        assert version == 1
    finally:
        second.close()


def test_db_migration_reraises_unexpected_sqlite_errors(monkeypatch, tmp_path):
    from drivepulse_app import db as db_module
    from drivepulse_app.db import DriveDB

    path = tmp_path / "drivepulse.sqlite3"
    first = DriveDB(path)
    with first._lock:
        first._conn.execute("PRAGMA user_version=0")
        first._conn.commit()
    first.close()

    monkeypatch.setattr(db_module, "_is_duplicate_column_error", lambda _exc: False)
    with pytest.raises(sqlite3.OperationalError, match="duplicate column"):
        DriveDB(path)


def test_db_rejects_newer_schema_version(tmp_path):
    from drivepulse_app.db import DriveDB

    path = tmp_path / "drivepulse.sqlite3"
    first = DriveDB(path)
    with first._lock:
        first._conn.execute("PRAGMA user_version=999")
        first._conn.commit()
    first.close()

    with pytest.raises(RuntimeError, match="newer than DrivePulse supports"):
        DriveDB(path)


def test_db_bulk_sample_insert_skips_malformed_rows_and_duplicates(tmp_path):
    from drivepulse_app.db import DriveDB

    db = DriveDB(tmp_path / "drivepulse.sqlite3")
    try:
        car_id = db.upsert_car(vin="BULKTEST")
        trip_id = db.start_trip(car_id)

        submitted = db.add_samples(trip_id, [
            {"ts": 1.0, "speed_kmh": 10, "rpm": 1000},
            {"ts": 1.0, "speed_kmh": 20, "rpm": 2000},
            {"ts": 2.0, "speed_kmh": "bad"},
            {"speed_kmh": 30},
            "not-a-sample",
            {"ts": 3.0, "gps_speed_kmh": 30},
        ])

        rows = db.samples_for_trip(trip_id)

        assert submitted == 3
        assert len(rows) == 2
        assert rows[0]["speed_kmh"] == 10
        assert rows[1]["gps_speed_kmh"] == 30
    finally:
        db.close()


def test_db_last_trip_stats_uses_latest_completed_trip(tmp_path):
    from datetime import datetime

    from drivepulse_app.db import DriveDB

    db = DriveDB(tmp_path / "drivepulse.sqlite3")
    try:
        car_id = db.upsert_car(vin="STATSVIN")
        old_trip = db.start_trip(car_id, datetime(2026, 1, 1, tzinfo=UTC))
        db.add_samples(old_trip, [
            {"ts": 1.0, "speed_kmh": 10, "rpm": 1000, "coolant_c": 80},
            {"ts": 2.0, "speed_kmh": 20, "rpm": 2000, "coolant_c": 90},
        ])
        db.end_trip(old_trip)

        latest_trip = db.start_trip(car_id, datetime(2026, 1, 2, tzinfo=UTC))
        db.add_samples(latest_trip, [
            {"ts": 3.0, "speed_kmh": 30, "rpm": 3000, "coolant_c": 70},
            {"ts": 4.0, "speed_kmh": 40, "rpm": 4000, "coolant_c": 75},
        ])
        db.end_trip(latest_trip)

        stats = db.get_last_trip_stats(car_id)

        assert stats is not None
        assert stats["id"] == latest_trip
        assert stats["min_rpm"] == 3000
        assert stats["max_rpm"] == 4000
        assert stats["min_coolant"] == 70
        assert stats["max_coolant"] == 75
    finally:
        db.close()


def test_profiles_load_vehicle_scan_data_from_database(tmp_path):
    from drivepulse_app.cars.profiles import _load_profiles
    from drivepulse_app.db import DriveDB

    vin = "WVWZZZ1JZXW000001"
    db = DriveDB(tmp_path / "drivepulse.sqlite3")
    try:
        car_id = db.upsert_car(vin=vin)
        db.add_scan(car_id, {
            "vin": vin,
            "vehicle_info": {"VIN": vin, "CALIBRATION_ID": "CAL"},
            "protocol": "6",
            "scanned_at": "2026-01-01T00:00:00+00:00",
            "live_data": {},
        })

        profiles = _load_profiles(db)

        assert len(profiles) == 1
        assert profiles[0]["car_id"] == car_id
        assert profiles[0]["vin"] == vin
        assert profiles[0]["data"]["vehicle_info"]["CALIBRATION_ID"] == "CAL"
    finally:
        db.close()
