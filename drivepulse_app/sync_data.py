from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import LOG_DIR
from .db import DriveDB

PAIRED_DEVICES_FILE = LOG_DIR / "paired_devices.json"


def export_all(db: DriveDB) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    cars_out: list[dict] = []
    all_trips: list[tuple[dict, dict]] = []

    for car in db.list_cars():
        car_dict: dict[str, Any] = {
            "vin": car["vin"],
            "brand": car["brand"],
            "label": car["label"],
            "cal_id": car["cal_id"],
            "cvn": car["cvn"],
            "protocol": car["protocol"],
            "first_seen": car["first_seen"],
            "last_seen": car["last_seen"],
        }
        trips_out: list[dict] = []
        for trip in db.list_trips_for_car(car["id"]):
            trips_out.append({
                "started_at": trip["started_at"],
                "ended_at": trip["ended_at"],
                "distance_km": trip["distance_km"],
                "duration_s": trip["duration_s"],
                "max_speed_kmh": trip["max_speed_kmh"],
                "avg_speed_kmh": trip["avg_speed_kmh"],
                "samples_count": trip["samples_count"],
            })
        car_dict["trips"] = trips_out
        for t in trips_out:
            all_trips.append((car_dict, t))
        cars_out.append(car_dict)

    # Keep only last 100 trips across all cars
    all_trips.sort(key=lambda x: x[1].get("started_at") or "", reverse=True)
    kept: set[int] = set()
    for i, (car_dict, _) in enumerate(all_trips[:100]):
        kept.add(id(car_dict))

    # Trim trips per car to only those in the top 100
    if len(all_trips) > 100:
        top_100_trips: set[str] = {t.get("started_at", "") for _, t in all_trips[:100]}
        for car_dict in cars_out:
            car_dict["trips"] = [t for t in car_dict["trips"] if t.get("started_at") in top_100_trips]

    return {"version": 1, "exported_at": now, "cars": cars_out}


def import_data(db: DriveDB, data: dict) -> dict:
    if data.get("version") != 1:
        return {"cars_added": 0, "cars_updated": 0, "trips_added": 0}

    cars_added = 0
    cars_updated = 0
    trips_added = 0

    for car in data.get("cars") or []:
        vin = car.get("vin")
        brand = car.get("brand")
        label = car.get("label")
        cal_id = car.get("cal_id")
        cvn = car.get("cvn")
        protocol = car.get("protocol")

        existing = db.list_cars()
        found = None
        if vin:
            for c in existing:
                if c["vin"] == vin:
                    found = c
                    break

        car_id = db.upsert_car(
            vin=vin, brand=brand, label=label,
            cal_id=cal_id, cvn=cvn, protocol=protocol,
        )
        if found is None:
            cars_added += 1
        else:
            cars_updated += 1

        existing_trips = db.list_trips_for_car(car_id)
        existing_started = {t["started_at"] for t in existing_trips}

        for trip in car.get("trips") or []:
            started_at = trip.get("started_at")
            if not started_at or started_at in existing_started:
                continue
            with db._lock:
                cur = db._conn.cursor()
                cur.execute(
                    "INSERT INTO trips(car_id, started_at, ended_at, distance_km,"
                    " duration_s, max_speed_kmh, avg_speed_kmh, samples_count)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        car_id,
                        started_at,
                        trip.get("ended_at"),
                        trip.get("distance_km"),
                        trip.get("duration_s"),
                        trip.get("max_speed_kmh"),
                        trip.get("avg_speed_kmh"),
                        trip.get("samples_count") or 0,
                    ),
                )
                db._conn.commit()
            trips_added += 1

    return {"cars_added": cars_added, "cars_updated": cars_updated, "trips_added": trips_added}


def load_paired_devices() -> list[dict]:
    try:
        return json.loads(PAIRED_DEVICES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_paired_devices(devices: list[dict]) -> None:
    try:
        PAIRED_DEVICES_FILE.parent.mkdir(parents=True, exist_ok=True)
        PAIRED_DEVICES_FILE.write_text(
            json.dumps(devices, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def upsert_paired_device(
    device_id: str,
    name: str,
    spki_fingerprint: str,
    host: str,
    port: int,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    devices = load_paired_devices()
    for d in devices:
        if d.get("device_id") == device_id:
            d["name"] = name
            d["spki_fingerprint"] = spki_fingerprint
            d["host"] = host
            d["port"] = port
            d["last_seen"] = now
            save_paired_devices(devices)
            return
    devices.append({
        "device_id": device_id,
        "name": name,
        "spki_fingerprint": spki_fingerprint,
        "host": host,
        "port": port,
        "last_seen": now,
    })
    save_paired_devices(devices)
