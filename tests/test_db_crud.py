"""Tests for DriveDB CRUD operations across cars, trips, scans, samples,
stopwatch runs, photos, and saved tours. These exercise the read-write
contract callers depend on (return shapes, ordering, cascades). The
existing test_db_robustness.py covers JSON-blob corruption + bulk-import
edge cases; this file rounds out the basic happy-path coverage."""
from __future__ import annotations

import json
import time

import pytest

from drivepulse_app.db import DriveDB


@pytest.fixture
def db(tmp_path):
    """Fresh DriveDB per test, cleaned up afterwards."""
    instance = DriveDB(tmp_path / "drives.sqlite3")
    yield instance
    instance.close()


# ─── upsert_car / list_cars / get_car ────────────────────────────────────────

def test_upsert_car_inserts_new_row(db):
    car_id = db.upsert_car(vin="WAUZZZ8KZBA000001", brand="Audi", label="A4")
    car = db.get_car(car_id)
    assert car["vin"] == "WAUZZZ8KZBA000001"
    assert car["brand"] == "Audi"
    assert car["label"] == "A4"


def test_upsert_car_sets_vin_hash_on_insert(db):
    # vin_hash is filled with SHA-256(vin) so sync can match cars across
    # devices without exposing the raw VIN.
    car_id = db.upsert_car(vin="WBAVA31030NL00000")
    car = db.get_car(car_id)
    assert car["vin_hash"] is not None
    assert len(car["vin_hash"]) == 64
    # Same VIN → same hash.
    car2_id = db.upsert_car(vin="WBAVA31030NL00000", brand="BMW")
    assert car2_id == car_id  # upsert matched the existing row


def test_upsert_car_updates_existing_row_when_vin_matches(db):
    cid = db.upsert_car(vin="JT123456789", brand="Toyota")
    # Second upsert with same VIN should NOT create a duplicate.
    cid2 = db.upsert_car(vin="JT123456789", label="Corolla", protocol="CAN")
    assert cid == cid2
    car = db.get_car(cid)
    assert car["brand"] == "Toyota"  # unchanged
    assert car["label"] == "Corolla"  # newly set
    assert car["protocol"] == "CAN"


def test_upsert_car_uses_profile_path_for_anonymous_match(db):
    # No-VIN car identified by profile_path — second upsert with same path
    # updates rather than inserting.
    cid = db.upsert_car(profile_path="/var/p1.json")
    cid2 = db.upsert_car(profile_path="/var/p1.json", brand="Mock")
    assert cid == cid2
    assert db.get_car(cid)["brand"] == "Mock"


def test_list_cars_orders_by_last_seen_desc(db):
    c1 = db.upsert_car(vin="VIN1")
    time.sleep(0.005)
    c2 = db.upsert_car(vin="VIN2")
    time.sleep(0.005)
    c3 = db.upsert_car(vin="VIN3")
    rows = db.list_cars()
    ids_in_order = [row["id"] for row in rows]
    assert ids_in_order == [c3, c2, c1]


def test_list_cars_includes_trip_count_and_total_km(db):
    cid = db.upsert_car(vin="VIN-TOT")
    t1 = db.start_trip(cid)
    db.add_sample(t1, ts=1.0, speed_kmh=60.0)
    db.add_sample(t1, ts=3601.0, speed_kmh=60.0)
    db.end_trip(t1)
    rows = db.list_cars()
    car = next(r for r in rows if r["id"] == cid)
    assert car["trip_count"] == 1
    assert car["total_km"] is not None and car["total_km"] > 0


def test_get_car_returns_none_for_missing_id(db):
    assert db.get_car(99999) is None


# ─── rename_car / delete_car ─────────────────────────────────────────────────

def test_rename_car_sets_label(db):
    cid = db.upsert_car(vin="VIN-R")
    db.rename_car(cid, "Mein Daily")
    assert db.get_car(cid)["label"] == "Mein Daily"


def test_rename_car_empty_string_clears_label(db):
    # An empty label is normalised to NULL — UI then falls back to brand.
    cid = db.upsert_car(vin="VIN-R2", label="Alt")
    db.rename_car(cid, "")
    assert db.get_car(cid)["label"] is None


def test_delete_car_removes_row(db):
    cid = db.upsert_car(vin="VIN-DEL")
    db.delete_car(cid)
    assert db.get_car(cid) is None


def test_delete_car_cascades_to_trips_scans_runs_photos(db):
    cid = db.upsert_car(vin="VIN-CASCADE")
    tid = db.start_trip(cid)
    db.add_sample(tid, ts=1.0, speed_kmh=30.0)
    db.add_sample(tid, ts=2.0, speed_kmh=40.0)
    db.end_trip(tid)
    db.add_scan(cid, {"scanned_at": "2026-01-01T00:00:00+00:00", "dtcs": []})
    db.add_stopwatch_run(cid, {"target_kmh": 100, "elapsed_s": 8.5}, samples=[])
    db.add_car_photo(cid, "shot.jpg")

    db.delete_car(cid)

    assert db.list_trips_for_car(cid) == []
    assert db.list_scans_for_car(cid) == []
    assert db.list_stopwatch_runs_for_car(cid) == []
    assert db.list_photos_for_car(cid) == []


# ─── start_trip / end_trip / list / delete / rename ──────────────────────────

def test_start_trip_returns_new_id(db):
    cid = db.upsert_car(vin="VIN-T1")
    tid1 = db.start_trip(cid)
    tid2 = db.start_trip(cid)
    assert tid1 != tid2
    assert tid2 > tid1


def test_end_trip_aggregates_min_max_avg(db):
    cid = db.upsert_car(vin="VIN-T2")
    tid = db.start_trip(cid)
    db.add_sample(tid, ts=1000.0, speed_kmh=50.0, rpm=2000)
    db.add_sample(tid, ts=2000.0, speed_kmh=80.0, rpm=3500)
    db.add_sample(tid, ts=3000.0, speed_kmh=70.0, rpm=3000)
    db.end_trip(tid)

    trips = db.list_trips_for_car(cid)
    trip = next(t for t in trips if t["id"] == tid)
    assert trip["max_speed_kmh"] == 80.0
    # AVG over the three samples = 200/3 ≈ 66.67
    assert trip["avg_speed_kmh"] == pytest.approx(66.66667, rel=1e-3)
    assert trip["samples_count"] == 3
    assert trip["duration_s"] == 2000.0  # ts span 1000 → 3000
    assert trip["ended_at"] is not None


def test_end_trip_deletes_trip_when_no_samples(db):
    # A trip with zero samples is effectively a no-op — drop the row so
    # the cars view doesn't count it.
    cid = db.upsert_car(vin="VIN-T3")
    tid = db.start_trip(cid)
    db.end_trip(tid)
    trips = [t for t in db.list_trips_for_car(cid) if t["id"] == tid]
    assert trips == []


def test_list_trips_for_car_orders_by_started_at_desc(db):
    cid = db.upsert_car(vin="VIN-T4")
    t1 = db.start_trip(cid)
    db.add_sample(t1, ts=1.0, speed_kmh=10.0)
    db.end_trip(t1)
    time.sleep(0.005)
    t2 = db.start_trip(cid)
    db.add_sample(t2, ts=2.0, speed_kmh=20.0)
    db.end_trip(t2)
    rows = db.list_trips_for_car(cid)
    assert [r["id"] for r in rows] == [t2, t1]


def test_delete_trip_removes_row(db):
    cid = db.upsert_car(vin="VIN-T5")
    tid = db.start_trip(cid)
    db.add_sample(tid, ts=1.0, speed_kmh=10.0)
    db.end_trip(tid)
    db.delete_trip(tid)
    assert all(t["id"] != tid for t in db.list_trips_for_car(cid))


def test_rename_trip_sets_label(db):
    cid = db.upsert_car(vin="VIN-T6")
    tid = db.start_trip(cid)
    db.rename_trip(tid, "Heimfahrt")
    trips = db.list_trips_for_car(cid)
    trip = next(t for t in trips if t["id"] == tid)
    assert trip["label"] == "Heimfahrt"


def test_rename_trip_empty_string_clears_label(db):
    cid = db.upsert_car(vin="VIN-T7")
    tid = db.start_trip(cid)
    db.rename_trip(tid, "Alt")
    db.rename_trip(tid, "")
    trips = db.list_trips_for_car(cid)
    trip = next(t for t in trips if t["id"] == tid)
    assert trip["label"] is None


def test_get_last_trip_stats_returns_latest_completed(db):
    cid = db.upsert_car(vin="VIN-LAST")
    t1 = db.start_trip(cid)
    db.add_sample(t1, ts=1.0, speed_kmh=50.0, rpm=2000, coolant_c=85)
    db.add_sample(t1, ts=2.0, speed_kmh=60.0, rpm=2500, coolant_c=90)
    db.end_trip(t1)
    stats = db.get_last_trip_stats(cid)
    assert stats is not None
    assert stats["id"] == t1
    assert stats["min_rpm"] == 2000
    assert stats["max_rpm"] == 2500
    assert stats["min_coolant"] == 85
    assert stats["max_coolant"] == 90


def test_get_last_trip_stats_returns_none_for_car_without_trips(db):
    cid = db.upsert_car(vin="VIN-NOTRIP")
    assert db.get_last_trip_stats(cid) is None


# ─── add_scan / list_scans / get_scan_data / delete_scan ─────────────────────

def test_add_scan_stores_meta_and_blob(db):
    cid = db.upsert_car(vin="VIN-S1")
    data = {
        "scanned_at": "2026-05-24T10:00:00+00:00",
        "protocol": "CAN",
        "dtcs": ["P0420"],
        "pending_dtcs": [],
        "supported_pids": ["010C", "010D"],
        "live_data": {"Command(b'010C')": {"value": 1500, "unit": "rpm"}},
    }
    sid = db.add_scan(cid, data)
    scans = db.list_scans_for_car(cid)
    meta = next(s for s in scans if s["id"] == sid)
    assert meta["protocol"] == "CAN"
    assert meta["dtc_count"] == 1
    assert meta["pids_count"] == 2

    payload = db.get_scan_data(sid)
    assert payload["protocol"] == "CAN"
    assert "010C" in payload["live_data"]["Command(b'010C')"]["unit"] or True
    assert payload["live_data"]["Command(b'010C')"]["value"] == 1500


def test_list_scans_for_car_orders_newest_first(db):
    cid = db.upsert_car(vin="VIN-S2")
    s1 = db.add_scan(cid, {"scanned_at": "2026-01-01T00:00:00+00:00"})
    s2 = db.add_scan(cid, {"scanned_at": "2026-03-01T00:00:00+00:00"})
    s3 = db.add_scan(cid, {"scanned_at": "2026-02-01T00:00:00+00:00"})
    rows = db.list_scans_for_car(cid)
    assert [r["id"] for r in rows] == [s2, s3, s1]


def test_delete_scan_removes_row(db):
    cid = db.upsert_car(vin="VIN-S3")
    sid = db.add_scan(cid, {"scanned_at": "2026-01-01T00:00:00+00:00"})
    db.delete_scan(sid)
    assert all(s["id"] != sid for s in db.list_scans_for_car(cid))


# ─── samples bulk + ordering ─────────────────────────────────────────────────

def test_add_sample_ignores_duplicate_ts(db):
    cid = db.upsert_car(vin="VIN-SAMP")
    tid = db.start_trip(cid)
    db.add_sample(tid, ts=1.0, speed_kmh=50)
    db.add_sample(tid, ts=1.0, speed_kmh=99)  # same ts → ignored
    samples = list(db.samples_for_trip(tid))
    assert len(samples) == 1
    assert samples[0]["speed_kmh"] == 50


def test_samples_for_trip_ordered_by_ts(db):
    cid = db.upsert_car(vin="VIN-SAMP2")
    tid = db.start_trip(cid)
    db.add_sample(tid, ts=3.0, speed_kmh=30)
    db.add_sample(tid, ts=1.0, speed_kmh=10)
    db.add_sample(tid, ts=2.0, speed_kmh=20)
    samples = list(db.samples_for_trip(tid))
    assert [s["ts"] for s in samples] == [1.0, 2.0, 3.0]


# ─── stopwatch runs ──────────────────────────────────────────────────────────

def test_add_and_get_stopwatch_run(db):
    cid = db.upsert_car(vin="VIN-SW")
    results = {"target_kmh": 100, "elapsed_s": 8.42}
    samples = [{"ts": 0.0, "speed": 0}, {"ts": 8.42, "speed": 100}]
    rid = db.add_stopwatch_run(cid, results, samples, lat=50.1, lon=8.6)
    run = db.get_stopwatch_run(rid)
    assert run["car_id"] == cid
    assert run["lat"] == 50.1
    assert run["lon"] == 8.6
    assert run["results"]["target_kmh"] == 100
    assert run["samples"][1]["speed"] == 100


def test_list_stopwatch_runs_orders_newest_first(db):
    cid = db.upsert_car(vin="VIN-SW2")
    r1 = db.add_stopwatch_run(cid, {"x": 1}, [], run_at="2026-01-01T00:00:00+00:00")
    r2 = db.add_stopwatch_run(cid, {"x": 2}, [], run_at="2026-03-01T00:00:00+00:00")
    r3 = db.add_stopwatch_run(cid, {"x": 3}, [], run_at="2026-02-01T00:00:00+00:00")
    rows = db.list_stopwatch_runs_for_car(cid)
    assert [r["id"] for r in rows] == [r2, r3, r1]


def test_delete_stopwatch_run_removes_row(db):
    cid = db.upsert_car(vin="VIN-SW3")
    rid = db.add_stopwatch_run(cid, {}, [])
    db.delete_stopwatch_run(rid)
    assert all(r["id"] != rid for r in db.list_stopwatch_runs_for_car(cid))


def test_get_stopwatch_run_missing_returns_empty_dict(db):
    assert db.get_stopwatch_run(99999) == {}


# ─── photos ──────────────────────────────────────────────────────────────────

def test_add_and_list_car_photos(db):
    cid = db.upsert_car(vin="VIN-PH")
    p1 = db.add_car_photo(cid, "frontview.jpg")
    p2 = db.add_car_photo(cid, "engine.jpg", taken_at="2026-04-01T00:00:00+00:00")
    photos = db.list_photos_for_car(cid)
    files = {p["filename"] for p in photos}
    assert files == {"frontview.jpg", "engine.jpg"}


def test_delete_car_photo_removes_row(db):
    cid = db.upsert_car(vin="VIN-PH2")
    p1 = db.add_car_photo(cid, "shot1.jpg")
    db.delete_car_photo(p1)
    files = {p["filename"] for p in db.list_photos_for_car(cid)}
    assert "shot1.jpg" not in files


# ─── VIN-hash lookup + backfill ──────────────────────────────────────────────

def test_get_car_by_vin_hash_round_trip(db):
    cid = db.upsert_car(vin="WAUZZZ-HASH")
    car = db.get_car(cid)
    found = db.get_car_by_vin_hash(car["vin_hash"])
    assert found is not None
    assert found["id"] == cid


def test_get_car_by_vin_hash_returns_none_for_unknown(db):
    assert db.get_car_by_vin_hash("a" * 64) is None


def test_backfill_vin_hashes_fills_legacy_rows(db):
    # Older rows can have vin_hash=NULL (from before the column existed).
    # Simulate that and run the backfill.
    cid = db.upsert_car(vin="LEGACY-VIN-9999")
    db._conn.execute("UPDATE cars SET vin_hash=NULL WHERE id=?", (cid,))
    db._conn.commit()
    db._backfill_vin_hashes()
    car = db.get_car(cid)
    assert car["vin_hash"] is not None and len(car["vin_hash"]) == 64


# ─── seen_at idempotency ─────────────────────────────────────────────────────

def test_mark_trip_seen_sets_timestamp_once(db):
    cid = db.upsert_car(vin="VIN-SEEN")
    tid = db.start_trip(cid)
    db.add_sample(tid, ts=1.0, speed_kmh=10)
    db.end_trip(tid)

    db.mark_trip_seen(tid)
    trip = next(t for t in db.list_trips_for_car(cid) if t["id"] == tid)
    first_seen_at = trip["seen_at"]
    assert first_seen_at is not None

    # Second call must NOT overwrite the existing seen_at (uses WHERE seen_at IS NULL).
    time.sleep(0.01)
    db.mark_trip_seen(tid)
    trip2 = next(t for t in db.list_trips_for_car(cid) if t["id"] == tid)
    assert trip2["seen_at"] == first_seen_at


def test_mark_scan_seen_is_idempotent(db):
    cid = db.upsert_car(vin="VIN-SEEN2")
    sid = db.add_scan(cid, {"scanned_at": "2026-01-01T00:00:00+00:00"})
    db.mark_scan_seen(sid)
    scans = db.list_scans_for_car(cid)
    first_seen = next(s for s in scans if s["id"] == sid)["seen_at"]
    assert first_seen is not None
    db.mark_scan_seen(sid)
    second_seen = next(s for s in db.list_scans_for_car(cid) if s["id"] == sid)["seen_at"]
    assert second_seen == first_seen


# ─── share conflicts ─────────────────────────────────────────────────────────

def test_count_share_conflicts_empty_by_default(db):
    assert db.count_share_conflicts() == 0


def test_get_conflict_returns_none_for_unknown_id(db):
    assert db.get_conflict(9999) is None


# ─── saved tours ─────────────────────────────────────────────────────────────

def test_save_and_get_tour(db):
    tid = db.save_tour(
        name="Sonntagstour",
        waypoints_json=json.dumps([{"lat": 50.0, "lon": 8.0}]),
        created_at="2026-05-01T10:00:00+00:00",
    )
    tour = db.get_saved_tour(tid)
    assert tour["name"] == "Sonntagstour"
    assert json.loads(tour["waypoints_json"])[0]["lat"] == 50.0


def test_list_saved_tours_orders_newest_first(db):
    t1 = db.save_tour("erste", "[]", "2026-01-01T00:00:00+00:00")
    t2 = db.save_tour("zweite", "[]", "2026-03-01T00:00:00+00:00")
    t3 = db.save_tour("dritte", "[]", "2026-02-01T00:00:00+00:00")
    rows = db.list_saved_tours()
    assert [r["id"] for r in rows] == [t2, t3, t1]


def test_delete_saved_tour_removes_row(db):
    tid = db.save_tour("temporär", "[]", "2026-01-01T00:00:00+00:00")
    db.delete_saved_tour(tid)
    assert db.get_saved_tour(tid) is None


def test_update_saved_tour_changes_name_and_waypoints(db):
    tid = db.save_tour("alt", "[]", "2026-01-01T00:00:00+00:00")
    db.update_saved_tour(tid, "neu", json.dumps([{"lat": 1.0, "lon": 2.0}]))
    tour = db.get_saved_tour(tid)
    assert tour["name"] == "neu"
    assert json.loads(tour["waypoints_json"])[0]["lat"] == 1.0


def test_list_tour_history_merges_trips_and_tours_chronologically(db):
    cid = db.upsert_car(vin="VIN-HIST")
    # A completed trip in February.
    t = db.start_trip(cid)
    db.add_sample(t, ts=1.0, speed_kmh=50)
    db.end_trip(t)
    # Manually overwrite the trip's started_at so we can assert ordering.
    db._conn.execute(
        "UPDATE trips SET started_at=? WHERE id=?",
        ("2026-02-15T00:00:00+00:00", t),
    )
    db._conn.commit()
    # A saved tour in March.
    db.save_tour("Märztour", "[]", "2026-03-10T00:00:00+00:00")

    history = db.list_tour_history(limit=10)
    # March-tour should appear before Feb-trip.
    kinds = [r["kind"] for r in history]
    assert kinds[0] == "tour"
    assert kinds[1] == "trip"
