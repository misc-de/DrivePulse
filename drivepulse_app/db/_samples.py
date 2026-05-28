"""Per-trip telemetry samples (~1–2 Hz)."""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from typing import Any

from drivepulse_app.db._schema import _SAMPLE_COLUMNS


class SamplesMixin:
    # Provided by _DriveDBBase when composed into DriveDB. See
    # project_mixin_typing.md.
    _conn: sqlite3.Connection
    _lock: threading.Lock

    def add_sample(self, trip_id: int, ts: float, **fields: Any) -> None:
        cols: list[str] = ["trip_id", "ts"]
        vals: list[Any] = [trip_id, float(ts)]
        for key in _SAMPLE_COLUMNS:
            if key in fields and fields[key] is not None:
                cols.append(key)
                vals.append(float(fields[key]))
        placeholders = ",".join("?" for _ in cols)
        sql = f"INSERT OR IGNORE INTO samples({','.join(cols)}) VALUES({placeholders})"
        with self._lock:
            self._conn.execute(sql, vals)

    def add_samples(self, trip_id: int, samples: Iterable[dict[str, Any]]) -> int:
        """Insert many samples for one trip in a single DB roundtrip.

        Malformed samples are skipped. The return value is the number of valid
        rows submitted to SQLite; duplicates may still be ignored by the
        ``(trip_id, ts)`` primary key.
        """
        cols = ("trip_id", "ts", *_SAMPLE_COLUMNS)
        rows: list[tuple[Any, ...]] = []
        for sample in samples:
            if not isinstance(sample, dict) or sample.get("ts") is None:
                continue
            try:
                row: list[Any] = [trip_id, float(sample["ts"])]
                for key in _SAMPLE_COLUMNS:
                    value = sample.get(key)
                    row.append(None if value is None else float(value))
            except (TypeError, ValueError):
                continue
            rows.append(tuple(row))
        if not rows:
            return 0
        placeholders = ",".join("?" for _ in cols)
        sql = f"INSERT OR IGNORE INTO samples({','.join(cols)}) VALUES({placeholders})"
        with self._lock:
            self._conn.executemany(sql, rows)
        return len(rows)

    def samples_for_trip(self, trip_id: int) -> Iterable[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT * FROM samples WHERE trip_id=? ORDER BY ts",
                (trip_id,),
            ).fetchall())
