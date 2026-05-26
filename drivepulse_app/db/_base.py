"""Connection lifecycle, migrations and VIN hash backfill."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path

from drivepulse_app.db._schema import (
    _MIGRATION_STATEMENTS_V1,
    _MIGRATION_STATEMENTS_V2,
    _MIGRATION_STATEMENTS_V3,
    _SCHEMA,
    _SCHEMA_VERSION,
)


class _DriveDBBase:
    """Initializes the SQLite connection and applies pending migrations.

    All mixin methods rely on ``self._conn`` and ``self._lock`` set here.
    """

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
        """Persist pending inserts — call periodically."""
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
        # Look up the predicate via the package module so tests can monkeypatch
        # ``drivepulse_app.db._is_duplicate_column_error`` to simulate a buggy
        # detector.
        import drivepulse_app.db as _pkg

        for stmt in stmts:
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if stmt.lstrip().upper().startswith("ALTER TABLE") and _pkg._is_duplicate_column_error(exc):
                    continue
                raise

    def _backfill_vin_hashes(self) -> None:
        rows = self._conn.execute(
            "SELECT id, vin FROM cars WHERE vin IS NOT NULL AND vin_hash IS NULL"
        ).fetchall()
        for row in rows:
            h = hashlib.sha256(row["vin"].encode("utf-8")).hexdigest()
            self._conn.execute("UPDATE cars SET vin_hash=? WHERE id=?", (h, row["id"]))
        if rows:
            self._conn.commit()
