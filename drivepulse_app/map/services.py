"""Backwards-compat shim that re-exports symbols from the
``drivepulse_app.map._*`` submodules so legacy
``from drivepulse_app.map.services import …`` callers keep working.

The actual implementations live in:

* ``_geometry``     — math helpers (haversine, bearing, snap_to_route, …)
* ``_routing``      — OSRM + Valhalla backends and step flatteners
* ``_traffic``      — German Autobahn incident fetchers
* ``_geocoding``    — Nominatim wrapper + resolve_route_points
* ``_speed_zones``  — Overpass speed-limit queries + mock helpers
* ``_format``       — display formatters and maneuver icon/text lookups
* ``_tour_pipeline`` — GPS-trace → road-snapped tour reconstruction

The MAP_* style constants below stay here because they're consumed by
multiple UI mixins as a small style catalogue, not a routing concern.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from drivepulse_app.diagnostics import get_logger
from drivepulse_app.map._format import (  # noqa: F401
    format_distance,
    format_duration,
    maneuver_icon,
    maneuver_text_key,
    poi_category,
)
from drivepulse_app.map._geocoding import (  # noqa: F401
    geocode,
    resolve_route_points,
)
from drivepulse_app.map._geometry import (  # noqa: F401
    _decode_polyline,
    _pt_seg_dist2_approx,
    _subsample_coords,
    bearing,
    haversine,
    snap_to_route,
    zoom_for_bbox,
)
from drivepulse_app.map._routing import (  # noqa: F401
    _VALHALLA_MANEUVER,
    _VALHALLA_TRACE_URLS,
    _VALHALLA_URL,
    ROUTING_BACKENDS,
    _flatten_route_steps,
    _flatten_valhalla_maneuvers,
    _log_valhalla_trace_failure,
    _trace_bodies,
    _trace_shape,
    _valhalla_error_summary,
    compute_route,
    osrm_match_route,
    osrm_route,
    valhalla_route,
    valhalla_trace_route,
)
from drivepulse_app.map._speed_zones import (  # noqa: F401
    _MAXSPEED_NAMED,
    _OVERPASS_URL,
    _parse_maxspeed,
    _remap_speed_to_route,
    fetch_overpass_speed_zones,
    mock_speed_kmh,
)
from drivepulse_app.map._tour_pipeline import (  # noqa: F401
    _clean_gps_trace,
    _deduplicate_close_waypoints,
    _expand_turn_waypoints,
    _gps_route_deviations,
    _insert_correction_waypoints,
    _is_dead_end_uturn,
    _prune_bad_waypoints,
    _remove_uturn_waypoints,
    _route_orphan_corrections,
    _sample_waypoints,
    _snap_waypoints_to_road,
    _subsample_cleaned_track,
    _uturn_physically_impossible,
    _waypoint_bearings_from_track,
    extract_turn_waypoints,
    route_via_gps_waypoints,
)
from drivepulse_app.map._traffic import (  # noqa: F401
    BAB_BASE,
    NRW_AUTOBAHNEN,
    bab_fetch_all,
    bab_fetch_nrw,
    bab_fetch_road,
    bab_fetch_sources,
)

log = get_logger(__name__)

HttpGet = Callable[[str], Any]
GeocodeFn = Callable[[str], tuple[float, float] | None]


# Map-style catalogue — used by the UI to enumerate available tile layers,
# their icon + translation-key for the layer-toggle button, and the URL
# template + attribution string the map backend renders. Kept here (not in
# a submodule) because every map mixin imports it and it has no logic.
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
