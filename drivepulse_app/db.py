"""SQLite-Datenspeicher für Fahrzeuge, Fahrten und Telemetrie-Samples.

Schema (siehe ``_SCHEMA``):
- ``cars``     : ein Eintrag pro Fahrzeug (eindeutig per VIN, fallback per Profil-Pfad)
- ``trips``    : einzelne Fahrt, gebunden an ein Auto
- ``samples``  : ~1–2 Hz Telemetrie-Punkt (OBD + GPS gemerged)

Optimiert auf:
- Sehr schnelles Anhängen von Samples (WAL-Journal, INSERT OR IGNORE, Primärschlüssel ``(trip_id, ts)``).
- Schnelles Listen aller Fahrten eines Autos (Index ``car_id, started_at DESC``).
- Nachvollziehen einer Fahrt mit GPS-Track (Sample-Reihenfolge implizit per PK).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)

_SCHEMA_VERSION = 3


def _is_duplicate_column_error(exc: sqlite3.OperationalError) -> bool:
    return "duplicate column name" in str(exc).lower()


_MIGRATION_STATEMENTS_V1 = (
    "ALTER TABLE trips ADD COLUMN label TEXT",
    "ALTER TABLE cars ADD COLUMN vin_hash TEXT",
    "ALTER TABLE cars ADD COLUMN vin_anon TEXT",
    "ALTER TABLE trips ADD COLUMN seen_at TEXT",
    "ALTER TABLE trips ADD COLUMN shared_at TEXT",
    "ALTER TABLE scans ADD COLUMN seen_at TEXT",
    "ALTER TABLE scans ADD COLUMN shared_at TEXT",
    "ALTER TABLE acceleration_runs ADD COLUMN seen_at TEXT",
    "ALTER TABLE acceleration_runs ADD COLUMN shared_at TEXT",
    "ALTER TABLE cars ADD COLUMN vin_data_json TEXT",
    "ALTER TABLE car_photos ADD COLUMN seen_at TEXT",
    "ALTER TABLE car_photos ADD COLUMN shared_at TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_cars_vin_hash ON cars(vin_hash) WHERE vin_hash IS NOT NULL",
    "CREATE TABLE IF NOT EXISTS share_conflicts ("
    "    id            INTEGER PRIMARY KEY AUTOINCREMENT,"
    "    type          TEXT NOT NULL,"
    "    car_id        INTEGER REFERENCES cars(id) ON DELETE CASCADE,"
    "    local_id      INTEGER NOT NULL,"
    "    incoming_json TEXT NOT NULL,"
    "    received_at   TEXT NOT NULL"
    ")",
)

_MIGRATION_STATEMENTS_V2 = (
    "ALTER TABLE cars ADD COLUMN is_live INTEGER NOT NULL DEFAULT 0",
)

_MIGRATION_STATEMENTS_V3 = (
    "ALTER TABLE cars ADD COLUMN autodev_raw_json TEXT",
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS cars (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vin             TEXT UNIQUE,
    brand           TEXT,
    cal_id          TEXT,
    cvn             TEXT,
    label           TEXT,
    protocol        TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    profile_path    TEXT
);

CREATE TABLE IF NOT EXISTS trips (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    car_id          INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    distance_km     REAL,
    duration_s      REAL,
    max_speed_kmh   REAL,
    avg_speed_kmh   REAL,
    samples_count   INTEGER NOT NULL DEFAULT 0,
    notes           TEXT,
    label           TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    trip_id         INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    ts              REAL NOT NULL,
    speed_kmh       REAL,
    obd_speed_kmh   REAL,
    gps_speed_kmh   REAL,
    rpm             REAL,
    coolant_c       REAL,
    throttle_pct    REAL,
    engine_load     REAL,
    fuel_pct        REAL,
    intake_c        REAL,
    maf_gps         REAL,
    voltage_v       REAL,
    lat             REAL,
    lon             REAL,
    altitude_m      REAL,
    heading_deg     REAL,
    accel_g         REAL,
    PRIMARY KEY (trip_id, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_trips_car_started ON trips(car_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_cars_vin          ON cars(vin);
CREATE INDEX IF NOT EXISTS idx_cars_profile_path ON cars(profile_path) WHERE vin IS NULL;

CREATE TABLE IF NOT EXISTS scans (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    car_id            INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    scanned_at        TEXT NOT NULL,
    protocol          TEXT,
    dtc_count         INTEGER NOT NULL DEFAULT 0,
    pending_dtc_count INTEGER NOT NULL DEFAULT 0,
    pids_count        INTEGER NOT NULL DEFAULT 0,
    data_json         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_scans_car_date ON scans(car_id, scanned_at DESC);

CREATE TABLE IF NOT EXISTS scan_samples (
    scan_id  INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    ts       REAL    NOT NULL,
    pid      TEXT    NOT NULL,
    value    REAL    NOT NULL,
    unit     TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (scan_id, ts, pid)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_scan_samples_lookup ON scan_samples(scan_id, pid, ts);

CREATE TABLE IF NOT EXISTS acceleration_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    car_id          INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    run_at          TEXT NOT NULL,
    lat             REAL,
    lon             REAL,
    results_json    TEXT NOT NULL DEFAULT '{}',
    samples_json    TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_accel_runs_car ON acceleration_runs(car_id, run_at DESC);

CREATE TABLE IF NOT EXISTS car_photos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    car_id      INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    taken_at    TEXT NOT NULL,
    label       TEXT
);
CREATE INDEX IF NOT EXISTS idx_car_photos_car ON car_photos(car_id, taken_at DESC);

CREATE TABLE IF NOT EXISTS saved_tours (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    waypoints_json  TEXT NOT NULL
);
"""


_SAMPLE_COLUMNS: tuple[str, ...] = (
    "speed_kmh", "obd_speed_kmh", "gps_speed_kmh", "rpm",
    "coolant_c", "throttle_pct", "engine_load", "fuel_pct",
    "intake_c", "maf_gps", "voltage_v",
    "lat", "lon", "altitude_m", "heading_deg", "accel_g",
)


class DriveDB:
    """Schlanker Wrapper um ``sqlite3``."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._run_migrations()
            self._backfill_vin_hashes()

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
                self._conn.execute("PRAGMA optimize")
            finally:
                self._conn.close()

    def checkpoint(self) -> None:
        """Pending Inserts persistieren — periodisch aufrufen."""
        with self._lock:
            self._conn.commit()
            self._conn.execute("PRAGMA optimize")

    def _run_migrations(self) -> None:
        row = self._conn.execute("PRAGMA user_version").fetchone()
        version = int(row[0] if row is not None else 0)
        if version > _SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {version} is newer than DrivePulse supports ({_SCHEMA_VERSION})"
            )
        if version < 1:
            self._apply_migration(_MIGRATION_STATEMENTS_V1)
        if version < 2:
            self._apply_migration(_MIGRATION_STATEMENTS_V2)
        if version < 3:
            self._apply_migration(_MIGRATION_STATEMENTS_V3)
        if version < _SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            self._conn.commit()

    def _apply_migration(self, stmts: tuple[str, ...]) -> None:
        for stmt in stmts:
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if stmt.lstrip().upper().startswith("ALTER TABLE") and _is_duplicate_column_error(exc):
                    continue
                raise

    # ------------------------------------------------------------------ Cars

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
        """Legt einen Auto-Eintrag an oder aktualisiert ihn. Liefert ``car_id``.

        ``is_live`` markiert ein temporäres Live-Fahrzeug (Default False bei
        INSERT, unverändert bei UPDATE wenn None).
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
        """Liefert alle Fahrzeuge. Temporäre Live-Fahrzeuge sind per Default
        ausgeblendet — Aufrufer, die sie brauchen (Telemetry-Match, Cleanup),
        setzen ``include_live=True`` explizit."""
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
        """IDs aller als `is_live` markierten (temporären) Fahrzeuge."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM cars WHERE COALESCE(is_live, 0) = 1"
            ).fetchall()
        return [int(r["id"]) for r in rows]

    def promote_live_car(self, car_id: int) -> None:
        """Permanentes Fahrzeug aus einem temporären Live-Auto machen."""
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

    # ----------------------------------------------------------------- Trips

    def start_trip(self, car_id: int, started_at: datetime | None = None) -> int:
        ts = (started_at or datetime.now(UTC)).isoformat()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("INSERT INTO trips(car_id, started_at) VALUES(?,?)", (car_id, ts))
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def end_trip(self, trip_id: int) -> None:
        """Aggregat-Spalten berechnen + ``ended_at`` setzen. Leere Fahrten werden gelöscht."""
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
        """Min/Max-Werte des letzten abgeschlossenen Trips für car_id."""
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

    def rename_car(self, car_id: int, label: str) -> None:
        """Setzt den benutzerdefinierten Anzeigenamen eines Fahrzeugs."""
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

    def delete_scan(self, scan_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM scans WHERE id=?", (scan_id,))
            self._conn.commit()

    # --------------------------------------------------------------- Scans

    def add_scan(self, car_id: int, data: dict[str, Any]) -> int:
        """Store a full OBD scan snapshot. Returns the new scan id."""
        import json as _json
        scanned_at = data.get("scanned_at") or datetime.now(UTC).isoformat()
        protocol = data.get("protocol")
        dtc_count = len(data.get("dtcs") or [])
        pending_count = len(data.get("pending_dtcs") or [])
        pids_count = len(data.get("supported_pids") or [])
        blob = _json.dumps(data, ensure_ascii=False, default=str)
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
        """Aktualisiert Snapshot-Daten eines bestehenden Scans (Rescan in gleicher Session)."""
        import json as _json
        dtc_count = len(data.get("dtcs") or [])
        pending_count = len(data.get("pending_dtcs") or [])
        pids_count = len(data.get("supported_pids") or [])
        blob = _json.dumps(data, ensure_ascii=False, default=str)
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
        import json as _json
        with self._lock:
            row = self._conn.execute(
                "SELECT data_json FROM scans WHERE id=?", (scan_id,)
            ).fetchone()
        if row is None:
            return {}
        try:
            return _json.loads(row["data_json"])
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

    TRIP_MERGE_MAX_GAP_S: float = 30 * 60   # 30 min between consecutive trips
    TRIP_MERGE_FILL_INTERVAL_S: float = 1.0
    TRIP_MERGE_FILL_GAP_THRESHOLD_S: float = 5.0

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
                    row_values = [survivor_id, t]
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

    SCAN_MERGE_MAX_GAP_S: float = 30 * 60   # 30 min between consecutive scans
    SCAN_MERGE_FILL_INTERVAL_S: float = 1.0  # zero-fill cadence
    SCAN_MERGE_FILL_GAP_THRESHOLD_S: float = 5.0  # only fill gaps wider than this

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


    # --------------------------------------------------------------- Samples

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

    # -------------------------------------------------------- StopWatch runs
    # NB: the underlying SQL table is still `acceleration_runs` for backward
    # compatibility with existing user databases.

    def add_stopwatch_run(
        self,
        car_id: int,
        results: dict[str, Any],
        samples: list[Any],
        lat: float | None = None,
        lon: float | None = None,
        run_at: str | None = None,
    ) -> int:
        import json as _json
        ts = run_at or datetime.now(UTC).isoformat()
        blob_results = _json.dumps(results, ensure_ascii=False, default=str)
        blob_samples = _json.dumps(samples, ensure_ascii=False, default=str)
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
        import json as _json
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
                "results": _json.loads(row["results_json"]),
                "samples": _json.loads(row["samples_json"]),
            }
        except json.JSONDecodeError:
            log.exception("Could not decode stopwatch run JSON for run_id=%s", run_id)
            return {}

    def delete_stopwatch_run(self, run_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM acceleration_runs WHERE id=?", (run_id,))
            self._conn.commit()

    # ---------------------------------------------------------- Car photos

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

    # ---------------------------------------------------------- VIN hash helpers

    def _backfill_vin_hashes(self) -> None:
        rows = self._conn.execute(
            "SELECT id, vin FROM cars WHERE vin IS NOT NULL AND vin_hash IS NULL"
        ).fetchall()
        for row in rows:
            h = hashlib.sha256(row["vin"].encode("utf-8")).hexdigest()
            self._conn.execute("UPDATE cars SET vin_hash=? WHERE id=?", (h, row["id"]))
        if rows:
            self._conn.commit()

    def get_car_by_vin_hash(self, vin_hash: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM cars WHERE vin_hash=?", (vin_hash,)
            ).fetchone()

    # ---------------------------------------------------------- seen_at helpers

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

    # ---------------------------------------------------------- share conflicts

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
        import json as _json
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM share_conflicts WHERE id=?", (conflict_id,)
            ).fetchone()
            if row is None:
                return
            incoming = _json.loads(row["incoming_json"])
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
                        _json.dumps(incoming.get("results", {})),
                        _json.dumps(incoming.get("samples", [])),
                        local_id,
                    ),
                )
            self._conn.execute("DELETE FROM share_conflicts WHERE id=?", (conflict_id,))
            self._conn.commit()

    # ── Saved tours ───────────────────────────────────────────────────────────

    def save_tour(self, name: str, waypoints_json: str, created_at: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO saved_tours (name, created_at, waypoints_json) VALUES (?,?,?)",
                (name, created_at, waypoints_json),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

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
