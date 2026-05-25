"""Telemetry payload helpers for DrivePulse."""
from __future__ import annotations

from typing import Any


def plain_number(data: dict[str, Any], key: str) -> float | None:
    item = data.get(key)
    if item is None:
        return None
    if isinstance(item, dict):
        value = item.get("value")
    else:
        value = item
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def display_speed(speed_kmh: float | None, units: str) -> float | None:
    if speed_kmh is None:
        return None
    return speed_kmh if units == "metric" else speed_kmh * 0.621371


def has_obd_data(payload: dict[str, Any]) -> bool:
    return any(
        plain_number(payload, key) is not None
        for key in ("rpm", "speed", "coolant_temp", "throttle_pos", "engine_load")
    )
