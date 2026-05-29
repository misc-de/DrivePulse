"""Read-only UDS module discoveries and the per-car coding findings table.

Backs the "Car Lab" feature: a *discovery* is a one-shot inventory of a control
module (which DIDs answered, identification strings, DTCs); a *finding* is one
reverse-engineered byte/bit whose meaning the user described after a
before/after change. Findings accumulate into the car's coding table.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)


class DiscoveriesMixin:
    # Provided by _DriveDBBase when composed into DriveDB. See
    # project_mixin_typing.md.
    _conn: sqlite3.Connection
    _lock: threading.Lock

    # --- module discoveries -------------------------------------------------

    def add_discovery(self, car_id: int, data: dict[str, Any], label: str | None = None) -> int:
        """Store a module-discovery inventory. Returns the new discovery id."""
        created_at = data.get("created_at") or datetime.now(UTC).isoformat()
        blob = json.dumps(data, ensure_ascii=False, default=str)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO module_discoveries(car_id, created_at, label, data_json)"
                " VALUES(?,?,?,?)",
                (car_id, created_at, label, blob),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_discoveries_for_car(self, car_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT id, car_id, created_at, label FROM module_discoveries"
                " WHERE car_id=? ORDER BY created_at DESC",
                (car_id,),
            ).fetchall())

    def get_discovery_data(self, discovery_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT data_json FROM module_discoveries WHERE id=?", (discovery_id,)
            ).fetchone()
        if row is None:
            return {}
        try:
            return json.loads(row["data_json"])
        except json.JSONDecodeError:
            log.exception("Could not decode discovery JSON for id=%s", discovery_id)
            return {}

    def delete_discovery(self, discovery_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM module_discoveries WHERE id=?", (discovery_id,))
            self._conn.commit()

    # --- scanned modules (which control units a scan found present) ---------

    def save_scanned_modules(self, car_id: int, modules: list[dict[str, Any]]) -> None:
        """Replace the set of modules a scan found present on *car_id*.

        Each module is a ``{"name","tx","rx"}`` dict (as produced by the
        reader's module scan). The previous set for this car is dropped so the
        table always reflects the latest scan; the Car Lab's Discover/Functions
        views read from here so they only offer confirmed-present modules.
        """
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute("DELETE FROM scanned_modules WHERE car_id=?", (car_id,))
            self._conn.executemany(
                "INSERT OR REPLACE INTO scanned_modules(car_id, name, tx, rx, last_seen)"
                " VALUES(?,?,?,?,?)",
                [
                    (car_id, m["name"], m["tx"], m["rx"], now)
                    for m in modules
                    if m.get("tx") and m.get("rx")
                ],
            )
            self._conn.commit()

    def list_scanned_modules_for_car(self, car_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT id, car_id, name, tx, rx, last_seen FROM scanned_modules"
                " WHERE car_id=? ORDER BY name",
                (car_id,),
            ).fetchall())

    # --- coding findings ----------------------------------------------------

    def add_finding(self, car_id: int, finding: dict[str, Any]) -> int:
        """Store one reverse-engineered byte/bit + the user's description.

        *finding* keys: module, tx, rx, did, byte_index, bit_mask, before_hex,
        after_hex, description. Returns the new finding id.
        """
        created_at = finding.get("created_at") or datetime.now(UTC).isoformat()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO coding_findings"
                "(car_id, created_at, module, tx, rx, did, byte_index, bit_mask,"
                " before_hex, after_hex, description)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    car_id, created_at,
                    finding.get("module"), finding.get("tx"), finding.get("rx"),
                    int(finding.get("did", 0)), int(finding.get("byte_index", 0)),
                    int(finding.get("bit_mask", 0)),
                    finding.get("before_hex"), finding.get("after_hex"),
                    finding.get("description"),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_findings_for_car(self, car_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT id, car_id, created_at, module, tx, rx, did, byte_index,"
                " bit_mask, before_hex, after_hex, description"
                " FROM coding_findings WHERE car_id=? ORDER BY created_at DESC",
                (car_id,),
            ).fetchall())

    def delete_finding(self, finding_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM coding_findings WHERE id=?", (finding_id,))
            self._conn.commit()
