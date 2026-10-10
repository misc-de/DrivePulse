"""User-saved tours plus the merged trip + tour history view."""
from __future__ import annotations

import sqlite3
import threading


class ToursMixin:
    # Provided by _DriveDBBase when composed into DriveDB. See
    # project_mixin_typing.md.
    _conn: sqlite3.Connection
    _lock: threading.Lock

    def save_tour(self, name: str, waypoints_json: str, created_at: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO saved_tours (name, created_at, waypoints_json) VALUES (?,?,?)",
                (name, created_at, waypoints_json),
            )
            self._conn.commit()
            # lastrowid is int after a successful INSERT — the Optional[int]
            # in the sqlite3 stub covers cases where no row was inserted yet.
            assert cur.lastrowid is not None
            return cur.lastrowid

    def get_saved_tour(self, tour_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM saved_tours WHERE id=?", (tour_id,)
            ).fetchone()

    def list_saved_tours(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM saved_tours ORDER BY created_at DESC"
            ).fetchall()

    def list_tour_history(self, limit: int, offset: int = 0) -> list[sqlite3.Row]:
        """Recorded trips + explicitly saved tours, merged chronologically.

        Each row carries a ``kind`` column ("trip" or "tour") so callers can
        render them differently. Trips also expose distance/duration plus the
        owning car's identity; saved tours only have a name.

        Trips without a single real GPS fix are left out — there is nothing
        to show on the map for them (they stay in the car's Fahrtenbuch).
        """
        with self._lock:
            return self._conn.execute(
                """
                SELECT 'trip' AS kind, t.id AS id, t.started_at AS ts,
                       t.distance_km AS distance_km, t.duration_s AS duration_s,
                       t.label AS trip_label,
                       c.brand AS car_brand, c.label AS car_label, c.vin AS car_vin,
                       t.car_id AS car_id
                FROM trips t
                JOIN cars c ON c.id = t.car_id
                WHERE EXISTS (
                    SELECT 1 FROM samples s
                    WHERE s.trip_id = t.id
                      AND s.lat IS NOT NULL AND s.lon IS NOT NULL
                      AND NOT (s.lat = 0 AND s.lon = 0)
                )
                UNION ALL
                SELECT 'tour' AS kind, st.id AS id, st.created_at AS ts,
                       NULL AS distance_km, NULL AS duration_s,
                       st.name AS trip_label,
                       NULL AS car_brand, NULL AS car_label, NULL AS car_vin,
                       NULL AS car_id
                FROM saved_tours st
                ORDER BY ts DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()

    def delete_saved_tour(self, tour_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM saved_tours WHERE id=?", (tour_id,))
            self._conn.commit()

    def update_saved_tour(self, tour_id: int, name: str, waypoints_json: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE saved_tours SET name=?, waypoints_json=? WHERE id=?",
                (name, waypoints_json, tour_id),
            )
            self._conn.commit()

    def rename_saved_tour(self, tour_id: int, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE saved_tours SET name=? WHERE id=?",
                (name, tour_id),
            )
            self._conn.commit()
