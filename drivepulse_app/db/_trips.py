"""Trip CRUD plus the multi-trip merge with zero-speed gap fill."""
from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any


class TripsMixin:
    # Provided by _DriveDBBase when composed into DriveDB. See
    # project_mixin_typing.md.
    _conn: sqlite3.Connection
    _lock: threading.Lock

    TRIP_MERGE_MAX_GAP_S: float = 30 * 60   # 30 min between consecutive trips
    TRIP_MERGE_FILL_INTERVAL_S: float = 1.0
    TRIP_MERGE_FILL_GAP_THRESHOLD_S: float = 5.0

    def start_trip(self, car_id: int, started_at: datetime | None = None) -> int:
        ts = (started_at or datetime.now(UTC)).isoformat()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("INSERT INTO trips(car_id, started_at) VALUES(?,?)", (car_id, ts))
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def end_trip(self, trip_id: int) -> None:
        """Compute aggregate columns + set ``ended_at``. Empty trips are deleted."""
        with self._lock:
            cur = self._conn.cursor()
            row = cur.execute(
                "SELECT MIN(ts) AS first, MAX(ts) AS last,"
                " MAX(speed_kmh) AS max_kmh, AVG(speed_kmh) AS avg_kmh,"
                " COUNT(*) AS n FROM samples WHERE trip_id=?",
                (trip_id,),
            ).fetchone()
            if row is None or not row["n"]:
                cur.execute("DELETE FROM trips WHERE id=?", (trip_id,))
                self._conn.commit()
                return
            duration_s = float((row["last"] or 0) - (row["first"] or 0))
            distance_km = None
            if row["avg_kmh"]:
                distance_km = float(row["avg_kmh"]) * (duration_s / 3600.0)
            cur.execute(
                "UPDATE trips SET ended_at=?, duration_s=?, max_speed_kmh=?,"
                " avg_speed_kmh=?, samples_count=?, distance_km=? WHERE id=?",
                (
                    datetime.now(UTC).isoformat(),
                    duration_s,
                    row["max_kmh"],
                    row["avg_kmh"],
                    row["n"],
                    distance_km,
                    trip_id,
                ),
            )
            self._conn.commit()

    def list_trips_for_car(self, car_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT * FROM trips WHERE car_id=? ORDER BY started_at DESC",
                (car_id,),
            ).fetchall())

    def get_last_trip_stats(self, car_id: int) -> dict | None:
        """Min/max values of the last completed trip for ``car_id``."""
        with self._lock:
            trip = self._conn.execute(
                "SELECT id, started_at, ended_at, distance_km, duration_s,"
                " max_speed_kmh, avg_speed_kmh, samples_count"
                " FROM trips WHERE car_id=? AND ended_at IS NOT NULL"
                " ORDER BY started_at DESC LIMIT 1",
                (car_id,),
            ).fetchone()
            if trip is None:
                return None
            stats = self._conn.execute(
                "SELECT MIN(rpm) AS min_rpm, MAX(rpm) AS max_rpm,"
                " MIN(coolant_c) AS min_coolant, MAX(coolant_c) AS max_coolant"
                " FROM samples WHERE trip_id=?",
                (trip["id"],),
            ).fetchone()
        result = dict(trip)
        if stats is not None:
            result.update(dict(stats))
        return result

    def delete_trip(self, trip_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM trips WHERE id=?", (trip_id,))
            self._conn.commit()

    def rename_trip(self, trip_id: int, label: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE trips SET label=? WHERE id=?",
                (label or None, trip_id),
            )
            self._conn.commit()

    def merge_trips(self, trip_ids: list[int]) -> int:
        """Merge multiple trips into the earliest one. Returns survivor id.

        Behaves like merge_scans for scans, with the trip-specific twist that
        the *pause* between consecutive trips is booked as standstill, not as
        driving time: each gap is filled with zero-speed samples at
        TRIP_MERGE_FILL_INTERVAL_S cadence, while ``duration_s`` aggregates
        only the actual driving stretches (sum of source durations) rather
        than the elapsed wall-clock span. lat/lon are left NULL on the
        fill rows so the map polyline doesn't jump to (0, 0).

        Error codes (same shape as merge_scans):
          - "too_few", "trip_not_found", "different_cars", "gap_too_large"
        """
        if len(trip_ids) < 2:
            raise ValueError("too_few")

        def _parse_ts(s: Any) -> datetime:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))

        placeholders = ",".join("?" * len(trip_ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, car_id, started_at, ended_at, distance_km,"
                f" duration_s, max_speed_kmh FROM trips"
                f" WHERE id IN ({placeholders}) ORDER BY started_at ASC",
                tuple(trip_ids),
            ).fetchall()
            if len(rows) != len(trip_ids):
                raise ValueError("trip_not_found")
            car_id = rows[0]["car_id"]
            if any(r["car_id"] != car_id for r in rows):
                raise ValueError("different_cars")
            starts = [_parse_ts(r["started_at"]) for r in rows]
            ends = [_parse_ts(r["ended_at"]) if r["ended_at"] else starts[i]
                    for i, r in enumerate(rows)]
            for i in range(1, len(starts)):
                gap = (starts[i] - ends[i - 1]).total_seconds()
                if gap > self.TRIP_MERGE_MAX_GAP_S:
                    raise ValueError("gap_too_large")

            survivor_id = int(rows[0]["id"])
            loser_ids = [int(r["id"]) for r in rows[1:]]

            sample_cols = ("speed_kmh", "obd_speed_kmh", "gps_speed_kmh",
                           "rpm", "coolant_c", "throttle_pct", "engine_load",
                           "fuel_pct", "intake_c", "maf_gps", "voltage_v",
                           "lat", "lon", "altitude_m", "heading_deg", "accel_g")
            col_list = ", ".join(sample_cols)
            placeholders_cols = ", ".join("?" * (2 + len(sample_cols)))

            # Copy samples from losers into survivor.
            for loser in loser_ids:
                copy_rows = self._conn.execute(
                    f"SELECT ts, {col_list} FROM samples WHERE trip_id=?",
                    (loser,),
                ).fetchall()
                if copy_rows:
                    self._conn.executemany(
                        f"INSERT OR IGNORE INTO samples(trip_id, ts, {col_list})"
                        f" VALUES({placeholders_cols})",
                        [(survivor_id, *(r[c] for c in ("ts", *sample_cols)))
                         for r in copy_rows],
                    )

            # Gap-fill the merged sample stream with zero-speed rows so the
            # pause shows up as a clear standstill plateau. lat/lon stay
            # NULL — otherwise the map track jumps to the equator during
            # the pause.
            ts_rows = self._conn.execute(
                "SELECT ts FROM samples WHERE trip_id=? ORDER BY ts",
                (survivor_id,),
            ).fetchall()
            fill_interval = self.TRIP_MERGE_FILL_INTERVAL_S
            gap_threshold = self.TRIP_MERGE_FILL_GAP_THRESHOLD_S
            zero_fill_cols = ("speed_kmh", "obd_speed_kmh", "gps_speed_kmh",
                              "rpm", "throttle_pct", "engine_load", "accel_g")
            fills: list[tuple] = []
            for i in range(1, len(ts_rows)):
                prev_ts = float(ts_rows[i - 1]["ts"])
                curr_ts = float(ts_rows[i]["ts"])
                gap = curr_ts - prev_ts
                if gap <= gap_threshold:
                    continue
                n = int(gap / fill_interval)
                for j in range(1, n):
                    t = prev_ts + j * fill_interval
                    row_values: list[Any] = [survivor_id, t]
                    for c in sample_cols:
                        row_values.append(0.0 if c in zero_fill_cols else None)
                    fills.append(tuple(row_values))
            if fills:
                self._conn.executemany(
                    f"INSERT OR IGNORE INTO samples(trip_id, ts, {col_list})"
                    f" VALUES({placeholders_cols})",
                    fills,
                )

            # Aggregate columns: distance and drive-time are sums of the
            # source trips (the pause does NOT count as drive time), max
            # speed is the overall max, avg_speed is recomputed from the
            # combined drive time.
            sum_distance = sum(float(r["distance_km"] or 0) for r in rows)
            sum_duration = sum(float(r["duration_s"] or 0) for r in rows)
            max_speed = max((float(r["max_speed_kmh"] or 0) for r in rows), default=0.0)
            avg_speed = None
            if sum_duration > 0 and sum_distance:
                avg_speed = sum_distance / (sum_duration / 3600.0)
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM samples WHERE trip_id=?",
                (survivor_id,),
            ).fetchone()
            samples_count = int(count_row["n"] if count_row else 0)
            # ended_at = latest source's ended_at (falls back to its start).
            last = rows[-1]
            new_ended_at = last["ended_at"] or last["started_at"]

            self._conn.execute(
                "UPDATE trips SET ended_at=?, distance_km=?, duration_s=?,"
                " max_speed_kmh=?, avg_speed_kmh=?, samples_count=?"
                " WHERE id=?",
                (
                    new_ended_at,
                    sum_distance if sum_distance else None,
                    sum_duration,
                    max_speed if max_speed else None,
                    avg_speed,
                    samples_count,
                    survivor_id,
                ),
            )
            self._conn.executemany(
                "DELETE FROM trips WHERE id=?", [(i,) for i in loser_ids]
            )
            self._conn.commit()
            return survivor_id
