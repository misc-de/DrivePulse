"""Load vehicle profiles exclusively from the SQLite database.

Legacy JSON profiles in PROFILES_DIR are migrated into the scans table on
first run and then renamed to *.json.migrated so they are not processed again.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .common import PROFILES_DIR
from .db import DriveDB
from .cars_metadata import _extract_inner_string, _wmi_to_brand
from .diagnostics import get_logger


log = get_logger(__name__)


def _scan_label(scanned_at: Any) -> str:
    try:
        dt = datetime.fromisoformat(str(scanned_at).replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y")
    except Exception:
        return ""


def _migrate_json_profiles(db: DriveDB) -> None:
    """Import legacy JSON profiles into the scans table (runs once per file)."""
    if not PROFILES_DIR.exists():
        return
    for path in sorted(PROFILES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Migration: could not read %s", path)
            continue

        vin = _extract_inner_string(
            (data.get("vehicle_info") or {}).get("VIN") or data.get("vin")
        )
        scanned_at = (data.get("scanned_at") or "")[:10]

        car_id: int | None = None
        try:
            for row in db.list_cars():
                if vin and (row["vin"] or "") == vin:
                    car_id = int(row["id"])
                    break
                if (row["profile_path"] or "") == str(path):
                    car_id = int(row["id"])
                    break
        except Exception:
            log.exception("Migration: DB lookup failed for %s", path)
            continue

        if car_id is None:
            log.info("Migration: no DB car found for %s — skipping", path.name)
            continue

        # Skip if a scan with the same date already exists.
        try:
            existing = db.list_scans_for_car(car_id)
            if scanned_at and any((r["scanned_at"] or "").startswith(scanned_at) for r in existing):
                path.rename(path.with_suffix(".json.migrated"))
                log.info("Migration: scan already in DB, archived %s", path.name)
                continue
        except Exception:
            log.exception("Migration: could not check existing scans for %s", path.name)
            continue

        try:
            db.add_scan(car_id, data)
            path.rename(path.with_suffix(".json.migrated"))
            log.info("Migration: imported %s into DB", path.name)
        except Exception:
            log.exception("Migration: could not import %s", path.name)


def _load_profiles(db: DriveDB | None = None) -> list[dict[str, Any]]:
    """Return all known cars from the database only."""
    if db is None:
        return []

    _migrate_json_profiles(db)

    try:
        db_cars = db.list_cars()
    except Exception:
        log.exception("Could not list cars from database")
        return []

    entries: list[dict[str, Any]] = []
    for row in db_cars:
        car_id = int(row["id"])
        vin = row["vin"] or ""
        brand = row["brand"] or _wmi_to_brand(vin)

        # Use the latest scan's full data blob; fall back to a minimal dict.
        data: dict[str, Any]
        label_str = ""
        try:
            scans = db.list_scans_for_car(car_id)
        except Exception:
            scans = []

        if scans:
            latest = scans[0]
            try:
                data = db.get_scan_data(int(latest["id"]))
            except Exception:
                data = {}
            label_str = _scan_label(latest["scanned_at"])
        else:
            data = {
                "vehicle_info": {
                    "VIN": vin or None,
                    "CALIBRATION_ID": row["cal_id"],
                    "CVN": row["cvn"],
                },
                "protocol": row["protocol"],
                "scanned_at": row["first_seen"],
                "live_data": {},
            }

        entries.append({
            "path": f"car:{car_id}",
            "data": data,
            "vin": vin,
            "brand": brand,
            "label": row["label"] or "",
            "scan_label": label_str,
            "car_id": car_id,
            "trip_count": int(row["trip_count"] or 0),
            "total_km": float(row["total_km"] or 0.0),
        })

    return entries
