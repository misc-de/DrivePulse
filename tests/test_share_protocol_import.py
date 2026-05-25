"""Tests for share_protocol.share_import — the server-side handler that
applies an incoming share payload from another device. Covers all four
content types (trips, runs, scans, photos), conflict detection against
existing rows, and the "share_tours" sub-protocol for sending saved
routes."""
from __future__ import annotations

import base64
import hashlib
import json

import pytest

from drivepulse_app.db import DriveDB
from drivepulse_app.share.protocol import share_import


def _vin_hash(vin: str) -> str:
    return hashlib.sha256(vin.encode("utf-8")).hexdigest()


@pytest.fixture
def db(tmp_path):
    instance = DriveDB(tmp_path / "drives.sqlite3")
    yield instance
    instance.close()


@pytest.fixture
def photos_dir(tmp_path):
    d = tmp_path / "photos"
    d.mkdir()
    return d


# ─── payload validation ─────────────────────────────────────────────────────

def test_share_import_rejects_non_dict_payload(db):
    out = share_import(db, [])
    assert out == {"ok": False, "error": "invalid payload"}


def test_share_import_rejects_wrong_version(db):
    out = share_import(db, {"version": 99, "type": "share"})
    assert out == {"ok": False, "error": "invalid payload"}


def test_share_import_rejects_unknown_type(db):
    out = share_import(db, {"version": 1, "type": "garbage"})
    assert out["ok"] is False


def test_share_import_rejects_missing_vin_hash(db):
    payload = {"version": 1, "type": "share", "vehicle": {"label": "Foo"}}
    out = share_import(db, payload)
    assert out["ok"] is False
    assert "vin_hash" in out["error"]


def test_share_import_rejects_non_dict_vehicle(db):
    out = share_import(db, {"version": 1, "type": "share", "vehicle": "bad"})
    assert out == {"ok": False, "error": "invalid vehicle"}


def test_share_import_ignores_non_list_content_fields(db):
    payload = _share("VIN-BAD-LISTS")
    payload["trips"] = {"started_at": "2026-05-24T10:00:00+00:00"}
    payload["stopwatch_runs"] = {"run_at": "2026-05-24T11:00:00+00:00"}
    payload["scans"] = {"scanned_at": "2026-05-24T12:00:00+00:00"}
    payload["photos"] = {"taken_at": "2026-05-24T13:00:00+00:00"}

    out = share_import(db, payload)

    assert out["ok"] is True
    assert out["trips_added"] == 0
    assert out["runs_added"] == 0
    assert out["scans_added"] == 0
    assert out["photos_added"] == 0


# ─── share-tours sub-protocol ───────────────────────────────────────────────

def test_share_import_tours_adds_new_tours(db):
    payload = {
        "version": 1,
        "type": "share_tours",
        "tours": [
            {"name": "Sonntagstour", "waypoints_json": "[]", "created_at": "2026-05-01T00:00:00+00:00"},
            {"name": "Berlin-Tour", "waypoints_json": "[]", "created_at": "2026-05-10T00:00:00+00:00"},
        ],
    }
    out = share_import(db, payload)
    assert out == {"ok": True, "tours_added": 2}
    names = {t["name"] for t in db.list_saved_tours()}
    assert names == {"Sonntagstour", "Berlin-Tour"}


def test_share_import_tours_skips_duplicates_by_name(db):
    db.save_tour("Sonntagstour", "[]", "2026-04-01T00:00:00+00:00")
    payload = {
        "version": 1,
        "type": "share_tours",
        "tours": [
            {"name": "Sonntagstour", "waypoints_json": "[]"},
            {"name": "Neue Tour", "waypoints_json": "[]"},
        ],
    }
    out = share_import(db, payload)
    assert out["tours_added"] == 1
    assert {t["name"] for t in db.list_saved_tours()} == {"Sonntagstour", "Neue Tour"}


def test_share_import_tours_skips_entries_without_name(db):
    payload = {
        "version": 1,
        "type": "share_tours",
        "tours": [
            {"name": "", "waypoints_json": "[]"},
            {"name": None, "waypoints_json": "[]"},
            "not a dict",
            {"name": "Gültig"},
        ],
    }
    out = share_import(db, payload)
    assert out["tours_added"] == 1


# ─── share (trips/runs/scans/photos) — car creation ─────────────────────────

def _share(vin: str, *, trips=None, runs=None, scans=None, photos=None, label="Shared Car"):
    return {
        "version": 1,
        "type": "share",
        "vehicle": {
            "vin_hash": _vin_hash(vin),
            "vin": vin,
            "label": label,
            "brand": "Audi",
            "protocol": "CAN",
        },
        "trips": trips or [],
        "stopwatch_runs": runs or [],
        "scans": scans or [],
        "photos": photos or [],
    }


def test_share_import_creates_car_on_first_share(db):
    out = share_import(db, _share("VIN-SHARE-1"))
    assert out["ok"] is True
    car = db.get_car_by_vin_hash(_vin_hash("VIN-SHARE-1"))
    assert car is not None
    assert car["label"] == "Shared Car"
    assert car["brand"] == "Audi"


def test_share_import_uses_existing_car_when_vin_hash_matches(db):
    db.upsert_car(vin="VIN-EXISTING")
    share_import(db, _share("VIN-EXISTING"))
    cars = db.list_cars()
    # Still one car total — the share landed on the existing row.
    assert len([c for c in cars if c["vin"] == "VIN-EXISTING"]) == 1


# ─── trips: insert, dedupe, conflict ────────────────────────────────────────

def test_share_import_inserts_new_trip(db):
    payload = _share("VIN-TRIP", trips=[{
        "started_at": "2026-05-24T10:00:00+00:00",
        "ended_at":   "2026-05-24T10:30:00+00:00",
        "distance_km": 25.0,
        "duration_s": 1800,
        "max_speed_kmh": 80.0,
        "avg_speed_kmh": 50.0,
        "samples_count": 2,
        "samples": [
            {"ts": 1.0, "speed_kmh": 50.0},
            {"ts": 2.0, "speed_kmh": 80.0},
        ],
    }])
    out = share_import(db, payload)
    assert out["trips_added"] == 1
    cid = db.get_car_by_vin_hash(_vin_hash("VIN-TRIP"))["id"]
    trips = db.list_trips_for_car(cid)
    assert len(trips) == 1
    assert trips[0]["distance_km"] == 25.0
    # samples_count is recomputed from actual inserts (2), not the claim.
    assert trips[0]["samples_count"] == 2


def test_share_import_skips_identical_trip_silently(db):
    # The stored row's samples_count gets recomputed from the actual samples
    # delivered, so the claimed count must match the number of sample rows
    # we send or the second import sees them as different and conflicts.
    payload = _share("VIN-DUP", trips=[{
        "started_at": "2026-05-24T10:00:00+00:00",
        "distance_km": 12.34,
        "duration_s": 600,
        "max_speed_kmh": 100.0,
        "avg_speed_kmh": 50.5,
        "samples_count": 2,
        "samples": [
            {"ts": 1.0, "speed_kmh": 50.0},
            {"ts": 2.0, "speed_kmh": 60.0},
        ],
    }])
    share_import(db, payload)
    # Re-share the same trip — no new row, no conflict.
    out = share_import(db, payload)
    assert out["trips_added"] == 0
    assert out["conflicts"] == 0


def test_share_import_records_conflict_when_trip_differs(db):
    base_trip = {
        "started_at": "2026-05-24T10:00:00+00:00",
        "distance_km": 12.34,
        "duration_s": 600,
        "max_speed_kmh": 100.0,
        "avg_speed_kmh": 50.5,
        "samples_count": 10,
    }
    share_import(db, _share("VIN-DIFF", trips=[base_trip]))
    drifted = dict(base_trip, distance_km=99.9)
    out = share_import(db, _share("VIN-DIFF", trips=[drifted]))
    assert out["conflicts"] == 1
    assert out["trips_added"] == 0
    assert db.count_share_conflicts() == 1
    conflict = db.list_share_conflicts()[0]
    assert conflict["type"] == "trip"
    incoming = json.loads(conflict["incoming_json"])
    assert incoming["distance_km"] == 99.9


def test_share_import_skips_trip_without_started_at(db):
    out = share_import(db, _share("VIN-NOSTART", trips=[
        {"started_at": None, "distance_km": 1.0},
        {"started_at": "", "distance_km": 1.0},
        {"started_at": "2026-05-24T10:00:00+00:00", "distance_km": 1.0},
    ]))
    assert out["trips_added"] == 1


# ─── stopwatch runs: insert, dedupe, conflict ───────────────────────────────

def test_share_import_inserts_stopwatch_run(db):
    payload = _share("VIN-RUN", runs=[{
        "run_at": "2026-05-24T11:00:00+00:00",
        "lat": 50.0,
        "lon": 8.0,
        "results": {"target_kmh": 100, "elapsed_s": 8.2},
        "samples": [{"ts": 0, "speed": 0}],
    }])
    out = share_import(db, payload)
    assert out["runs_added"] == 1
    cid = db.get_car_by_vin_hash(_vin_hash("VIN-RUN"))["id"]
    runs = db.list_stopwatch_runs_for_car(cid)
    assert len(runs) == 1


def test_share_import_records_run_conflict_when_results_differ(db):
    base_run = {
        "run_at": "2026-05-24T11:00:00+00:00",
        "lat": 50.0, "lon": 8.0,
        "results": {"target_kmh": 100, "elapsed_s": 8.2},
        "samples": [{"ts": 0, "speed": 0}],
    }
    share_import(db, _share("VIN-RUN2", runs=[base_run]))
    drifted = dict(base_run, results={"target_kmh": 100, "elapsed_s": 7.5})
    out = share_import(db, _share("VIN-RUN2", runs=[drifted]))
    assert out["conflicts"] == 1
    assert out["runs_added"] == 0


# ─── scans: insert, dedupe, conflict ────────────────────────────────────────

def test_share_import_inserts_scan(db):
    payload = _share("VIN-SCAN", scans=[{
        "scanned_at": "2026-05-24T12:00:00+00:00",
        "protocol": "CAN",
        "dtc_count": 1,
        "pending_dtc_count": 0,
        "pids_count": 5,
        "data_json": json.dumps({"dtcs": ["P0420"], "live_data": {}}),
    }])
    out = share_import(db, payload)
    assert out["scans_added"] == 1
    cid = db.get_car_by_vin_hash(_vin_hash("VIN-SCAN"))["id"]
    scans = db.list_scans_for_car(cid)
    assert len(scans) == 1
    assert scans[0]["dtc_count"] == 1


def test_share_import_skips_scan_with_non_string_data_json(db):
    payload = _share("VIN-SCANBAD", scans=[{
        "scanned_at": "2026-05-24T12:00:00+00:00",
        "data_json": {"dtcs": ["P0420"]},
    }])

    out = share_import(db, payload)

    assert out["scans_added"] == 0


def test_share_import_records_scan_conflict_when_data_differs(db):
    base = {
        "scanned_at": "2026-05-24T12:00:00+00:00",
        "protocol": "CAN",
        "data_json": json.dumps({"dtcs": ["P0420"]}),
    }
    share_import(db, _share("VIN-SCAN2", scans=[base]))
    drifted = dict(base, data_json=json.dumps({"dtcs": ["P0420", "P0301"]}))
    out = share_import(db, _share("VIN-SCAN2", scans=[drifted]))
    assert out["conflicts"] == 1
    assert out["scans_added"] == 0


# ─── photos: decode, write to disk, dedupe ──────────────────────────────────

def test_share_import_writes_photo_to_disk_and_db(db, photos_dir):
    photo_bytes = b"\xff\xd8\xff" + b"\x00" * 100  # minimal-ish JPEG-like
    payload = _share("VIN-PHOTO", photos=[{
        "taken_at": "2026-05-24T13:00:00+00:00",
        "filename": "shot.jpg",
        "data_b64": base64.b64encode(photo_bytes).decode("ascii"),
    }])
    out = share_import(db, payload, photos_dir=photos_dir)
    assert out["photos_added"] == 1
    cid = db.get_car_by_vin_hash(_vin_hash("VIN-PHOTO"))["id"]
    rows = db.list_photos_for_car(cid)
    assert len(rows) == 1
    # File ended up under photos_dir/<car_id>/<uuid>.jpg
    written = list((photos_dir / str(cid)).iterdir())
    assert len(written) == 1
    assert written[0].suffix == ".jpg"
    assert written[0].read_bytes() == photo_bytes


def test_share_import_skips_photo_when_taken_at_already_present(db, photos_dir):
    payload = _share("VIN-PDUP", photos=[{
        "taken_at": "2026-05-24T13:00:00+00:00",
        "filename": "x.jpg",
        "data_b64": base64.b64encode(b"abc").decode("ascii"),
    }])
    share_import(db, payload, photos_dir=photos_dir)
    out = share_import(db, payload, photos_dir=photos_dir)
    # Same taken_at → conflicts++ (not photos_added++).
    assert out["photos_added"] == 0
    assert out["conflicts"] == 1


def test_share_import_skips_photo_with_invalid_base64(db, photos_dir):
    payload = _share("VIN-PBAD", photos=[{
        "taken_at": "2026-05-24T13:00:00+00:00",
        "filename": "x.jpg",
        "data_b64": "not valid base64 !!!",
    }])
    out = share_import(db, payload, photos_dir=photos_dir)
    assert out["photos_added"] == 0
    # Nothing written under photos_dir/<car_id>/ either.
    cid = db.get_car_by_vin_hash(_vin_hash("VIN-PBAD"))["id"]
    car_photo_dir = photos_dir / str(cid)
    assert not car_photo_dir.exists() or list(car_photo_dir.iterdir()) == []


def test_share_import_rejects_whitespace_tolerant_base64(db, photos_dir):
    payload = _share("VIN-PSTRICT", photos=[{
        "taken_at": "2026-05-24T13:00:00+00:00",
        "filename": "x.jpg",
        "data_b64": "YW Jj",
    }])

    out = share_import(db, payload, photos_dir=photos_dir)

    assert out["photos_added"] == 0


def test_share_import_skips_photo_without_taken_at_or_data(db, photos_dir):
    payload = _share("VIN-PSKIP", photos=[
        {"taken_at": None, "data_b64": "abc"},
        {"taken_at": "2026-05-24T13:00:00+00:00", "data_b64": None},
        {"taken_at": "", "data_b64": "abc"},
    ])
    out = share_import(db, payload, photos_dir=photos_dir)
    assert out["photos_added"] == 0
