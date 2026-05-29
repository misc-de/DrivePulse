from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from drivepulse_app.common import LOG_DIR
from drivepulse_app.db import DriveDB
from drivepulse_app.diagnostics import atomic_write_text, get_logger

PAIRED_DEVICES_FILE = LOG_DIR / "paired_devices.json"
log = get_logger(__name__)

_SAMPLE_COLS = (
    "speed_kmh", "obd_speed_kmh", "gps_speed_kmh", "rpm", "coolant_c",
    "throttle_pct", "engine_load", "fuel_pct", "intake_c", "maf_gps",
    "voltage_v", "lat", "lon", "altitude_m", "heading_deg", "accel_g",
)


def _payload_list(value: Any, *, field: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    log.warning("Ignoring sync payload field %s with non-list value", field)
    return []


def export_all(db: DriveDB) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
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
        # sqlite3.Row has no __contains__, so `in car` checks values not columns — keep .keys().
        vin_data_raw = car["vin_data_json"] if "vin_data_json" in car.keys() else None  # noqa: SIM118
        vin_data: dict | None = None
        if vin_data_raw is not None:
            try:
                vin_data = json.loads(vin_data_raw)
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
        cars_out.append({
            "vin": car["vin"],
            "brand": car["brand"],
            "label": car["label"],
            "cal_id": car["cal_id"],
            "cvn": car["cvn"],
            "protocol": car["protocol"],
            "profile_path": car["profile_path"],
            "first_seen": car["first_seen"],
            "last_seen": car["last_seen"],
            "vin_data": vin_data,
            "trips": trips_out,
        })

    return {"version": 1, "exported_at": now, "cars": cars_out}


def import_data(db: DriveDB, data: dict[str, Any], mode: str = "merge") -> dict[str, Any]:
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
    vin_data_review: list[dict[str, Any]] = []

    for car in _payload_list(data.get("cars"), field="cars"):
        if not isinstance(car, dict):
            log.warning("Skipping malformed car entry in sync payload")
            continue
        vin = car.get("vin")
        profile_path = car.get("profile_path")
        if not isinstance(vin, str):
            vin = None
        if not isinstance(profile_path, str):
            profile_path = None
        if not vin and not profile_path:
            log.warning("Skipping sync car without vin or profile_path")
            continue
        found = None
        if mode != "replace_all":
            for c in db.list_cars():
                if vin and c["vin"] == vin:
                    found = c
                    break
                if profile_path and c["vin"] is None and c["profile_path"] == profile_path:
                    found = c
                    break

        car_id = db.upsert_car(
            vin=vin,
            brand=car.get("brand"),
            label=car.get("label"),
            cal_id=car.get("cal_id"),
            cvn=car.get("cvn"),
            protocol=car.get("protocol"),
            profile_path=profile_path,
        )
        if found is None:
            cars_added += 1
        else:
            cars_updated += 1

        incoming_vin_data = car.get("vin_data")
        if incoming_vin_data is not None:
            try:
                local_row = db.get_car(car_id)
                local_raw = (
                    local_row["vin_data_json"]
                    if local_row and "vin_data_json" in local_row.keys()  # noqa: SIM118
                    else None
                )
                if local_raw is None:
                    db.update_car_vin_data(car_id, json.dumps(incoming_vin_data))
                elif isinstance(incoming_vin_data, dict) and incoming_vin_data:
                    try:
                        local_vin: dict = json.loads(local_raw)
                    except (json.JSONDecodeError, ValueError):
                        local_vin = {}
                    new_fields = {
                        k: v for k, v in incoming_vin_data.items()
                        if local_vin.get(k) != v
                    }
                    if new_fields:
                        vin_data_review.append({
                            "car_id": car_id,
                            "vin": vin or "",
                            "fields": new_fields,
                        })
            except Exception:
                log.warning("Could not process vin_data for car_id=%s", car_id, exc_info=True)

        if mode in ("replace", "replace_all"):
            with db._lock:
                db._conn.execute("DELETE FROM trips WHERE car_id=?", (car_id,))
                db._conn.commit()
            existing_started: set[str] = set()
        else:
            existing_started = {t["started_at"] for t in db.list_trips_for_car(car_id)}

        for trip in _payload_list(car.get("trips"), field="car.trips"):
            if not isinstance(trip, dict):
                log.warning("Skipping malformed trip entry for vin=%s", vin)
                continue
            started_at = trip.get("started_at")
            if not isinstance(started_at, str) or not started_at:
                log.warning("Skipping sync trip without string started_at for vin=%s", vin)
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
                trip_id = int(cur.lastrowid or 0)
                db._conn.commit()
            trips_added += 1

            try:
                db.add_samples(trip_id, _payload_list(trip.get("samples"), field="trip.samples"))
            except Exception:
                log.exception("Could not import samples for trip started_at=%s", started_at)

            with db._lock:
                row = db._conn.execute(
                    "SELECT COUNT(*) AS n FROM samples WHERE trip_id=?",
                    (trip_id,),
                ).fetchone()
                actual_samples = int(row["n"] if row is not None else 0)
                db._conn.execute(
                    "UPDATE trips SET samples_count=? WHERE id=?",
                    (actual_samples, trip_id),
                )
                db._conn.commit()
            samples_added += actual_samples

    return {
        "cars_added": cars_added,
        "cars_updated": cars_updated,
        "trips_added": trips_added,
        "samples_added": samples_added,
        "vin_data_review": vin_data_review,
    }


def load_paired_devices() -> list[dict[str, Any]]:
    try:
        payload = json.loads(PAIRED_DEVICES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        log.warning("Ignoring invalid paired devices JSON at %s", PAIRED_DEVICES_FILE)
        return []
    except OSError as exc:
        log.warning("Could not read paired devices from %s: %s", PAIRED_DEVICES_FILE, exc)
        return []
    if not isinstance(payload, list):
        log.warning("Ignoring paired devices JSON with non-list root at %s", PAIRED_DEVICES_FILE)
        return []
    devices = [device for device in payload if isinstance(device, dict)]
    if len(devices) != len(payload):
        log.warning("Ignoring malformed paired device entries at %s", PAIRED_DEVICES_FILE)
    return devices


def save_paired_devices(devices: list[dict[str, Any]]) -> None:
    try:
        atomic_write_text(
            PAIRED_DEVICES_FILE,
            json.dumps(devices, ensure_ascii=False, indent=2),
            mode=0o600,
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
    now = datetime.now(UTC).isoformat()
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
