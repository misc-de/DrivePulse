"""Schema definitions, migration steps and shared column tuples."""
from __future__ import annotations

import sqlite3

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

CREATE TABLE IF NOT EXISTS module_discoveries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    car_id      INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    label       TEXT,
    data_json   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_discoveries_car ON module_discoveries(car_id, created_at DESC);

CREATE TABLE IF NOT EXISTS coding_findings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    car_id      INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    module      TEXT,
    tx          TEXT,
    rx          TEXT,
    did         INTEGER NOT NULL,
    byte_index  INTEGER NOT NULL,
    bit_mask    INTEGER NOT NULL DEFAULT 0,
    before_hex  TEXT,
    after_hex   TEXT,
    description TEXT
);
CREATE INDEX IF NOT EXISTS idx_findings_car ON coding_findings(car_id, created_at DESC);

CREATE TABLE IF NOT EXISTS scanned_modules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    car_id      INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    tx          TEXT NOT NULL,
    rx          TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    UNIQUE(car_id, tx, rx)
);
CREATE INDEX IF NOT EXISTS idx_scanned_modules_car ON scanned_modules(car_id);
"""


_SAMPLE_COLUMNS: tuple[str, ...] = (
    "speed_kmh", "obd_speed_kmh", "gps_speed_kmh", "rpm",
    "coolant_c", "throttle_pct", "engine_load", "fuel_pct",
    "intake_c", "maf_gps", "voltage_v",
    "lat", "lon", "altitude_m", "heading_deg", "accel_g",
)
