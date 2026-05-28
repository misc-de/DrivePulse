"""Mock OBD telemetry generator for DrivePulse."""
from __future__ import annotations

import math
import random
import time
from typing import Any


class MockObdSimulator:
    """Generates plausible telemetry when no real OBD adapter is available."""

    def __init__(self) -> None:
        self._accel_start: float | None = None
        self._speed: float = 0.0
        self._prev_tick: float = time.time()

    def trigger_acceleration(self) -> None:
        """Start a mock 0-230 km/h acceleration run."""
        self._accel_start = time.monotonic()
        self._speed = 0.0
        self._prev_tick = time.time()

    @staticmethod
    def _accel_g_for_speed(speed_kmh: float) -> float:
        """Target longitudinal acceleration in g for the mock run, by speed."""
        if speed_kmh < 100:
            # 0→100 km/h: launch at ~1.0g, tapering to ~0.45g
            return 1.00 - 0.0055 * speed_kmh
        if speed_kmh < 200:
            # 100→200 km/h: ~0.45g tapering to ~0.10g
            return 0.45 - 0.0035 * (speed_kmh - 100)
        if speed_kmh < 230:
            # 200→230 km/h: ~0.10g tapering to ~0.02g
            return max(0.02, 0.10 - 0.002667 * (speed_kmh - 200))
        return -0.03  # slight engine drag above 230

    def read(self) -> dict[str, Any]:
        now = time.time()
        now_mono = time.monotonic()
        temp = 84 + 4 * math.sin(now / 15) + random.uniform(-0.5, 0.5)

        if self._accel_start is not None:
            dt = max(0.001, now - self._prev_tick)
            self._prev_tick = now

            target_g = self._accel_g_for_speed(self._speed)
            noise_g = random.gauss(0, 0.018)
            acceleration_g = target_g + noise_g
            accel_ms2 = acceleration_g * 9.80665
            new_speed = self._speed + accel_ms2 * 3.6 * dt
            speed = max(0.0, new_speed)
            self._speed = speed

            if target_g <= -0.03 and speed < 1.0:
                self._accel_start = None

            throttle = max(5.0, min(100.0, 95.0 * (target_g / 0.50) + random.gauss(0, 2)))
            load = max(10.0, min(100.0, 90.0 * (target_g / 0.50) + random.gauss(0, 3)))
            rpm = 1000 + 5500 * min(1.0, speed / 230.0) + random.gauss(0, 60)
        else:
            # Idle: stable cruising speed, near-zero G
            speed = 30.0 + 1.5 * math.sin(now / 30.0) + random.gauss(0, 0.15)
            speed = max(0.0, speed)
            acceleration_g = random.gauss(0, 0.008)
            throttle = random.uniform(8, 18)
            load = random.uniform(12, 28)
            rpm = 900 + 700 * (math.sin(now / 3) + 1) + random.uniform(-80, 80)

        heading = (now_mono * 8.0) % 360.0
        return {
            "rpm": {"value": rpm, "unit": "rpm"},
            "speed": {"value": speed, "unit": "km/h"},
            "gps_speed": {"value": max(0.0, speed + random.gauss(0, 0.8)), "unit": "km/h"},
            "gps_heading": {"value": heading, "unit": "deg"},
            "acceleration_g": {"value": acceleration_g, "unit": "g"},
            "coolant_temp": {"value": temp, "unit": "degC"},
            "fuel_level": {"value": 68 + 5 * math.sin(now / 60), "unit": "percent"},
            "throttle_pos": {"value": throttle, "unit": "percent"},
            "engine_load": {"value": load, "unit": "percent"},
            "intake_temp": {"value": 20 + random.uniform(-3, 5), "unit": "degC"},
            "control_module_voltage": {"value": 13.8 + random.uniform(-0.25, 0.25), "unit": "volt"},
        }


class MockUdsSimulator:
    """In-memory stand-in for a UDS control module, for exercising the Car Lab
    (discover / find-functions) workflow without a real adapter or vehicle.

    Models a small VAG-style instrument cluster: a handful of identification
    DIDs plus a long-coding DID (0x0600). The coding response carries a trailing
    *counter* byte that ticks on every read, so the baseline step has genuine
    "noise" to detect and filter. :meth:`toggle_function` flips one real coding
    bit — call it between baseline and capture to simulate the user changing a
    setting in the car, producing a clean, describable diff.
    """

    def __init__(self) -> None:
        # Stable long-coding bytes; byte 3 bit3 is our toggleable "function".
        self._coding = bytearray([0x03, 0x12, 0x00, 0x00, 0xA5])
        self._counter = 0
        self._ident: dict[int, bytes] = {
            0xF190: b"WAUZZZ4GXDN000001",   # VIN
            0xF187: b"4G0920900",           # spare part number
            0xF189: b"H05",                  # SW version
            0xF18A: b"VAG",                  # supplier id
            0xF191: b"4G0920900A",          # HW number
            0xF197: b"KOMBIINSTRUMENT",      # system name
        }

    # Addresses the simulated vehicle "answers" on (engine, instruments,
    # central electrics, gateway) — a believable mixed result for a module scan.
    _PRESENT_TX = frozenset({"7E0", "714", "70E", "710"})

    def scan_modules(self, candidates: list[Any]) -> list[dict[str, str]]:
        """Return the subset of *candidates* this mock module "answers" on."""
        return [
            {"name": m.name, "tx": m.tx, "rx": m.rx}
            for m in candidates
            if m.tx.upper() in self._PRESENT_TX
        ]

    def toggle_function(self) -> None:
        """Flip the simulated function bit (byte 3, bit 3) in the coding."""
        self._coding[3] ^= 0x08

    def _did_bytes(self, did: int) -> bytes | None:
        from drivepulse_app.obd.uds import VAG_CODING_DID

        if did == VAG_CODING_DID:
            # Real coding bytes + one self-changing counter byte (the "noise").
            return bytes(self._coding) + bytes([self._counter])
        return self._ident.get(did)

    def snapshot(self, dids: list[int]) -> dict[int, str]:
        self._counter = (self._counter + 1) & 0xFF
        out: dict[int, str] = {}
        for did in dids:
            data = self._did_bytes(did)
            if data is not None:
                out[did] = data.hex().upper()
        return out

    def discover(self, tx: str, rx: str) -> dict[str, Any]:
        from datetime import UTC, datetime

        from drivepulse_app.obd.uds import (
            IDENTIFICATION_DIDS,
            VAG_CODING_DID,
            as_ascii,
        )

        self._counter = (self._counter + 1) & 0xFF
        out: dict[str, Any] = {
            "created_at": datetime.now(UTC).isoformat(),
            "tx": tx.upper(), "rx": rx.upper(), "mock": True,
            "identification": {}, "coding": {}, "did_responses": {},
        }
        for did in (*IDENTIFICATION_DIDS, VAG_CODING_DID):
            data = self._did_bytes(did)
            key = f"{did:04X}"
            if data is None:
                out["did_responses"][key] = {"nrc": "31", "nrc_name": "requestOutOfRange"}
                continue
            entry: dict[str, Any] = {"hex": data.hex().upper()}
            ascii_val = as_ascii(data)
            if ascii_val is not None:
                entry["ascii"] = ascii_val
            out["did_responses"][key] = entry
            if did in IDENTIFICATION_DIDS:
                out["identification"][IDENTIFICATION_DIDS[did]] = entry
            if did == VAG_CODING_DID:
                out["coding"][key] = entry
        return out
