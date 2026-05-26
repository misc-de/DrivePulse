"""Fetch additional vehicle data from NHTSA vpic, auto.dev and vindecoder.eu."""
from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)

_NHTSA_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVin/{}?format=json"
_AUTODEV_URL = "https://api.auto.dev/vin/{}"
_VINDECODER_URL = "https://api.vindecoder.eu/3.2/{}/{}/decode/{}.json"

_NHTSA_FIELDS: dict[str, str] = {
    "Model":                       "model",
    "Model Year":                  "year",
    "Vehicle Type":                "vehicle_type",
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
    "model":         "model",
    "trim":          "trim",
    "style":         "style",
    "body":          "body",
    "drive":         "drive",
    "transmission":  "transmission",
    "origin":        "plant_country",
}

_VINDECODER_FIELDS: dict[str, str] = {
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


class AutodevError(Exception):
    """Fehler bei der auto.dev API (HTTP-Fehler oder ungültige Antwort)."""
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _fetch_autodev(
    vin: str,
    api_key: str,
    on_request: Callable[[], None] | None = None,
) -> dict[str, Any]:
    url = _AUTODEV_URL.format(urllib.parse.quote(vin.upper(), safe=""))
    print(f"[VIN] auto.dev GET {url} key_len={len(api_key)} key_repr={api_key[-10:]!r}", flush=True)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "DrivePulse/1.0",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if on_request:
                on_request()
            data = json.loads(resp.read().decode("utf-8"))
        print(f"[VIN] auto.dev OK fields={[k for k in data if not k.startswith('_') and k not in ('api','links','examples','photos','discover','actions','user')]}", flush=True)
    except urllib.error.HTTPError as exc:
        if on_request:
            on_request()
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        print(f"[VIN] auto.dev HTTP {exc.code} for {vin}: {body[:200]}", flush=True)
        log.warning("auto.dev HTTP %s for VIN %s: %s", exc.code, vin, body[:200])
        raise AutodevError(exc.code, f"HTTP {exc.code}") from exc
    except Exception as exc:
        print(f"[VIN] auto.dev exception for {vin}: {exc}", flush=True)
        log.warning("auto.dev fetch failed for VIN %s: %s", vin, exc)
        raise AutodevError(0, str(exc)) from exc

    # Fahrzeugspezifische Rohdaten — Metadaten (api, links, user, …) weglassen
    _META_KEYS = {"api", "links", "examples", "photos", "discover", "actions", "user"}
    raw: dict[str, Any] = {k: v for k, v in data.items() if k not in _META_KEYS}

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
        out["engine"] = engine
        import re
        if not out.get("cylinders"):
            m = re.search(r"V(\d+)|(\d+)-?[Cc]yl", engine)
            if m:
                out["cylinders"] = m.group(1) or m.group(2)
        if not out.get("displacement"):
            m = re.search(r"(\d+\.\d+)\s*L", engine)
            if m:
                out["displacement"] = m.group(1)
    out["_raw"] = raw
    return out


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
    nhtsa_enabled: bool = True,
    on_autodev_call: Callable[[], None] | None = None,
    on_source_done: Callable[[str, bool, str, int], None] | None = None,
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

    if nhtsa_enabled:
        nhtsa = _fetch_nhtsa(vin)
        if on_source_done:
            on_source_done("NHTSA", True, "", len(nhtsa))
        if nhtsa:
            sources["NHTSA"] = nhtsa

    _AUTODEV_VALID_LENGTHS = {10, 11, 13, 17}
    if autodev_api_key:
        if len(vin) not in _AUTODEV_VALID_LENGTHS:
            if on_source_done:
                on_source_done("auto.dev", False, "vin_format", 0)
            sources["auto.dev_error"] = {
                "_error": f"VIN length {len(vin)} not supported by auto.dev",
                "_status": 0,
                "_code": "INVALID_VIN_FORMAT",
            }
        else:
            try:
                ad = _fetch_autodev(vin, autodev_api_key, on_request=on_autodev_call)
                if ad:
                    raw = ad.pop("_raw", None)
                    if raw:
                        sources["auto.dev_raw"] = raw
                    if ad:
                        sources["auto.dev"] = ad
                if on_source_done:
                    on_source_done("auto.dev", True, "", len(sources.get("auto.dev", {})))
            except AutodevError as exc:
                if exc.status in (401, 403):
                    error_code = "auth"
                elif exc.status == 404:
                    error_code = "not_found"
                else:
                    error_code = "generic"
                if on_source_done:
                    on_source_done("auto.dev", False, error_code, 0)
                sources["auto.dev_error"] = {"_error": str(exc), "_status": exc.status}

    if vindecoder_api_key and vindecoder_secret_key:
        if len(vin) != 17:
            if on_source_done:
                on_source_done("vindecoder.eu", False, "vin_format", 0)
        else:
            vd = _fetch_vindecoder(vin, vindecoder_api_key, vindecoder_secret_key)
            if on_source_done:
                on_source_done("vindecoder.eu", True, "", len(vd))
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
