"""Pure-math helpers used across the map subsystem.

Everything in here is self-contained — only stdlib math. No HTTP, no DB, no
GTK. That makes these the easy functions to unit-test and the safe ones to
import from anywhere in the map package without risking circular imports.
"""
from __future__ import annotations

import math


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
