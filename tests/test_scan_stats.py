"""Unit tests for the pure per-PID scan-stats aggregator."""

from drivepulse_app.cars._scan_stats import aggregate_scan_pid_stats


def test_empty_inputs_yield_empty_stats():
    assert aggregate_scan_pid_stats([], []) == {}


def test_single_snapshot_seeds_all_fields():
    stats = aggregate_scan_pid_stats([("rpm", "2026-01-01", 800.0, "rpm")], [])
    s = stats["rpm"]
    assert s["min"] == 800.0
    assert s["max"] == 800.0
    assert s["avg"] == 800.0
    assert s["count"] == 1
    assert s["unit"] == "rpm"
    assert s["values"] == [("2026-01-01", 800.0)]
    assert s["intra_series"] == {}


def test_multiple_snapshots_fold_min_max_avg():
    snapshots = [
        ("rpm", "2026-01-03", 1500.0, "rpm"),
        ("rpm", "2026-01-01", 500.0, "rpm"),
        ("rpm", "2026-01-02", 1000.0, "rpm"),
    ]
    s = aggregate_scan_pid_stats(snapshots, [])["rpm"]
    assert s["min"] == 500.0
    assert s["max"] == 1500.0
    assert s["avg"] == 1000.0
    assert s["count"] == 3
    # values are sorted by timestamp string, not insertion order.
    assert [ts for ts, _ in s["values"]] == ["2026-01-01", "2026-01-02", "2026-01-03"]


def test_unit_taken_from_first_snapshot_occurrence():
    snapshots = [("temp", "t1", 90.0, "°C"), ("temp", "t2", 95.0, "")]
    assert aggregate_scan_pid_stats(snapshots, [])["temp"]["unit"] == "°C"


def test_intra_samples_fold_into_snapshot_stats():
    # Snapshot says 100; intra-scan series spans 80..130 -> overview reflects the
    # full range, not just the snapshot value.
    snapshots = [("rpm", "t1", 100.0, "rpm")]
    intra = [(7, {"rpm": [(0.0, 80.0), (1.0, 130.0)]})]
    s = aggregate_scan_pid_stats(snapshots, intra)["rpm"]
    assert s["min"] == 80.0
    assert s["max"] == 130.0
    assert s["count"] == 3  # 1 snapshot + 2 intra samples
    assert s["avg"] == (100.0 + 80.0 + 130.0) / 3
    assert s["intra_series"][7] == [(0.0, 80.0), (1.0, 130.0)]


def test_intra_series_is_sorted_by_relative_time():
    intra = [(1, {"rpm": [(2.0, 30.0), (0.0, 10.0), (1.0, 20.0)]})]
    s = aggregate_scan_pid_stats([], intra)["rpm"]
    assert s["intra_series"][1] == [(0.0, 10.0), (1.0, 20.0), (2.0, 30.0)]


def test_pid_present_only_in_intra_series():
    # A PID with no snapshot still produces complete stats from intra samples.
    intra = [(3, {"boost": [(0.0, 1.2), (1.0, 1.8)]})]
    s = aggregate_scan_pid_stats([], intra)["boost"]
    assert s["min"] == 1.2
    assert s["max"] == 1.8
    assert s["count"] == 2
    assert s["avg"] == 1.5
    assert s["unit"] == ""
    assert s["values"] == []


def test_zero_count_pid_gets_no_avg():
    # An intra entry with an empty point list leaves count at 0 -> no avg key.
    intra = [(1, {"rpm": []})]
    s = aggregate_scan_pid_stats([], intra)["rpm"]
    assert s["count"] == 0
    assert "avg" not in s
    assert s["intra_series"][1] == []
