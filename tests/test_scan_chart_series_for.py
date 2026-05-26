"""Tests for ScanChartContent._series_for — the per-PID datapoint
extractor that the chart uses. Locks in the fallback behaviour: when
the selected scan has no intra-scan time series, the chart now returns
the per-scan trend instead of collapsing to a single snapshot."""
from __future__ import annotations

from types import SimpleNamespace

from drivepulse_app.chart.scan_chart import ScanChartContent


def _fake(language: str = "de") -> SimpleNamespace:
    fake = SimpleNamespace(_language=language)
    fake._series_for = lambda stats, pid, scan_ts=None, scan_id=None: (
        ScanChartContent._series_for(fake, stats, pid, scan_ts, scan_id)
    )
    return fake


def test_series_for_returns_intra_when_present():
    fake = _fake()
    stats = {
        "010C": {
            "values": [("2026-05-01T10:00:00", 850.0)],
            "intra_series": {1: [(0.0, 800.0), (5.0, 1200.0), (10.0, 1500.0)]},
            "unit": "rpm",
        }
    }
    vals, _ts, _unit = fake._series_for(stats, "010C", "2026-05-01T10:00:00", 1)
    assert vals == [800.0, 1200.0, 1500.0]


def test_series_for_falls_back_to_cross_scan_trend_when_no_intra():
    # PID has 3 per-scan snapshots but no intra-series data. With the
    # old code, selecting any scan filtered down to that scan's single
    # value -> chart showed 1 dot. The fix: fall through to the full
    # value list so the user gets the cross-scan trend.
    fake = _fake()
    stats = {
        "012F": {
            "values": [
                ("2026-04-15T10:00:00", 78.0),
                ("2026-04-22T10:00:00", 42.0),
                ("2026-04-29T10:00:00", 88.0),
            ],
            "intra_series": {},
            "unit": "%",
        }
    }
    vals, ts, _unit = fake._series_for(stats, "012F", "2026-04-22T10:00:00", 7)
    assert vals == [78.0, 42.0, 88.0]
    assert ts == [
        "2026-04-15T10:00:00",
        "2026-04-22T10:00:00",
        "2026-04-29T10:00:00",
    ]


def test_series_for_keeps_snapshot_when_only_one_value_exists():
    # Edge case: single-scan car without intra-series — there's
    # nothing to compare to, so the snapshot stays as the lone point.
    fake = _fake()
    stats = {
        "0105": {
            "values": [("2026-05-01T10:00:00", 90.0)],
            "intra_series": {},
            "unit": "°C",
        }
    }
    vals, _ts, _unit = fake._series_for(stats, "0105", "2026-05-01T10:00:00", 1)
    assert vals == [90.0]


def test_series_for_returns_empty_for_unknown_pid():
    fake = _fake()
    stats = {"010C": {"values": [], "intra_series": {}, "unit": "rpm"}}
    assert fake._series_for(stats, "ABCD") == ([], [], "")
    assert fake._series_for(None, "010C") == ([], [], "")
    assert fake._series_for(stats, None) == ([], [], "")
