"""Tests for DriveDB.merge_scans — the multi-scan merge into the earliest
scan, used when the user records multiple short scans around a fuelling
stop or similar break. Verifies adjacency check, sample concatenation,
zero-fill across gaps, DTC union and metadata recomputation."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from drivepulse_app.db import DriveDB


@pytest.fixture
def db(tmp_path):
    instance = DriveDB(tmp_path / "drives.sqlite3")
    yield instance
    instance.close()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _add_scan(db, car_id, when, samples=None, dtcs=None, pending=None):
    data = {
        "scanned_at": _iso(when),
        "protocol": "CAN",
        "dtcs": dtcs or [],
        "pending_dtcs": pending or [],
        "supported_pids": ["010C", "010D"],
    }
    sid = db.add_scan(car_id, data)
    if samples:
        db.add_scan_samples(sid, samples)
    return sid


def test_merge_rejects_single_scan(db):
    car = db.upsert_car(vin="VINMERGE1")
    s = _add_scan(db, car, datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    with pytest.raises(ValueError, match="too_few"):
        db.merge_scans([s])


def test_merge_rejects_different_cars(db):
    a = db.upsert_car(vin="VINMERGEA")
    b = db.upsert_car(vin="VINMERGEB")
    sa = _add_scan(db, a, datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    sb = _add_scan(db, b, datetime(2026, 1, 1, 12, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="different_cars"):
        db.merge_scans([sa, sb])


def test_merge_rejects_large_gap(db):
    # Reject when the gap exceeds SCAN_MERGE_MAX_GAP_S (default 30 min).
    car = db.upsert_car(vin="VINMERGEG")
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    s1 = _add_scan(db, car, t0)
    s2 = _add_scan(db, car, t0 + timedelta(minutes=45))  # over 30-min threshold
    with pytest.raises(ValueError, match="gap_too_large"):
        db.merge_scans([s1, s2])


def test_merge_keeps_earliest_as_survivor_and_deletes_others(db):
    car = db.upsert_car(vin="VINMERGEK")
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    s_old = _add_scan(db, car, t0)
    s_new = _add_scan(db, car, t0 + timedelta(minutes=10))
    survivor = db.merge_scans([s_new, s_old])  # order should not matter
    assert survivor == s_old
    # Loser must be gone.
    assert db.get_scan_data(s_new) == {}


def test_merge_concatenates_samples(db):
    car = db.upsert_car(vin="VINMERGES")
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    ts0 = t0.timestamp()
    s1 = _add_scan(db, car, t0, samples=[
        {"ts": ts0 + 0, "pid": "010C", "value": 800.0, "unit": "rpm"},
        {"ts": ts0 + 1, "pid": "010C", "value": 810.0, "unit": "rpm"},
    ])
    t1 = t0 + timedelta(minutes=10)
    ts1 = t1.timestamp()
    s2 = _add_scan(db, car, t1, samples=[
        {"ts": ts1 + 0, "pid": "010C", "value": 900.0, "unit": "rpm"},
        {"ts": ts1 + 1, "pid": "010C", "value": 910.0, "unit": "rpm"},
    ])
    survivor = db.merge_scans([s1, s2])
    rows = db.get_scan_samples(survivor, "010C")
    values = [r["value"] for r in rows]
    # All four real samples are present.
    assert 800.0 in values and 810.0 in values
    assert 900.0 in values and 910.0 in values
    # The 10-minute gap is filled — many zero rows show up between the
    # two real segments.
    zero_count = sum(1 for v in values if v == 0.0)
    assert zero_count > 100, f"expected lots of zero-fill, got {zero_count}"


def test_merge_unions_dtcs(db):
    car = db.upsert_car(vin="VINMERGED")
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    s1 = _add_scan(db, car, t0, dtcs=[{"code": "P0420", "description": "Cat"}])
    s2 = _add_scan(
        db, car, t0 + timedelta(minutes=5),
        dtcs=[{"code": "P0420", "description": "Cat"}, {"code": "P0171", "description": "Lean"}],
        pending=[{"code": "U0100", "description": ""}],
    )
    survivor = db.merge_scans([s1, s2])
    data = db.get_scan_data(survivor)
    codes = sorted(d["code"] for d in data["dtcs"])
    assert codes == ["P0171", "P0420"]  # de-duplicated union
    assert [d["code"] for d in data["pending_dtcs"]] == ["U0100"]
