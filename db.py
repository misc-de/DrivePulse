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
    notes           TEXT
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
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            finally:
                self._conn.close()

    def checkpoint(self) -> None:
        """Pending Inserts persistieren — periodisch aufrufen."""
        with self._lock:
            self._conn.commit()

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

    def delete_trip(self, trip_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM trips WHERE id=?", (trip_id,))
            self._conn.commit()

    def delete_car(self, car_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM cars WHERE id=?", (car_id,))
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
                "SELECT id, car_id, scanned_at, protocol, dtc_count, pending_dtc_count, pids_count"
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
            return {}

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

    def samples_for_trip(self, trip_id: int) -> Iterable[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT * FROM samples WHERE trip_id=? ORDER BY ts",
                (trip_id,),
            ).fetchall())


class TripRecorder:
    """Hält den aktuellen Fahrt-Kontext und merged OBD-/GPS-Payloads in DB-Rows."""

    IDLE_TIMEOUT_S = 120.0

    def __init__(self, db: DriveDB) -> None:
        self.db = db
        self.car_id: int | None = None
        self.trip_id: int | None = None
        self._last_gps: dict[str, float] = {}
        self._last_obd_ts: float = 0.0

    # Identitäts-Update — typischerweise nach erfolgreichem Scan
    def set_car(
        self,
        vin: str | None = None,
        brand: str | None = None,
        cal_id: str | None = None,
        cvn: str | None = None,
        label: str | None = None,
        protocol: str | None = None,
        profile_path: str | None = None,
    ) -> int:
        # Wechsel des Autos beendet eine laufende Fahrt
        new_id = self.db.upsert_car(
            vin=vin, brand=brand, cal_id=cal_id, cvn=cvn,
            label=label, protocol=protocol, profile_path=profile_path,
        )
        if self.car_id is not None and self.car_id != new_id and self.trip_id is not None:
            self.end_trip()
        self.car_id = new_id
        return new_id

    # GPS-Cache, damit OBD-Samples die letzten Koordinaten mitführen
    def update_gps(self, *, lat: float | None = None, lon: float | None = None,
                   altitude_m: float | None = None, heading_deg: float | None = None,
                   gps_speed_kmh: float | None = None) -> None:
        if lat is not None:
            self._last_gps["lat"] = lat
        if lon is not None:
            self._last_gps["lon"] = lon
        if altitude_m is not None:
            self._last_gps["altitude_m"] = altitude_m
        if heading_deg is not None:
            self._last_gps["heading_deg"] = heading_deg
        if gps_speed_kmh is not None:
            self._last_gps["gps_speed_kmh"] = gps_speed_kmh

    def record_obd(self, ts: float, **fields: Any) -> None:
        """Schreibt ein OBD-Sample (inklusive zuletzt gesehener GPS-Daten)."""
        if self.car_id is None:
            # Fahrzeugidentität noch nicht bekannt — Sample verwerfen
            return
        if self.trip_id is None:
            self.trip_id = self.db.start_trip(self.car_id)
        merged = dict(self._last_gps)
        merged.update({k: v for k, v in fields.items() if v is not None})
        self.db.add_sample(self.trip_id, ts, **merged)
        self._last_obd_ts = ts

    def maybe_end_idle_trip(self, now: float) -> bool:
        if self.trip_id is None or not self._last_obd_ts:
            return False
        if now - self._last_obd_ts > self.IDLE_TIMEOUT_S:
            self.end_trip()
            return True
        return False

    def end_trip(self) -> None:
        if self.trip_id is None:
            return
        try:
            self.db.end_trip(self.trip_id)
        finally:
            self.trip_id = None
            self._last_obd_ts = 0.0
