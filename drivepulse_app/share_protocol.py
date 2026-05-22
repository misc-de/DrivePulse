"""Share protocol: payload builders, VIN helpers, and server-side import logic."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .db import DriveDB
from .diagnostics import get_logger

log = get_logger(__name__)


def make_vin_hash(vin: str) -> str:
    return hashlib.sha256(vin.encode("utf-8")).hexdigest()


def make_anon_vin(vin: str) -> str:
    if len(vin) < 8:
        return vin
    return vin[:4] + "0" * (len(vin) - 8) + vin[-4:]


def build_vehicle_block(car: Any, anon: bool, include_obd: bool) -> dict:
    block: dict = {
        "vin_hash": car["vin_hash"],
        "label": car["label"] or "",
    }
    vin = car["vin"] or "" if car["vin"] else ""
    if vin:
        block["vin_anon"] = make_anon_vin(vin)
    if not anon and vin:
        block["vin"] = vin
        block["brand"] = car["brand"] or ""
    if not anon and include_obd:
        for key in ("cal_id", "cvn", "protocol"):
            val = car[key] if key in car.keys() else None
            if val:
                block[key] = val
    return block


def build_trips_payload(db: DriveDB, car_id: int, trip_ids: list[int] | None = None) -> list[dict]:
    from .sync_data import _SAMPLE_COLS
    trips = db.list_trips_for_car(car_id)
    out = []
    for trip in trips:
        if trip_ids is not None and int(trip["id"]) not in trip_ids:
            continue
        samples_out = [
            {k: row[k] for k in ("ts", *_SAMPLE_COLS) if row[k] is not None}
            for row in db.samples_for_trip(trip["id"])
        ]
        out.append({
            "started_at": trip["started_at"],
            "ended_at": trip["ended_at"],
            "distance_km": trip["distance_km"],
            "duration_s": trip["duration_s"],
            "max_speed_kmh": trip["max_speed_kmh"],
            "avg_speed_kmh": trip["avg_speed_kmh"],
            "samples_count": trip["samples_count"],
            "label": trip["label"] if "label" in trip.keys() else None,
            "samples": samples_out,
        })
    return out


def build_runs_payload(db: DriveDB, car_id: int, run_ids: list[int] | None = None) -> list[dict]:
    runs = db.list_stopwatch_runs_for_car(car_id)
    out = []
    for run in runs:
        if run_ids is not None and int(run["id"]) not in run_ids:
            continue
        full = db.get_stopwatch_run(int(run["id"]))
        out.append({
            "run_at": run["run_at"],
            "lat": run["lat"],
            "lon": run["lon"],
            "results": full.get("results", {}),
            "samples": full.get("samples", []),
        })
    return out


def build_scans_payload(db: DriveDB, car_id: int, scan_ids: list[int] | None = None) -> list[dict]:
    scans = db.list_scans_for_car(car_id)
    out = []
    for scan in scans:
        if scan_ids is not None and int(scan["id"]) not in scan_ids:
            continue
        data = db.get_scan_data(int(scan["id"]))
        out.append({
            "scanned_at": scan["scanned_at"],
            "protocol": scan["protocol"],
            "dtc_count": scan["dtc_count"],
            "pending_dtc_count": scan["pending_dtc_count"],
            "pids_count": scan["pids_count"],
            "data_json": json.dumps(data, ensure_ascii=False),
        })
    return out


def _round2(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _trips_identical(existing: Any, incoming: dict) -> bool:
    fields = ("distance_km", "duration_s", "max_speed_kmh", "avg_speed_kmh")
    for f in fields:
        if _round2(existing[f]) != _round2(incoming.get(f)):
            return False
    try:
        if int(existing["samples_count"] or 0) != int(incoming.get("samples_count") or 0):
            return False
    except (TypeError, ValueError):
        pass
    return True


def build_tour_payload(tour: Any) -> dict:
    return {
        "name": tour["name"],
        "created_at": tour["created_at"],
        "waypoints_json": tour["waypoints_json"],
    }


def share_import(db: DriveDB, payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        log.warning("Ignoring invalid share payload")
        return {"ok": False, "error": "invalid payload"}

    if payload.get("type") == "share_tours":
        now = datetime.now(timezone.utc).isoformat()
        existing = {t["name"]: t for t in db.list_saved_tours()}
        tours_added = 0
        for tour in payload.get("tours") or []:
            if not isinstance(tour, dict) or not tour.get("name"):
                continue
            if tour["name"] in existing:
                continue
            db.save_tour(tour["name"], tour.get("waypoints_json", "[]"), tour.get("created_at") or now)
            tours_added += 1
        return {"ok": True, "tours_added": tours_added}

    if payload.get("type") != "share":
        log.warning("Ignoring invalid share payload")
        return {"ok": False, "error": "invalid payload"}

    now = datetime.now(timezone.utc).isoformat()
    trips_added = 0
    runs_added = 0
    scans_added = 0
    conflicts = 0

    vehicle = payload.get("vehicle") or {}
    vin_hash = vehicle.get("vin_hash")
    if not vin_hash:
        return {"ok": False, "error": "missing vin_hash"}

    car = db.get_car_by_vin_hash(vin_hash)
    if car is None:
        vin = vehicle.get("vin")
        vin_anon = vehicle.get("vin_anon")
        label = vehicle.get("label") or ""
        with db._lock:
            cur = db._conn.cursor()
            cur.execute(
                "INSERT INTO cars(vin, brand, cal_id, cvn, label, protocol,"
                " first_seen, last_seen, vin_hash, vin_anon)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    vin,
                    vehicle.get("brand"),
                    vehicle.get("cal_id"),
                    vehicle.get("cvn"),
                    label,
                    vehicle.get("protocol"),
                    now,
                    now,
                    vin_hash,
                    vin_anon,
                ),
            )
            car_id = int(cur.lastrowid)
            db._conn.commit()
    else:
        car_id = int(car["id"])

    # ---- trips ----
    existing_trips = db.list_trips_for_car(car_id)
    existing_by_started = {t["started_at"]: t for t in existing_trips}

    for trip in payload.get("trips") or []:
        if not isinstance(trip, dict):
            continue
        started_at = trip.get("started_at")
        if not started_at:
            continue
        if started_at in existing_by_started:
            existing = existing_by_started[started_at]
            if _trips_identical(existing, trip):
                continue
            conflict_data = json.dumps(trip, ensure_ascii=False)
            with db._lock:
                db._conn.execute(
                    "INSERT INTO share_conflicts(type, car_id, local_id, incoming_json, received_at)"
                    " VALUES(?,?,?,?,?)",
                    ("trip", car_id, int(existing["id"]), conflict_data, now),
                )
                db._conn.commit()
            conflicts += 1
            continue
        with db._lock:
            cur = db._conn.cursor()
            cur.execute(
                "INSERT INTO trips(car_id, started_at, ended_at, distance_km,"
                " duration_s, max_speed_kmh, avg_speed_kmh, samples_count, label,"
                " shared_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    car_id,
                    started_at,
                    trip.get("ended_at"),
                    trip.get("distance_km"),
                    trip.get("duration_s"),
                    trip.get("max_speed_kmh"),
                    trip.get("avg_speed_kmh"),
                    trip.get("samples_count") or 0,
                    trip.get("label"),
                    now,
                ),
            )
            trip_id = int(cur.lastrowid)
            db._conn.commit()
        trips_added += 1
        try:
            db.add_samples(trip_id, trip.get("samples") or [])
        except Exception:
            log.exception("Could not import samples for shared trip started_at=%s", started_at)
        with db._lock:
            row = db._conn.execute(
                "SELECT COUNT(*) AS n FROM samples WHERE trip_id=?", (trip_id,)
            ).fetchone()
            actual = int(row["n"]) if row else 0
            db._conn.execute("UPDATE trips SET samples_count=? WHERE id=?", (actual, trip_id))
            db._conn.commit()

    # ---- stopwatch runs ----
    existing_runs = db.list_stopwatch_runs_for_car(car_id)
    existing_runs_by_at = {r["run_at"]: r for r in existing_runs}

    for run in payload.get("stopwatch_runs") or []:
        if not isinstance(run, dict):
            continue
        run_at = run.get("run_at")
        if not run_at:
            continue
        if run_at in existing_runs_by_at:
            conflicts += 1
            continue
        with db._lock:
            cur = db._conn.cursor()
            cur.execute(
                "INSERT INTO acceleration_runs(car_id, run_at, lat, lon,"
                " results_json, samples_json, shared_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    car_id,
                    run_at,
                    run.get("lat"),
                    run.get("lon"),
                    json.dumps(run.get("results", {})),
                    json.dumps(run.get("samples", [])),
                    now,
                ),
            )
            db._conn.commit()
        runs_added += 1

    # ---- scans ----
    existing_scans = db.list_scans_for_car(car_id)
    existing_scans_by_at = {s["scanned_at"]: s for s in existing_scans}

    for scan in payload.get("scans") or []:
        if not isinstance(scan, dict):
            continue
        scanned_at = scan.get("scanned_at")
        if not scanned_at:
            continue
        if scanned_at in existing_scans_by_at:
            conflicts += 1
            continue
        with db._lock:
            cur = db._conn.cursor()
            cur.execute(
                "INSERT INTO scans(car_id, scanned_at, protocol, dtc_count,"
                " pending_dtc_count, pids_count, data_json, shared_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    car_id,
                    scanned_at,
                    scan.get("protocol"),
                    scan.get("dtc_count") or 0,
                    scan.get("pending_dtc_count") or 0,
                    scan.get("pids_count") or 0,
                    scan.get("data_json", "{}"),
                    now,
                ),
            )
            db._conn.commit()
        scans_added += 1

    return {
        "ok": True,
        "trips_added": trips_added,
        "runs_added": runs_added,
        "scans_added": scans_added,
        "conflicts": conflicts,
    }
