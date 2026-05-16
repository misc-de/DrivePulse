from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .common import LOG_DIR
from .db import DriveDB
from .diagnostics import get_logger

PAIRED_DEVICES_FILE = LOG_DIR / "paired_devices.json"
log = get_logger(__name__)

_SAMPLE_COLS = (
    "speed_kmh", "obd_speed_kmh", "gps_speed_kmh", "rpm", "coolant_c",
    "throttle_pct", "engine_load", "fuel_pct", "intake_c", "maf_gps",
    "voltage_v", "lat", "lon", "altitude_m", "heading_deg", "accel_g",
)


def export_all(db: DriveDB) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    cars_out: list[dict[str, Any]] = []

    for car in db.list_cars():
        trips_out: list[dict[str, Any]] = []
        for trip in db.list_trips_for_car(car["id"]):
            samples_out = [
                {k: row[k] for k in ("ts", *_SAMPLE_COLS) if row[k] is not None}
                for row in db.samples_for_trip(trip["id"])
            ]
            trips_out.append({
                "started_at": trip["started_at"],
                "ended_at": trip["ended_at"],
                "distance_km": trip["distance_km"],
                "duration_s": trip["duration_s"],
                "max_speed_kmh": trip["max_speed_kmh"],
                "avg_speed_kmh": trip["avg_speed_kmh"],
                "samples_count": trip["samples_count"],
                "samples": samples_out,
            })
        cars_out.append({
            "vin": car["vin"],
            "brand": car["brand"],
            "label": car["label"],
            "cal_id": car["cal_id"],
            "cvn": car["cvn"],
            "protocol": car["protocol"],
            "first_seen": car["first_seen"],
            "last_seen": car["last_seen"],
            "trips": trips_out,
        })

    return {"version": 1, "exported_at": now, "cars": cars_out}


def import_data(db: DriveDB, data: dict[str, Any], mode: str = "merge") -> dict[str, int]:
    """Import sync data into the local DB.

    mode:
      "merge"       — insert missing cars/trips/samples, skip existing
      "replace"     — for each incoming car delete all its local trips first, then insert
      "replace_all" — wipe the entire local DB first, then insert everything fresh
    """
    if not isinstance(data, dict) or data.get("version") != 1:
        log.warning("Ignoring unsupported sync payload")
        return {"cars_added": 0, "cars_updated": 0, "trips_added": 0, "samples_added": 0}
    if mode not in {"merge", "replace", "replace_all"}:
        log.warning("Unknown sync import mode %r; falling back to merge", mode)
        mode = "merge"

    if mode == "replace_all":
        with db._lock:
            db._conn.execute("DELETE FROM cars")  # trips + samples cascade
            db._conn.commit()

    cars_added = 0
    cars_updated = 0
    trips_added = 0
    samples_added = 0

    for car in data.get("cars") or []:
        if not isinstance(car, dict):
            log.warning("Skipping malformed car entry in sync payload")
            continue
        vin = car.get("vin")
        found = None
        if vin and mode != "replace_all":
            for c in db.list_cars():
                if c["vin"] == vin:
                    found = c
                    break

        car_id = db.upsert_car(
            vin=vin,
            brand=car.get("brand"),
            label=car.get("label"),
            cal_id=car.get("cal_id"),
            cvn=car.get("cvn"),
            protocol=car.get("protocol"),
        )
        if found is None:
            cars_added += 1
        else:
            cars_updated += 1

        if mode in ("replace", "replace_all"):
            with db._lock:
                db._conn.execute("DELETE FROM trips WHERE car_id=?", (car_id,))
                db._conn.commit()
            existing_started: set[str] = set()
        else:
            existing_started = {t["started_at"] for t in db.list_trips_for_car(car_id)}

        for trip in car.get("trips") or []:
            if not isinstance(trip, dict):
                log.warning("Skipping malformed trip entry for vin=%s", vin)
                continue
            started_at = trip.get("started_at")
            if not started_at:
                continue
            if started_at in existing_started:
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
                trip_id = int(cur.lastrowid)
                db._conn.commit()
            trips_added += 1

            for s in trip.get("samples") or []:
                if not isinstance(s, dict):
                    log.warning("Skipping malformed sample for trip started_at=%s", started_at)
                    continue
                ts = s.get("ts")
                if ts is None:
                    continue
                try:
                    db.add_sample(
                        trip_id, float(ts),
                        **{k: v for k, v in s.items() if k in _SAMPLE_COLS and v is not None},
                    )
                    samples_added += 1
                except Exception:
                    log.exception("Could not import sample for trip started_at=%s", started_at)

    return {
        "cars_added": cars_added,
        "cars_updated": cars_updated,
        "trips_added": trips_added,
        "samples_added": samples_added,
    }


def load_paired_devices() -> list[dict[str, Any]]:
    try:
        return json.loads(PAIRED_DEVICES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        log.warning("Ignoring invalid paired devices JSON at %s", PAIRED_DEVICES_FILE)
        return []
    except OSError as exc:
        log.warning("Could not read paired devices from %s: %s", PAIRED_DEVICES_FILE, exc)
        return []


def save_paired_devices(devices: list[dict[str, Any]]) -> None:
    try:
        PAIRED_DEVICES_FILE.parent.mkdir(parents=True, exist_ok=True)
        PAIRED_DEVICES_FILE.write_text(
            json.dumps(devices, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        log.exception("Could not save paired devices to %s", PAIRED_DEVICES_FILE)


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
