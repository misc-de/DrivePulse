"""Cross-device share helpers: seen_at flags and share-conflict resolution."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime


class SyncMixin:
    def mark_trip_seen(self, trip_id: int) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE trips SET seen_at=? WHERE id=? AND seen_at IS NULL",
                (now, trip_id),
            )
            self._conn.commit()

    def mark_scan_seen(self, scan_id: int) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE scans SET seen_at=? WHERE id=? AND seen_at IS NULL",
                (now, scan_id),
            )
            self._conn.commit()

    def mark_run_seen(self, run_id: int) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE acceleration_runs SET seen_at=? WHERE id=? AND seen_at IS NULL",
                (now, run_id),
            )
            self._conn.commit()

    def mark_photo_seen(self, photo_id: int) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE car_photos SET seen_at=? WHERE id=? AND seen_at IS NULL",
                (now, photo_id),
            )
            self._conn.commit()

    def mark_all_seen_for_car(self, car_id: int, kind: str) -> int:
        """Clear the unread-dot for every row of *kind* on *car_id*.
        Returns the number of rows that were actually flipped to seen,
        so the caller can decide whether the UI needs a refresh.
        """
        tables = {
            "trips":           "trips",
            "scans":           "scans",
            "stopwatch_runs":  "acceleration_runs",
            "photos":          "car_photos",
        }
        table = tables.get(kind)
        if table is None:
            return 0
        now = datetime.now(UTC).isoformat()
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE {table} SET seen_at=? WHERE car_id=? AND seen_at IS NULL",
                (now, car_id),
            )
            self._conn.commit()
            return cur.rowcount or 0

    def count_share_conflicts(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM share_conflicts").fetchone()
            return int(row["n"]) if row else 0

    def list_share_conflicts(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT * FROM share_conflicts ORDER BY received_at DESC"
            ).fetchall())

    def get_conflict(self, conflict_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM share_conflicts WHERE id=?", (conflict_id,)
            ).fetchone()

    def discard_conflict(self, conflict_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM share_conflicts WHERE id=?", (conflict_id,))
            self._conn.commit()

    def resolve_conflict(self, conflict_id: int) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM share_conflicts WHERE id=?", (conflict_id,)
            ).fetchone()
            if row is None:
                return
            incoming = json.loads(row["incoming_json"])
            typ = row["type"]
            local_id = row["local_id"]
            if typ == "trip":
                self._conn.execute(
                    "UPDATE trips SET ended_at=?, distance_km=?, duration_s=?,"
                    " max_speed_kmh=?, avg_speed_kmh=?, samples_count=?, label=?"
                    " WHERE id=?",
                    (
                        incoming.get("ended_at"),
                        incoming.get("distance_km"),
                        incoming.get("duration_s"),
                        incoming.get("max_speed_kmh"),
                        incoming.get("avg_speed_kmh"),
                        incoming.get("samples_count") or 0,
                        incoming.get("label"),
                        local_id,
                    ),
                )
            elif typ == "scan":
                self._conn.execute(
                    "UPDATE scans SET protocol=?, dtc_count=?, pending_dtc_count=?,"
                    " pids_count=?, data_json=? WHERE id=?",
                    (
                        incoming.get("protocol"),
                        incoming.get("dtc_count") or 0,
                        incoming.get("pending_dtc_count") or 0,
                        incoming.get("pids_count") or 0,
                        incoming.get("data_json", "{}"),
                        local_id,
                    ),
                )
            elif typ == "run":
                self._conn.execute(
                    "UPDATE acceleration_runs SET results_json=?, samples_json=? WHERE id=?",
                    (
                        json.dumps(incoming.get("results", {})),
                        json.dumps(incoming.get("samples", [])),
                        local_id,
                    ),
                )
            self._conn.execute("DELETE FROM share_conflicts WHERE id=?", (conflict_id,))
            self._conn.commit()
