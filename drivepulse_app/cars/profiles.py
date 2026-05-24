"""Load vehicle profiles from the SQLite database."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from drivepulse_app.db import DriveDB
from drivepulse_app.cars.metadata import _wmi_to_brand
from drivepulse_app.diagnostics import get_logger


log = get_logger(__name__)


def _scan_label(scanned_at: Any, dtc_count: int = 0) -> str:
    try:
        dt = datetime.fromisoformat(str(scanned_at).replace("Z", "+00:00"))
        date_str = dt.strftime("%d.%m.%Y")
    except Exception:
        return ""
    dtc_part = f"{dtc_count} DTC" if dtc_count != 1 else "1 DTC"
    return f"{date_str} · {dtc_part}"


def _load_profiles(db: DriveDB | None = None) -> list[dict[str, Any]]:
    """Return all known cars from the database."""
    if db is None:
        return []


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

        latest_scan_at: str | None = None
        latest_dtc_count: int = 0
        if scans:
            latest = scans[0]
            latest_scan_at = str(latest["scanned_at"]) if latest["scanned_at"] else None
            latest_dtc_count = int(latest["dtc_count"] or 0)
            try:
                data = db.get_scan_data(int(latest["id"]))
            except Exception:
                data = {}
            label_str = _scan_label(latest["scanned_at"], latest_dtc_count)
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

        vin_data_raw = row["vin_data_json"] if "vin_data_json" in row.keys() else None
        vin_data: dict[str, Any] = {}
        if vin_data_raw is not None:
            try:
                vin_data = json.loads(vin_data_raw)
            except Exception:
                pass
        data["vin_data"] = vin_data

        entries.append({
            "path": f"car:{car_id}",
            "data": data,
            "vin": vin,
            "brand": brand,
            "label": row["label"] or "",
            "scan_label": label_str,
            "latest_scan_at": latest_scan_at,
            "latest_dtc_count": latest_dtc_count,
            "car_id": car_id,
            "trip_count": int(row["trip_count"] or 0),
            "total_km": float(row["total_km"] or 0.0),
            "vin_data_fetched": vin_data_raw is not None,
        })

    return entries
