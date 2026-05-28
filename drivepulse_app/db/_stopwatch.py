"""Stopwatch (acceleration) runs. The underlying table is still
``acceleration_runs`` for backward compatibility with existing user databases."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)


class StopwatchMixin:
    # Provided by _DriveDBBase when composed into DriveDB. See
    # project_mixin_typing.md.
    _conn: sqlite3.Connection
    _lock: threading.Lock

    def add_stopwatch_run(
        self,
        car_id: int,
        results: dict[str, Any],
        samples: list[Any],
        lat: float | None = None,
        lon: float | None = None,
        run_at: str | None = None,
    ) -> int:
        ts = run_at or datetime.now(UTC).isoformat()
        blob_results = json.dumps(results, ensure_ascii=False, default=str)
        blob_samples = json.dumps(samples, ensure_ascii=False, default=str)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO acceleration_runs"
                "(car_id, run_at, lat, lon, results_json, samples_json)"
                " VALUES(?,?,?,?,?,?)",
                (car_id, ts, lat, lon, blob_results, blob_samples),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_stopwatch_runs_for_car(self, car_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT id, car_id, run_at, lat, lon, seen_at, shared_at"
                " FROM acceleration_runs"
                " WHERE car_id=? ORDER BY run_at DESC",
                (car_id,),
            ).fetchall())

    def get_stopwatch_run(self, run_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM acceleration_runs WHERE id=?", (run_id,)
            ).fetchone()
        if row is None:
            return {}
        try:
            return {
                "id": row["id"],
                "car_id": row["car_id"],
                "run_at": row["run_at"],
                "lat": row["lat"],
                "lon": row["lon"],
                "results": json.loads(row["results_json"]),
                "samples": json.loads(row["samples_json"]),
            }
        except json.JSONDecodeError:
            log.exception("Could not decode stopwatch run JSON for run_id=%s", run_id)
            return {}

    def delete_stopwatch_run(self, run_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM acceleration_runs WHERE id=?", (run_id,))
            self._conn.commit()
