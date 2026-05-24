"""Fetch additional vehicle data from NHTSA vpic, auto.dev and vindecoder.eu."""
from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from typing import Any

from drivepulse_app.diagnostics import get_logger

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


def _clean(val: Any) -> str:
    s = str(val).strip()
    return s if s.lower() not in _SKIP_VALUES else ""


def _fetch_nhtsa(vin: str) -> dict[str, Any]:
    try:
        url = _NHTSA_URL.format(urllib.parse.quote(vin.upper(), safe=""))
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


def _fetch_autodev(vin: str, api_key: str) -> dict[str, Any]:
    try:
        url = _AUTODEV_URL.format(urllib.parse.quote(vin.upper(), safe=""))
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
        for api_field, field in _AUTODEV_FIELDS.items():
            val = _clean(data.get(api_field) or "")
            if val:
                out[field] = val
        vehicle = data.get("vehicle") or {}
        if vehicle.get("year"):
            out["year"] = str(int(vehicle["year"]))
        if vehicle.get("manufacturer"):
            val = _clean(vehicle["manufacturer"])
            if val:
                out["manufacturer"] = val
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
        url = _VINDECODER_URL.format(
            urllib.parse.quote(api_key, safe=""),
            control,
            urllib.parse.quote(vin_upper, safe=""),
        )
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
    autodev_api_key: str | None = None,
    vindecoder_api_key: str | None = None,
    vindecoder_secret_key: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch VIN data from each configured source independently.

    Returns a dict keyed by source name, each containing the fields that
    source returned.  Empty sources are omitted.  The caller (review dialog)
    decides how to merge or present the per-source values.

    Example::

        {
            "NHTSA":        {"make": "VOLKSWAGEN", "year": "1999", ...},
            "auto.dev":     {"make": "Volkswagen", "model": "Golf", ...},
            "vindecoder.eu":{"make": "Volkswagen", "model": "Golf", "fuel": "Gasoline", ...},
        }
    """
    if not vin or len(vin) < 11:
        return {}

    sources: dict[str, dict[str, Any]] = {}

    nhtsa = _fetch_nhtsa(vin)
    if nhtsa:
        sources["NHTSA"] = nhtsa

    if autodev_api_key:
        ad = _fetch_autodev(vin, autodev_api_key)
        if ad:
            sources["auto.dev"] = ad

    if vindecoder_api_key and vindecoder_secret_key:
        vd = _fetch_vindecoder(vin, vindecoder_api_key, vindecoder_secret_key)
        if vd:
            sources["vindecoder.eu"] = vd

    return sources


def merge_sources(sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Merge all source dicts, last-write-wins (vindecoder > auto.dev > NHTSA)."""
    result: dict[str, Any] = {}
    for src_data in sources.values():
        for k, v in src_data.items():
            if v:
                result[k] = v
    return result


def strip_source_keys(data: dict[str, Any]) -> dict[str, Any]:
    """Remove any leftover internal keys before persisting."""
    return {k: v for k, v in data.items() if not k.startswith("_src_")}
