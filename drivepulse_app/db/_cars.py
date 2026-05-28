"""Car-row CRUD: upsert, listing, lookup, live-flag lifecycle, deletion."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)


class CarsMixin:
    # Provided by _DriveDBBase when composed into DriveDB. See
    # project_mixin_typing.md.
    _conn: sqlite3.Connection
    _lock: threading.Lock

    def upsert_car(
        self,
        vin: str | None = None,
        brand: str | None = None,
        cal_id: str | None = None,
        cvn: str | None = None,
        label: str | None = None,
        protocol: str | None = None,
        profile_path: str | None = None,
        is_live: bool | None = None,
    ) -> int:
        """Insert or update a car row. Returns ``car_id``.

        ``is_live`` flags a temporary live vehicle (defaults to False on
        INSERT, unchanged on UPDATE when None).
        """
        now = datetime.now(UTC).isoformat()
        with self._lock:
            cur = self._conn.cursor()
            row = None
            if vin:
                row = cur.execute("SELECT id FROM cars WHERE vin = ?", (vin,)).fetchone()
            if row is None and profile_path:
                row = cur.execute(
                    "SELECT id FROM cars WHERE profile_path = ? AND vin IS NULL",
                    (profile_path,),
                ).fetchone()
            if row is not None:
                car_id = int(row["id"])
                # User-editable master data (vin, brand, label) is preserved
                # if already set; OBD only fills these when they are still
                # empty. Technical OBD/profile fields (cal_id, cvn, protocol,
                # profile_path) still let the newer scan win.
                if is_live is None:
                    cur.execute(
                        "UPDATE cars SET last_seen=?,"
                        " vin=COALESCE(vin,?),"
                        " brand=COALESCE(brand,?),"
                        " cal_id=COALESCE(?,cal_id),"
                        " cvn=COALESCE(?,cvn),"
                        " label=COALESCE(label,?),"
                        " protocol=COALESCE(?,protocol),"
                        " profile_path=COALESCE(?,profile_path)"
                        " WHERE id=?",
                        (now, vin, brand, cal_id, cvn, label, protocol, profile_path, car_id),
                    )
                else:
                    cur.execute(
                        "UPDATE cars SET last_seen=?,"
                        " vin=COALESCE(vin,?),"
                        " brand=COALESCE(brand,?),"
                        " cal_id=COALESCE(?,cal_id),"
                        " cvn=COALESCE(?,cvn),"
                        " label=COALESCE(label,?),"
                        " protocol=COALESCE(?,protocol),"
                        " profile_path=COALESCE(?,profile_path),"
                        " is_live=?"
                        " WHERE id=?",
                        (now, vin, brand, cal_id, cvn, label, protocol, profile_path,
                         int(bool(is_live)), car_id),
                    )
                if vin:
                    hash_row = cur.execute(
                        "SELECT vin, vin_hash FROM cars WHERE id=?", (car_id,)
                    ).fetchone()
                    if hash_row and hash_row["vin"] and not hash_row["vin_hash"]:
                        h = hashlib.sha256(hash_row["vin"].encode("utf-8")).hexdigest()
                        cur.execute(
                            "UPDATE cars SET vin_hash=? WHERE id=?", (h, car_id)
                        )
            else:
                cur.execute(
                    "INSERT INTO cars(vin,brand,cal_id,cvn,label,protocol,first_seen,last_seen,profile_path,is_live)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (vin, brand, cal_id, cvn, label, protocol, now, now, profile_path,
                     int(bool(is_live)) if is_live is not None else 0),
                )
                car_id = int(cur.lastrowid or 0)
                if vin:
                    h = hashlib.sha256(vin.encode("utf-8")).hexdigest()
                    cur.execute("UPDATE cars SET vin_hash=? WHERE id=?", (h, car_id))
            self._conn.commit()
            return car_id

    def list_cars(self, include_live: bool = False) -> list[sqlite3.Row]:
        """Return all vehicles. Temporary live vehicles are hidden by default —
        callers that need them (telemetry match, cleanup) pass
        ``include_live=True`` explicitly."""
        sql = (
            "SELECT c.*,"
            " (SELECT COUNT(*) FROM trips WHERE car_id=c.id) AS trip_count,"
            " (SELECT COALESCE(SUM(distance_km),0) FROM trips WHERE car_id=c.id) AS total_km"
            " FROM cars c"
        )
        if not include_live:
            sql += " WHERE COALESCE(c.is_live, 0) = 0"
        sql += " ORDER BY last_seen DESC"
        with self._lock:
            return list(self._conn.execute(sql).fetchall())

    def list_live_car_ids(self) -> list[int]:
        """IDs of all vehicles flagged ``is_live`` (temporary cars)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM cars WHERE COALESCE(is_live, 0) = 1"
            ).fetchall()
        return [int(r["id"]) for r in rows]

    def promote_live_car(self, car_id: int) -> None:
        """Turn a temporary live car into a permanent one."""
        with self._lock:
            self._conn.execute("UPDATE cars SET is_live=0 WHERE id=?", (car_id,))
            self._conn.commit()

    def get_car(self, car_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM cars WHERE id=?", (car_id,)).fetchone()

    def update_car_vin_data(self, car_id: int, vin_data_json: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cars SET vin_data_json=? WHERE id=?",
                (vin_data_json, car_id),
            )
            self._conn.commit()

    def save_autodev_raw(self, car_id: int, raw: dict) -> None:
        blob = json.dumps(raw, ensure_ascii=False, default=str)
        with self._lock:
            self._conn.execute(
                "UPDATE cars SET autodev_raw_json=? WHERE id=?",
                (blob, car_id),
            )
            self._conn.commit()

    def get_autodev_raw(self, car_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT autodev_raw_json FROM cars WHERE id=?", (car_id,)
            ).fetchone()
        if row is None or row[0] is None:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None

    def reset_car_vin_data(self, car_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cars SET vin_data_json=NULL WHERE id=?",
                (car_id,),
            )
            self._conn.commit()

    def update_car_vin(self, car_id: int, new_vin: str) -> None:
        new_vin = new_vin.strip().upper()
        h = hashlib.sha256(new_vin.encode()).hexdigest() if new_vin else None
        with self._lock:
            self._conn.execute(
                "UPDATE cars SET vin=?, vin_hash=?, vin_data_json=NULL WHERE id=?",
                (new_vin or None, h, car_id),
            )
            self._conn.commit()

    def update_car_brand(self, car_id: int, brand: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cars SET brand=? WHERE id=?",
                (brand.strip() or None, car_id),
            )
            self._conn.commit()

    def rename_car(self, car_id: int, label: str) -> None:
        """Set the user-defined display name for a vehicle."""
        with self._lock:
            self._conn.execute(
                "UPDATE cars SET label=? WHERE id=?",
                (label or None, car_id),
            )
            self._conn.commit()

    def delete_car(self, car_id: int) -> None:
        """Delete a car and all its dependent rows (trips, samples, scans,
        scan_samples, acceleration_runs, car_photos, share_conflicts).

        Wrapped in an explicit transaction so a mid-way failure rolls back
        completely rather than leaving the car visible but half-stripped.
        Raises ``RuntimeError`` if the cars row is still present afterwards
        — callers used to silently swallow exceptions and rely on the UI
        list refresh, which let phantom rows accumulate.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                self._conn.execute("DELETE FROM samples WHERE trip_id IN (SELECT id FROM trips WHERE car_id=?)", (car_id,))
                self._conn.execute("DELETE FROM trips WHERE car_id=?", (car_id,))
                self._conn.execute("DELETE FROM scan_samples WHERE scan_id IN (SELECT id FROM scans WHERE car_id=?)", (car_id,))
                self._conn.execute("DELETE FROM scans WHERE car_id=?", (car_id,))
                self._conn.execute("DELETE FROM acceleration_runs WHERE car_id=?", (car_id,))
                self._conn.execute("DELETE FROM car_photos WHERE car_id=?", (car_id,))
                self._conn.execute("DELETE FROM share_conflicts WHERE car_id=?", (car_id,))
                self._conn.execute("DELETE FROM cars WHERE id=?", (car_id,))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            still_there = self._conn.execute(
                "SELECT 1 FROM cars WHERE id=?", (car_id,)
            ).fetchone()
            if still_there is not None:
                raise RuntimeError(f"delete_car failed: cars row {car_id} still present")

    def car_has_data(self, car_id: int) -> bool:
        """True if the car has any trips, scans, acceleration_runs, or photos."""
        with self._lock:
            for table, col in (
                ("trips", "car_id"),
                ("scans", "car_id"),
                ("acceleration_runs", "car_id"),
                ("car_photos", "car_id"),
            ):
                row = self._conn.execute(
                    f"SELECT 1 FROM {table} WHERE {col}=? LIMIT 1", (car_id,)
                ).fetchone()
                if row is not None:
                    return True
        return False

    def recover_stale_live_cars(self) -> dict[str, list[int]]:
        """Reconcile is_live=1 cars left over from a previous run.

        Live cars are normally throwaway: the dongle creates them on connect,
        and the user promotes the real one via the "+" button. Anything not
        promoted by the next app start used to be wiped unconditionally —
        which silently destroyed data when a live car had already accumulated
        trips/scans/runs but never got promoted (e.g. app crash, dongle
        disconnect at the wrong moment). It also left these cars invisible to
        the user because ``list_cars`` hides ``is_live=1`` rows.

        New policy:
          - Live car *with* attached data → promote to ``is_live=0`` so the
            user can see it in the list and decide (rename, share, delete).
          - Live car *without* any data → safe to purge, same as before.

        Returns ``{"promoted": [...], "purged": [...]}`` for logging.
        """
        promoted: list[int] = []
        purged: list[int] = []
        for car_id in self.list_live_car_ids():
            if self.car_has_data(car_id):
                self.promote_live_car(car_id)
                promoted.append(car_id)
            else:
                try:
                    self.delete_car(car_id)
                    purged.append(car_id)
                except Exception:
                    log.exception("Could not purge empty live car id=%s", car_id)
        return {"promoted": promoted, "purged": purged}

    def get_car_by_vin_hash(self, vin_hash: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM cars WHERE vin_hash=?", (vin_hash,)
            ).fetchone()
