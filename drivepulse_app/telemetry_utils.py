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


def normalize_vin(vin: str | None) -> str:
    return (vin or "").strip().upper()


def vins_same_vehicle(a: str | None, b: str | None) -> bool:
    """True if two VINs denote the same vehicle, tolerating one dropped char.

    A VIN is always 17 characters, but some adapters / the python-obd mode-09
    decoder return only 16 (one position lost in the multi-frame response).
    Equal VINs match; otherwise they match only when the shorter equals the
    longer with exactly one character removed (length difference of one). Two
    genuinely distinct VINs are both 17 chars, so this never merges them —
    it only reunites an incomplete OBD read with its corrected/full VIN.
    """
    a = normalize_vin(a)
    b = normalize_vin(b)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(long) - len(short) != 1:
        return False
    return any(long[:i] + long[i + 1:] == short for i in range(len(long)))
