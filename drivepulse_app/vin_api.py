"""Fetch additional vehicle data from NHTSA vpic and vindecoder.eu."""
from __future__ import annotations

import hashlib
import json
import urllib.request
from typing import Any

from .diagnostics import get_logger

log = get_logger(__name__)

_NHTSA_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVin/{}?format=json"
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
        return out
    except Exception:
        log.warning("NHTSA fetch failed for VIN %s", vin)
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
        return out
    except Exception:
        log.warning("vindecoder.eu fetch failed for VIN %s", vin)
        return {}


def fetch_vin_data(
    vin: str,
    vindecoder_api_key: str | None = None,
    vindecoder_secret_key: str | None = None,
) -> dict[str, Any]:
    """Return merged VIN data from NHTSA and optionally vindecoder.eu.

    Returns an empty dict if VIN is too short or all requests fail.
    vindecoder values fill in gaps that NHTSA left blank.
    """
    if not vin or len(vin) < 11:
        return {}

    result = _fetch_nhtsa(vin)

    if vindecoder_api_key and vindecoder_secret_key:
        vd = _fetch_vindecoder(vin, vindecoder_api_key, vindecoder_secret_key)
        for k, v in vd.items():
            if k not in result or not result[k]:
                result[k] = v

    return result
