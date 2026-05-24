"""Pure map data helpers for routing, traffic and geometry."""
from __future__ import annotations

import concurrent.futures
import json as _json
import math
import urllib.parse
from typing import Any, Callable

from .diagnostics import get_logger
from .http_client import http_get


log = get_logger(__name__)


HttpGet = Callable[[str], Any]
GeocodeFn = Callable[[str], tuple[float, float] | None]

ROUTING_BACKENDS = ["osrm", "valhalla"]

_VALHALLA_URL = "https://valhalla.openstreetmap.de/route"

# Valhalla integer maneuver type → (osrm_type, osrm_modifier)
_VALHALLA_MANEUVER: dict[int, tuple[str, str]] = {
    0:  ("notification", ""),
    1:  ("depart",       ""),
    2:  ("depart",       "right"),
    3:  ("depart",       "left"),
    4:  ("arrive",       ""),
    5:  ("arrive",       "right"),
    6:  ("arrive",       "left"),
    7:  ("new name",     ""),
    8:  ("continue",     "straight"),
    9:  ("turn",         "slight right"),
    10: ("turn",         "right"),
    11: ("turn",         "sharp right"),
    12: ("turn",         "uturn"),
    13: ("turn",         "uturn"),
    14: ("turn",         "sharp left"),
    15: ("turn",         "left"),
    16: ("turn",         "slight left"),
    17: ("on ramp",      "straight"),
    18: ("on ramp",      "right"),
    19: ("on ramp",      "left"),
    20: ("off ramp",     "right"),
    21: ("off ramp",     "left"),
    22: ("fork",         "straight"),
    23: ("fork",         "right"),
    24: ("fork",         "left"),
    25: ("merge",        ""),
    26: ("roundabout",   ""),
    27: ("exit roundabout", ""),
    37: ("merge",        "right"),
    38: ("merge",        "left"),
}

MAP_TYPES = ["map", "satellite", "dark"]
MAP_LABEL_KEYS = {
    "map": "map.type.map",
    "satellite": "map.type.satellite",
    "dark": "map.type.dark",
}
MAP_ICONS = {
    "map": "dialog-layers-symbolic",
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
    http_get_fn: HttpGet = http_get,
) -> tuple[list[list[float]], float, float, list[dict]] | None:
    if len(waypoints) < 2:
        return None
    coord_str = ";".join(f"{lon},{lat}" for lat, lon in waypoints)
    url = (
        f"https://router.project-osrm.org/route/v1/driving/{coord_str}"
        f"?overview=full&geometries=geojson&steps=true"
    )
    data = http_get_fn(url)
    if data and data.get("code") == "Ok" and data.get("routes"):
        route = data["routes"][0]
        try:
            coords = route["geometry"]["coordinates"]
            duration = float(route.get("duration", 0))
            distance = float(route.get("distance", 0))
        except (KeyError, TypeError, ValueError):
            return None
        if not isinstance(coords, list):
            return None
        steps = _flatten_route_steps(route.get("legs", []))
        return (coords, duration, distance, steps)
    return None


def compute_route(
    waypoints: list[tuple[float, float]],
    http_get_fn: HttpGet = http_get,
) -> tuple[list[list[float]], float, float, list[dict]] | None:
    """Backend-agnostic car route lookup.

    Valhalla is the primary backend: it returns richer maneuvers + speed
    limits. OSRM is the fallback so a route is still returned if Valhalla
    is unreachable.
    """
    result = valhalla_route(waypoints, http_get_fn=http_get_fn)
    if result is not None:
        return result
    return osrm_route(waypoints, http_get_fn=http_get_fn)


def _flatten_route_steps(legs: list[dict]) -> list[dict]:
    """Reduce OSRM legs/steps to a flat list of upcoming maneuvers."""
    result: list[dict] = []
    for leg in legs:
        for step in leg.get("steps", []) or []:
            man = step.get("maneuver") or {}
            loc = man.get("location") or [0.0, 0.0]
            try:
                lon = float(loc[0])
                lat = float(loc[1])
            except (TypeError, ValueError, IndexError):
                continue
            result.append({
                "lat": lat,
                "lon": lon,
                "type": str(man.get("type") or ""),
                "modifier": str(man.get("modifier") or ""),
                "name": str(step.get("name") or ""),
                "ref": str(step.get("ref") or ""),
                "distance": float(step.get("distance") or 0.0),
                "exit": man.get("exit"),
            })
    return result


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


def _decode_polyline(encoded: str, precision: int = 6) -> list[list[float]]:
    """Decode a Valhalla/Google encoded polyline into [[lon, lat], ...] pairs."""
    factor = 10 ** precision
    result: list[list[float]] = []
    index = lat = lon = 0

    def _next_coord() -> int:
        nonlocal index
        shift = val = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            val |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        return ~(val >> 1) if val & 1 else val >> 1

    while index < len(encoded):
        lat += _next_coord()
        lon += _next_coord()
        result.append([lon / factor, lat / factor])
    return result


def valhalla_route(
    waypoints: list[tuple[float, float]],
    http_get_fn: HttpGet = http_get,
) -> tuple[list[list[float]], float, float, list[dict]] | None:
    """Car route via Valhalla (valhalla.openstreetmap.de).

    Returns the same (coords, duration_s, distance_m, steps) tuple as
    osrm_route so callers are backend-agnostic.  Steps include a
    ``speed_limit`` key (km/h) when Valhalla provides one.
    """
    if len(waypoints) < 2:
        return None
    costing = "auto"
    locations = [{"lon": lon, "lat": lat} for lat, lon in waypoints]

    def _try() -> tuple[list[list[float]], float, float, list[dict]] | None:
        body: dict[str, Any] = {
            "locations": locations,
            "costing": costing,
            "directions_options": {"units": "kilometers"},
        }
        url = f"{_VALHALLA_URL}?json={urllib.parse.quote(_json.dumps(body, separators=(',', ':')))}"
        data = http_get_fn(url)
        if not data:
            return None
        try:
            trip = data["trip"]
            summary = trip["summary"]
            duration_s = float(summary["time"])
            distance_m = float(summary["length"]) * 1000.0
            legs = trip.get("legs") or []
            coords: list[list[float]] = []
            for leg in legs:
                coords.extend(_decode_polyline(leg.get("shape", "")))
            if not coords:
                return None
            steps = _flatten_valhalla_maneuvers(legs)
            return coords, duration_s, distance_m, steps
        except (KeyError, TypeError, ValueError):
            return None

    return _try()


def _flatten_valhalla_maneuvers(legs: list[dict]) -> list[dict]:
    """Convert Valhalla maneuver objects into our internal step format."""
    result: list[dict] = []
    for leg in legs:
        shape = _decode_polyline(leg.get("shape", ""))
        for man in leg.get("maneuvers") or []:
            vtype = int(man.get("type") or 0)
            osrm_type, osrm_mod = _VALHALLA_MANEUVER.get(vtype, ("turn", ""))
            # Position: first shape point of this maneuver.
            begin_idx = int(man.get("begin_shape_index") or 0)
            if begin_idx < len(shape):
                lon, lat = shape[begin_idx][0], shape[begin_idx][1]
            elif shape:
                lon, lat = shape[0][0], shape[0][1]
            else:
                continue
            names: list[str] = man.get("street_names") or man.get("begin_street_names") or []
            speed_limit = man.get("speed_limit")
            step: dict[str, Any] = {
                "lat": lat,
                "lon": lon,
                "type": osrm_type,
                "modifier": osrm_mod,
                "name": names[0] if names else "",
                "ref": "",
                "distance": float(man.get("length") or 0.0) * 1000.0,
                "exit": man.get("roundabout_exit_count"),
            }
            if speed_limit is not None:
                step["speed_limit"] = float(speed_limit)
            lanes = man.get("lanes")
            if lanes:
                step["lanes"] = lanes
            sign = man.get("sign")
            if sign:
                step["sign"] = sign
            result.append(step)
    return result


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
    total = max(0, int(seconds))
    h = total // 3600
    m = (total % 3600) // 60
    if h > 0:
        return f"{h}h {m}min"
    return f"{m}min"


def format_distance(meters: float, units: str = "metric") -> str:
    meters = max(0.0, meters)
    if units == "imperial":
        miles = meters / 1609.344
        if miles < 0.2:
            # Below ~320 m, switch to feet rounded to a friendly 10 ft step.
            feet = meters * 3.28084
            return f"{int(round(feet / 10) * 10)} ft"
        if miles >= 10 and abs(miles - round(miles)) < 0.05:
            return f"{miles:.0f} mi"
        return f"{miles:.1f} mi"
    # metric
    if meters < 1000:
        # Show metres directly (rounded to 10 m) instead of "0.x km".
        return f"{int(round(meters / 10) * 10)} m"
    km = meters / 1000.0
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


def maneuver_icon(maneuver_type: str, modifier: str) -> str:
    """Map an OSRM maneuver type+modifier to a bundled dp-nav-* icon name."""
    if maneuver_type == "depart":
        return "dp-nav-depart-symbolic"
    if maneuver_type == "arrive":
        return "dp-nav-arrive-symbolic"
    if maneuver_type in {"roundabout", "rotary", "roundabout turn",
                          "exit roundabout", "exit rotary"}:
        return "dp-nav-roundabout-symbolic"
    if maneuver_type == "merge":
        return "dp-nav-merge-symbolic"
    if maneuver_type in {"on ramp", "off ramp"}:
        return "dp-nav-ramp-symbolic"
    if maneuver_type == "fork":
        if modifier in {"left", "slight left", "sharp left"}:
            return "dp-nav-fork-left-symbolic"
        return "dp-nav-fork-right-symbolic"
    if modifier == "uturn":
        return "dp-nav-uturn-symbolic"
    if modifier == "sharp left":
        return "dp-nav-sharp-left-symbolic"
    if modifier == "sharp right":
        return "dp-nav-sharp-right-symbolic"
    if modifier == "slight left":
        return "dp-nav-slight-left-symbolic"
    if modifier == "slight right":
        return "dp-nav-slight-right-symbolic"
    if modifier == "left":
        return "dp-nav-left-symbolic"
    if modifier == "right":
        return "dp-nav-right-symbolic"
    return "dp-nav-straight-symbolic"


def maneuver_text_key(maneuver_type: str, modifier: str) -> str:
    """Map an OSRM maneuver to a translation key."""
    if maneuver_type == "depart":
        return "map.maneuver.depart"
    if maneuver_type == "arrive":
        return "map.maneuver.arrive"
    if maneuver_type in {"roundabout", "rotary", "roundabout turn"}:
        return "map.maneuver.roundabout"
    if maneuver_type in {"exit roundabout", "exit rotary"}:
        return "map.maneuver.exit_roundabout"
    if maneuver_type == "merge":
        return "map.maneuver.merge"
    if maneuver_type == "fork":
        if modifier in {"left", "slight left", "sharp left"}:
            return "map.maneuver.fork.left"
        return "map.maneuver.fork.right"
    if maneuver_type == "on ramp":
        return "map.maneuver.on_ramp"
    if maneuver_type == "off ramp":
        return "map.maneuver.off_ramp"
    if modifier == "uturn":
        return "map.maneuver.uturn"
    if modifier == "sharp left":
        return "map.maneuver.turn.sharp_left"
    if modifier == "sharp right":
        return "map.maneuver.turn.sharp_right"
    if modifier == "slight left":
        return "map.maneuver.turn.slight_left"
    if modifier == "slight right":
        return "map.maneuver.turn.slight_right"
    if modifier == "left":
        return "map.maneuver.turn.left"
    if modifier == "right":
        return "map.maneuver.turn.right"
    return "map.maneuver.straight"


def snap_to_route(
    lat: float,
    lon: float,
    coords: list[list[float]],  # [[lon, lat], ...]
    cum_m: list[float],
    start_idx: int = 0,
    window: int = 200,
) -> tuple[float, float, int, float]:
    """Project (lat, lon) onto the nearest forward route segment.

    Only searches from *start_idx* up to *window* segments ahead so the
    result advances monotonically — no GPS jitter can snap backward.

    Returns (snapped_lat, snapped_lon, seg_idx, cum_m_to_snap).
    Falls back to the raw position when no valid segment exists.
    """
    n = len(coords)
    if n < 2 or not cum_m:
        return lat, lon, start_idx, cum_m[start_idx] if cum_m and start_idx < len(cum_m) else 0.0

    cos_lat = math.cos(math.radians(lat))
    lo = max(0, start_idx)
    hi = min(n - 1, lo + window)

    best_seg = min(lo, n - 2)
    best_t = 0.0
    best_dist2 = float("inf")

    for i in range(lo, hi):
        ax = coords[i][0] * cos_lat
        ay = coords[i][1]
        bx = coords[i + 1][0] * cos_lat
        by = coords[i + 1][1]
        px = lon * cos_lat
        py = lat

        abx = bx - ax
        aby = by - ay
        ab2 = abx * abx + aby * aby
        if ab2 == 0.0:
            t = 0.0
        else:
            t = (px - ax) * abx + (py - ay) * aby
            t = max(0.0, min(1.0, t / ab2))

        dx = px - (ax + t * abx)
        dy = py - (ay + t * aby)
        d2 = dx * dx + dy * dy

        if d2 < best_dist2:
            best_dist2 = d2
            best_seg = i
            best_t = t

    a = coords[best_seg]
    b = coords[best_seg + 1]
    snapped_lat = a[1] + best_t * (b[1] - a[1])
    snapped_lon = a[0] + best_t * (b[0] - a[0])
    seg_len = haversine(a[1], a[0], b[1], b[0])
    snapped_cum = cum_m[best_seg] + best_t * seg_len
    return snapped_lat, snapped_lon, best_seg, snapped_cum


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_m = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return radius_m * 2 * math.asin(math.sqrt(a))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial compass bearing in degrees (0=N, 90=E) from p1 to p2."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    x = math.sin(dlam) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


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
