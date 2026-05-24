"""Tests for the pure helpers in scan_chart_page: number/timestamp formatting,
RGB→hex conversion, safe-int extraction from sqlite rows, and the stats
aggregator that builds the chart series. These run without a live GTK loop;
the helpers are deliberately side-effect free."""
from __future__ import annotations

import pytest

from drivepulse_app.chart.scan_chart import (
    _compute_stats_for_car,
    _fmt,
    _fmt_scan_label,
    _fmt_ts,
    _rgb_to_hex,
    _safe_pids_count,
)

# ─── _fmt: numeric value bands ────────────────────────────────────────────────

def test_fmt_large_value_no_decimals():
    assert _fmt(2500.0) == "2500"
    assert _fmt(123.4) == "123"


def test_fmt_mid_value_one_decimal():
    assert _fmt(87.5) == "87.5"
    assert _fmt(10.0) == "10.0"


def test_fmt_small_value_two_decimals():
    assert _fmt(0.34) == "0.34"
    assert _fmt(9.999) == "10.00"  # rounds into the same band


def test_fmt_negative_uses_magnitude_for_band():
    # Magnitude (not signed value) decides the precision band.
    assert _fmt(-150.0) == "-150"
    assert _fmt(-25.5) == "-25.5"


# ─── _fmt_ts: chart axis label ────────────────────────────────────────────────

def test_fmt_ts_truncates_iso_to_date():
    assert _fmt_ts("2026-05-24T14:30:25.123456+00:00") == "2026-05-24"


def test_fmt_ts_passes_short_string_through():
    # Don't crash on unexpected formats — keep whatever came in.
    assert _fmt_ts("garbage") == "garbage"
    assert _fmt_ts("") == ""


# ─── _fmt_scan_label: dropdown row text ──────────────────────────────────────

def test_fmt_scan_label_formats_iso_to_date_and_time():
    assert _fmt_scan_label("2026-05-24T14:30:25.123456+00:00") == "2026-05-24 14:30"


def test_fmt_scan_label_too_short_passes_through():
    assert _fmt_scan_label("abc") == "abc"


# ─── _rgb_to_hex: GdkRGBA → markup colour ────────────────────────────────────

def test_rgb_to_hex_basic_components():
    assert _rgb_to_hex((1.0, 0.0, 0.0)) == "#ff0000"
    assert _rgb_to_hex((0.0, 1.0, 0.0)) == "#00ff00"
    assert _rgb_to_hex((0.0, 0.0, 1.0)) == "#0000ff"


def test_rgb_to_hex_rounds_components():
    # 0.5 → 128 in 8-bit.
    assert _rgb_to_hex((0.5, 0.5, 0.5)) == "#808080"


def test_rgb_to_hex_clamps_out_of_range():
    # Color values out of [0,1] shouldn't crash the markup.
    assert _rgb_to_hex((2.0, -1.0, 0.0)) == "#ff0000"


# ─── _safe_pids_count: scan-meta sanitiser ───────────────────────────────────

def test_safe_pids_count_returns_int():
    assert _safe_pids_count({"pids_count": 5}) == 5


def test_safe_pids_count_handles_none():
    # pids_count can legitimately be NULL on older DB rows.
    assert _safe_pids_count({"pids_count": None}) == 0


def test_safe_pids_count_handles_missing_key():
    assert _safe_pids_count({}) == 0


def test_safe_pids_count_handles_non_numeric():
    # sqlite columns are typed but be defensive against migration drift.
    assert _safe_pids_count({"pids_count": "x"}) == 0


# ─── _compute_stats_for_car: aggregation across scans ────────────────────────

class _FakeDB:
    """Minimal db.list_scans_for_car / get_scan_data stand-in. Returns
    sqlite3.Row-style records the production code actually queries."""

    def __init__(self, scans: list[dict], data: dict[int, dict]):
        self._scans = scans
        self._data = data

    def list_scans_for_car(self, _car_id: int) -> list[dict]:
        return self._scans

    def get_scan_data(self, scan_id: int) -> dict:
        return self._data[scan_id]


def test_compute_stats_for_car_aggregates_min_max_avg():
    db = _FakeDB(
        scans=[
            {"id": 1, "scanned_at": "2026-05-20T10:00:00"},
            {"id": 2, "scanned_at": "2026-05-21T10:00:00"},
            {"id": 3, "scanned_at": "2026-05-22T10:00:00"},
        ],
        data={
            1: {"live_data": {"Command(b'010C')": {"value": 800, "unit": "rpm"}}},
            2: {"live_data": {"Command(b'010C')": {"value": 1600, "unit": "rpm"}}},
            3: {"live_data": {"Command(b'010C')": {"value": 1200, "unit": "rpm"}}},
        },
    )
    stats = _compute_stats_for_car(db, car_id=1)
    rpm = stats["010C"]
    assert rpm["min"] == 800
    assert rpm["max"] == 1600
    assert rpm["avg"] == 1200
    assert rpm["count"] == 3
    assert rpm["unit"] == "rpm"


def test_compute_stats_for_car_skips_non_numeric_values():
    db = _FakeDB(
        scans=[
            {"id": 1, "scanned_at": "2026-05-20T10:00:00"},
            {"id": 2, "scanned_at": "2026-05-21T10:00:00"},
        ],
        data={
            1: {"live_data": {"Command(b'010C')": {"value": "n/a"}}},
            2: {"live_data": {"Command(b'010C')": {"value": 1500, "unit": "rpm"}}},
        },
    )
    stats = _compute_stats_for_car(db, car_id=1)
    # Only the numeric reading should be folded into the stats.
    assert stats["010C"]["count"] == 1
    assert stats["010C"]["min"] == stats["010C"]["max"] == 1500


def test_compute_stats_for_car_values_sorted_by_timestamp():
    # Scans arrive newest-first from list_scans_for_car, but the chart
    # wants oldest-first so the line goes left-to-right.
    db = _FakeDB(
        scans=[
            {"id": 1, "scanned_at": "2026-05-22T10:00:00"},
            {"id": 2, "scanned_at": "2026-05-20T10:00:00"},
            {"id": 3, "scanned_at": "2026-05-21T10:00:00"},
        ],
        data={
            1: {"live_data": {"Command(b'010C')": {"value": 1200}}},
            2: {"live_data": {"Command(b'010C')": {"value": 800}}},
            3: {"live_data": {"Command(b'010C')": {"value": 1500}}},
        },
    )
    stats = _compute_stats_for_car(db, car_id=1)
    timestamps = [ts for ts, _ in stats["010C"]["values"]]
    assert timestamps == sorted(timestamps)


def test_compute_stats_for_car_empty_when_db_raises():
    class _BrokenDB:
        def list_scans_for_car(self, _car_id):
            raise RuntimeError("db gone")

    assert _compute_stats_for_car(_BrokenDB(), car_id=1) == {}


# ─── _prefs_load / _prefs_save: JSON persistence ─────────────────────────────
#
# Note: the test fixture below imports scan_chart_page fresh and patches
# _PREFS_FILE on whatever module instance is currently in sys.modules. We
# call _prefs_save/_prefs_load via that instance (not the top-of-file
# imports) so the patch survives in cases where another fixture has
# previously evicted drivepulse_app modules from sys.modules.

@pytest.fixture
def prefs_setup(tmp_path, monkeypatch):
    """Patch _PREFS_FILE on the currently-resident scan_chart_page module
    and return (module, tmp file path) for the test to use."""
    import drivepulse_app.chart.scan_chart as scp
    path = tmp_path / "scan_chart_prefs.json"
    monkeypatch.setattr(scp, "_PREFS_FILE", path)
    return scp, path


def test_prefs_load_returns_empty_when_file_missing(prefs_setup):
    scp, _path = prefs_setup
    assert scp._prefs_load() == {}


def test_prefs_load_ignores_invalid_json(prefs_setup):
    scp, path = prefs_setup
    path.write_text("not json", encoding="utf-8")
    assert scp._prefs_load() == {}


def test_prefs_save_then_load_roundtrip(prefs_setup):
    scp, _path = prefs_setup
    payload = {
        "1:010C": {
            "value2": "0105",
            "main_scan_ts": "2026-05-24T10:00:00",
            "cars": [
                {"car_id": 2, "color": [1.0, 0.6, 0.2], "scan_ts": None},
                {"car_id": 3, "color": [0.3, 0.8, 0.45], "scan_ts": "2026-05-21T08:00:00"},
            ],
        },
    }
    scp._prefs_save(payload)
    assert scp._prefs_load() == payload


def test_prefs_load_returns_empty_when_top_level_is_null(prefs_setup):
    # If the JSON file got truncated to "null" or "false", treat as empty
    # rather than letting None propagate into the chart restore path.
    scp, path = prefs_setup
    path.write_text("null", encoding="utf-8")
    assert scp._prefs_load() == {}
