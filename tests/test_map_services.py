from __future__ import annotations


def test_format_duration():
    from drivepulse_app.map.services import format_distance, format_duration

    assert format_duration(59) == "0min"
    assert format_duration(125) == "2min"
    assert format_duration(3661) == "1h 1min"
    assert format_distance(30_000) == "30 km"
    assert format_distance(12_500) == "12.5 km"
    assert format_distance(1_250) == "1.2 km"
    # Sub-kilometre distances render in metres, not "0.x km".
    assert format_distance(820) == "820 m"
    assert format_distance(47) == "50 m"
    assert format_distance(999) == "1000 m"
    # Imperial: feet under ~0.2 mi, miles otherwise.
    assert format_distance(150, "imperial") == "490 ft"
    assert format_distance(2_500, "imperial") == "1.6 mi"
    assert format_distance(50_000, "imperial") == "31.1 mi"


def test_geocode_handles_empty_and_malformed_responses():
    from drivepulse_app.map.services import geocode

    assert geocode("Nowhere", lambda _url: []) is None
    assert geocode("Broken", lambda _url: [{"lat": "bad"}]) is None
    assert geocode("Munich", lambda _url: [{"lat": "48.137", "lon": "11.576"}]) == (
        48.137,
        11.576,
    )


def test_osrm_route_builds_request_and_parses_result():
    from drivepulse_app.map.services import osrm_route

    seen_urls = []

    def fake_get(url: str):
        seen_urls.append(url)
        return {
            "code": "Ok",
            "routes": [
                {
                    "duration": 125.0,
                    "distance": 30000.0,
                    "geometry": {"coordinates": [[11.0, 48.0], [12.0, 49.0]]},
                }
            ],
        }

    result = osrm_route([(48.0, 11.0), (49.0, 12.0)], fake_get)

    assert result == ([[11.0, 48.0], [12.0, 49.0]], 125.0, 30000.0, [])
    assert "/route/v1/driving/11.0,48.0;12.0,49.0" in seen_urls[0]
    assert "steps=true" in seen_urls[0]


def test_osrm_route_rejects_missing_waypoints():
    from drivepulse_app.map.services import osrm_route

    assert osrm_route([(48.0, 11.0)], lambda _url: {}) is None


def test_osrm_route_rejects_malformed_success_response():
    from drivepulse_app.map.services import osrm_route

    assert osrm_route(
        [(48.0, 11.0), (49.0, 12.0)],
        lambda _url: {"code": "Ok", "routes": [{"duration": "bad"}]},
    ) is None
    assert osrm_route(
        [(48.0, 11.0), (49.0, 12.0)],
        lambda _url: {"code": "Ok", "routes": [{"geometry": {"coordinates": "bad"}}]},
    ) is None


def test_resolve_route_points_uses_gps_as_empty_start_and_skips_empty_waypoints():
    from drivepulse_app.map.services import resolve_route_points

    lookup = {
        "Via": (48.5, 11.5),
        "End": (49.0, 12.0),
    }

    points = resolve_route_points(
        "",
        ["", "Via"],
        "End",
        (48.0, 11.0),
        lookup.get,
    )

    assert points == [(48.0, 11.0), (48.5, 11.5), (49.0, 12.0)]


def test_resolve_route_points_requires_start_and_end():
    from drivepulse_app.map.services import resolve_route_points

    geocode = {"Start": (48.0, 11.0), "End": (49.0, 12.0)}.get

    assert resolve_route_points("", [], "End", None, geocode) is None
    assert resolve_route_points("Start", [], "", None, geocode) is None
    assert resolve_route_points("Start", ["Missing"], "End", None, geocode) is None


def test_bab_fetch_road_marks_kind_and_road():
    from drivepulse_app.map.services import bab_fetch_road

    def fake_get(url: str):
        if url.endswith("/services/roadworks"):
            return {"roadworks": [{"title": "Work"}]}
        if url.endswith("/services/warning"):
            return {"warning": [{"title": "Jam"}]}
        return {}

    items = bab_fetch_road("A 9", fake_get)

    assert items == [
        {"title": "Work", "_kind": "roadworks", "_road": "A 9"},
        {"title": "Jam", "_kind": "incidents", "_road": "A 9"},
    ]


def test_poi_category_and_geometry_helpers():
    from drivepulse_app.map.services import haversine, poi_category, zoom_for_bbox

    assert poi_category({"amenity": "fuel"}) == "fuel"
    assert poi_category({"shop": "convenience"}) == "shop"
    assert poi_category({}) == "other"
    assert 100_000 < haversine(48.137, 11.576, 49.0, 12.0) < 110_000
    assert 1 <= zoom_for_bbox(48.0, 11.0, 49.0, 12.0) <= 17


def test_snap_to_route_midpoint():
    """Point exactly on the segment midpoint snaps to that point."""
    from drivepulse_app.map.services import snap_to_route

    # Simple East-West segment along the equator: lon 0→1, lat 0
    coords = [[0.0, 0.0], [1.0, 0.0]]
    cum_m = [0.0]  # cumulative distance to vertex 0

    slat, slon, seg, cum = snap_to_route(0.0, 0.5, coords, cum_m)

    assert seg == 0
    assert abs(slat - 0.0) < 1e-9
    assert abs(slon - 0.5) < 1e-6
    # cum ≈ half the segment length (~55 600 m)
    assert 50_000 < cum < 60_000


def test_snap_to_route_clamps_before_start():
    """Point before the segment start snaps to the first vertex."""
    from drivepulse_app.map.services import snap_to_route

    coords = [[0.0, 0.0], [1.0, 0.0]]
    cum_m = [0.0]

    slat, slon, seg, cum = snap_to_route(0.0, -1.0, coords, cum_m)

    assert seg == 0
    assert abs(slat - 0.0) < 1e-9
    assert abs(slon - 0.0) < 1e-9
    assert abs(cum) < 1.0  # at the route start


def test_snap_to_route_clamps_after_end():
    """Point past the segment end snaps to the last vertex."""
    from drivepulse_app.map.services import snap_to_route

    coords = [[0.0, 0.0], [1.0, 0.0]]
    cum_m = [0.0]

    slat, slon, seg, cum = snap_to_route(0.0, 2.0, coords, cum_m)

    assert seg == 0
    assert abs(slat - 0.0) < 1e-9
    assert abs(slon - 1.0) < 1e-6
    # cum ≈ full segment length
    assert cum > 100_000


def test_snap_to_route_perpendicular_offset():
    """Point beside the road (offset in lat) snaps to the nearest foot."""
    from drivepulse_app.map.services import snap_to_route

    coords = [[0.0, 0.0], [1.0, 0.0]]
    cum_m = [0.0]

    # 0.1° north of the midpoint
    slat, slon, seg, cum = snap_to_route(0.1, 0.5, coords, cum_m)

    assert seg == 0
    assert abs(slat - 0.0) < 1e-6   # snapped back onto the road (lat=0)
    assert abs(slon - 0.5) < 1e-4   # same longitude as the foot


def test_snap_to_route_monotonic_start_idx():
    """start_idx prevents snapping back to an earlier segment."""
    from drivepulse_app.map.services import snap_to_route
    from drivepulse_app.map.services import haversine

    # Three-point route: A→B→C
    coords = [[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]]
    seg0_len = haversine(0.0, 0.0, 0.0, 1.0)
    cum_m = [0.0, seg0_len]  # cum at vertex 0 and 1

    # GPS is on the first segment but start_idx=1 forces forward-only search
    slat, slon, seg, cum = snap_to_route(0.0, 0.5, coords, cum_m, start_idx=1)

    assert seg == 1          # must NOT snap back to segment 0
    assert abs(slon - 1.0) < 1e-6   # clamped to start of segment 1


def test_snap_to_route_fallback_no_coords():
    """Returns raw position when route is empty."""
    from drivepulse_app.map.services import snap_to_route

    slat, slon, seg, cum = snap_to_route(48.0, 11.0, [], [], start_idx=0)

    assert slat == 48.0
    assert slon == 11.0
