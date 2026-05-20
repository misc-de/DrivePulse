"""Fetch additional vehicle data from NHTSA vpic, auto.dev and vindecoder.eu."""
from __future__ import annotations

import hashlib
import json
import urllib.request
from typing import Any

from .diagnostics import get_logger

log = get_logger(__name__)

_NHTSA_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVin/{}?format=json"
_AUTODEV_URL = "https://api.auto.dev/vin/{}"
_VINDECODER_URL = "https://api.vindecoder.eu/3.2/{}/{}/decode/{}.json"

_NHTSA_FIELDS: dict[str, str] = {
    "Make":                        "make",
    "Model":                       "model",
    "Model Year":                  "year",
    "Body Class":                  "body",
    "Fuel Type - Primary":         "fuel",
    "Drive Type":                  "drive",
    "Engine Number of Cylinders":  "cylinders",
    "Displacement (L)":            "displacement",
    "Transmission Style":          "transmission",
    "Manufacturer Name":           "manufacturer",
    "Plant Country":               "plant_country",
}

_AUTODEV_FIELDS: dict[str, str] = {
    "make":          "make",
    "model":         "model",
    "body":          "body",
    "drive":         "drive",
    "transmission":  "transmission",
    "origin":        "plant_country",
}

_VINDECODER_FIELDS: dict[str, str] = {
    "Make":                        "make",
    "Model":                       "model",
    "Model Year":                  "year",
    "Body":                        "body",
    "Fuel Type":                   "fuel",
    "Drive":                       "drive",
    "Number of Cylinders":         "cylinders",
    "Engine Displacement (ccm)":   "displacement_ccm",
    "Transmission":                "transmission",
    "Manufacturer":                "manufacturer",
    "Country of Origin":           "plant_country",
}

_SKIP_VALUES = {"not applicable", "n/a", "—", "", "none", "null"}

# Internal source-tracking keys (stripped before persisting user-accepted data)
SOURCE_KEY_NHTSA = "_src_nhtsa"
SOURCE_KEY_AUTODEV = "_src_autodev"
SOURCE_KEY_VINDECODER = "_src_vd"


def _clean(val: Any) -> str:
    s = str(val).strip()
    return s if s.lower() not in _SKIP_VALUES else ""


def _fetch_nhtsa(vin: str) -> dict[str, Any]:
    try:
        url = _NHTSA_URL.format(vin.upper())
        req = urllib.request.Request(url, headers={"User-Agent": "DrivePulse/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("Results") or []
        out: dict[str, Any] = {}
        for item in results:
            key = _NHTSA_FIELDS.get(item.get("Variable", ""))
            if key:
                val = _clean(item.get("Value") or "")
                if val:
                    out[key] = val
        if out:
            out[SOURCE_KEY_NHTSA] = True
        return out
    except Exception:
        log.warning("NHTSA fetch failed for VIN %s", vin)
        return {}


def _fetch_autodev(vin: str, api_key: str) -> dict[str, Any]:
    try:
        url = _AUTODEV_URL.format(vin.upper())
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "DrivePulse/1.0",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out: dict[str, Any] = {}
        for api_key_name, field in _AUTODEV_FIELDS.items():
            val = _clean(data.get(api_key_name) or "")
            if val:
                out[field] = val
        vehicle = data.get("vehicle") or {}
        if vehicle.get("year"):
            out["year"] = str(int(vehicle["year"]))
        if vehicle.get("manufacturer"):
            val = _clean(vehicle["manufacturer"])
            if val:
                out["manufacturer"] = val
        # Parse engine string for cylinders / displacement when not yet set
        engine = _clean(data.get("engine") or "")
        if engine:
            import re
            if not out.get("cylinders"):
                m = re.search(r"V(\d+)|(\d+)-?[Cc]yl", engine)
                if m:
                    out["cylinders"] = m.group(1) or m.group(2)
            if not out.get("displacement"):
                m = re.search(r"(\d+\.\d+)\s*L", engine)
                if m:
                    out["displacement"] = m.group(1)
        if out:
            out[SOURCE_KEY_AUTODEV] = True
        return out
    except Exception:
        log.warning("auto.dev fetch failed for VIN %s", vin)
        return {}


def _fetch_vindecoder(vin: str, api_key: str, secret_key: str) -> dict[str, Any]:
    try:
        vin_upper = vin.upper()
        control = hashlib.sha1(
            f"{vin_upper}|decode|{api_key}|{secret_key}".encode()
        ).hexdigest()[:10]
        url = _VINDECODER_URL.format(api_key, control, vin_upper)
        req = urllib.request.Request(url, headers={"User-Agent": "DrivePulse/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("decode") or []
        out: dict[str, Any] = {}
        for item in items:
            key = _VINDECODER_FIELDS.get(item.get("label", ""))
            if key:
                val = _clean(item.get("value") or "")
                if val:
                    out[key] = val
        if out:
            out[SOURCE_KEY_VINDECODER] = True
        return out
    except Exception:
        log.warning("vindecoder.eu fetch failed for VIN %s", vin)
        return {}


def fetch_vin_data(
    vin: str,
    autodev_api_key: str | None = None,
    vindecoder_api_key: str | None = None,
    vindecoder_secret_key: str | None = None,
) -> dict[str, Any]:
    """Return merged VIN data from NHTSA and optionally auto.dev / vindecoder.eu.

    Source-tracking keys (SOURCE_KEY_*) are included so the caller can show
    which services contributed data. Strip them before persisting.

    Returns an empty dict if VIN is too short or all requests fail.
    Priority: vindecoder > auto.dev > NHTSA (each fills gaps left by the next).
    """
    if not vin or len(vin) < 11:
        return {}

    result = _fetch_nhtsa(vin)

    if autodev_api_key:
        ad = _fetch_autodev(vin, autodev_api_key)
        for k, v in ad.items():
            if k not in result or not result[k]:
                result[k] = v

    if vindecoder_api_key and vindecoder_secret_key:
        vd = _fetch_vindecoder(vin, vindecoder_api_key, vindecoder_secret_key)
        for k, v in vd.items():
            if k not in result or not result[k]:
                result[k] = v

    return result


def strip_source_keys(data: dict[str, Any]) -> dict[str, Any]:
    """Remove internal source-tracking keys before persisting."""
    return {k: v for k, v in data.items() if not k.startswith("_src_")}
