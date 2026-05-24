"""Tests for sync_data: export/import roundtrip, replace-vs-merge import
modes, and the paired-devices JSON persistence layer.

test_sync_robustness already covers malformed-payload + duplicate-timestamp
edge cases; this file adds the happy-path roundtrip plus mode semantics."""
from __future__ import annotations

import json

import pytest

from drivepulse_app.sync import data as sync_data
from drivepulse_app.db import DriveDB
from drivepulse_app.sync.data import (
    export_all,
    import_data,
    load_paired_devices,
    save_paired_devices,
    upsert_paired_device,
)


@pytest.fixture
def db(tmp_path):
    instance = DriveDB(tmp_path / "drives.sqlite3")
    yield instance
    instance.close()


def _seed_one_car_one_trip(db, vin="VIN-EXP", brand="Audi"):
    cid = db.upsert_car(vin=vin, brand=brand, label="My Car")
    tid = db.start_trip(cid)
    db.add_sample(tid, ts=1.0, speed_kmh=50.0, rpm=2000, lat=50.0, lon=8.0)
    db.add_sample(tid, ts=2.0, speed_kmh=80.0, rpm=3000, lat=50.001, lon=8.001)
    db.end_trip(tid)
    return cid, tid


# ─── export_all ──────────────────────────────────────────────────────────────

def test_export_all_shape_for_empty_db(db):
    data = export_all(db)
    assert data["version"] == 1
    assert data["cars"] == []
    assert "exported_at" in data


def test_export_all_includes_trip_and_samples(db):
    _seed_one_car_one_trip(db)
    data = export_all(db)
    assert len(data["cars"]) == 1
    car = data["cars"][0]
    assert car["vin"] == "VIN-EXP"
    assert car["brand"] == "Audi"
    assert len(car["trips"]) == 1
    trip = car["trips"][0]
    assert len(trip["samples"]) == 2
    # samples are dicts with only non-None keys.
    s0 = trip["samples"][0]
    assert s0["ts"] == 1.0
    assert s0["speed_kmh"] == 50.0


def test_export_all_omits_none_sample_fields(db):
    cid = db.upsert_car(vin="VIN-SPARSE")
    tid = db.start_trip(cid)
    # Only ts + speed_kmh; everything else None.
    db.add_sample(tid, ts=1.0, speed_kmh=50.0)
    db.end_trip(tid)
    data = export_all(db)
    sample = data["cars"][0]["trips"][0]["samples"][0]
    # None-valued columns must be omitted to keep the payload small.
    assert "ts" in sample and "speed_kmh" in sample
    assert "rpm" not in sample
    assert "lat" not in sample


# ─── import_data: roundtrip ──────────────────────────────────────────────────

def test_import_data_merge_roundtrips_export(db, tmp_path):
    # Export from one DB, import into a fresh one — they should match.
    _seed_one_car_one_trip(db, vin="VIN-RT")
    payload = export_all(db)

    other = DriveDB(tmp_path / "other.sqlite3")
    try:
        result = import_data(other, payload, mode="merge")
        assert result["cars_added"] == 1
        assert result["trips_added"] == 1
        assert result["samples_added"] == 2

        cars = other.list_cars()
        assert len(cars) == 1
        assert cars[0]["vin"] == "VIN-RT"
        trips = other.list_trips_for_car(cars[0]["id"])
        assert len(trips) == 1
        samples = list(other.samples_for_trip(trips[0]["id"]))
        assert len(samples) == 2
    finally:
        other.close()


def test_import_data_merge_skips_existing_trip_by_started_at(db, tmp_path):
    # Identical payload imported twice should add nothing the second time.
    _seed_one_car_one_trip(db, vin="VIN-DUP")
    payload = export_all(db)

    other = DriveDB(tmp_path / "other.sqlite3")
    try:
        import_data(other, payload, mode="merge")
        second = import_data(other, payload, mode="merge")
        # Trip already present (matched by started_at) → 0 new trips.
        assert second["trips_added"] == 0
        assert second["samples_added"] == 0
        # Car still tallies as "updated" rather than "added" the 2nd time.
        assert second["cars_added"] == 0
        assert second["cars_updated"] == 1
    finally:
        other.close()


def test_import_data_replace_wipes_existing_trips_for_car(db, tmp_path):
    # Local has trip A, incoming has trip B for the same car. Replace mode
    # should delete trip A before inserting trip B.
    cid = db.upsert_car(vin="VIN-REPLACE")
    tid = db.start_trip(cid)
    db.add_sample(tid, ts=1.0, speed_kmh=10)
    db.end_trip(tid)
    original_trip_id = tid

    payload = {
        "version": 1,
        "cars": [{
            "vin": "VIN-REPLACE",
            "brand": "Audi",
            "trips": [{
                "started_at": "2026-05-24T10:00:00+00:00",
                "ended_at": "2026-05-24T10:30:00+00:00",
                "distance_km": 12.0,
                "duration_s": 1800,
                "max_speed_kmh": 100.0,
                "avg_speed_kmh": 60.0,
                "samples_count": 1,
                "samples": [{"ts": 100.0, "speed_kmh": 50.0}],
            }],
        }],
    }
    result = import_data(db, payload, mode="replace")
    assert result["trips_added"] == 1
    # The original trip is gone.
    rows = db._conn.execute("SELECT id FROM trips WHERE id=?", (original_trip_id,)).fetchall()
    assert rows == []


def test_import_data_replace_all_wipes_everything_first(db, tmp_path):
    # Two cars with trips — replace_all kills both before reinserting.
    cid_a = db.upsert_car(vin="VIN-AA")
    ta = db.start_trip(cid_a)
    db.add_sample(ta, ts=1.0, speed_kmh=10)
    db.end_trip(ta)
    cid_b = db.upsert_car(vin="VIN-BB")
    tb = db.start_trip(cid_b)
    db.add_sample(tb, ts=2.0, speed_kmh=20)
    db.end_trip(tb)

    payload = {"version": 1, "cars": [{"vin": "VIN-NEW"}]}
    import_data(db, payload, mode="replace_all")
    cars = db.list_cars()
    assert len(cars) == 1
    assert cars[0]["vin"] == "VIN-NEW"


def test_import_data_rejects_payload_with_wrong_version(db):
    result = import_data(db, {"version": 99, "cars": []}, mode="merge")
    assert result == {"cars_added": 0, "cars_updated": 0, "trips_added": 0, "samples_added": 0}


def test_import_data_rejects_non_dict_payload(db):
    # Anything that isn't a dict (list, None, str) → no-op.
    result = import_data(db, [], mode="merge")
    assert result["cars_added"] == 0


def test_import_data_unknown_mode_falls_back_to_merge(db, tmp_path):
    # Unknown mode strings get normalised to "merge" so a typo doesn't
    # accidentally wipe data.
    _seed_one_car_one_trip(db, vin="VIN-MODE")
    payload = export_all(db)

    other = DriveDB(tmp_path / "o.sqlite3")
    try:
        result = import_data(other, payload, mode="not-a-real-mode")
        # Imported as merge — trip survives.
        assert result["trips_added"] == 1
    finally:
        other.close()


def test_import_data_skips_trip_without_started_at(db):
    payload = {
        "version": 1,
        "cars": [{
            "vin": "VIN-NOSTART",
            "trips": [
                {"started_at": None, "samples": []},
                {"started_at": "", "samples": []},
                {"started_at": "2026-01-01T00:00:00+00:00", "samples": []},
            ],
        }],
    }
    result = import_data(db, payload, mode="merge")
    assert result["trips_added"] == 1


def test_import_data_recomputes_samples_count_from_inserts(db, tmp_path):
    # The incoming trip claims 99 samples but only delivers 2 — the row
    # must end up with the actual count, not the claimed count.
    payload = {
        "version": 1,
        "cars": [{
            "vin": "VIN-COUNT",
            "trips": [{
                "started_at": "2026-05-24T10:00:00+00:00",
                "samples_count": 99,
                "samples": [
                    {"ts": 1.0, "speed_kmh": 50.0},
                    {"ts": 2.0, "speed_kmh": 60.0},
                ],
            }],
        }],
    }
    other = DriveDB(tmp_path / "o.sqlite3")
    try:
        import_data(other, payload, mode="merge")
        cid = other.list_cars()[0]["id"]
        trip = other.list_trips_for_car(cid)[0]
        assert trip["samples_count"] == 2
    finally:
        other.close()


# ─── Paired-devices file ─────────────────────────────────────────────────────

@pytest.fixture
def paired_file(tmp_path, monkeypatch):
    path = tmp_path / "paired_devices.json"
    monkeypatch.setattr(sync_data, "PAIRED_DEVICES_FILE", path)
    return path


def test_load_paired_devices_empty_when_file_missing(paired_file):
    assert load_paired_devices() == []


def test_load_paired_devices_ignores_invalid_json(paired_file):
    paired_file.write_text("not json", encoding="utf-8")
    assert load_paired_devices() == []


def test_save_then_load_paired_devices_roundtrip(paired_file):
    payload = [
        {"device_id": "abc", "name": "Phone", "spki_fingerprint": "fp1",
         "host": "10.0.0.5", "port": 5555, "last_seen": "2026-05-24T10:00:00+00:00"},
        {"device_id": "xyz", "name": "Tablet", "spki_fingerprint": "fp2",
         "host": "10.0.0.6", "port": 5556, "last_seen": "2026-05-24T11:00:00+00:00"},
    ]
    save_paired_devices(payload)
    assert load_paired_devices() == payload


def test_save_paired_devices_writes_atomically_to_0600(paired_file):
    # Pairing fingerprints are security-sensitive — file mode must be 0600.
    import os
    save_paired_devices([{"device_id": "x"}])
    assert paired_file.stat().st_mode & 0o777 == 0o600


def test_upsert_paired_device_adds_new_entry(paired_file):
    upsert_paired_device("dev1", "Phone", "fp1", "10.0.0.5", 5555)
    devs = load_paired_devices()
    assert len(devs) == 1
    assert devs[0]["device_id"] == "dev1"
    assert devs[0]["host"] == "10.0.0.5"
    assert devs[0]["port"] == 5555


def test_upsert_paired_device_updates_existing_by_device_id(paired_file):
    upsert_paired_device("dev1", "Old name", "fp-old", "10.0.0.5", 5555)
    upsert_paired_device("dev1", "New name", "fp-new", "10.0.0.6", 6666)
    devs = load_paired_devices()
    assert len(devs) == 1
    assert devs[0]["name"] == "New name"
    assert devs[0]["spki_fingerprint"] == "fp-new"
    assert devs[0]["host"] == "10.0.0.6"
    assert devs[0]["port"] == 6666


def test_upsert_paired_device_preserves_other_entries(paired_file):
    upsert_paired_device("a", "A", "fa", "h", 1)
    upsert_paired_device("b", "B", "fb", "h", 2)
    upsert_paired_device("a", "A renamed", "fa2", "h", 1)
    devs = load_paired_devices()
    ids = {d["device_id"] for d in devs}
    assert ids == {"a", "b"}
