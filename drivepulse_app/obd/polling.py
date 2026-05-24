"""OBD polling definitions and response normalization."""
from __future__ import annotations

from typing import Any

OBD_COMMAND_ATTRS: tuple[tuple[str, str], ...] = (
    ("rpm", "RPM"),
    ("speed", "SPEED"),
    ("coolant_temp", "COOLANT_TEMP"),
    ("throttle_pos", "THROTTLE_POS"),
    ("engine_load", "ENGINE_LOAD"),
    ("intake_temp", "INTAKE_TEMP"),
    ("maf", "MAF"),
    ("fuel_level", "FUEL_LEVEL"),
    ("runtime", "RUN_TIME"),
    ("control_module_voltage", "CONTROL_MODULE_VOLTAGE"),
)


OBD_POLL_INTERVALS: dict[str, float] = {
    "rpm": 0.0,
    "speed": 0.0,
    "coolant_temp": 0.0,
    "throttle_pos": 2.0,
    "engine_load": 2.0,
    "intake_temp": 5.0,
    "maf": 2.0,
    "fuel_level": 10.0,
    "runtime": 10.0,
    "control_module_voltage": 5.0,
}


def command_map(obd_module: Any) -> dict[str, Any]:
    """Return key -> python-OBD command object for known live telemetry PIDs."""
    return {
        key: getattr(obd_module.commands, attr, None)
        for key, attr in OBD_COMMAND_ATTRS
    }


def should_query_key(key: str, now: float, last_query: dict[str, float]) -> bool:
    """Poll fast-moving PIDs every tick and slower PIDs less often."""
    interval = OBD_POLL_INTERVALS.get(key, 2.0)
    if interval <= 0:
        return True
    last = last_query.get(key)
    return last is None or now - last >= interval


def response_to_plain_value(response: Any) -> Any:
    if response is None or response.is_null():
        return None
    value = response.value
    try:
        magnitude = value.magnitude
        unit = str(value.units)
        return {"value": float(magnitude), "unit": unit}
    except Exception:
        return str(value)
