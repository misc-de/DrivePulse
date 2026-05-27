"""Pure map data helpers for routing, traffic and geometry."""
from __future__ import annotations

import concurrent.futures
import json as _json
import logging
import math
import urllib.parse
from collections.abc import Callable
from typing import Any

from drivepulse_app.diagnostics import get_logger, write_diagnostic_log
from drivepulse_app.http_client import http_get, http_post, http_post_json_result

log = get_logger(__name__)


def _log_valhalla_trace_failure(message: str, *args: Any, exc_info: Any = None, level: int = logging.WARNING) -> None:
    write_diagnostic_log(__name__, level, message, *args, exc_info=exc_info)


HttpGet = Callable[[str], Any]
GeocodeFn = Callable[[str], tuple[float, float] | None]

ROUTING_BACKENDS = ["osrm", "valhalla"]

_VALHALLA_URL = "https://valhalla.openstreetmap.de/route"
_VALHALLA_TRACE_URLS = [
    "https://valhalla1.openstreetmap.de/trace_route",
]
_OVERPASS_URL = "https://overpass-api.de/api/interpreter"

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

MAP_TYPES = ["map", "satellite", "dark", "grayscale"]
MAP_LABEL_KEYS = {
    "map": "map.type.map",
    "satellite": "map.type.satellite",
    "dark": "map.type.dark",
    "grayscale": "map.type.grayscale",
}
MAP_ICONS = {
    "map": "dialog-layers-symbolic",
    "satellite": "image-x-generic-symbolic",
    "dark": "night-light-symbolic",
    "grayscale": "preferences-color-symbolic",
}
TILE_URLS = {
    "map": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "satellite": (
        "https://server.arcgisonline.com/ArcGIS/rest/services"
        "/World_Imagery/MapServer/tile/{z}/{y}/{x}"
    ),
    "dark": "https://cartodb-basemaps-a.global.ssl.fastly.net/dark_all/{z}/{x}/{y}.png",
    "grayscale": "https://cartodb-basemaps-a.global.ssl.fastly.net/light_all/{z}/{x}/{y}.png",
}
TILE_ATTRIBUTION = {
    "map": "© OpenStreetMap contributors",
    "satellite": "© Esri, Maxar, Earthstar Geographics",
    "dark": "© OpenStreetMap, © CARTO",
    "grayscale": "© OpenStreetMap, © CARTO",
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
    """Reduce OSRM legs/steps to a flat list of upcoming maneuvers.

    Intermediate waypoints (via-points) produce spurious arrive+depart pairs
    between legs — these are routing hints, not real destinations.  Only the
    very first depart and the very last arrive are kept; all intermediate ones
    are dropped so the turn list reads as one continuous journey.
    """
    result: list[dict] = []
    n_legs = len(legs)
    for leg_idx, leg in enumerate(legs):
        steps = leg.get("steps", []) or []
        for step in steps:
            man = step.get("maneuver") or {}
            step_type = str(man.get("type") or "")
            # Drop re-depart at every via-point (keep only the trip's first depart)
            if step_type == "depart" and leg_idx > 0:
                continue
            # Drop arrive at every via-point (keep only the final destination arrive)
            if step_type == "arrive" and leg_idx < n_legs - 1:
                continue
            loc = man.get("location") or [0.0, 0.0]
            try:
                lon = float(loc[0])
                lat = float(loc[1])
            except (TypeError, ValueError, IndexError):
                continue
            result.append({
                "lat": lat,
                "lon": lon,
                "type": step_type,
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


def _pt_seg_dist2_approx(
    plat: float, plon: float,
    alat: float, alon: float,
    blat: float, blon: float,
) -> float:
    """Squared approximate distance (flat-earth) from point P to segment AB."""
    cos_lat = math.cos(math.radians((alat + blat) * 0.5))
    ax = (alon - plon) * cos_lat
    ay = alat - plat
    abx = (blon - alon) * cos_lat
    aby = blat - alat
    ab2 = abx * abx + aby * aby
    if ab2 < 1e-18:
        return ax * ax + ay * ay
    t = max(0.0, min(1.0, ((-ax) * abx + (-ay) * aby) / ab2))
    dx = ax + t * abx
    dy = ay + t * aby
    return dx * dx + dy * dy


def fetch_overpass_speed_zones(
    coords: list[list[float]],
    sample_every_m: float = 200.0,
    around_m: float = 30.0,
    http_post_fn=http_post,
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

    data = http_post_fn(_OVERPASS_URL, query)
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

    return zones


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


def _subsample_coords(
    coords: list[list[float]], max_pts: int
) -> list[list[float]]:
    """Return at most *max_pts* evenly-spaced points, always keeping first and last."""
    if len(coords) <= max_pts:
        return coords
    stride = (len(coords) - 1) / (max_pts - 1)
    result = [coords[round(i * stride)] for i in range(max_pts - 1)]
    result.append(coords[-1])
    return result


def _valhalla_error_summary(data: Any) -> str:
    if not isinstance(data, dict):
        return f"unexpected response type {type(data).__name__}"
    for key in ("error", "error_message", "message", "status"):
        value = data.get(key)
        if value:
            return str(value)
    if "trip" not in data:
        return f"missing trip in response keys={sorted(data.keys())}"
    return "malformed trip response"


def _trace_shape(coords: list[list[float]], *, typed: bool) -> list[dict[str, float | str]]:
    shape: list[dict[str, float | str]] = []
    for idx, c in enumerate(coords):
        point: dict[str, float | str] = {"lat": float(c[1]), "lon": float(c[0])}
        if typed:
            point["type"] = "break" if idx in {0, len(coords) - 1} else "via"
        shape.append(point)
    return shape


def _trace_bodies(shape: list[dict[str, float | str]]) -> list[tuple[str, dict[str, Any]]]:
    base: dict[str, Any] = {
        "costing": "auto",
        "shape_match": "map_snap",
        "directions_options": {"units": "kilometers"},
        "trace_options": {
            "gps_accuracy": 30,
            "search_radius": 50,
            "breakage_distance": 2_000,
        },
    }
    untyped_shape = [{"lat": p["lat"], "lon": p["lon"]} for p in shape]
    walk_or_snap_base = {**base, "shape_match": "walk_or_snap"}
    return [
        ("typed_map_snap", {"shape": shape, **base}),
        ("untyped_map_snap", {"shape": untyped_shape, **base}),
        ("walk_or_snap", {"shape": untyped_shape, **walk_or_snap_base}),
    ]


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


def _clean_gps_trace(
    coords: list[list[float]],
    timestamps: list[float] | None = None,
    spike_min_m: float = 30.0,
    cluster_radius_m: float = 15.0,
    stop_gap_s: float = 60.0,
) -> tuple[list[list[float]], list[int]]:
    """Remove GPS artefacts before waypoint extraction.

    Returns ``(cleaned_coords, stop_indices)`` where *stop_indices* is a list
    of indices in *cleaned_coords* at which a new leg begins (the first point
    after a stop gap).  Callers should split the trace at these indices so
    that each leg is routed independently and the stop boundary is guaranteed
    to appear as a waypoint.

    Pass 1 – spikes: a point that jumps far from both neighbours while the
    neighbours remain close to each other (classic multipath bounce).
    Condition: d(prev→curr) > spike_min_m AND d(curr→next) > spike_min_m
               AND d(prev→next) < 0.5 * max(d(prev→curr), d(curr→next)).

    Pass 2 – clusters: consecutive points within *cluster_radius_m* of the
    last committed point (e.g. maneuvering on a forecourt) are collapsed to
    just the cluster entry and exit.  When *timestamps* are supplied a gap
    of more than *stop_gap_s* seconds within a cluster is treated as a
    deliberate stop (e.g. engine off for refuelling): both the last point
    before the gap and the first point after are kept and the post-gap index
    is added to *stop_indices*.
    """
    if len(coords) < 3:
        return list(coords), []

    ts = timestamps  # parallel array; may be None

    # Pass 1: spike removal — carry timestamps in a parallel array
    no_spikes: list[list[float]] = [coords[0]]
    no_spikes_ts: list[float] = [ts[0] if ts else 0.0]
    i = 1
    while i < len(coords) - 1:
        prev = no_spikes[-1]
        curr = coords[i]
        nxt = coords[i + 1]
        d_prev = haversine(prev[1], prev[0], curr[1], curr[0])
        d_next = haversine(curr[1], curr[0], nxt[1], nxt[0])
        d_skip = haversine(prev[1], prev[0], nxt[1], nxt[0])
        if (d_prev > spike_min_m and d_next > spike_min_m
                and d_skip < max(d_prev, d_next) * 0.5):
            i += 1
            continue
        no_spikes.append(curr)
        no_spikes_ts.append(ts[i] if ts else 0.0)
        i += 1
    no_spikes.append(coords[-1])
    no_spikes_ts.append(ts[-1] if ts else 0.0)

    if len(no_spikes) < 2:
        return no_spikes, []

    # Pass 2: cluster collapse with stop-gap detection
    result: list[list[float]] = [no_spikes[0]]
    stop_indices: list[int] = []
    last_skipped: list[float] | None = None
    last_skipped_ts: float = 0.0

    for i in range(1, len(no_spikes)):
        pt = no_spikes[i]
        pt_ts = no_spikes_ts[i]

        # Stop gap within a cluster: end-of-leg / start-of-leg boundary
        if (last_skipped is not None and ts is not None
                and pt_ts - last_skipped_ts > stop_gap_s):
            result.append(last_skipped)      # last point before engine-off
            result.append(pt)               # first point after engine-on
            stop_indices.append(len(result) - 1)
            last_skipped = None
            continue

        anchor = result[-1]
        d = haversine(anchor[1], anchor[0], pt[1], pt[0])
        if d < cluster_radius_m:
            last_skipped = pt
            last_skipped_ts = pt_ts
        else:
            if last_skipped is not None:
                result.append(last_skipped)
                last_skipped = None
            result.append(pt)

    if last_skipped is not None:
        result.append(last_skipped)

    return result, stop_indices


def extract_turn_waypoints(
    coords_lonlat: list[list[float]],
    min_turn_deg: float = 30.0,
    min_segment_m: float = 150.0,
    max_waypoints: int = 25,
) -> list[tuple[float, float]]:
    """Extract start, significant turns, and end as (lat, lon) waypoints.

    Accumulates GPS points into segments of at least *min_segment_m* before
    computing each segment bearing — this filters per-point GPS noise before
    turn detection.  A waypoint is added when adjacent segment bearings differ
    by at least *min_turn_deg* degrees.
    """
    if len(coords_lonlat) < 2:
        return [(c[1], c[0]) for c in coords_lonlat]

    waypoints: list[tuple[float, float]] = [(coords_lonlat[0][1], coords_lonlat[0][0])]
    seg_start_idx = 0
    seg_dist = 0.0
    prev_seg_bearing: float | None = None

    for i in range(1, len(coords_lonlat)):
        a, b = coords_lonlat[i - 1], coords_lonlat[i]
        seg_dist += haversine(a[1], a[0], b[1], b[0])
        if seg_dist < min_segment_m:
            continue

        s = coords_lonlat[seg_start_idx]
        curr_bearing = bearing(s[1], s[0], b[1], b[0])

        if prev_seg_bearing is not None:
            diff = abs(curr_bearing - prev_seg_bearing) % 360.0
            if diff > 180.0:
                diff = 360.0 - diff
            if diff >= min_turn_deg:
                waypoints.append((b[1], b[0]))

        prev_seg_bearing = curr_bearing
        seg_start_idx = i
        seg_dist = 0.0

    last = (coords_lonlat[-1][1], coords_lonlat[-1][0])
    if waypoints[-1] != last:
        waypoints.append(last)

    if len(waypoints) > max_waypoints:
        step = (len(waypoints) - 1) / (max_waypoints - 1)
        sampled = [waypoints[round(i * step)] for i in range(max_waypoints - 1)]
        waypoints = sampled + [last]

    return waypoints


def _snap_waypoints_to_road(
    waypoints: list[tuple[float, float]],
    http_get_fn: HttpGet = http_get,
) -> list[tuple[float, float]]:
    """Snap each (lat, lon) waypoint to the nearest driveable road.

    Uses OSRM /nearest.  Falls back to the original coordinate if the
    request fails.  Calls are issued in parallel to keep latency low.
    """
    def _snap_one(wp: tuple[float, float]) -> tuple[float, float]:
        lat, lon = wp
        url = f"https://router.project-osrm.org/nearest/v1/driving/{lon},{lat}"
        try:
            data = http_get_fn(url)
            loc = data["waypoints"][0]["location"]  # [lon, lat]
            return (loc[1], loc[0])
        except (KeyError, IndexError, TypeError, AttributeError):
            return wp

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(_snap_one, waypoints))


def _subsample_cleaned_track(
    coords_lonlat: list[list[float]],
    interval_m: float = 50.0,
) -> list[tuple[float, float]]:
    """Sample a cleaned GPS track at ~interval_m distance intervals.

    Returns (lat, lon) tuples.  Always includes the first and last point.
    """
    if not coords_lonlat:
        return []
    result: list[tuple[float, float]] = [(coords_lonlat[0][1], coords_lonlat[0][0])]
    dist_acc = 0.0
    for i in range(1, len(coords_lonlat)):
        a, b = coords_lonlat[i - 1], coords_lonlat[i]
        dist_acc += haversine(a[1], a[0], b[1], b[0])
        if dist_acc >= interval_m:
            result.append((b[1], b[0]))
            dist_acc = 0.0
    last = (coords_lonlat[-1][1], coords_lonlat[-1][0])
    if result[-1] != last:
        result.append(last)
    return result


def _prune_bad_waypoints(
    waypoints: list[tuple[float, float]],
    protected: set[int] | None = None,
    save_threshold_m: float = 200.0,
    max_segment_m: float = float("inf"),
    http_get_fn: HttpGet = http_get,
) -> list[tuple[float, float]]:
    """Remove inner waypoints that force excessive routing detours.

    For each inner waypoint WP[i], compares the combined routing distance
    WP[i-1]→WP[i] + WP[i]→WP[i+1]  against the direct segment
    WP[i-1]→WP[i+1].  If skipping WP[i] saves more than *save_threshold_m*
    metres AND the resulting segment would stay under *max_segment_m*, the
    waypoint is removed.

    The *max_segment_m* guard prevents the pruner from creating long,
    guidance-free jumps that let the router pick arbitrary wrong streets.

    *protected* is a set of waypoint indices that must never be removed —
    use this to preserve stop-gap boundary points (e.g. the gas-station
    turnaround) that are genuine visited locations, not routing artefacts.

    Iterates until no further savings above the threshold are found.
    """
    wps = list(waypoints)
    prot: list[bool] = [False] * len(wps)
    prot[0] = True
    prot[-1] = True
    if protected:
        for idx in protected:
            if 0 <= idx < len(prot):
                prot[idx] = True

    changed = True
    while changed and len(wps) > 2:
        changed = False
        best_i, best_save = -1, 0.0
        for i in range(1, len(wps) - 1):
            if prot[i]:
                continue
            r_prev = compute_route([wps[i - 1], wps[i]], http_get_fn=http_get_fn)
            r_next = compute_route([wps[i], wps[i + 1]], http_get_fn=http_get_fn)
            r_skip = compute_route([wps[i - 1], wps[i + 1]], http_get_fn=http_get_fn)
            if r_prev is None or r_next is None or r_skip is None:
                continue
            if r_skip[2] > max_segment_m:
                continue  # removing this WP would create a too-long guidance gap
            save = (r_prev[2] + r_next[2]) - r_skip[2]
            if save > best_save:
                best_save = save
                best_i = i
        if best_i >= 0 and best_save > save_threshold_m:
            write_diagnostic_log(
                __name__, logging.INFO,
                "_prune_bad_waypoints removed WP%d (%.1f,%.1f) saved=%.0fm",
                best_i, wps[best_i][0], wps[best_i][1], best_save,
            )
            wps.pop(best_i)
            prot.pop(best_i)
            changed = True
    return wps


def _sample_waypoints(
    coords_lonlat: list[list[float]],
    min_segment_m: float = 50.0,
    max_waypoints: int = 60,
    sample_interval_m: float = 100.0,
) -> list[tuple[float, float]]:
    """Combine turn-apex waypoints with evenly spaced GPS samples.

    Extracts turn waypoints (bearing change ≥ 30°) and additionally adds one
    GPS point per *sample_interval_m* of accumulated distance.  This prevents
    long straight stretches or gradual curves from having no routing anchors,
    which would let the router choose wrong streets freely.

    The combined list is deduplicated (points within 10 m of an already-kept
    point are skipped) and capped at *max_waypoints*.
    """
    if len(coords_lonlat) < 2:
        return [(c[1], c[0]) for c in coords_lonlat]

    turn_wps = set(extract_turn_waypoints(
        coords_lonlat, min_segment_m=min_segment_m, max_waypoints=max_waypoints
    ))

    # Walk the trace; emit a sample every sample_interval_m AND all turn points.
    result: list[tuple[float, float]] = []
    acc = 0.0
    next_sample = 0.0

    def _add(lat: float, lon: float) -> None:
        if not result or haversine(result[-1][0], result[-1][1], lat, lon) > 10.0:
            result.append((lat, lon))

    for i, c in enumerate(coords_lonlat):
        lat, lon = c[1], c[0]
        is_turn = (lat, lon) in turn_wps
        if i > 0:
            prev = coords_lonlat[i - 1]
            acc += haversine(prev[1], prev[0], lat, lon)
        if is_turn or acc >= next_sample:
            _add(lat, lon)
            if acc >= next_sample:
                next_sample = acc + sample_interval_m

    last = (coords_lonlat[-1][1], coords_lonlat[-1][0])
    _add(*last)

    if len(result) > max_waypoints:
        step = (len(result) - 1) / (max_waypoints - 1)
        sampled = [result[round(i * step)] for i in range(max_waypoints - 1)]
        result = sampled + [last]

    return result


def _deduplicate_close_waypoints(
    waypoints: list[tuple[float, float]],
    min_dist_m: float = 20.0,
) -> list[tuple[float, float]]:
    """Drop consecutive inner waypoints within min_dist_m of the previous one.

    Always preserves the first and last waypoint.  Removes duplicated
    stop-boundary pairs (e.g. last-before-stop and first-after-stop that
    are both on the same gas-station forecourt 13 m apart).
    """
    if len(waypoints) <= 2:
        return waypoints
    result = [waypoints[0]]
    for wp in waypoints[1:-1]:
        if haversine(result[-1][0], result[-1][1], wp[0], wp[1]) >= min_dist_m:
            result.append(wp)
    result.append(waypoints[-1])
    return result


def _expand_turn_waypoints(
    turn_waypoints: list[tuple[float, float]],
    seg_coords: list[list[float]],
    window: int = 2,
) -> list[tuple[float, float]]:
    """Widen each turn apex to a ±window band of neighbouring GPS points.

    A single GPS apex at a turn may land on the wrong side of a one-way
    street, forcing a large routing detour.  Adding the neighbouring GPS
    points gives the router directional context so it can pick the correct
    approach road.  Points within 5 m of each other are deduplicated.
    """
    if window <= 0 or not turn_waypoints or not seg_coords:
        return turn_waypoints

    n = len(seg_coords)
    added: set[int] = set()
    result: list[tuple[int, tuple[float, float]]] = []

    for wp_lat, wp_lon in turn_waypoints:
        best_i, best_d = 0, float("inf")
        for j, c in enumerate(seg_coords):
            d = haversine(wp_lat, wp_lon, c[1], c[0])
            if d < best_d:
                best_d = d
                best_i = j
        for j in range(max(0, best_i - window), min(n, best_i + window + 1)):
            if j not in added:
                added.add(j)
                c = seg_coords[j]
                result.append((j, (c[1], c[0])))

    result.sort(key=lambda x: x[0])
    out: list[tuple[float, float]] = []
    for _, wp in result:
        if not out or haversine(out[-1][0], out[-1][1], wp[0], wp[1]) > 5.0:
            out.append(wp)
    return out


def route_via_gps_waypoints(
    coords_lonlat: list[list[float]],
    timestamps: list[float] | None = None,
    http_get_fn: HttpGet = http_get,
) -> tuple[list[list[float]], float, float, list[dict]] | None:
    """Compute a road-snapped route from a noisy GPS trace.

    Tries Valhalla map-matching (trace_route) first — it sends the full
    GPS shape and returns the exact roads driven.  Falls back to the
    waypoint-extraction approach (turn detection + routing) if
    map-matching is unavailable or fails.
    """
    if len(coords_lonlat) < 2:
        return None

    # Primary: map-matching via Valhalla trace_route, one leg at a time.
    # The full GPS trace often fails map-matching because stationary GPS
    # jitter (standing still = random drift in place) looks physically
    # impossible for a car.  We therefore:
    #   1. Clean the trace (spikes + cluster-collapse removes jitter).
    #   2. Split at stop-gaps so each driving leg is matched independently.
    #   3. Merge the per-leg results into a single route.
    cleaned_legs, stop_indices_legs = _clean_gps_trace(
        coords_lonlat, timestamps=timestamps
    )
    leg_boundaries = [0] + stop_indices_legs + [len(cleaned_legs)]
    leg_results: list[tuple[list[list[float]], float, float, list[dict]]] = []
    match_ok = True
    for k in range(len(leg_boundaries) - 1):
        leg = cleaned_legs[leg_boundaries[k]:leg_boundaries[k + 1]]
        if len(leg) < 2:
            match_ok = False
            break
        lr = valhalla_trace_route(leg)
        if lr is None:
            match_ok = False
            break
        leg_results.append(lr)

    if match_ok and leg_results:
        merged_coords: list[list[float]] = []
        merged_dur = 0.0
        merged_dist = 0.0
        merged_steps: list[dict] = []
        for coords_r, dur_r, dist_r, steps_r in leg_results:
            if merged_coords and coords_r and merged_coords[-1] == coords_r[0]:
                coords_r = coords_r[1:]
            merged_coords.extend(coords_r)
            merged_dur += dur_r
            merged_dist += dist_r
            merged_steps.extend(steps_r)
        write_diagnostic_log(
            __name__, logging.INFO,
            "route_via_gps_waypoints map_match_ok legs=%d dist_km=%.1f",
            len(leg_results), merged_dist / 1000.0,
        )
        return merged_coords, merged_dur, merged_dist, merged_steps

    write_diagnostic_log(
        __name__, logging.INFO,
        "route_via_gps_waypoints map_match_failed fallback_to_waypoints pts=%d",
        len(coords_lonlat),
    )

    # Fallback: waypoint extraction + routing + deviation correction.
    cleaned, _stop_indices = _clean_gps_trace(coords_lonlat, timestamps=timestamps)

    # Treat the entire trip as one continuous route.  Splitting at stop-gap
    # boundaries caused OSRM to emit arrive/depart steps at each via-point,
    # producing spurious "destination reached" markers mid-trip.  Genuine
    # U-turns (gas station etc.) are preserved by the reversal-protection below.
    wps = extract_turn_waypoints(cleaned, min_segment_m=30.0, max_waypoints=60)
    all_waypoints: list[tuple[float, float]] = list(wps)
    protected_wp_indices: set[int] = {0, len(all_waypoints) - 1}

    write_diagnostic_log(
        __name__, logging.INFO,
        "route_via_gps_waypoints pts=%d cleaned=%d waypoints=%d protected=%d",
        len(coords_lonlat), len(cleaned),
        len(all_waypoints), len(protected_wp_indices),
    )
    all_waypoints = _prune_bad_waypoints(
        all_waypoints, protected=protected_wp_indices,
        save_threshold_m=100.0, http_get_fn=http_get_fn
    )
    all_waypoints = _snap_waypoints_to_road(all_waypoints, http_get_fn=http_get_fn)
    all_waypoints = _deduplicate_close_waypoints(all_waypoints, min_dist_m=50.0)
    return compute_route(all_waypoints, http_get_fn=http_get_fn)


def _gps_route_deviations(
    gps_coords: list[list[float]],
    route_coords: list[list[float]],
    threshold_m: float = 50.0,
    min_streak: int = 3,
) -> list[tuple[int, tuple[float, float]]]:
    """Find GPS points that deviate significantly from the computed route.

    Returns a list of ``(gps_index, (lat, lon))`` — one entry per consecutive
    run of *min_streak* or more GPS points that are all farther than
    *threshold_m* from the route.  The returned point is the one with the
    maximum deviation within each run.
    """
    if len(route_coords) < 2 or not gps_coords:
        return []

    cos_lat = math.cos(math.radians(
        sum(c[1] for c in gps_coords) / len(gps_coords)
    ))

    def _dist_to_route(lon: float, lat: float) -> float:
        best_d2 = float("inf")
        px, py = lon * cos_lat, lat
        for i in range(len(route_coords) - 1):
            ax = route_coords[i][0] * cos_lat
            ay = route_coords[i][1]
            bx = route_coords[i + 1][0] * cos_lat
            by = route_coords[i + 1][1]
            abx, aby = bx - ax, by - ay
            ab2 = abx * abx + aby * aby
            if ab2 > 0.0:
                t = max(0.0, min(1.0, ((px - ax) * abx + (py - ay) * aby) / ab2))
            else:
                t = 0.0
            dx = px - (ax + t * abx)
            dy = py - (ay + t * aby)
            d2 = dx * dx + dy * dy
            if d2 < best_d2:
                best_d2 = d2
        return math.sqrt(best_d2) * 111_000.0

    corrections: list[tuple[int, tuple[float, float]]] = []
    streak_start: int | None = None
    worst_idx: int | None = None
    worst_dist = 0.0

    def _flush(end: int) -> None:
        nonlocal streak_start, worst_idx, worst_dist
        if streak_start is not None and worst_idx is not None:
            if end - streak_start >= min_streak:
                c = gps_coords[worst_idx]
                corrections.append((worst_idx, (c[1], c[0])))
        streak_start = None
        worst_idx = None
        worst_dist = 0.0

    for i, coord in enumerate(gps_coords):
        d = _dist_to_route(coord[0], coord[1])
        if d > threshold_m:
            if streak_start is None:
                streak_start = i
            if d > worst_dist:
                worst_idx = i
                worst_dist = d
        else:
            _flush(i)

    _flush(len(gps_coords))
    return corrections


def _route_orphan_corrections(
    route_coords: list[list[float]],
    gps_coords: list[list[float]],
    sample_m: float = 30.0,
    threshold_m: float = 40.0,
    min_orphan_m: float = 80.0,
) -> list[tuple[int, tuple[float, float]]]:
    """Find GPS correction points for route segments with no GPS support.

    Walks the route in *sample_m* steps.  Where a consecutive run of
    sample points all have no GPS point within *threshold_m*, the route
    passes through territory the car never visited.  For each such orphan
    segment longer than *min_orphan_m*, the nearest GPS point to the
    segment midpoint is returned as a correction waypoint.

    Return format matches _gps_route_deviations: list of
    ``(gps_index, (lat, lon))``.
    """
    if len(route_coords) < 2 or not gps_coords:
        return []

    cos_lat = math.cos(math.radians(
        sum(c[1] for c in route_coords) / len(route_coords)
    ))

    # Sample the route polyline at sample_m intervals
    samples: list[tuple[float, float, float]] = []  # (cum_m, lat, lon)
    cum = 0.0
    remainder = 0.0
    for i in range(len(route_coords) - 1):
        a, b = route_coords[i], route_coords[i + 1]
        seg_m = haversine(a[1], a[0], b[1], b[0])
        t = remainder / seg_m if seg_m > 0 else 0.0
        while t <= 1.0:
            lat = a[1] + t * (b[1] - a[1])
            lon = a[0] + t * (b[0] - a[0])
            samples.append((cum + t * seg_m, lat, lon))
            t += sample_m / seg_m if seg_m > 0 else 1.0
        remainder = (t - 1.0) * seg_m if seg_m > 0 else 0.0
        cum += seg_m

    # For each sample, find distance to nearest GPS point
    corrections: list[tuple[int, tuple[float, float]]] = []
    orphan_start_cum: float | None = None
    orphan_samples: list[tuple[float, float, float]] = []

    def _flush_orphan() -> None:
        if orphan_start_cum is None or not orphan_samples:
            return
        orphan_end_cum = orphan_samples[-1][0]
        if orphan_end_cum - orphan_start_cum < min_orphan_m:
            return
        mid_idx = len(orphan_samples) // 2
        _, mid_lat, mid_lon = orphan_samples[mid_idx]
        best_gi, best_d = 0, float("inf")
        for gi, g in enumerate(gps_coords):
            d = haversine(mid_lat, mid_lon, g[1], g[0])
            if d < best_d:
                best_d = d
                best_gi = gi
        gc = gps_coords[best_gi]
        corrections.append((best_gi, (gc[1], gc[0])))

    for cum_m, lat, lon in samples:
        px, py = lon * cos_lat, lat
        best_d2 = float("inf")
        for g in gps_coords:
            gx, gy = g[0] * cos_lat, g[1]
            d2 = (px - gx) ** 2 + (py - gy) ** 2
            if d2 < best_d2:
                best_d2 = d2
        dist_m = math.sqrt(best_d2) * 111_000.0
        if dist_m > threshold_m:
            if orphan_start_cum is None:
                orphan_start_cum = cum_m
            orphan_samples.append((cum_m, lat, lon))
        else:
            _flush_orphan()
            orphan_start_cum = None
            orphan_samples = []

    _flush_orphan()
    return corrections


def _insert_correction_waypoints(
    waypoints: list[tuple[float, float]],
    corrections: list[tuple[int, tuple[float, float]]],
    gps_coords: list[list[float]],
) -> list[tuple[float, float]]:
    """Merge GPS correction points into an ordered waypoint list.

    Each existing waypoint is mapped to its nearest GPS index; the corrections
    (also GPS indices) are then interleaved so the combined list stays in
    route order.  Points closer than 5 m to an already-present waypoint are
    dropped to avoid duplicates.
    """
    if not corrections:
        return waypoints

    # Map each waypoint to the nearest GPS point index
    wp_gps: list[tuple[int, tuple[float, float]]] = []
    for wp_lat, wp_lon in waypoints:
        best_i, best_d = 0, float("inf")
        for i, c in enumerate(gps_coords):
            d = haversine(wp_lat, wp_lon, c[1], c[0])
            if d < best_d:
                best_d = d
                best_i = i
        wp_gps.append((best_i, (wp_lat, wp_lon)))

    combined = wp_gps + corrections
    combined.sort(key=lambda x: x[0])

    result: list[tuple[float, float]] = []
    for _, wp in combined:
        if not result or haversine(result[-1][0], result[-1][1], wp[0], wp[1]) > 5.0:
            result.append(wp)

    return result


def osrm_match_route(
    coords_lonlat: list[list[float]],
    http_get_fn: HttpGet = http_get,
) -> tuple[list[list[float]], float, float, list[dict]] | None:
    """Map-match a GPS trace to roads via the OSRM /match service.

    Subsamples to ≤ 100 points (public-server limit).  Merges all
    returned matchings into a single (coords, duration_s, distance_m,
    steps) result so callers stay backend-agnostic.
    """
    if len(coords_lonlat) < 2:
        return None
    pts = _subsample_coords(coords_lonlat, 100)
    coord_str = ";".join(f"{c[0]},{c[1]}" for c in pts)
    url = (
        f"https://router.project-osrm.org/match/v1/driving/{coord_str}"
        "?overview=full&geometries=geojson&steps=true"
    )
    data = http_get_fn(url)
    code = (data or {}).get("code") if isinstance(data, dict) else None
    if not data or code != "Ok":
        write_diagnostic_log(
            __name__,
            logging.WARNING,
            "osrm_match_route failed code=%r message=%r pts=%d sampled_pts=%d",
            code,
            (data or {}).get("message") if isinstance(data, dict) else None,
            len(coords_lonlat),
            len(pts),
        )
        return None
    matchings = data.get("matchings") or []
    if not matchings:
        write_diagnostic_log(
            __name__, logging.WARNING,
            "osrm_match_route no matchings pts=%d sampled_pts=%d",
            len(coords_lonlat), len(pts),
        )
        return None
    coords: list[list[float]] = []
    duration_s = 0.0
    distance_m = 0.0
    steps: list[dict] = []
    for m in matchings:
        try:
            m_coords = m["geometry"]["coordinates"]
            if isinstance(m_coords, list):
                coords.extend(m_coords)
            duration_s += float(m.get("duration") or 0)
            distance_m += float(m.get("distance") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        steps.extend(_flatten_route_steps(m.get("legs") or []))
    if not coords:
        return None
    return coords, duration_s, distance_m, steps


def valhalla_trace_route(
    coords_lonlat: list[list[float]],
    http_post_json_fn=http_post_json_result,
) -> tuple[list[list[float]], float, float, list[dict]] | None:
    """Map-match a GPS trace to roads via Valhalla trace_route.

    Subsamples the input to ≤ 500 points so the request stays within
    Valhalla's default shape limit.  Returns the same
    (coords, duration_s, distance_m, steps) tuple as valhalla_route.
    """
    if len(coords_lonlat) < 2:
        _log_valhalla_trace_failure(
            "Valhalla trace_route skipped: need at least 2 points, got %d",
            len(coords_lonlat),
        )
        return None
    data = None
    shape: list[dict] = []
    prev_sampled = 0
    for max_pts in (500, 200):
        pts = _subsample_coords(coords_lonlat, max_pts)
        if len(pts) == prev_sampled:
            break
        prev_sampled = len(pts)
        try:
            shape = _trace_shape(pts, typed=True)
        except (IndexError, TypeError, ValueError) as exc:
            _log_valhalla_trace_failure("Valhalla trace_route invalid input: %s", exc)
            return None
        for url in _VALHALLA_TRACE_URLS:
            for variant, body in _trace_bodies(shape):
                response = http_post_json_fn(url, body)
                if isinstance(response, tuple):
                    data, status = response
                else:
                    data, status = response, None
                if data and not (isinstance(data, dict) and data.get("error_code")):
                    break
                reason = _valhalla_error_summary(data)
                _log_valhalla_trace_failure(
                    "Valhalla trace_route endpoint failed url=%s variant=%s "
                    "status=%r pts=%d sampled_pts=%d reason=%s",
                    url,
                    variant,
                    status,
                    len(coords_lonlat),
                    len(shape),
                    reason,
                    level=logging.DEBUG,
                )
            if data and not (isinstance(data, dict) and data.get("error_code")):
                break
        if data and not (isinstance(data, dict) and data.get("error_code")):
            break
        _log_valhalla_trace_failure(
            "Valhalla trace_route all variants failed sampled_pts=%d retrying",
            len(shape),
            level=logging.DEBUG,
        )
    if not data or (isinstance(data, dict) and data.get("error_code")):
        _log_valhalla_trace_failure(
            "Valhalla trace_route failed: no response pts=%d sampled_pts=%d",
            len(coords_lonlat), len(shape),
        )
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
            _log_valhalla_trace_failure(
                "Valhalla trace_route failed: decoded empty shape pts=%d "
                "sampled_pts=%d reason=%s",
                len(coords_lonlat),
                len(shape),
                _valhalla_error_summary(data),
            )
            return None
        steps = _flatten_valhalla_maneuvers(legs)
        return coords, duration_s, distance_m, steps
    except (KeyError, TypeError, ValueError) as exc:
        _log_valhalla_trace_failure(
            "Valhalla trace_route failed: parse error pts=%d sampled_pts=%d "
            "reason=%s error=%s",
            len(coords_lonlat),
            len(shape),
            _valhalla_error_summary(data),
            exc,
            exc_info=True,
        )
        return None


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
    heading: float | None = None,
) -> tuple[float, float, int, float]:
    """Project (lat, lon) onto the nearest forward route segment.

    Only searches from *start_idx* up to *window* segments ahead so the
    result advances monotonically — no GPS jitter can snap backward.

    When *heading* (degrees, 0=N, 90=E) is provided, segments that run
    more than 90° opposite to the vehicle's heading are penalised by 20×
    so the same road traversed in both directions snaps to the correct leg.

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

        # Direction-aware penalty: when the vehicle heading is known and the
        # segment runs more than 90° against the direction of travel, penalise
        # it strongly so the same road traversed in both directions (outgoing
        # vs. return leg) always snaps to the correct leg.
        # max(d2, 1e-18) ensures the penalty is nonzero even when the car is
        # exactly on the segment (d2 == 0.0), so the tiebreak is always resolved
        # by heading rather than by arbitrary float ordering.
        if heading is not None and ab2 > 0.0:
            seg_bearing = math.degrees(math.atan2(abx, aby)) % 360.0
            diff = abs(heading - seg_bearing) % 360.0
            if diff > 180.0:
                diff = 360.0 - diff
            if diff > 90.0:
                d2 = max(d2, 1e-18) * 20.0

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
    # 0.95 leaves a slim padding around the route; previous 0.88 + math.floor
    # was overly conservative and produced visibly empty borders. Shumate
    # accepts fractional zoom, so use the raw log2 result for a tighter fit.
    z_lat = math.log2(px_h * 0.95 / tile / dlat)
    z_lon = math.log2(px_w * 0.95 / tile / dlon)
    return float(max(1.0, min(float(zoom_max), z_lat, z_lon)))
