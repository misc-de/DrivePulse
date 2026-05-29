"""Pure aggregation of per-PID statistics across a car's OBD scans.

``CarsPage._bg_compute_scan_stats`` loads scan snapshots and intra-scan sample
series from the database; this module turns those raw values into the per-PID
``{min, max, sum, count, avg, unit, values, intra_series}`` summary the detail
view renders. Keeping the folding logic here — free of GTK and the DB — makes
it unit-testable.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def aggregate_scan_pid_stats(
    snapshots: Iterable[tuple[str, str, float, str]],
    intra_series: Iterable[tuple[int, dict[str, list[tuple[float, float]]]]],
) -> dict[str, dict[str, Any]]:
    """Fold scan snapshots and intra-scan sample series into per-PID stats.

    ``snapshots`` yields ``(pid, scanned_at, value, unit)`` — one per-scan
    snapshot value. ``intra_series`` yields ``(scan_id, {pid: [(rel_s, value)]})``
    — the higher-resolution sample series recorded within individual scans.

    The intra-scan samples are folded into the same min/max/avg so the overview
    row reflects the full range the chart plots, not just the snapshot value.
    The ``unit`` is taken from the first snapshot occurrence of each PID.
    """
    stats: dict[str, dict[str, Any]] = {}
    raw_values: dict[str, list[tuple[str, float]]] = {}

    for pid, ts_str, num, unit in snapshots:
        if pid not in stats:
            stats[pid] = {"min": num, "max": num, "sum": num, "count": 1, "unit": unit}
        else:
            stats[pid]["min"] = min(stats[pid]["min"], num)
            stats[pid]["max"] = max(stats[pid]["max"], num)
            stats[pid]["sum"] += num
            stats[pid]["count"] += 1
        raw_values.setdefault(pid, []).append((ts_str, num))

    for pid, s in stats.items():
        pts = raw_values.get(pid) or []
        pts.sort(key=lambda t: t[0])
        s["values"] = pts
        s["intra_series"] = {}

    for scan_id, pid_pts in intra_series:
        for pid, intra_pts in pid_pts.items():
            if pid not in stats:
                stats[pid] = {"sum": 0.0, "count": 0, "unit": "",
                              "values": [], "intra_series": {}}
            s = stats[pid]
            s["intra_series"][scan_id] = sorted(intra_pts, key=lambda t: t[0])
            # Fold the intra-scan samples into min/max/avg so the overview row
            # reflects the full range the chart plots, not just the snapshot.
            for _rel_s, val in intra_pts:
                s["min"] = val if "min" not in s else min(s["min"], val)
                s["max"] = val if "max" not in s else max(s["max"], val)
                s["sum"] += val
                s["count"] += 1

    # Compute averages once min/max and counts include both the per-scan
    # snapshots and the intra-scan sample series.
    for s in stats.values():
        if s.get("count"):
            s["avg"] = s["sum"] / s["count"]

    return stats
