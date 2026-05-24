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

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from drivepulse_app.diagnostics import get_logger


log = get_logger(__name__)


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
            for stmt in (
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
            ):
                try:
                    self._conn.execute(stmt)
                    self._conn.commit()
                except Exception:
                    pass  # column already exists in older databases
            self._conn.executescript(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_cars_vin_hash ON cars(vin_hash) WHERE vin_hash IS NOT NULL;"
                "CREATE TABLE IF NOT EXISTS share_conflicts ("
                "    id            INTEGER PRIMARY KEY AUTOINCREMENT,"
                "    type          TEXT NOT NULL,"
                "    car_id        INTEGER REFERENCES cars(id) ON DELETE CASCADE,"
                "    local_id      INTEGER NOT NULL,"
                "    incoming_json TEXT NOT NULL,"
                "    received_at   TEXT NOT NULL"
                ");"
            )
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
    ) -> int:
        """Legt einen Auto-Eintrag an oder aktualisiert ihn. Liefert ``car_id``."""
        now = datetime.now(timezone.utc).isoformat()
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
                cur.execute(
                    "UPDATE cars SET last_seen=?,"
                    " brand=COALESCE(?,brand), cal_id=COALESCE(?,cal_id),"
                    " cvn=COALESCE(?,cvn), label=COALESCE(?,label),"
                    " protocol=COALESCE(?,protocol),"
                    " profile_path=COALESCE(?,profile_path)"
                    " WHERE id=?",
                    (now, brand, cal_id, cvn, label, protocol, profile_path, car_id),
                )
            else:
                cur.execute(
                    "INSERT INTO cars(vin,brand,cal_id,cvn,label,protocol,first_seen,last_seen,profile_path)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (vin, brand, cal_id, cvn, label, protocol, now, now, profile_path),
                )
                car_id = int(cur.lastrowid)
                if vin:
                    import hashlib as _hashlib
                    h = _hashlib.sha256(vin.encode("utf-8")).hexdigest()
                    cur.execute("UPDATE cars SET vin_hash=? WHERE id=?", (h, car_id))
            self._conn.commit()
            return car_id

    def list_cars(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT c.*,"
                " (SELECT COUNT(*) FROM trips WHERE car_id=c.id) AS trip_count,"
                " (SELECT COALESCE(SUM(distance_km),0) FROM trips WHERE car_id=c.id) AS total_km"
                " FROM cars c ORDER BY last_seen DESC"
            ).fetchall())

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

    def reset_car_vin_data(self, car_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cars SET vin_data_json=NULL WHERE id=?",
                (car_id,),
            )
            self._conn.commit()

    # ----------------------------------------------------------------- Trips

    def start_trip(self, car_id: int, started_at: datetime | None = None) -> int:
        ts = (started_at or datetime.now(timezone.utc)).isoformat()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("INSERT INTO trips(car_id, started_at) VALUES(?,?)", (car_id, ts))
            self._conn.commit()
            return int(cur.lastrowid)

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
                    datetime.now(timezone.utc).isoformat(),
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

    def get_last_trip_stats(self, car_id: int) -> "dict | None":
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
        with self._lock:
            self._conn.execute("DELETE FROM cars WHERE id=?", (car_id,))
            self._conn.commit()

    def delete_scan(self, scan_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM scans WHERE id=?", (scan_id,))
            self._conn.commit()

    # --------------------------------------------------------------- Scans

    def add_scan(self, car_id: int, data: dict[str, Any]) -> int:
        """Store a full OBD scan snapshot. Returns the new scan id."""
        import json as _json
        scanned_at = data.get("scanned_at") or datetime.now(timezone.utc).isoformat()
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
            return int(cur.lastrowid)

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
        except Exception:
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
        ts = run_at or datetime.now(timezone.utc).isoformat()
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
            return int(cur.lastrowid)

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
        except Exception:
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
        ts = taken_at or datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO car_photos(car_id, filename, taken_at, shared_at) VALUES(?,?,?,?)",
                (car_id, filename, ts, shared_at),
            )
            self._conn.commit()
            return int(cur.lastrowid)

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
        import hashlib
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
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE trips SET seen_at=? WHERE id=? AND seen_at IS NULL",
                (now, trip_id),
            )
            self._conn.commit()

    def mark_scan_seen(self, scan_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE scans SET seen_at=? WHERE id=? AND seen_at IS NULL",
                (now, scan_id),
            )
            self._conn.commit()

    def mark_run_seen(self, run_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE acceleration_runs SET seen_at=? WHERE id=? AND seen_at IS NULL",
                (now, run_id),
            )
            self._conn.commit()

    def mark_photo_seen(self, photo_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE car_photos SET seen_at=? WHERE id=? AND seen_at IS NULL",
                (now, photo_id),
            )
            self._conn.commit()

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
                       c.brand AS car_brand, c.label AS car_label, c.vin AS car_vin
                FROM trips t
                JOIN cars c ON c.id = t.car_id
                UNION ALL
                SELECT 'tour' AS kind, st.id AS id, st.created_at AS ts,
                       NULL AS distance_km, NULL AS duration_s,
                       st.name AS trip_label,
                       NULL AS car_brand, NULL AS car_label, NULL AS car_vin
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


from drivepulse_app.trip_recorder import TripRecorder  # noqa: E402
