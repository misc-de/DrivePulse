"""Tests for ScanChartContent._car_has_unused_scans — the gate that
decides whether a car is offered in the „Compare car" dropdown.

The original requirement: the user can add the SAME car multiple times
as a compare entry so they can pick different scan histories — but only
if there are scans left that aren't already shown. Once every scan of a
car is loaded (main + compare entries), the car drops out of the
dropdown to avoid empty compare entries (and the crash that followed
from those previously).

We can't instantiate ScanChartContent itself without GTK, so the tests
bind the unbound mixin-style methods to a SimpleNamespace fake that
carries only the attributes the methods actually touch."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from drivepulse_app.chart.scan_chart import ScanChartContent
from drivepulse_app.db import DriveDB


@pytest.fixture
def db(tmp_path):
    instance = DriveDB(tmp_path / "drives.sqlite3")
    yield instance
    instance.close()


def _add_scan(db, car_id, ts: str, pids: int = 1):
    data = {
        "scanned_at": ts,
        "protocol": "CAN",
        "supported_pids": [f"01{i:02X}" for i in range(pids)],
    }
    return db.add_scan(car_id, data)


def _fake(db, main_car_id: int, main_scan_ts: str | None,
          compare_cars: list[dict] | None = None) -> SimpleNamespace:
    """Hand-rolled stand-in for ScanChartContent that exposes just the
    attributes _car_has_unused_scans / _comparison_scan_ts_for_car
    touch. Methods are bound via lambdas pointing at the real impls."""
    fake = SimpleNamespace(
        _db=db,
        _main_car_id=main_car_id,
        _main_scan_ts=main_scan_ts,
        _compare_cars=list(compare_cars or []),
    )
    fake._comparison_scan_ts_for_car = (
        lambda cid, exclude_entry=None:
        ScanChartContent._comparison_scan_ts_for_car(fake, cid, exclude_entry)
    )
    fake._car_has_unused_scans = (
        lambda cid: ScanChartContent._car_has_unused_scans(fake, cid)
    )
    return fake


def test_filter_hides_car_when_only_scan_is_main(db):
    # Reproduces the user's reported crash scenario: a car with exactly
    # one scan, already loaded as the main scan. The dropdown must not
    # offer this car for compare — there's nothing useful to add.
    car = db.upsert_car(vin="VIN-ONE-SCAN")
    _add_scan(db, car, "2026-01-01T10:00:00")
    fake = _fake(db, main_car_id=car, main_scan_ts="2026-01-01T10:00:00")
    assert fake._car_has_unused_scans(car) is False


def test_filter_allows_car_with_extra_scan(db):
    # Same car as main, but a second scan exists → user can add the
    # car again as compare to see that other history.
    car = db.upsert_car(vin="VIN-TWO-SCANS")
    _add_scan(db, car, "2026-01-01T10:00:00")
    _add_scan(db, car, "2026-01-02T10:00:00")
    fake = _fake(db, main_car_id=car, main_scan_ts="2026-01-02T10:00:00")
    assert fake._car_has_unused_scans(car) is True


def test_filter_hides_car_after_all_scans_loaded(db):
    # 2 scans total, 1 main + 1 already in compare → no unused left.
    car = db.upsert_car(vin="VIN-ALL-LOADED")
    _add_scan(db, car, "2026-01-01T10:00:00")
    _add_scan(db, car, "2026-01-02T10:00:00")
    fake = _fake(
        db, main_car_id=car, main_scan_ts="2026-01-02T10:00:00",
        compare_cars=[{"car_id": car, "scan_ts": "2026-01-01T10:00:00"}],
    )
    assert fake._car_has_unused_scans(car) is False


def test_filter_allows_repeated_adds_until_exhausted(db):
    # Three scans → can add the car twice as compare; the third add
    # would have no scan left → filter hides it.
    car = db.upsert_car(vin="VIN-THREE-SCANS")
    _add_scan(db, car, "2026-01-01T10:00:00")
    _add_scan(db, car, "2026-01-02T10:00:00")
    _add_scan(db, car, "2026-01-03T10:00:00")
    fake = _fake(db, main_car_id=car, main_scan_ts="2026-01-03T10:00:00")
    # 1st add: 2 scans unused
    assert fake._car_has_unused_scans(car) is True
    fake._compare_cars.append({"car_id": car, "scan_ts": "2026-01-02T10:00:00"})
    # 2nd add: 1 scan unused
    assert fake._car_has_unused_scans(car) is True
    fake._compare_cars.append({"car_id": car, "scan_ts": "2026-01-01T10:00:00"})
    # 3rd add would be empty → filter blocks
    assert fake._car_has_unused_scans(car) is False


def test_filter_ignores_scans_without_sensor_data(db):
    # A scan whose pids_count == 0 (no usable sensor data) doesn't
    # count as „available" — adding it would just yield an empty
    # compare entry. The car should be hidden in that case too.
    car = db.upsert_car(vin="VIN-EMPTY-SCAN")
    _add_scan(db, car, "2026-01-01T10:00:00", pids=1)  # the main scan
    _add_scan(db, car, "2026-01-02T10:00:00", pids=0)  # empty extra
    fake = _fake(db, main_car_id=car, main_scan_ts="2026-01-01T10:00:00")
    assert fake._car_has_unused_scans(car) is False


def test_filter_returns_false_when_db_missing(db):
    # Defensive — without a DB we can't list scans, so we must never
    # offer the car for compare.
    car = db.upsert_car(vin="VIN-NO-DB")
    _add_scan(db, car, "2026-01-01T10:00:00")
    fake = _fake(db, main_car_id=car, main_scan_ts=None)
    fake._db = None
    assert fake._car_has_unused_scans(car) is False
