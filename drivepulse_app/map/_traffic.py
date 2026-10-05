"""Traffic fetchers — Autobahn API plus open city feeds (Köln, Bonn).

Every source is free and needs no API key:

* verkehr.autobahn.de — roadworks, warnings and closures on German
  Autobahnen. Parallelised per-road via a small thread pool.
* Stadt Köln — live congestion per road section (``traffic.php``) and the
  Verkehrskalender (closures, roadworks, events). Datenlizenz Deutschland
  Zero 2.0.
* Bundesstadt Bonn — live congestion per road section. CC0, attribution
  requested: "Datenquelle: Bundesstadt Bonn, Amt 66".

:func:`fetch_traffic` merges everything into two lists: point *events*
(one dict per incident, shape documented on :func:`normalize_bab_items`)
and *flow* line segments (``coords`` as ``[[lon, lat], ...]`` plus a
``level`` of ``free`` / ``slow`` / ``jam``).
"""
from __future__ import annotations

import concurrent.futures
import time
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from drivepulse_app.http_client import http_get

HttpGet = Callable[[str], Any]

BAB_BASE = "https://verkehr.autobahn.de/o/autobahn"

# Autobahnen with sections in North Rhine-Westphalia (NRW).
NRW_AUTOBAHNEN = frozenset([
    "A1", "A2", "A3", "A4", "A31", "A33", "A40", "A42", "A43", "A44",
    "A45", "A46", "A52", "A57", "A59", "A61", "A516", "A524", "A535",
    "A540", "A542", "A544", "A553", "A555", "A559", "A560", "A561",
    "A562", "A563", "A564", "A565",
])


def bab_fetch_road(road: str, http_get_fn: HttpGet = http_get) -> list[dict]:
    items: list[dict] = []
    encoded = urllib.parse.quote(road, safe="")
    for service, key, kind in (
        ("roadworks", "roadworks", "roadworks"),
        ("warning", "warning", "incidents"),
        ("closure", "closure", "closure"),
    ):
        data = http_get_fn(f"{BAB_BASE}/{encoded}/services/{service}")
        if data:
            for entry in data.get(key, []):
                entry["_kind"] = kind
                entry["_road"] = road
                items.append(entry)
    return items


def bab_fetch_all(http_get_fn: HttpGet = http_get) -> list[dict]:
    roads_resp = http_get_fn(f"{BAB_BASE}/")
    if not roads_resp:
        return []
    roads: list[str] = roads_resp.get("roads", [])
    all_items: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(lambda road: bab_fetch_road(road, http_get_fn), roads):
            all_items.extend(result)
    return all_items


def bab_fetch_nrw(http_get_fn: HttpGet = http_get) -> list[dict]:
    """Fetch traffic items only for NRW Autobahnen — faster than a full federal fetch."""
    all_items: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(
            lambda road: bab_fetch_road(road, http_get_fn),
            sorted(NRW_AUTOBAHNEN),
        ):
            all_items.extend(result)
    return all_items


def bab_fetch_sources(
    *,
    bundesweit: bool,
    nrw: bool,
    http_get_fn: HttpGet = http_get,
) -> list[dict]:
    """Fetch traffic items according to the enabled source flags.

    If *bundesweit* is set, fetches all German Autobahnen (superset of NRW).
    If only *nrw* is set, fetches only the NRW Autobahnen — faster and more focused.
    Returns an empty list when neither flag is set.
    """
    if bundesweit:
        return bab_fetch_all(http_get_fn)
    if nrw:
        return bab_fetch_nrw(http_get_fn)
    return []


# ── Normalisation ─────────────────────────────────────────────────────────────

# Shumate draws one widget per marker, so keep the Autobahn list bounded.
# Live warnings go first, then closures, so the cap drops roadworks before
# anything a driver needs right now.
MAX_BAB_EVENTS = 500
_KIND_PRIORITY = {"incidents": 0, "closure": 1, "roadworks": 2}

SOURCE_AUTOBAHN = "Autobahn GmbH"
SOURCE_KOELN = "Stadt Köln"
SOURCE_BONN = "Bundesstadt Bonn"


# abnormalTrafficType on Autobahn warnings → the same levels as city flow.
_BAB_TRAFFIC_LEVELS = {
    "SLOW_TRAFFIC": "slow",
    "QUEUING_TRAFFIC": "jam",
    "STATIONARY_TRAFFIC": "jam",
}


def _line_coords(geometry: Any) -> list[list[float]]:
    """``[[lon, lat], ...]`` of a GeoJSON LineString/MultiLineString, else ``[]``."""
    if not isinstance(geometry, dict):
        return []
    raw = geometry.get("coordinates") or []
    if geometry.get("type") == "MultiLineString":
        raw = [p for part in raw for p in part]
    elif geometry.get("type") != "LineString":
        return []
    coords: list[list[float]] = []
    for p in raw:
        try:
            coords.append([round(float(p[0]), 6), round(float(p[1]), 6)])
        except (TypeError, ValueError, IndexError):
            continue
    return coords if len(coords) >= 2 else []


def _truthy(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw or "").lower() == "true"


def normalize_bab_items(items: list[dict]) -> list[dict]:
    """Turn raw Autobahn API entries into map events.

    Each event has ``lat``, ``lon``, ``kind`` (``roadworks`` / ``incidents``
    / ``closure`` / ``event``), ``title``, ``subtitle``, ``description``
    (list of lines), ``road``, ``start`` (ISO timestamp), ``blocked``,
    ``delay`` (minutes as string), ``source``, ``line`` (affected stretch as
    ``[[lon, lat], ...]``, empty when unknown) and ``level`` (``slow`` /
    ``jam`` for congestion warnings, else ``None``). Announced-but-not-yet-
    active entries (``future``) are skipped.
    """
    ordered = sorted(items, key=lambda it: _KIND_PRIORITY.get(it.get("_kind", ""), 3))
    result: list[dict] = []
    for item in ordered:
        if len(result) >= MAX_BAB_EVENTS:
            break
        if _truthy(item.get("future")):
            continue
        point = item.get("point") or ""
        try:
            parts = point.split(",")
            lat = float(parts[0].strip())
            lon = float(parts[1].strip())
        except (ValueError, IndexError):
            continue
        if lat == 0.0 and lon == 0.0:
            continue
        kind = item.get("_kind", "incidents")
        desc_raw = item.get("description") or []
        if isinstance(desc_raw, str):
            description = [desc_raw]
        else:
            description = [str(s) for s in desc_raw if s]
        result.append({
            "lat": lat,
            "lon": lon,
            "kind": kind,
            "title": item.get("title") or (description[0] if description else kind),
            "subtitle": item.get("subtitle") or "",
            "description": description,
            "road": item.get("_road", ""),
            "start": item.get("startTimestamp") or "",
            "blocked": _truthy(item.get("isBlocked")),
            "delay": str(item.get("delayTimeValue") or ""),
            "source": SOURCE_AUTOBAHN,
            "line": _line_coords(item.get("geometry")),
            "level": _BAB_TRAFFIC_LEVELS.get(item.get("abnormalTrafficType") or ""),
        })
    return result


# ── Stadt Köln ────────────────────────────────────────────────────────────────

KOELN_FLOW_URL = "https://www.stadt-koeln.de/externe-dienste/open-data/traffic.php"
KOELN_CALENDAR_URL = (
    "https://geoportal.stadt-koeln.de/arcgis/rest/services/verkehr/"
    "verkehrskalender/MapServer/0/query?where=1%3D1&outFields=*&f=geojson"
)

# auslastung: 0 frei, 1 zähfließend, 2 Stau, 16 keine Daten, 32 Hinweis im Link.
_KOELN_LEVELS: dict[Any, str] = {0: "free", 1: "slow", 2: "jam"}

# Verkehrskalender typ: 1 Beeinträchtigung, 2 Vollsperrung, 3 Baustelle /
# Einengung, 8/9/11/14 Veranstaltungen.
_KOELN_KINDS: dict[Any, str] = {1: "incidents", 2: "closure", 3: "roadworks"}


def koeln_fetch_flow(http_get_fn: HttpGet = http_get) -> list[dict]:
    data = http_get_fn(KOELN_FLOW_URL)
    if not data:
        return []
    segments: list[dict] = []
    for feature in data.get("features", []):
        attrs = feature.get("attributes") or {}
        level = _KOELN_LEVELS.get(attrs.get("auslastung"))
        if level is None:
            continue
        for path in (feature.get("geometry") or {}).get("paths") or []:
            coords = [[float(p[0]), float(p[1])] for p in path if len(p) >= 2]
            if len(coords) >= 2:
                segments.append({
                    "coords": coords,
                    "level": level,
                    "name": attrs.get("name") or "",
                    "speed": None,
                    "source": SOURCE_KOELN,
                })
    return segments


def _ms_to_iso(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def koeln_fetch_events(
    http_get_fn: HttpGet = http_get, now: float | None = None,
) -> list[dict]:
    """Verkehrskalender entries that are active right now."""
    data = http_get_fn(KOELN_CALENDAR_URL)
    if not data:
        return []
    now_ms = (time.time() if now is None else now) * 1000.0
    events: list[dict] = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2 or props.get("anzeige") != 1:
            continue
        start, end = props.get("datum_von"), props.get("datum_bis")
        if isinstance(start, (int, float)) and start > now_ms:
            continue
        if isinstance(end, (int, float)) and end < now_ms:
            continue
        kind = _KOELN_KINDS.get(props.get("typ"), "event")
        desc = props.get("beschreibung") or ""
        events.append({
            "lat": float(coords[1]),
            "lon": float(coords[0]),
            "kind": kind,
            "title": props.get("name") or "",
            "subtitle": "",
            "description": [desc] if desc else [],
            "road": "",
            "start": _ms_to_iso(start),
            "blocked": kind == "closure",
            "delay": "",
            "source": SOURCE_KOELN,
            "line": [],
            "level": None,
        })
    return events


# ── Bundesstadt Bonn ──────────────────────────────────────────────────────────

BONN_FLOW_URL = "https://stadtplan.bonn.de/geojson?Thema=19584"

_BONN_LEVELS = {
    "normales verkehrsaufkommen": "free",
    "erhöhte verkehrsbelastung": "slow",
    "staugefahr": "jam",
}


def bonn_fetch_flow(http_get_fn: HttpGet = http_get) -> list[dict]:
    data = http_get_fn(BONN_FLOW_URL)
    if not data:
        return []
    segments: list[dict] = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        level = _BONN_LEVELS.get(str(props.get("verkehrsstatus") or "").strip().lower())
        if level is None:
            continue
        geom = feature.get("geometry") or {}
        lines = geom.get("coordinates") or []
        if geom.get("type") == "LineString":
            lines = [lines]
        speed = props.get("geschwindigkeit")
        for line in lines:
            coords = [[float(p[0]), float(p[1])] for p in line if len(p) >= 2]
            if len(coords) >= 2:
                segments.append({
                    "coords": coords,
                    "level": level,
                    "name": "",
                    "speed": speed if isinstance(speed, (int, float)) and speed > 0 else None,
                    "source": SOURCE_BONN,
                })
    return segments


# ── Aggregation ───────────────────────────────────────────────────────────────

def fetch_traffic(
    *,
    bundesweit: bool,
    nrw: bool,
    city: bool,
    http_get_fn: HttpGet = http_get,
) -> dict[str, list[dict]]:
    """Fetch every enabled source in parallel; returns ``{"events", "flow"}``."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        bab = pool.submit(
            bab_fetch_sources, bundesweit=bundesweit, nrw=nrw, http_get_fn=http_get_fn,
        )
        city_jobs = []
        if city:
            city_jobs = [
                pool.submit(koeln_fetch_events, http_get_fn),
                pool.submit(koeln_fetch_flow, http_get_fn),
                pool.submit(bonn_fetch_flow, http_get_fn),
            ]
        events = normalize_bab_items(bab.result())
        flow: list[dict] = []
        for job in city_jobs:
            for entry in job.result():
                (flow if "coords" in entry else events).append(entry)
    return {"events": events, "flow": flow}
