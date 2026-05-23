"""Seed three fictitious vehicles with realistic full datasets for mock mode.

Idempotent: each car is keyed by its synthetic VIN; running again is a no-op.
Activated from DashboardWindow when mock_mode is on at startup and the cars
table does not yet contain the three mock VINs.
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import DriveDB


# ---------------------------------------------------------------------------
# Vehicle catalogue
# ---------------------------------------------------------------------------

# Each entry: VIN (synthetic but well-formed), display label, OBD protocol,
# tuning-relevant constants used by the data generators below.
_MOCK_CARS: tuple[dict[str, Any], ...] = (
    {
        "vin": "WBAVB31060NL12345",
        "brand": "BMW",
        "label": "BMW 318i E91 (2009)",
        "protocol": "ISO 15765-4 (CAN 11/500)",
        "cal_id": "7600100",
        "cvn": "B7 91 A1 23",
        # 1.8 L N46B20, 105 kW, 5-Gang Touring
        "displacement_l": 1.8,
        "idle_rpm": 700,
        "redline_rpm": 6800,
        "shift_rpm": 4400,
        "kerb_weight_kg": 1485,
        "wltp_lp100": 8.4,
        "t0_100": 9.7,
        "vmax_kmh": 211,
        "color": (0.13, 0.36, 0.58),  # bavarian blue
        "vin_data": {
            "vin": "WBAVB31060NL12345",
            "make": "BMW",
            "model": "318i Touring",
            "year": 2009,
            "engine": "1.8 L N46B20",
            "power_kw": 105,
            "transmission": "5-speed manual",
            "body_style": "Estate",
            "fuel_type": "Petrol",
        },
    },
    {
        "vin": "WVWZZZ1KZ7W123456",
        "brand": "Volkswagen",
        "label": "Golf V GTI (2007)",
        "protocol": "ISO 15765-4 (CAN 11/500)",
        "cal_id": "06F906026K_2400",
        "cvn": "44 B0 31 1F",
        # 2.0 TFSI EA113, 147 kW, 6-Gang
        "displacement_l": 2.0,
        "idle_rpm": 800,
        "redline_rpm": 7000,
        "shift_rpm": 5200,
        "kerb_weight_kg": 1336,
        "wltp_lp100": 8.0,
        "t0_100": 6.9,
        "vmax_kmh": 235,
        "color": (0.78, 0.16, 0.16),  # GTI red
        "vin_data": {
            "vin": "WVWZZZ1KZ7W123456",
            "make": "Volkswagen",
            "model": "Golf V GTI",
            "year": 2007,
            "engine": "2.0 TFSI EA113",
            "power_kw": 147,
            "transmission": "6-speed manual",
            "body_style": "Hatchback",
            "fuel_type": "Petrol",
        },
    },
    {
        "vin": "WVWZZZ6RZE0123456",
        "brand": "Volkswagen",
        "label": "VW Polo V 1.2 TSI (2014)",
        "protocol": "ISO 15765-4 (CAN 11/500)",
        "cal_id": "04E906016K_0001",
        "cvn": "9D 2F 80 4C",
        # 1.2 TSI CJZD, 66 kW, 5-Gang
        "displacement_l": 1.2,
        "idle_rpm": 750,
        "redline_rpm": 6500,
        "shift_rpm": 4000,
        "kerb_weight_kg": 1115,
        "wltp_lp100": 5.4,
        "t0_100": 10.8,
        "vmax_kmh": 184,
        "color": (0.85, 0.85, 0.86),  # reflex silver
        "vin_data": {
            "vin": "WVWZZZ6RZE0123456",
            "make": "Volkswagen",
            "model": "Polo V 1.2 TSI",
            "year": 2014,
            "engine": "1.2 TSI CJZD",
            "power_kw": 66,
            "transmission": "5-speed manual",
            "body_style": "Hatchback",
            "fuel_type": "Petrol",
        },
    },
)


# ---------------------------------------------------------------------------
# Routes (anchor polylines in the Rhineland — purely synthetic)
# ---------------------------------------------------------------------------

# (label, [(lat, lon), …]) — three routes shared across cars but with
# per-car timing and OBD profile so they don't end up identical.
_ROUTES: tuple[dict[str, Any], ...] = (
    {
        "label": "Stadtrunde Düsseldorf",
        "anchors": [
            (51.2277, 6.7735),  # Hbf
            (51.2280, 6.7820),
            (51.2330, 6.7820),
            (51.2380, 6.7760),
            (51.2350, 6.7700),
            (51.2310, 6.7680),
            (51.2277, 6.7735),
        ],
        "target_kmh": 50,
        "highway_fraction": 0.0,
    },
    {
        "label": "A57 nach Köln",
        "anchors": [
            (51.2200, 6.7600),
            (51.1800, 6.7900),
            (51.1100, 6.8400),
            (51.0500, 6.8900),
            (50.9810, 6.9220),  # Köln Mülheim
            (50.9410, 6.9580),
        ],
        "target_kmh": 110,
        "highway_fraction": 0.85,
    },
    {
        "label": "Landstraße Bergisches",
        "anchors": [
            (51.1850, 7.1850),
            (51.1900, 7.2150),
            (51.2050, 7.2400),
            (51.2200, 7.2600),
            (51.2350, 7.2400),
            (51.2300, 7.2100),
        ],
        "target_kmh": 75,
        "highway_fraction": 0.0,
    },
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_R_EARTH_KM = 6371.0088


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _R_EARTH_KM * math.asin(math.sqrt(h))


def _bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _interp_polyline(anchors: list[tuple[float, float]], step_km: float = 0.05) -> list[tuple[float, float]]:
    """Linear interpolation between anchor points, ~step_km spacing.

    Crude (treats lat/lon as planar) but plenty close enough for synthetic
    visuals at neighbourhood scale.
    """
    pts: list[tuple[float, float]] = []
    for i in range(len(anchors) - 1):
        a, b = anchors[i], anchors[i + 1]
        dist = _haversine_km(a, b)
        n = max(1, int(dist / step_km))
        for k in range(n):
            t = k / n
            pts.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    pts.append(anchors[-1])
    return pts


def _generate_trip_samples(
    car: dict[str, Any],
    route: dict[str, Any],
    rng: random.Random,
    start_ts: float,
) -> list[dict[str, Any]]:
    """Generate one ~1 Hz sample list following the route polyline."""
    points = _interp_polyline(list(route["anchors"]), step_km=0.04)
    target = float(route["target_kmh"])
    # Real-world average speed is below the cruise target; ~0.65 factor.
    avg_kmh = target * 0.65
    # Speed profile: ease in at start, ease out at end.
    n = max(20, len(points))
    samples: list[dict[str, Any]] = []
    cur_ts = start_ts
    prev_pt = points[0]
    cur_speed = 0.0
    # Pace points so total distance / avg speed ~ duration
    total_km = sum(_haversine_km(points[i], points[i + 1]) for i in range(n - 1))
    duration_s = (total_km / max(avg_kmh, 1.0)) * 3600.0
    dt = duration_s / max(1, n - 1)
    # Cruise speed targets per stretch of trip.
    def _profile(i: int) -> float:
        # Smooth in/out via cosine; small random throttle wave.
        u = i / max(1, n - 1)
        ramp = 0.5 - 0.5 * math.cos(math.pi * min(1.0, u * 8))
        cooldown = 0.5 - 0.5 * math.cos(math.pi * min(1.0, (1 - u) * 8))
        envelope = min(ramp, cooldown)
        # Small ~10% wave on top of the target speed.
        wave = 0.10 * math.sin(2 * math.pi * u * 3)
        return target * envelope * (1 + wave)

    accel_smooth = 0.0
    for i, pt in enumerate(points):
        cur_ts += dt
        target_i = _profile(i)
        # First-order lag toward target speed.
        cur_speed += (target_i - cur_speed) * 0.18 + rng.gauss(0, 0.6)
        cur_speed = max(0.0, cur_speed)

        if i > 0:
            d_km = _haversine_km(prev_pt, pt)
            heading = _bearing_deg(prev_pt, pt)
        else:
            d_km = 0.0
            heading = 0.0
        prev_pt = pt

        # Drivetrain derivations.
        # Effective gear ratio: scales rpm with speed; pick a band per speed.
        if cur_speed < 25:
            gear_factor = 95.0
        elif cur_speed < 55:
            gear_factor = 55.0
        elif cur_speed < 90:
            gear_factor = 38.0
        else:
            gear_factor = 28.0
        rpm = max(car["idle_rpm"], cur_speed * gear_factor + rng.gauss(0, 35))
        rpm = min(rpm, car["redline_rpm"])

        # Throttle: rough proxy of acceleration demand.
        # Hold a smoothed accel value for nicer-looking samples.
        if i > 1:
            inst_a = (cur_speed - samples[-1]["speed_kmh"]) / max(dt, 0.1)
        else:
            inst_a = 0.0
        accel_smooth = 0.65 * accel_smooth + 0.35 * inst_a
        throttle = max(5.0, min(100.0, 12.0 + accel_smooth * 18.0 + cur_speed * 0.25))
        engine_load = max(10.0, min(100.0, throttle * 0.85 + 10.0 + rng.gauss(0, 1.5)))

        coolant = 88.0 + 4.0 * math.sin(cur_ts / 220.0) + rng.gauss(0, 0.4)
        intake = 24.0 + 10.0 * math.sin(cur_ts / 600.0) + rng.gauss(0, 0.6)
        # MAF g/s ≈ (lp100 / 100) * speed_kmh * fuel_density / lambda; rough.
        maf = max(1.5, (car["wltp_lp100"] / 100.0) * max(cur_speed, 5.0) * 7.4 + rng.gauss(0, 0.8))
        # Fuel pct slowly decreases.
        fuel_pct = max(15.0, 78.0 - (i / n) * 8.0 + rng.gauss(0, 0.3))

        sample = {
            "ts": cur_ts,
            "speed_kmh": cur_speed,
            "obd_speed_kmh": cur_speed + rng.gauss(0, 0.6),
            "gps_speed_kmh": cur_speed + rng.gauss(0, 1.2),
            "rpm": rpm,
            "coolant_c": coolant,
            "throttle_pct": throttle,
            "engine_load": engine_load,
            "fuel_pct": fuel_pct,
            "intake_c": intake,
            "maf_gps": maf,
            "voltage_v": 14.05 + rng.gauss(0, 0.05),
            "lat": pt[0],
            "lon": pt[1],
            "altitude_m": 38.0 + 6.0 * math.sin(cur_ts / 280.0),
            "heading_deg": heading,
            "accel_g": accel_smooth / 9.80665,
        }
        samples.append(sample)
    return samples


def _scan_blob_for(
    car: dict[str, Any],
    when: datetime,
    rng: random.Random,
    dtcs: list[tuple[str, str]] | None,
) -> dict[str, Any]:
    """Build a scan JSON snapshot in the format obd_scanner emits."""
    live: dict[str, dict[str, Any]] = {}

    def _v(pid: str, value: Any, unit: str = "") -> None:
        live[pid] = {"value": value, "unit": unit}

    coolant = 91.0 + rng.gauss(0, 1.5)
    intake = 28.0 + rng.gauss(0, 3.0)
    voltage = 14.1 + rng.gauss(0, 0.05)
    rpm_idle = car["idle_rpm"] + rng.gauss(0, 25)

    _v("0105", round(coolant, 1), "°C")
    _v("010B", int(101 + rng.gauss(0, 1)), "kPa")
    _v("010C", round(rpm_idle, 0), "rpm")
    _v("010D", 0.0, "km/h")
    _v("010F", round(intake, 1), "°C")
    _v("0111", round(14.0 + rng.gauss(0, 1), 1), "%")
    _v("0114", round(0.78 + rng.gauss(0, 0.03), 3), "V")
    _v("0142", round(voltage, 2), "V")
    _v("0146", round(intake - 4.0 + rng.gauss(0, 2.0), 1), "°C")
    _v("0149", round(18 + rng.gauss(0, 1.5), 1), "%")
    _v("0104", round(22 + rng.gauss(0, 2.5), 1), "%")
    _v("012F", round(60 + rng.gauss(0, 8), 1), "%")
    # Friendly aliases used by the chart layer (PID + space + label).
    return {
        "scanned_at": when.isoformat(),
        "identity": car["brand"] + " " + car["label"],
        "vin": car["vin"],
        "port": "MOCK",
        "protocol": car["protocol"],
        "adapter_kind": "MOCK",
        "adapter_version": "1.0",
        "supported_pids": sorted(live.keys()),
        "live_data": live,
        "dtcs": dtcs or [],
        "pending_dtcs": [],
        "vehicle_info": {
            "VIN": car["vin"],
            "CALIBRATION_ID": car["cal_id"],
            "CVN": car["cvn"],
            "ECU_NAME": "ECM",
        },
    }


def _stopwatch_run_for(
    car: dict[str, Any],
    when: datetime,
    rng: random.Random,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build a plausible 0-100 acceleration run (results + samples)."""
    t100_base = car["t0_100"]
    t100 = t100_base + rng.gauss(0, 0.18)

    # Build a polynomial-ish curve: speed(t) = vmax * (1 - exp(-k*t))
    vmax = car["vmax_kmh"] * (0.95 + rng.uniform(-0.02, 0.02))
    # Solve k so that speed at t100 ≈ 100 km/h: 100 = vmax*(1 - e^(-k*t100))
    k = -math.log(1.0 - 100.0 / vmax) / max(t100, 0.1)

    def _speed_at(t: float) -> float:
        if t <= 0:
            return 0.0
        return vmax * (1.0 - math.exp(-k * t))

    # Sample at 50 Hz for ~ enough seconds to roll into top gear
    end_t = max(t100 + 12.0, 16.0)
    samples: list[dict[str, Any]] = []
    t = 0.0
    dt = 0.02
    prev_v = 0.0
    max_g = 0.0
    while t <= end_t:
        v = _speed_at(t) + rng.gauss(0, 0.25)
        v = max(0.0, v)
        a_kmh_s = (v - prev_v) / dt
        a_g = (a_kmh_s / 3.6) / 9.80665
        max_g = max(max_g, a_g)
        samples.append({
            "ts": round(t, 3),
            "speed_obd_kmh": round(v, 2),
            "speed_gps_kmh": round(v + rng.gauss(0, 0.5), 2),
            "accel_g": round(a_g, 3),
            "rpm": round(min(car["redline_rpm"], 1100 + 25 * v + rng.gauss(0, 30)), 0),
        })
        prev_v = v
        t += dt

    def _time_to(target: float) -> float | None:
        if target >= vmax:
            return None
        # Closed-form inverse
        return -math.log(1.0 - target / vmax) / k

    targets: dict[str, dict[str, float | None]] = {}
    for tgt in (30, 50, 60, 80, 100, 120, 140, 160, 180, 200):
        t_tgt = _time_to(float(tgt))
        if t_tgt is None or t_tgt > end_t:
            continue
        # OBD vs GPS small offset
        targets[str(tgt)] = {
            "obd": round(t_tgt + rng.gauss(0, 0.05), 3),
            "gps": round(t_tgt + rng.gauss(0, 0.10), 3),
        }

    ranges: dict[str, dict[str, float | None]] = {}
    for lo, hi in ((80, 120), (60, 100), (100, 140)):
        t_lo = _time_to(float(lo))
        t_hi = _time_to(float(hi))
        if t_lo is None or t_hi is None or t_hi > end_t:
            continue
        delta = t_hi - t_lo
        ranges[f"{lo}-{hi}"] = {
            "obd": round(delta + rng.gauss(0, 0.06), 3),
            "gps": round(delta + rng.gauss(0, 0.12), 3),
        }

    results = {
        "targets": targets,
        "ranges": ranges,
        "max_obd_kmh": round(samples[-1]["speed_obd_kmh"], 1),
        "max_gps_kmh": round(samples[-1]["speed_gps_kmh"], 1),
        "max_g": round(max_g, 3),
    }
    return results, samples


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

# Per-car salt for the RNG so runs are deterministic but vary per vehicle.
_SEED_BASE = 0xD12E_BABE


def _make_rng(salt: int) -> random.Random:
    return random.Random(_SEED_BASE ^ salt)


def seed_mock_data(db: DriveDB) -> int:
    """Populate three mock vehicles plus trips, scans, and stopwatch runs.

    Returns the number of cars that were newly added. Existing cars
    (identified by VIN) are left untouched.
    """
    existing = {row["vin"] for row in db.list_cars() if row["vin"]}
    added = 0
    # All entries dated relative to "now" so timestamps look fresh on each
    # fresh DB but a re-run skips already-populated cars.
    base_now = datetime.now(timezone.utc)

    for car_idx, car in enumerate(_MOCK_CARS):
        if car["vin"] in existing:
            continue
        car_id = db.upsert_car(
            vin=car["vin"],
            brand=car["brand"],
            cal_id=car["cal_id"],
            cvn=car["cvn"],
            label=car["label"],
            protocol=car["protocol"],
        )
        try:
            db.update_car_vin_data(car_id, json.dumps(car["vin_data"]))
        except Exception:
            pass

        rng = _make_rng(car_idx)

        # --- Three trips per car ---------------------------------------
        for trip_idx, route in enumerate(_ROUTES):
            trip_rng = _make_rng(car_idx * 100 + trip_idx)
            # Spread trips across the past two weeks so the list looks lived-in.
            started_at = base_now - timedelta(days=2 + trip_idx * 3 + car_idx, hours=trip_rng.randint(7, 18))
            trip_id = db.start_trip(car_id, started_at=started_at)
            samples = _generate_trip_samples(
                car, route, trip_rng, start_ts=started_at.timestamp()
            )
            db.add_samples(trip_id, samples)
            try:
                db.rename_trip(trip_id, route["label"])
            except Exception:
                pass
            db.end_trip(trip_id)

        # --- Three scans per car (oldest first; second carries one DTC) -
        dtc_catalog: list[list[tuple[str, str]]] = [
            [],
            [("P0420", "Catalyst System Efficiency Below Threshold (Bank 1)")]
            if car["brand"] == "Volkswagen" else
            [("P0171", "System Too Lean (Bank 1)")],
            [],
        ]
        for scan_idx in range(3):
            scan_rng = _make_rng(car_idx * 200 + scan_idx + 17)
            scanned_at = base_now - timedelta(days=21 - scan_idx * 7 - car_idx)
            blob = _scan_blob_for(car, scanned_at, scan_rng, dtc_catalog[scan_idx])
            db.add_scan(car_id, blob)

        # --- Three stopwatch runs per car ------------------------------
        for run_idx in range(3):
            run_rng = _make_rng(car_idx * 300 + run_idx + 91)
            run_at = base_now - timedelta(days=14 - run_idx * 4 - car_idx, hours=run_rng.randint(10, 20))
            results, samples = _stopwatch_run_for(car, run_at, run_rng)
            db.add_stopwatch_run(
                car_id=car_id,
                results=results,
                samples=samples,
                lat=51.2277,
                lon=6.7735,
                run_at=run_at.isoformat(),
            )

        added += 1

    return added
