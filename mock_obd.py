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
            # 0→100 km/h: ~0.50g tapering to ~0.30g → ~7 s
            return 0.50 - 0.002 * speed_kmh
        if speed_kmh < 200:
            # 100→200 km/h: ~0.22g tapering to ~0.09g → ~18 s
            return 0.22 - 0.0013 * (speed_kmh - 100)
        if speed_kmh < 230:
            # 200→230 km/h: ~0.09g tapering to ~0.02g
            return max(0.02, 0.09 - 0.002333 * (speed_kmh - 200))
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
