"""OBD scan snapshots, per-PID sample series, and scan merge."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)


class ScansMixin:
    SCAN_MERGE_MAX_GAP_S: float = 30 * 60   # 30 min between consecutive scans
    SCAN_MERGE_FILL_INTERVAL_S: float = 1.0  # zero-fill cadence
    SCAN_MERGE_FILL_GAP_THRESHOLD_S: float = 5.0  # only fill gaps wider than this

    def delete_scan(self, scan_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM scans WHERE id=?", (scan_id,))
            self._conn.commit()

    def add_scan(self, car_id: int, data: dict[str, Any]) -> int:
        """Store a full OBD scan snapshot. Returns the new scan id."""
        scanned_at = data.get("scanned_at") or datetime.now(UTC).isoformat()
        protocol = data.get("protocol")
        dtc_count = len(data.get("dtcs") or [])
        pending_count = len(data.get("pending_dtcs") or [])
        pids_count = len(data.get("supported_pids") or [])
        blob = json.dumps(data, ensure_ascii=False, default=str)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO scans"
                "(car_id, scanned_at, protocol, dtc_count, pending_dtc_count, pids_count, data_json)"
                " VALUES(?,?,?,?,?,?,?)",
                (car_id, scanned_at, protocol, dtc_count, pending_count, pids_count, blob),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def update_scan_data(self, scan_id: int, data: dict[str, Any]) -> None:
        """Refresh snapshot data of an existing scan (re-scan in same session)."""
        dtc_count = len(data.get("dtcs") or [])
        pending_count = len(data.get("pending_dtcs") or [])
        pids_count = len(data.get("supported_pids") or [])
        blob = json.dumps(data, ensure_ascii=False, default=str)
        with self._lock:
            self._conn.execute(
                "UPDATE scans SET dtc_count=?, pending_dtc_count=?, pids_count=?, data_json=?"
                " WHERE id=?",
                (dtc_count, pending_count, pids_count, blob, scan_id),
            )
            self._conn.commit()

    def list_scans_for_car(self, car_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT id, car_id, scanned_at, protocol, dtc_count, pending_dtc_count,"
                " pids_count, seen_at, shared_at"
                " FROM scans WHERE car_id=? ORDER BY scanned_at DESC",
                (car_id,),
            ).fetchall())

    def get_scan_data(self, scan_id: int) -> dict[str, Any]:
        """Return the full JSON blob for a single scan."""
        with self._lock:
            row = self._conn.execute(
                "SELECT data_json FROM scans WHERE id=?", (scan_id,)
            ).fetchone()
        if row is None:
            return {}
        try:
            return json.loads(row["data_json"])
        except json.JSONDecodeError:
            log.exception("Could not decode scan JSON for scan_id=%s", scan_id)
            return {}

    def add_scan_samples(self, scan_id: int, rows: list[dict]) -> int:
        """Bulk-insert (scan_id, ts, pid, value, unit) rows. Returns count inserted."""
        if not rows:
            return 0
        data = [
            (scan_id, float(r["ts"]), str(r["pid"]), float(r["value"]), str(r.get("unit", "")))
            for r in rows
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO scan_samples(scan_id, ts, pid, value, unit) VALUES(?,?,?,?,?)",
                data,
            )
            self._conn.commit()
        return len(data)

    def get_scan_samples(
        self, scan_id: int, pid: str | None = None
    ) -> list[sqlite3.Row]:
        """Return scan_samples rows ordered by ts. Optionally filter to one pid."""
        with self._lock:
            if pid is not None:
                return list(self._conn.execute(
                    "SELECT ts, pid, value, unit FROM scan_samples"
                    " WHERE scan_id=? AND pid=? ORDER BY ts",
                    (scan_id, pid),
                ).fetchall())
            return list(self._conn.execute(
                "SELECT ts, pid, value, unit FROM scan_samples"
                " WHERE scan_id=? ORDER BY ts",
                (scan_id,),
            ).fetchall())

    def scan_has_series(self, scan_id: int) -> bool:
        """True when at least one sample row exists for this scan."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM scan_samples WHERE scan_id=? LIMIT 1", (scan_id,)
            ).fetchone()
        return row is not None

    def merge_scans(self, scan_ids: list[int]) -> int:
        """Merge multiple scans into the earliest one. Returns survivor id.

        Raises ValueError with codes the caller can map to UI messages:
          - "too_few"        : fewer than 2 scans selected
          - "scan_not_found" : not all ids exist
          - "different_cars" : scans belong to multiple cars
          - "gap_too_large"  : consecutive scans more than
            SCAN_MERGE_MAX_GAP_S apart — refuses to merge non-adjacent ones
            because the user's mental model is "scan → short break → scan",
            not "two unrelated recordings stitched together".

        Gap-filling: for each PID, every gap wider than
        SCAN_MERGE_FILL_GAP_THRESHOLD_S between adjacent samples is filled
        with value=0 rows at SCAN_MERGE_FILL_INTERVAL_S cadence so the chart
        renders the offline stretch as a visible flatline instead of
        connecting the two real segments with a straight line.
        """
        if len(scan_ids) < 2:
            raise ValueError("too_few")

        def _parse_ts(s: Any) -> datetime:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))

        placeholders = ",".join("?" * len(scan_ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, car_id, scanned_at, data_json FROM scans"
                f" WHERE id IN ({placeholders}) ORDER BY scanned_at ASC",
                tuple(scan_ids),
            ).fetchall()
            if len(rows) != len(scan_ids):
                raise ValueError("scan_not_found")
            car_id = rows[0]["car_id"]
            if any(r["car_id"] != car_id for r in rows):
                raise ValueError("different_cars")
            timestamps = [_parse_ts(r["scanned_at"]) for r in rows]
            for i in range(1, len(timestamps)):
                gap = (timestamps[i] - timestamps[i - 1]).total_seconds()
                if gap > self.SCAN_MERGE_MAX_GAP_S:
                    raise ValueError("gap_too_large")

            survivor_id = int(rows[0]["id"])
            loser_ids = [int(r["id"]) for r in rows[1:]]

            for loser in loser_ids:
                samples = self._conn.execute(
                    "SELECT ts, pid, value, unit FROM scan_samples WHERE scan_id=?",
                    (loser,),
                ).fetchall()
                if samples:
                    self._conn.executemany(
                        "INSERT OR IGNORE INTO scan_samples(scan_id, ts, pid, value, unit) VALUES(?,?,?,?,?)",
                        [(survivor_id, s["ts"], s["pid"], s["value"], s["unit"]) for s in samples],
                    )

            pid_rows = self._conn.execute(
                "SELECT DISTINCT pid FROM scan_samples WHERE scan_id=?", (survivor_id,)
            ).fetchall()
            fill_interval = self.SCAN_MERGE_FILL_INTERVAL_S
            gap_threshold = self.SCAN_MERGE_FILL_GAP_THRESHOLD_S
            for pr in pid_rows:
                pid = pr["pid"]
                sample_rows = self._conn.execute(
                    "SELECT ts, unit FROM scan_samples WHERE scan_id=? AND pid=? ORDER BY ts",
                    (survivor_id, pid),
                ).fetchall()
                if len(sample_rows) < 2:
                    continue
                fills: list[tuple] = []
                for i in range(1, len(sample_rows)):
                    prev_ts = float(sample_rows[i - 1]["ts"])
                    curr_ts = float(sample_rows[i]["ts"])
                    unit = sample_rows[i - 1]["unit"]
                    gap = curr_ts - prev_ts
                    if gap > gap_threshold:
                        n = int(gap / fill_interval)
                        for j in range(1, n):
                            fills.append((survivor_id, prev_ts + j * fill_interval, pid, 0.0, unit))
                if fills:
                    self._conn.executemany(
                        "INSERT OR IGNORE INTO scan_samples(scan_id, ts, pid, value, unit) VALUES(?,?,?,?,?)",
                        fills,
                    )

            try:
                survivor_data = json.loads(rows[0]["data_json"])
            except (ValueError, json.JSONDecodeError):
                survivor_data = {}
            merged_dtcs = list(survivor_data.get("dtcs") or [])
            merged_pending = list(survivor_data.get("pending_dtcs") or [])
            for r in rows[1:]:
                try:
                    d = json.loads(r["data_json"])
                except (ValueError, json.JSONDecodeError):
                    continue
                for entry in d.get("dtcs") or []:
                    if entry not in merged_dtcs:
                        merged_dtcs.append(entry)
                for entry in d.get("pending_dtcs") or []:
                    if entry not in merged_pending:
                        merged_pending.append(entry)
            survivor_data["dtcs"] = merged_dtcs
            survivor_data["pending_dtcs"] = merged_pending

            pid_count_row = self._conn.execute(
                "SELECT COUNT(DISTINCT pid) AS n FROM scan_samples WHERE scan_id=?",
                (survivor_id,),
            ).fetchone()
            pids_count = int(pid_count_row["n"] if pid_count_row else 0)

            self._conn.execute(
                "UPDATE scans SET data_json=?, dtc_count=?, pending_dtc_count=?,"
                " pids_count=? WHERE id=?",
                (
                    json.dumps(survivor_data),
                    len(merged_dtcs),
                    len(merged_pending),
                    pids_count,
                    survivor_id,
                ),
            )

            self._conn.executemany(
                "DELETE FROM scans WHERE id=?", [(i,) for i in loser_ids]
            )
            self._conn.commit()
            return survivor_id
