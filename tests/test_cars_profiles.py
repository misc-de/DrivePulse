"""Tests for cars_profiles._load_profiles — the bridge between the DB
schema and the cars-list UI. _scan_label formats the latest-scan timestamp
+ DTC count for the row subtitle."""
from __future__ import annotations

import json

import pytest

from drivepulse_app.cars_profiles import _load_profiles, _scan_label
from drivepulse_app.db import DriveDB


# ─── _scan_label ─────────────────────────────────────────────────────────────

def test_scan_label_formats_iso_to_german_date_with_dtc_plural():
    out = _scan_label("2026-05-24T10:00:00+00:00", dtc_count=3)
    assert out == "24.05.2026 · 3 DTC"


def test_scan_label_uses_singular_for_one_dtc():
    out = _scan_label("2026-05-24T10:00:00+00:00", dtc_count=1)
    assert out.endswith("· 1 DTC")


def test_scan_label_zero_dtc_uses_plural():
    out = _scan_label("2026-05-24T10:00:00+00:00", dtc_count=0)
    assert out.endswith("· 0 DTC")


def test_scan_label_accepts_z_suffix_as_utc():
    # python datetime.fromisoformat dislikes "Z" — _scan_label converts it.
    out = _scan_label("2026-05-24T10:00:00Z")
    assert out.startswith("24.05.2026")


def test_scan_label_empty_for_unparseable_input():
    assert _scan_label("not a date") == ""
    assert _scan_label(None) == ""


# ─── _load_profiles ──────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    instance = DriveDB(tmp_path / "drives.sqlite3")
    yield instance
    instance.close()


def test_load_profiles_none_db_returns_empty_list():
    assert _load_profiles(None) == []


def test_load_profiles_empty_db_returns_empty_list(db):
    assert _load_profiles(db) == []


def test_load_profiles_for_car_without_scans_synthesises_minimal_data(db):
    cid = db.upsert_car(vin="VINFOO", brand="VW", label="My Polo", cal_id="CAL1", cvn="CVN1", protocol="CAN")
    profiles = _load_profiles(db)
    assert len(profiles) == 1
    p = profiles[0]
    assert p["car_id"] == cid
    assert p["path"] == f"car:{cid}"
    assert p["vin"] == "VINFOO"
    assert p["brand"] == "VW"
    assert p["label"] == "My Polo"
    # No scans → synthesised data with VIN/CAL/CVN block + empty live_data.
    assert p["data"]["vehicle_info"]["VIN"] == "VINFOO"
    assert p["data"]["vehicle_info"]["CALIBRATION_ID"] == "CAL1"
    assert p["data"]["live_data"] == {}
    assert p["scan_label"] == ""
    assert p["latest_scan_at"] is None
    assert p["latest_dtc_count"] == 0
    assert p["trip_count"] == 0
    assert p["total_km"] == 0.0


def test_load_profiles_pulls_latest_scan_data(db):
    cid = db.upsert_car(vin="VINBAR")
    s1 = db.add_scan(cid, {
        "scanned_at": "2026-04-01T10:00:00+00:00",
        "dtcs": [],
        "live_data": {"Command(b'010C')": {"value": 1500, "unit": "rpm"}},
    })
    s2 = db.add_scan(cid, {
        "scanned_at": "2026-05-01T10:00:00+00:00",
        "dtcs": ["P0420", "P0301"],
        "live_data": {"Command(b'010C')": {"value": 1700, "unit": "rpm"}},
    })
    profiles = _load_profiles(db)
    p = profiles[0]
    # Latest scan is s2 (newer scanned_at).
    assert p["latest_scan_at"] == "2026-05-01T10:00:00+00:00"
    assert p["latest_dtc_count"] == 2
    # data is the latest scan's full blob.
    assert "Command(b'010C')" in p["data"]["live_data"]
    assert p["data"]["live_data"]["Command(b'010C')"]["value"] == 1700
    assert p["scan_label"].startswith("01.05.2026")


def test_load_profiles_falls_back_to_wmi_brand_for_unknown_brand(db):
    # No brand stored, but VIN WMI is known → derive brand from VIN.
    db.upsert_car(vin="WAUZZZ8KZBA000000")
    profiles = _load_profiles(db)
    assert profiles[0]["brand"] == "Audi"


def test_load_profiles_includes_trip_count_and_total_km(db):
    cid = db.upsert_car(vin="VINTRIP")
    tid = db.start_trip(cid)
    db.add_sample(tid, ts=1.0, speed_kmh=60.0)
    db.add_sample(tid, ts=3601.0, speed_kmh=60.0)
    db.end_trip(tid)
    profiles = _load_profiles(db)
    p = profiles[0]
    assert p["trip_count"] == 1
    assert p["total_km"] > 0


def test_load_profiles_parses_vin_data_json_column(db):
    cid = db.upsert_car(vin="VINDATA")
    db.update_car_vin_data(cid, json.dumps({"model": "Golf VI", "year": "2010"}))
    profiles = _load_profiles(db)
    p = profiles[0]
    assert p["vin_data_fetched"] is True
    assert p["data"]["vin_data"]["model"] == "Golf VI"


def test_load_profiles_handles_corrupted_vin_data_json(db):
    cid = db.upsert_car(vin="VINBAD")
    # Bypass update_car_vin_data so we can write invalid JSON directly.
    db._conn.execute("UPDATE cars SET vin_data_json=? WHERE id=?", ("{bad json", cid))
    db._conn.commit()
    profiles = _load_profiles(db)
    p = profiles[0]
    # vin_data_fetched is True (something is stored) but the dict is empty
    # because the JSON couldn't be parsed.
    assert p["vin_data_fetched"] is True
    assert p["data"]["vin_data"] == {}


def test_load_profiles_returns_empty_when_db_list_cars_raises(db):
    # If list_cars itself blows up, the function must return [] rather
    # than propagate the exception into the cars-list render.
    orig = db.list_cars

    def boom():
        raise RuntimeError("db gone")

    db.list_cars = boom  # type: ignore[method-assign]
    try:
        assert _load_profiles(db) == []
    finally:
        db.list_cars = orig  # type: ignore[method-assign]
