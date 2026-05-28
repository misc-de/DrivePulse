"""Autobahn traffic-incident fetchers (verkehr.autobahn.de).

These call the federal traffic JSON API to collect roadworks + warnings
for German Autobahnen. Parallelised per-road via a small thread pool;
no parsing of locations/coordinates here — that happens in the caller
once the raw entries are merged with the map view's bbox filter.
"""
from __future__ import annotations

import concurrent.futures
import urllib.parse
from collections.abc import Callable
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
