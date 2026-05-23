"""Vehicle metadata, PID categories and formatting helpers for DrivePulse."""
from __future__ import annotations

import re
from typing import Any

LIVE_KEY_TO_PID: dict[str, str] = {
    "rpm":                    "010C",
    "speed":                  "010D",
    "coolant_temp":           "0105",
    "throttle_pos":           "0111",
    "engine_load":            "0104",
    "intake_temp":            "010F",
    "maf":                    "0110",
    "fuel_level":             "012F",
    "runtime":                "011F",
    "control_module_voltage": "0142",
}

_SPECIAL_VIN = "__VIN__"
_SPECIAL_CAL = "__CAL__"
_SPECIAL_CVN = "__CVN__"
_SPECIAL_PROTO = "__PROTO__"
_SPECIAL_SCAN_DATE = "__SCAN_DATE__"
_SPECIAL_DTC = "__DTC__"
_SPECIAL_PENDING = "__PENDING_DTC__"
_SPECIAL_ADAPTER_V = "__ATRV__"

_SPECIAL_VIN_MAKE = "__VIN_MAKE__"
_SPECIAL_VIN_MODEL = "__VIN_MODEL__"
_SPECIAL_VIN_YEAR = "__VIN_YEAR__"
_SPECIAL_VIN_BODY = "__VIN_BODY__"
_SPECIAL_VIN_FUEL = "__VIN_FUEL__"
_SPECIAL_VIN_DRIVE = "__VIN_DRIVE__"
_SPECIAL_VIN_CYLINDERS = "__VIN_CYLINDERS__"
_SPECIAL_VIN_DISPLACEMENT = "__VIN_DISPLACEMENT__"
_SPECIAL_VIN_TRANSMISSION = "__VIN_TRANSMISSION__"
_SPECIAL_VIN_MANUFACTURER = "__VIN_MANUFACTURER__"
_SPECIAL_VIN_COUNTRY = "__VIN_COUNTRY__"

VIN_DATA_SPECIAL_KEYS: dict[str, str] = {
    "make":         _SPECIAL_VIN_MAKE,
    "model":        _SPECIAL_VIN_MODEL,
    "year":         _SPECIAL_VIN_YEAR,
    "body":         _SPECIAL_VIN_BODY,
    "fuel":         _SPECIAL_VIN_FUEL,
    "drive":        _SPECIAL_VIN_DRIVE,
    "cylinders":    _SPECIAL_VIN_CYLINDERS,
    "displacement": _SPECIAL_VIN_DISPLACEMENT,
    "transmission": _SPECIAL_VIN_TRANSMISSION,
    "manufacturer": _SPECIAL_VIN_MANUFACTURER,
    "plant_country": _SPECIAL_VIN_COUNTRY,
}

_CHART_METRICS: tuple[tuple, ...] = (
    ("speed_kmh",    "cars.metric.speed_kmh",    "km/h", (0.34, 0.62, 0.86), "{:.0f}"),
    ("rpm",          "cars.metric.rpm",           "RPM",  (0.95, 0.60, 0.20), "{:.0f}"),
    ("coolant_c",    "cars.metric.coolant_c",     "°C",   (0.90, 0.30, 0.30), "{:.0f}"),
    ("intake_c",     "cars.metric.intake_c",      "°C",   (0.95, 0.50, 0.20), "{:.0f}"),
    ("throttle_pct", "cars.metric.throttle_pct",  "%",    (0.30, 0.80, 0.40), "{:.0f}"),
    ("engine_load",  "cars.metric.engine_load",   "%",    (0.60, 0.85, 0.30), "{:.0f}"),
    ("maf_gps",      "cars.metric.maf_gps",       "g/s",  (0.70, 0.40, 0.90), "{:.1f}"),
    ("voltage_v",    "cars.metric.voltage_v",     "V",    (0.95, 0.75, 0.10), "{:.2f}"),
    ("accel_g",      "cars.metric.accel_g",       "g",    (0.90, 0.40, 0.20), "{:.2f}"),
    ("fuel_pct",     "cars.metric.fuel_pct",      "%",    (0.95, 0.80, 0.10), "{:.0f}"),
)

CATEGORIES: tuple[tuple[str, str, str, tuple[tuple[str, str], ...]], ...] = (
    ("vehicle", "cars.category.vehicle", "info-symbolic", (
        (_SPECIAL_VIN,              "cars.pid.VIN"),
        (_SPECIAL_VIN_MANUFACTURER, "cars.pid.VIN_MANUFACTURER"),
        (_SPECIAL_VIN_MAKE,         "cars.pid.VIN_MAKE"),
        (_SPECIAL_VIN_MODEL,        "cars.pid.VIN_MODEL"),
        (_SPECIAL_VIN_YEAR,         "cars.pid.VIN_YEAR"),
        (_SPECIAL_VIN_BODY,         "cars.pid.VIN_BODY"),
        (_SPECIAL_VIN_FUEL,         "cars.pid.VIN_FUEL"),
        (_SPECIAL_VIN_DRIVE,        "cars.pid.VIN_DRIVE"),
        (_SPECIAL_VIN_CYLINDERS,    "cars.pid.VIN_CYLINDERS"),
        (_SPECIAL_VIN_DISPLACEMENT, "cars.pid.VIN_DISPLACEMENT"),
        (_SPECIAL_VIN_TRANSMISSION, "cars.pid.VIN_TRANSMISSION"),
        (_SPECIAL_VIN_COUNTRY,      "cars.pid.VIN_COUNTRY"),
        (_SPECIAL_CAL,              "cars.pid.CAL"),
        (_SPECIAL_CVN,              "cars.pid.CVN"),
        (_SPECIAL_PROTO,            "cars.pid.PROTO"),
        ("011C",                    "cars.pid.011C"),
        (_SPECIAL_SCAN_DATE,        "cars.pid.SCAN_DATE"),
    )),
    ("diagnostics", "cars.category.diagnostics", "dialog-warning-symbolic", (
        (_SPECIAL_DTC,        "cars.pid.DTC"),
        (_SPECIAL_PENDING,    "cars.pid.PENDING_DTC"),
        ("0141", "cars.pid.0141"),
    )),
    ("scans", "cars.category.scans", "library-symbolic", ()),
    ("trips", "cars.category.trips", "globe-symbolic", ()),
    ("photos", "cars.category.photos", "camera-photo-symbolic", ()),
    ("stopwatch_runs", "cars.category.stopwatch_runs", "stopwatch-symbolic", ()),
    ("engine", "cars.category.engine", "step_object_LinearMotor-symbolic", (
        ("010C", "cars.pid.010C"),
        ("0104", "cars.pid.0104"),
        ("0143", "cars.pid.0143"),
        ("010E", "cars.pid.010E"),
        ("011F", "cars.pid.011F"),
        ("0142", "cars.pid.0142"),
        (_SPECIAL_ADAPTER_V, "cars.pid.ATRV"),
    )),
    ("temperatures", "cars.category.temperatures", "thermometer-symbolic", (
        ("0105", "cars.pid.0105"),
        ("010F", "cars.pid.010F"),
        ("0146", "cars.pid.0146"),
        ("013C", "cars.pid.013C"),
    )),
    ("throttle", "cars.category.throttle", "emblem-system-symbolic", (
        ("0111", "cars.pid.0111"),
        ("0145", "cars.pid.0145"),
        ("0147", "cars.pid.0147"),
        ("0149", "cars.pid.0149"),
        ("014A", "cars.pid.014A"),
        ("014C", "cars.pid.014C"),
    )),
    ("mixture", "cars.category.mixture", "applications-science-symbolic", (
        ("0103", "cars.pid.0103"),
        ("0106", "cars.pid.0106"),
        ("0107", "cars.pid.0107"),
        ("0156", "cars.pid.0156"),
        ("0134", "cars.pid.0134"),
        ("0144", "cars.pid.0144"),
        ("0115", "cars.pid.0115"),
    )),
    ("fuel", "cars.category.fuel", "weather-windy-symbolic", (
        ("0110", "cars.pid.0110"),
        ("012F", "cars.pid.012F"),
        ("0123", "cars.pid.0123"),
        ("012E", "cars.pid.012E"),
        ("0133", "cars.pid.0133"),
    )),
    ("drive", "cars.category.drive", "speedometer4-symbolic", (
        ("010D", "cars.pid.010D"),
        ("0131", "cars.pid.0131"),
        ("0121", "cars.pid.0121"),
        ("0130", "cars.pid.0130"),
    )),
)

_UNIT_DISPLAY: dict[str, str] = {
    "revolutions_per_minute": "rpm",
    "kilometer_per_hour":     "km/h",
    "kilometer":              "km",
    "degree_Celsius":         "°C",
    "degree":                 "°",
    "percent":                "%",
    "kilopascal":             "kPa",
    "volt":                   "V",
    "second":                 "s",
    "milliampere":            "mA",
    "gps":                    "g/s",
    "ratio":                  "",
    "count":                  "",
    "rpm":                    "rpm",
    "km/h":                   "km/h",
    "degC":                   "°C",
    "deg":                    "°",
    "g":                      "g",
}

_UNIT_DISPLAY_DE: dict[str, str] = {
    "revolutions_per_minute": "U/min",
    "rpm":                    "U/min",
}

_WMI_BRANDS: dict[str, str] = {
    "WAU": "Audi", "TRU": "Audi", "WUA": "Audi",
    "WBA": "BMW", "WBS": "BMW", "WBY": "BMW",
    "WVW": "VW", "WV1": "VW", "WV2": "VW", "WVG": "VW",
    "WP0": "Porsche", "WP1": "Porsche",
    "WDB": "Mercedes-Benz", "WDD": "Mercedes-Benz", "WDC": "Mercedes-Benz",
    "VF1": "Renault", "VF3": "Peugeot", "VF7": "Citroën",
    "ZFA": "Fiat", "ZAR": "Alfa Romeo",
    "JT1": "Toyota", "JT2": "Toyota", "JTD": "Toyota",
    "JN1": "Nissan", "JN8": "Nissan",
    "KMH": "Hyundai", "KNA": "Kia",
    "VS5": "SEAT", "VSS": "SEAT", "TMB": "Škoda",
    "WF0": "Ford", "1FT": "Ford", "1FA": "Ford",
    "YV1": "Volvo", "YV4": "Volvo",
}


def _unit_display(unit: str, language: str = "en") -> str:
    if language == "de":
        return _UNIT_DISPLAY_DE.get(unit) or _UNIT_DISPLAY.get(unit, unit)
    return _UNIT_DISPLAY.get(unit, unit)


def _extract_inner_string(raw: Any) -> str:
    if raw is None:
        return ""
    s = str(raw)
    m = re.search(r"b['\"]([^'\"]+)['\"]", s)
    if m:
        return m.group(1)
    return s.strip()


def _wmi_to_brand(vin: str) -> str:
    return _WMI_BRANDS.get(vin[:3], "")


def _parse_profile_pid_key(key: str) -> str:
    m = re.search(r"b['\"]([0-9A-Fa-f]+)['\"]", key)
    return m.group(1).upper() if m else ""


def _format_status_string(value: str) -> str:
    if value.startswith("(") and "'" in value:
        m = re.match(r"^\(\s*'([^']*)'", value)
        if m:
            return m.group(1)
    return value


def _format_value_unit(payload: Any, language: str = "en") -> str:
    if payload is None:
        return "—"
    if isinstance(payload, dict) and "value" in payload:
        value = payload.get("value")
        unit = payload.get("unit") or ""
        unit_disp = _unit_display(unit, language)
        if value is None:
            return "—"
        try:
            v = float(value)
        except (TypeError, ValueError):
            return f"{value} {unit_disp}".strip()
        if abs(v) >= 100:
            text = f"{v:.0f}"
        elif abs(v) >= 10:
            text = f"{v:.1f}"
        else:
            text = f"{v:.2f}"
        return f"{text} {unit_disp}".strip() if unit_disp else text
    if isinstance(payload, str):
        return _format_status_string(payload) or "—"
    return str(payload)
