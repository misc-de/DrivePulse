"""The preview start arrow must not survive into an active tour.

It is drawn exactly like the live car marker, so leaving it on the map once
navigation starts shows two vehicle arrows (start point + real position).
"""
from __future__ import annotations

import pytest

pytest.importorskip("gi")

from drivepulse_app.map.shumate import MapShumateMixin


class _FakeLayer:
    def __init__(self) -> None:
        self.markers: list[object] = []

    def add_marker(self, m: object) -> None:
        self.markers.append(m)

    def remove_marker(self, m: object) -> None:
        self.markers.remove(m)

    def remove_all(self) -> None:
        self.markers.clear()


def _make_inst(tour_active: bool) -> MapShumateMixin:
    inst = object.__new__(MapShumateMixin)
    inst._wp_layer = _FakeLayer()
    inst._wp_start_marker = None
    inst._path_layer = None
    inst._shumate_map = None
    inst._tour_active = tour_active
    inst._shumate_set_path = lambda *_a: None
    inst._shumate_set_route_muted = lambda *_a: None
    inst._make_wp_marker = lambda lat, lon, role: (role, lat, lon)
    return inst


POINTS = [(50.0, 7.0), (50.1, 7.1), (50.2, 7.2)]


def test_preview_keeps_start_arrow() -> None:
    inst = _make_inst(tour_active=False)
    inst._shumate_show_route(POINTS, [])
    assert [m[0] for m in inst._wp_layer.markers] == ["start", "via", "end"]
    assert inst._wp_start_marker == ("start", 50.0, 7.0)


def test_drop_start_marker_on_tour_begin() -> None:
    inst = _make_inst(tour_active=False)
    inst._shumate_show_route(POINTS, [])
    inst._tour_active = True
    inst._shumate_drop_start_marker()
    assert [m[0] for m in inst._wp_layer.markers] == ["via", "end"]
    assert inst._wp_start_marker is None
    inst._shumate_drop_start_marker()  # idempotent
    assert len(inst._wp_layer.markers) == 2


def test_active_tour_route_has_no_start_arrow() -> None:
    inst = _make_inst(tour_active=True)
    inst._shumate_show_route(POINTS, [])
    assert [m[0] for m in inst._wp_layer.markers] == ["via", "end"]
    assert inst._wp_start_marker is None
