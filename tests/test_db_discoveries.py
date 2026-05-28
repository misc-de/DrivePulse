"""Tests for the DiscoveriesMixin: module discoveries + coding findings.

Verifies the read-write contract the Car Lab UI depends on (round-trip of the
JSON inventory, finding fields, ordering, cascade delete with the car)."""
from __future__ import annotations

import pytest

from drivepulse_app.db import DriveDB


@pytest.fixture
def db(tmp_path):
    instance = DriveDB(tmp_path / "drives.sqlite3")
    yield instance
    instance.close()


def test_add_and_get_discovery_round_trips_inventory(db):
    car_id = db.upsert_car(vin="WAUZZZ4GXDN000001", brand="Audi")
    data = {"module": "instruments", "tx": "714", "rx": "77E",
            "dids": {"F190": "WAUZZZ4GXDN000001"}, "dtcs": []}
    did = db.add_discovery(car_id, data, label="Kombi")
    assert db.get_discovery_data(did) == data
    rows = db.list_discoveries_for_car(car_id)
    assert len(rows) == 1
    assert rows[0]["label"] == "Kombi"


def test_get_discovery_data_missing_returns_empty(db):
    assert db.get_discovery_data(999) == {}


def test_discoveries_listed_newest_first(db):
    car_id = db.upsert_car(vin="V1")
    db.add_discovery(car_id, {"created_at": "2026-01-01T00:00:00Z"}, label="old")
    db.add_discovery(car_id, {"created_at": "2026-05-01T00:00:00Z"}, label="new")
    rows = db.list_discoveries_for_car(car_id)
    assert [r["label"] for r in rows] == ["new", "old"]


def test_add_and_list_findings(db):
    car_id = db.upsert_car(vin="V2")
    fid = db.add_finding(car_id, {
        "module": "central_electrics", "tx": "70E", "rx": "778",
        "did": 0x0600, "byte_index": 5, "bit_mask": 0x08,
        "before_hex": "00", "after_hex": "08", "description": "Ambiente an",
    })
    rows = db.list_findings_for_car(car_id)
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == fid
    assert r["did"] == 0x0600
    assert r["byte_index"] == 5
    assert r["bit_mask"] == 0x08
    assert r["description"] == "Ambiente an"


def test_delete_finding_removes_it(db):
    car_id = db.upsert_car(vin="V3")
    fid = db.add_finding(car_id, {"did": 1, "byte_index": 0, "description": "x"})
    db.delete_finding(fid)
    assert db.list_findings_for_car(car_id) == []


def test_discoveries_and_findings_cascade_on_car_delete(db):
    car_id = db.upsert_car(vin="V4")
    db.add_discovery(car_id, {"a": 1})
    db.add_finding(car_id, {"did": 1, "byte_index": 0})
    db.delete_car(car_id)
    assert db.list_discoveries_for_car(car_id) == []
    assert db.list_findings_for_car(car_id) == []
