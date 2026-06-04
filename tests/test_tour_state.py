"""Unit tests for active-tour progress persistence (_tour_state)."""
from __future__ import annotations

import json
import time

import pytest

from drivepulse_app.map import _tour_state


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(_tour_state, "_STATE_FILE", tmp_path / "active_tour.json")
    yield


def test_load_returns_none_when_no_file():
    assert _tour_state.load_active_tour() is None


def test_save_then_load_roundtrip():
    remaining = [(50.1, 8.1), (50.2, 8.2)]
    _tour_state.save_active_tour(remaining, name="Heimweg", tour_id=7)
    loaded = _tour_state.load_active_tour()
    assert loaded is not None
    assert loaded["remaining"] == remaining
    assert loaded["name"] == "Heimweg"
    assert loaded["id"] == 7


def test_save_empty_clears_state():
    _tour_state.save_active_tour([(50.1, 8.1)])
    assert _tour_state.load_active_tour() is not None
    _tour_state.save_active_tour([])  # empty -> clear
    assert _tour_state.load_active_tour() is None


def test_clear_removes_state():
    _tour_state.save_active_tour([(50.1, 8.1)])
    _tour_state.clear_active_tour()
    assert _tour_state.load_active_tour() is None


def test_clear_is_idempotent_when_absent():
    _tour_state.clear_active_tour()  # no file -> no error
    assert _tour_state.load_active_tour() is None


def test_malformed_json_is_ignored():
    _tour_state._STATE_FILE.write_text("{ not json", encoding="utf-8")
    assert _tour_state.load_active_tour() is None


def test_missing_remaining_key_is_ignored():
    _tour_state._STATE_FILE.write_text(json.dumps({"name": "x"}), encoding="utf-8")
    assert _tour_state.load_active_tour() is None


def test_stale_tour_is_ignored():
    _tour_state.save_active_tour([(50.1, 8.1)])
    data = json.loads(_tour_state._STATE_FILE.read_text())
    data["saved_at"] = time.time() - 13 * 3600  # 13 h ago
    _tour_state._STATE_FILE.write_text(json.dumps(data), encoding="utf-8")
    assert _tour_state.load_active_tour() is None


def test_recent_tour_is_kept():
    _tour_state.save_active_tour([(50.1, 8.1)])
    data = json.loads(_tour_state._STATE_FILE.read_text())
    data["saved_at"] = time.time() - 60  # 1 min ago
    _tour_state._STATE_FILE.write_text(json.dumps(data), encoding="utf-8")
    assert _tour_state.load_active_tour() is not None
