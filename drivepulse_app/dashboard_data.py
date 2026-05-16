"""Data shaping helpers for dashboard updates and trip recording."""
from __future__ import annotations

from typing import Any, Callable

from .cars_metadata import _extract_inner_string, _parse_profile_pid_key, _wmi_to_brand


def obd_sample_fields(
    payload: dict[str, Any],
    plain_number_fn: Callable[[dict[str, Any], str], float | None],
) -> dict[str, float | None]:
    accel = plain_number_fn(payload, "acceleration_g")
    obd_speed = plain_number_fn(payload, "speed")
    gps_speed = plain_number_fn(payload, "gps_speed")
    speed = obd_speed if obd_speed is not None else gps_speed
    return {
        "speed_kmh":     speed,
        "obd_speed_kmh": obd_speed,
        "gps_speed_kmh": gps_speed,
        "rpm":           plain_number_fn(payload, "rpm"),
        "coolant_c":     plain_number_fn(payload, "coolant_temp"),
        "throttle_pct":  plain_number_fn(payload, "throttle_pos"),
        "engine_load":   plain_number_fn(payload, "engine_load"),
        "fuel_pct":      plain_number_fn(payload, "fuel_level"),
        "intake_c":      plain_number_fn(payload, "intake_temp"),
        "maf_gps":       plain_number_fn(payload, "maf"),
        "voltage_v":     plain_number_fn(payload, "control_module_voltage"),
        "accel_g":       accel,
    }


def scan_profile_dashboard_data(data: dict[str, Any]) -> tuple[dict[str, float | None], dict[str, str], list[str], list[str]]:
    pids: dict[str, float | None] = {}
    for raw_key, raw_val in (data.get("live_data") or {}).items():
        pid = _parse_profile_pid_key(raw_key)
        if not pid:
            continue
        if isinstance(raw_val, dict):
            value = raw_val.get("value")
        else:
            value = raw_val
        try:
            pids[pid] = float(value) if value is not None else None
        except (TypeError, ValueError):
            pids[pid] = None

    info_src = data.get("vehicle_info") or {}
    info: dict[str, str] = {}
    vin = _extract_inner_string(info_src.get("VIN") or "")
    if vin:
        info["vin"] = vin
        brand = _wmi_to_brand(vin)
        if brand:
            info["brand"] = brand
    cal = _extract_inner_string(info_src.get("CALIBRATION_ID") or "")
    if cal:
        info["cal_id"] = cal
    cvn = _extract_inner_string(info_src.get("CVN") or "")
    if cvn:
        info["cvn"] = cvn
    if data.get("protocol"):
        info["protocol"] = str(data["protocol"])
    obd_std = pids.pop("011C", None)
    if obd_std is not None:
        info["obd_standard"] = str(int(obd_std)) if obd_std == int(obd_std) else str(obd_std)

    dtcs = [str(dtc) for dtc in (data.get("dtcs") or [])]
    pending = [str(dtc) for dtc in (data.get("pending_dtcs") or [])]
    return pids, info, dtcs, pending


def scan_identity_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    vin = _extract_inner_string(payload.get("vin")) or None
    cal_id = _extract_inner_string(payload.get("cal_id")) or None
    cvn = _extract_inner_string(payload.get("cvn")) or None
    protocol = payload.get("protocol") if isinstance(payload.get("protocol"), str) else None
    brand = _wmi_to_brand(vin or "") or None

    identity: dict[str, str] = {}
    if vin:
        identity["VIN"] = vin
    if cal_id:
        identity["CALIBRATION_ID"] = cal_id
    if cvn:
        identity["CVN"] = cvn
    if protocol:
        identity["protocol"] = protocol

    return {
        "vin": vin,
        "brand": brand,
        "cal_id": cal_id,
        "cvn": cvn,
        "protocol": protocol,
        "profile_path": payload.get("profile_path"),
        "identity": identity,
    }
