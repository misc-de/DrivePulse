"""Routing-backend clients for OSRM and Valhalla.

Three families of routes live here:

* Point-to-point routing via OSRM ``/route`` and Valhalla ``/route``,
  wrapped behind ``compute_route`` so the rest of the app stays
  backend-agnostic.
* GPS-trace map-matching via OSRM ``/match`` and Valhalla
  ``/trace_route`` — used by the tour-recompute pipeline to snap a
  driven track to actual roads.
* Step-list flatteners that normalise both backends' maneuver
  representations into the same dict shape downstream UI code consumes.

The module knows nothing about waypoint extraction, U-turn handling
or GPS cleaning — those callers live in services.py and call into
this module's public entry points.
"""
from __future__ import annotations

import json as _json
import logging
import urllib.parse
from collections.abc import Callable
from typing import Any

from drivepulse_app.diagnostics import get_logger, write_diagnostic_log
from drivepulse_app.http_client import http_get, http_post_json_result
from drivepulse_app.map._geometry import _decode_polyline, _subsample_coords

log = get_logger(__name__)

HttpGet = Callable[[str], Any]


def _log_valhalla_trace_failure(
    message: str, *args: Any, exc_info: Any = None, level: int = logging.WARNING,
) -> None:
    write_diagnostic_log(__name__, level, message, *args, exc_info=exc_info)


ROUTING_BACKENDS = ["osrm", "valhalla"]

_VALHALLA_URL = "https://valhalla.openstreetmap.de/route"
_VALHALLA_TRACE_URLS = [
    "https://valhalla1.openstreetmap.de/trace_route",
]

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
