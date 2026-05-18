"""Pure map data helpers for routing, traffic and geometry."""
from __future__ import annotations

import concurrent.futures
import math
import urllib.parse
from typing import Any, Callable

from .http_client import http_get


HttpGet = Callable[[str], Any]
GeocodeFn = Callable[[str], tuple[float, float] | None]

MAP_TYPES = ["map", "satellite", "dark"]
MAP_LABEL_KEYS = {
    "map": "map.type.map",
    "satellite": "map.type.satellite",
    "dark": "map.type.dark",
}
MAP_ICONS = {
    "map": "map-symbolic",
    "satellite": "image-x-generic-symbolic",
    "dark": "night-light-symbolic",
}
TILE_URLS = {
    "map": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "satellite": (
        "https://server.arcgisonline.com/ArcGIS/rest/services"
        "/World_Imagery/MapServer/tile/{z}/{y}/{x}"
    ),
    "dark": "https://cartodb-basemaps-a.global.ssl.fastly.net/dark_all/{z}/{x}/{y}.png",
}

BAB_BASE = "https://verkehr.autobahn.de/o/autobahn"
OSRM_PROFILE = {"car": "driving", "bicycle": "cycling", "motorcycle": "driving"}


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


def geocode(query: str, http_get_fn: HttpGet = http_get) -> tuple[float, float] | None:
    url = (
        "https://nominatim.openstreetmap.org/search"
        f"?q={urllib.parse.quote(query)}&format=json&limit=1"
    )
    data = http_get_fn(url)
    if not data:
        return None
    try:
        return float(data[0]["lat"]), float(data[0]["lon"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def osrm_route(
    waypoints: list[tuple[float, float]],
    mode: str,
    http_get_fn: HttpGet = http_get,
) -> tuple[list[list[float]], float, float] | None:
    if len(waypoints) < 2:
        return None
    profile = OSRM_PROFILE.get(mode, "driving")
    coord_str = ";".join(f"{lon},{lat}" for lat, lon in waypoints)
    url = (
        f"https://router.project-osrm.org/route/v1/{profile}/{coord_str}"
        "?overview=full&geometries=geojson"
    )
    data = http_get_fn(url)
    if data and data.get("code") == "Ok" and data.get("routes"):
        route = data["routes"][0]
        return (
            route["geometry"]["coordinates"],
            float(route.get("duration", 0)),
            float(route.get("distance", 0)),
        )
    return None


def resolve_route_points(
    start_text: str,
    waypoint_texts: list[str],
    end_text: str,
    gps: tuple[float, float] | None,
    geocode_fn: GeocodeFn = geocode,
) -> list[tuple[float, float]] | None:
    """Resolve route entries to coordinates, using GPS as empty start."""
    if not end_text:
        return None
    if start_text:
        start = geocode_fn(start_text)
    else:
        start = gps
    if start is None:
        return None

    points = [start]
    for text in waypoint_texts:
        if not text:
            continue
        point = geocode_fn(text)
        if point is None:
            return None
        points.append(point)

    end = geocode_fn(end_text)
    if end is None:
        return None
    points.append(end)
    return points


def format_duration(seconds: float) -> str:
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    if h > 0:
        return f"{h}h {m}min"
    return f"{m}min"


def format_distance(meters: float) -> str:
    km = max(0.0, meters / 1000.0)
    if km >= 10 and abs(km - round(km)) < 0.05:
        return f"{km:.0f} km"
    return f"{km:.1f} km"


def poi_category(tags: dict) -> str:
    amenity = tags.get("amenity", "")
    if amenity == "fuel":
        return "fuel"
    if amenity == "parking":
        return "parking"
    if amenity in {"restaurant", "fast_food", "cafe"}:
        return "food"
    if amenity in {"supermarket"} or tags.get("shop"):
        return "shop"
    if amenity in {"hospital", "pharmacy"}:
        return "medical"
    if tags.get("tourism"):
        return "tourism"
    return "other"


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_m = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return radius_m * 2 * math.asin(math.sqrt(a))


def zoom_for_bbox(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    px_w: int = 400,
    px_h: int = 600,
) -> float:
    tile = 256
    zoom_max = 17

    def lat_rad(lat: float) -> float:
        s = math.sin(math.radians(lat))
        return math.log((1 + s) / (1 - s)) / 2

    dlat = max(abs(lat_rad(lat2) - lat_rad(lat1)) / math.pi, 1e-9)
    dlon = max(abs(lon2 - lon1) / 360.0, 1e-9)
    z_lat = math.floor(math.log2(px_h * 0.88 / tile / dlat))
    z_lon = math.floor(math.log2(px_w * 0.88 / tile / dlon))
    return float(max(1, min(zoom_max, z_lat, z_lon)))
