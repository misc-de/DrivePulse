from __future__ import annotations


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


def test_db_returns_empty_acceleration_run_for_invalid_json(tmp_path):
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

        assert db.get_acceleration_run(run_id) == {}
    finally:
        db.close()


def test_profiles_hide_scan_files_until_vehicle_is_registered(monkeypatch, tmp_path):
    import json

    from drivepulse_app import cars_profiles
    from drivepulse_app.db import DriveDB
    from drivepulse_app.cars_profiles import _load_profiles

    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    monkeypatch.setattr(cars_profiles, "PROFILES_DIR", profiles_dir)

    vin = "WVWZZZ1JZXW000001"
    (profiles_dir / "scan.json").write_text(
        json.dumps({
            "vin": vin,
            "vehicle_info": {"VIN": vin, "CALIBRATION_ID": "CAL"},
            "protocol": "6",
            "scanned_at": "2026-01-01T00:00:00+00:00",
            "live_data": {},
        }),
        encoding="utf-8",
    )

    db = DriveDB(tmp_path / "drivepulse.sqlite3")
    try:
        assert _load_profiles(db) == []

        car_id = db.upsert_car(vin=vin)
        profiles = _load_profiles(db)

        assert len(profiles) == 1
        assert profiles[0]["car_id"] == car_id
        assert profiles[0]["vin"] == vin
        assert profiles[0]["data"]["vehicle_info"]["CALIBRATION_ID"] == "CAL"
    finally:
        db.close()
