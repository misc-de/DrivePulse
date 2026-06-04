"""Speed-limit handling for the map view.

Two unrelated speed concerns live here:

* ``fetch_overpass_speed_zones`` queries the Overpass API for the
  maxspeed tags along a calculated route — used to overlay the
  posted speed limit on the route line.
* ``_remap_speed_to_route`` projects the *recorded* GPS speed of a
  past trip onto the snapped route coordinates so a replay can colour
  the route by historical pace.

Plus the maxspeed-tag parser (``_parse_maxspeed``) and a tiny mock
helper that synthesises realistic speeds from OSRM ``ref`` tags when
Valhalla/OSRM didn't provide them.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

from drivepulse_app.http_client import http_post
from drivepulse_app.map._geometry import _pt_seg_dist2_approx

HttpPost = Callable[..., Any]

# Try the main instance first, then community mirrors — the main overpass-api.de
# returns 504/empty under load often enough that a single failure used to leave a
# whole tour without real speed limits.
_OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
_OVERPASS_URL = _OVERPASS_URLS[0]

# Small TTL cache of computed zone breakpoints, keyed by the exact Overpass query
# (which encodes the sampled route points). Lets an identical/recomputed route —
# e.g. an app-restart resume — reuse limits instead of re-hitting a flaky
# Overpass, so a single 504 can't blank the whole tour.
_ZONE_CACHE: dict[str, tuple[float, list[tuple[float, float]]]] = {}
_ZONE_CACHE_TTL_S = 900.0


def _zone_cache_get(query: str) -> list[tuple[float, float]] | None:
    hit = _ZONE_CACHE.get(query)
    if hit is None:
        return None
    ts, zones = hit
    if time.monotonic() - ts > _ZONE_CACHE_TTL_S:
        _ZONE_CACHE.pop(query, None)
        return None
    return zones


def _zone_cache_put(query: str, zones: list[tuple[float, float]]) -> None:
    _ZONE_CACHE[query] = (time.monotonic(), zones)
    if len(_ZONE_CACHE) > 64:  # bound memory — drop the oldest entries
        for k, _ in sorted(_ZONE_CACHE.items(), key=lambda kv: kv[1][0])[:16]:
            _ZONE_CACHE.pop(k, None)

# Named maxspeed tags used by OSM / Valhalla (→ km/h)
_MAXSPEED_NAMED: dict[str, float] = {
    "de:living_street": 7.0,
    "de:pedestrian": 10.0,
    "de:urban": 50.0,
    "de:rural": 100.0,
    "de:motorway": 130.0,
    "at:living_street": 10.0,
    "at:urban": 50.0,
    "at:rural": 100.0,
    "at:motorway": 130.0,
    "walk": 7.0,
    "none": 130.0,
}


def mock_speed_kmh(ref: str) -> float:
    """Return a realistic mock speed for a road segment based on its OSRM ref tag.

    A* (Autobahn) → 120 km/h, B* (Bundesstraße) / other refs → 70 km/h,
    no ref (urban streets) → 40 km/h.
    """
    r = ref.strip()
    if not r:
        return 40.0
    first = r[0].upper()
    if first == "A" and (len(r) == 1 or not r[1].isalpha()):
        return 120.0
    return 70.0


def _parse_maxspeed(raw: str) -> float | None:
    """Parse an OSM maxspeed tag value to km/h, or return None if unparseable."""
    v = raw.strip().lower()
    if not v:
        return None
    if v in _MAXSPEED_NAMED:
        return _MAXSPEED_NAMED[v]
    if v.endswith(" mph"):
        try:
            return round(float(v[:-4].strip()) * 1.60934)
        except ValueError:
            return None
    try:
        val = float(v)
        return val if val > 0 else None
    except ValueError:
        return None


def fetch_overpass_speed_zones(
    coords: list[list[float]],
    sample_every_m: float = 200.0,
    around_m: float = 30.0,
    http_post_fn: HttpPost = http_post,
) -> list[tuple[float, float]]:
    """Pre-fetch speed limits for the entire route via Overpass API.

    Samples the route polyline every *sample_every_m* metres, fetches all
    highway ways with a maxspeed tag within *around_m* metres of those
    points in one Overpass query, then assigns the nearest way's speed to
    each sample and returns (cum_dist_m, speed_kmh) zone breakpoints —
    the same format consumed by _update_speed_zone_overlay().

    Runs in a background thread; returns [] on network or parse failure.
    """
    if len(coords) < 2:
        return []

    R = 6_371_000.0

    # Cumulative distances along the route polyline.
    cum: list[float] = [0.0]
    for i in range(1, len(coords)):
        a_lon, a_lat = coords[i - 1]
        b_lon, b_lat = coords[i]
        dlat = math.radians(b_lat - a_lat)
        dlon = math.radians(b_lon - a_lon)
        mlat = math.radians((a_lat + b_lat) * 0.5)
        cum.append(cum[-1] + R * math.sqrt(dlat ** 2 + (math.cos(mlat) * dlon) ** 2))

    # Sample the route at regular intervals.
    samples: list[tuple[float, float, float]] = []  # (cum_m, lat, lon)
    next_target = 0.0
    for i, (c, coord) in enumerate(zip(cum, coords, strict=True)):
        if c >= next_target or i == 0:
            lon, lat = coord
            samples.append((c, lat, lon))
            next_target = c + sample_every_m
    last_lon, last_lat = coords[-1]
    if not samples or samples[-1][0] < cum[-1] - 1.0:
        samples.append((cum[-1], last_lat, last_lon))

    # Build Overpass QL query — one around-buffer along all sample points.
    pts = "".join(f",{lat},{lon}" for _, lat, lon in samples)
    query = (
        f"[out:json][timeout:60];\n"
        f"way(around:{int(around_m)}{pts})[highway][maxspeed];\n"
        f"out body geom;\n"
    )

    cached = _zone_cache_get(query)
    if cached is not None:
        return list(cached)

    data = None
    for url in _OVERPASS_URLS:
        data = http_post_fn(url, query)
        if data:
            break
    if not data:
        return []

    # Parse returned ways into (speed_kmh, [(lat, lon), ...]).
    ways: list[tuple[float, list[tuple[float, float]]]] = []
    for el in data.get("elements") or []:
        if el.get("type") != "way":
            continue
        speed = _parse_maxspeed(str((el.get("tags") or {}).get("maxspeed", "")))
        if speed is None or speed <= 0:
            continue
        geometry = el.get("geometry") or []
        nodes: list[tuple[float, float]] = [
            (n["lat"], n["lon"]) for n in geometry if "lat" in n and "lon" in n
        ]
        if len(nodes) >= 2:
            ways.append((speed, nodes))

    if not ways:
        return []

    # Assign the nearest way's speed to each sample point.
    zones: list[tuple[float, float]] = []
    prev_speed: float | None = None

    for cum_m, s_lat, s_lon in samples:
        best_d2 = float("inf")
        best_speed: float | None = None
        for speed, nodes in ways:
            for j in range(len(nodes) - 1):
                a_lat, a_lon = nodes[j]
                b_lat, b_lon = nodes[j + 1]
                d2 = _pt_seg_dist2_approx(s_lat, s_lon, a_lat, a_lon, b_lat, b_lon)
                if d2 < best_d2:
                    best_d2 = d2
                    best_speed = speed
        if best_speed is not None and best_speed != prev_speed:
            zones.append((cum_m, best_speed))
            prev_speed = best_speed

    if zones:
        _zone_cache_put(query, zones)
    return zones


def _remap_speed_to_route(
    route_coords: list[list[float]],
    latlon_speed: list[tuple[float, float, float | None]],
) -> list[tuple[float, float, float | None]]:
    """Project GPS speed values onto calculated route coordinates.

    For each route point, picks the nearest original GPS sample by
    flat-earth squared distance and carries over its speed value.
    Returns a list of (lat, lon, speed_or_None) matching route_coords.
    """
    result: list[tuple[float, float, float | None]] = []
    for lon, lat in route_coords:
        best_speed: float | None = None
        best_d2 = float("inf")
        cos_lat = math.cos(math.radians(lat))
        for g_lat, g_lon, speed in latlon_speed:
            dlat = lat - g_lat
            dlon = (lon - g_lon) * cos_lat
            d2 = dlat * dlat + dlon * dlon
            if d2 < best_d2:
                best_d2 = d2
                best_speed = speed
        result.append((lat, lon, best_speed))
    return result
