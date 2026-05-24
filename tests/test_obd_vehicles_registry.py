"""Smoke tests for the generic OBD-II PID registry."""
from drivepulse_app.obd_vehicles import load_standard_pids, pids_by_category


def test_standard_pids_load() -> None:
    pids = load_standard_pids()
    assert "rpm" in pids
    assert "speed" in pids
    assert pids["rpm"].pid == "010C"
    assert pids["speed"].unit == "km/h"


def test_standard_pids_categorised() -> None:
    pids = load_standard_pids()
    categories = {pid.category for pid in pids.values()}
    assert {"engine", "vehicle", "fuel"}.issubset(categories)


def test_pids_by_category_grouping() -> None:
    pids = load_standard_pids()
    grouped = pids_by_category(pids)
    assert "engine" in grouped
    assert any(p.key == "rpm" for p in grouped["engine"])
    assert any(p.key == "speed" for p in grouped["vehicle"])
