"""Unit tests for the pure live-session folding helpers."""

from drivepulse_app.cars._live_session import (
    extract_session_number,
    fold_live_payload,
)

# A minimal set of "tracked" keys standing in for LIVE_KEY_TO_PID.
_LIVE_KEYS = {"rpm", "coolant"}


# --- extract_session_number -------------------------------------------------

def test_extract_from_value_dict():
    assert extract_session_number({"value": "42", "unit": "rpm"}) == 42.0


def test_extract_from_raw_number():
    assert extract_session_number(7) == 7.0
    assert extract_session_number(3.5) == 3.5


def test_extract_returns_none_for_unparseable():
    assert extract_session_number({"value": "n/a"}) is None
    assert extract_session_number("hello") is None
    assert extract_session_number({"no_value": 1}) is None
    assert extract_session_number(None) is None


# --- fold_live_payload ------------------------------------------------------

def test_fold_records_latest_value_for_every_non_meta_key():
    latest: dict = {}
    stats: dict = {}
    fold_live_payload(
        {"source": "obd", "timestamp": 1, "rpm": {"value": 800}, "vin": "ABC"},
        latest, stats, _LIVE_KEYS,
    )
    # Meta keys skipped, everything else stored as latest.
    assert latest == {"rpm": {"value": 800}, "vin": "ABC"}


def test_fold_skips_private_and_meta_keys():
    latest: dict = {}
    stats: dict = {}
    fold_live_payload(
        {"_internal": 1, "connection_status": "ok", "mock_reason": "x", "rpm": 500},
        latest, stats, _LIVE_KEYS,
    )
    assert latest == {"rpm": 500}


def test_fold_tracks_min_max_only_for_live_keys():
    latest: dict = {}
    stats: dict = {}
    # "vin" is not a tracked live key, so it gets no min/max entry.
    fold_live_payload({"rpm": {"value": 800, "unit": "rpm"}, "vin": "ABC"}, latest, stats, _LIVE_KEYS)
    assert "rpm" in stats
    assert "vin" not in stats
    assert stats["rpm"] == {"unit": "rpm", "min": 800.0, "max": 800.0}


def test_fold_accumulates_min_max_across_calls():
    latest: dict = {}
    stats: dict = {}
    for val in (800, 1200, 600, 900):
        fold_live_payload({"rpm": {"value": val, "unit": "rpm"}}, latest, stats, _LIVE_KEYS)
    assert stats["rpm"]["min"] == 600.0
    assert stats["rpm"]["max"] == 1200.0


def test_fold_ignores_unparseable_live_values():
    latest: dict = {}
    stats: dict = {}
    fold_live_payload({"rpm": {"value": "n/a"}}, latest, stats, _LIVE_KEYS)
    # Latest is still recorded, but no min/max stats since the value isn't numeric.
    assert latest == {"rpm": {"value": "n/a"}}
    assert stats == {}


def test_fold_unit_defaults_to_empty_for_raw_number():
    latest: dict = {}
    stats: dict = {}
    fold_live_payload({"coolant": 90}, latest, stats, _LIVE_KEYS)
    assert stats["coolant"] == {"unit": "", "min": 90.0, "max": 90.0}
