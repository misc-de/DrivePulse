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
