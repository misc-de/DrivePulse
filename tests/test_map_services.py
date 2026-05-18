from __future__ import annotations


def test_format_duration():
    from drivepulse_app.map_services import format_distance, format_duration

    assert format_duration(59) == "0min"
    assert format_duration(125) == "2min"
    assert format_duration(3661) == "1h 1min"
    assert format_distance(30_000) == "30 km"
    assert format_distance(12_500) == "12.5 km"
    assert format_distance(1_250) == "1.2 km"


def test_geocode_handles_empty_and_malformed_responses():
    from drivepulse_app.map_services import geocode

    assert geocode("Nowhere", lambda _url: []) is None
    assert geocode("Broken", lambda _url: [{"lat": "bad"}]) is None
    assert geocode("Munich", lambda _url: [{"lat": "48.137", "lon": "11.576"}]) == (
        48.137,
        11.576,
    )


def test_osrm_route_builds_request_and_parses_result():
    from drivepulse_app.map_services import osrm_route

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

    result = osrm_route([(48.0, 11.0), (49.0, 12.0)], "car", fake_get)

    assert result == ([[11.0, 48.0], [12.0, 49.0]], 125.0, 30000.0, [])
    assert "/route/v1/driving/11.0,48.0;12.0,49.0" in seen_urls[0]
    assert "steps=true" in seen_urls[0]


def test_osrm_route_rejects_missing_waypoints():
    from drivepulse_app.map_services import osrm_route

    assert osrm_route([(48.0, 11.0)], "car", lambda _url: {}) is None


def test_resolve_route_points_uses_gps_as_empty_start_and_skips_empty_waypoints():
    from drivepulse_app.map_services import resolve_route_points

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
    from drivepulse_app.map_services import resolve_route_points

    geocode = {"Start": (48.0, 11.0), "End": (49.0, 12.0)}.get

    assert resolve_route_points("", [], "End", None, geocode) is None
    assert resolve_route_points("Start", [], "", None, geocode) is None
    assert resolve_route_points("Start", ["Missing"], "End", None, geocode) is None


def test_bab_fetch_road_marks_kind_and_road():
    from drivepulse_app.map_services import bab_fetch_road

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
    from drivepulse_app.map_services import haversine, poi_category, zoom_for_bbox

    assert poi_category({"amenity": "fuel"}) == "fuel"
    assert poi_category({"shop": "convenience"}) == "shop"
    assert poi_category({}) == "other"
    assert 100_000 < haversine(48.137, 11.576, 49.0, 12.0) < 110_000
    assert 1 <= zoom_for_bbox(48.0, 11.0, 49.0, 12.0) <= 17
