"""Car photo metadata. The image files themselves live on disk."""
from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime


class PhotosMixin:
    # Provided by _DriveDBBase when composed into DriveDB. See
    # project_mixin_typing.md.
    _conn: sqlite3.Connection
    _lock: threading.Lock

    def add_car_photo(
        self,
        car_id: int,
        filename: str,
        taken_at: str | None = None,
        shared_at: str | None = None,
    ) -> int:
        ts = taken_at or datetime.now(UTC).isoformat()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO car_photos(car_id, filename, taken_at, shared_at) VALUES(?,?,?,?)",
                (car_id, filename, ts, shared_at),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_photos_for_car(self, car_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT id, car_id, filename, taken_at, label, seen_at, shared_at"
                " FROM car_photos WHERE car_id=? ORDER BY taken_at DESC",
                (car_id,),
            ).fetchall())

    def delete_car_photo(self, photo_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM car_photos WHERE id=?", (photo_id,))
            self._conn.commit()
