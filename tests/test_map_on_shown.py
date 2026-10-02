"""Opening the map tab must re-engage GPS follow unless a route owns the camera."""
from __future__ import annotations

import pytest

page_mod = pytest.importorskip("drivepulse_app.map.page")
MapPage = page_mod.MapPage


def _page(**state):
    p = MapPage.__new__(MapPage)
    calls: list = []
    p.__dict__.update(
        _backend="shumate", _tour_active=False, _tour_paused=False,
        _route_coords=[], _pending_route_draw=False,
        _gps_lat=None, _gps_lon=None, _follow_gps=False,
    )
    p.__dict__.update(state)
    p._js = lambda code: calls.append(("js", code))
    p._set_follow = lambda on: calls.append(("follow", on))
    p._goto = lambda lat, lon: calls.append(("goto", lat, lon))
    return p, calls


def test_reengages_follow_without_fix():
    p, calls = _page()
    p.on_shown()
    assert ("follow", True) in calls
    assert not [c for c in calls if c[0] == "goto"]


def test_centres_on_known_fix():
    p, calls = _page(_gps_lat=50.9, _gps_lon=6.9)
    p.on_shown()
    assert ("follow", True) in calls
    assert ("goto", 50.9, 6.9) in calls


@pytest.mark.parametrize("state", [
    {"_tour_active": True}, {"_tour_paused": True},
    {"_route_coords": [[6.9, 50.9], [7.0, 51.0]]}, {"_pending_route_draw": True},
])
def test_route_keeps_its_camera(state):
    p, calls = _page(_gps_lat=50.9, _gps_lon=6.9, **state)
    p.on_shown()
    assert not [c for c in calls if c[0] in ("follow", "goto")]
