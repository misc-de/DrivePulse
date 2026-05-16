"""Load and merge vehicle profiles from JSON files and the trip database."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .common import PROFILES_DIR
from .db import DriveDB
from .cars_metadata import _extract_inner_string, _wmi_to_brand


def _load_profiles(db: DriveDB | None = None) -> list[dict[str, Any]]:
    """Liefert alle bekannten Autos: aus JSON-Profilen + aus der DB, per VIN gemerged."""
    entries: list[dict[str, Any]] = []
    seen_vins: set[str] = set()

    if PROFILES_DIR.exists():
        for path in sorted(PROFILES_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            vin = _extract_inner_string(data.get("vin"))
            brand = _wmi_to_brand(vin)
            try:
                dt = datetime.fromisoformat(str(data.get("scanned_at", "")).replace("Z", "+00:00"))
                scan_label = dt.strftime("%d.%m.%Y")
            except Exception:
                scan_label = ""
            entries.append({
                "path": path,
                "data": data,
                "vin": vin,
                "brand": brand,
                "scan_label": scan_label,
                "car_id": None,
                "trip_count": 0,
                "total_km": 0.0,
            })
            if vin:
                seen_vins.add(vin)

    if db is not None:
        try:
            db_cars = db.list_cars()
        except Exception:
            db_cars = []
        for entry in entries:
            if not entry["vin"]:
                continue
            for row in db_cars:
                if (row["vin"] or "") == entry["vin"]:
                    entry["car_id"] = int(row["id"])
                    entry["trip_count"] = int(row["trip_count"] or 0)
                    entry["total_km"] = float(row["total_km"] or 0.0)
                    entry["label"] = row["label"] or ""
                    break
        for row in db_cars:
            vin = row["vin"] or ""
            if vin and vin in seen_vins:
                continue
            entries.append({
                "path": None,
                "data": {
                    "vehicle_info": {
                        "VIN": vin or None,
                        "CALIBRATION_ID": row["cal_id"],
                        "CVN": row["cvn"],
                    },
                    "protocol": row["protocol"],
                    "scanned_at": row["first_seen"],
                    "live_data": {},
                },
                "vin": vin,
                "brand": row["brand"] or _wmi_to_brand(vin),
                "label": row["label"] or "",
                "scan_label": "",
                "car_id": int(row["id"]),
                "trip_count": int(row["trip_count"] or 0),
                "total_km": float(row["total_km"] or 0.0),
            })
    return entries
