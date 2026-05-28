"""Unit tests for the OSM tile-coordinate helpers in cars/_osm_map.

These four functions translate between geographic coordinates and the
slippy-tile (zoom/x/y) addressing scheme that OSM tile servers use.
Getting them wrong shifts the trip-detail mini-map off the actual route
— a regression that's easy to miss visually and hard to debug after the
fact, hence the explicit roundtrip + boundary tests.
"""
from __future__ import annotations

import math

import pytest

from drivepulse_app.cars._osm_map import (
    _disk_tile_path,
    _lat_to_ty,
    _lon_to_tx,
    _pick_zoom,
    _tx_to_lon,
    _ty_to_lat,
)

# ── _lon_to_tx ────────────────────────────────────────────────────────────────


def test_lon_to_tx_zoom_0_is_single_tile():
    # At zoom 0 the whole world fits in tile 0/0/0 — every lon must land there.
    for lon in (-180.0, -90.0, 0.0, 90.0, 179.9):
        assert _lon_to_tx(lon, 0) == 0


def test_lon_to_tx_zoom_1_splits_at_meridian():
    # At zoom 1 the world is two columns; lon < 0 → tx 0, lon ≥ 0 → tx 1.
    assert _lon_to_tx(-180.0, 1) == 0
    assert _lon_to_tx(-0.001, 1) == 0
    assert _lon_to_tx(0.0, 1) == 1
    assert _lon_to_tx(179.0, 1) == 1


def test_lon_to_tx_matches_known_osm_address():
    # OSM canonical example: lon=7.0  zoom=12  → tx=2127 (Cologne area).
    assert _lon_to_tx(7.0, 12) == 2127


# ── _lat_to_ty ────────────────────────────────────────────────────────────────


def test_lat_to_ty_equator_is_middle_row():
    # At every non-zero zoom the equator sits one row above the southern half.
    for zoom in (1, 5, 12, 17):
        ty = _lat_to_ty(0.0, zoom)
        assert ty == (1 << zoom) // 2


def test_lat_to_ty_north_of_equator_smaller_than_southern_mirror():
    # The y-axis grows southward in slippy-tile coords: at the same zoom
    # level the northern hemisphere always lands on a smaller ty than its
    # equator-mirrored counterpart.
    assert _lat_to_ty(50.0, 12) < _lat_to_ty(0.0, 12)
    assert _lat_to_ty(-50.0, 12) > _lat_to_ty(0.0, 12)


# ── Inverse round-trip ────────────────────────────────────────────────────────


def test_tile_index_roundtrip_preserves_coordinates():
    # Snap-to-tile-center round-trips: forward then inverse must land within
    # the same tile's longitude/latitude range. We pick a real-world point
    # (Köln Hbf) at zoom 14 and check that the lat/lon of the tile's top-
    # left corner is within one tile width/height of the original.
    lat, lon = 50.9429, 6.9583  # Köln Hbf
    zoom = 14
    tx, ty = _lon_to_tx(lon, zoom), _lat_to_ty(lat, zoom)

    lon_at_tile = _tx_to_lon(tx, zoom)
    lat_at_tile = _ty_to_lat(ty, zoom)

    tile_lon_width = 360.0 / (1 << zoom)
    # Tiles get narrower in latitude as we move away from the equator, but the
    # immediate tile-to-tile lat delta at zoom 14 in Central Europe is ~0.011°.
    assert 0.0 <= (lon - lon_at_tile) < tile_lon_width
    # Top-left of the tile is the *northern* edge → at_tile_lat ≥ lat
    assert lat_at_tile >= lat
    assert lat_at_tile - lat < 0.02


def test_ty_to_lat_zoom_0_returns_northern_world_edge():
    # At zoom 0 ty=0 starts at the top of the projection (~85.0511°N) — that's
    # the latitude limit of Web-Mercator before the math diverges.
    assert _ty_to_lat(0, 0) == pytest.approx(
        math.degrees(math.atan(math.sinh(math.pi))), abs=1e-6,
    )


# ── _pick_zoom ────────────────────────────────────────────────────────────────


def test_pick_zoom_fits_small_bbox_at_high_zoom():
    # A 100 m × 100 m bbox should land at the upper zoom bound (16).
    # 100 m at 50° lat ≈ 0.001° lon / 0.0009° lat.
    z = _pick_zoom(50.000, 50.0009, 7.000, 7.0014)
    assert z == 16


def test_pick_zoom_drops_to_minimum_for_huge_bbox():
    # A continent-spanning bbox can't fit in 4×4 tiles at any zoom in the
    # search range — the function falls back to its zoom floor (10).
    z = _pick_zoom(-50.0, 60.0, -120.0, 120.0)
    assert z == 10


def test_pick_zoom_returns_int_in_documented_range():
    z = _pick_zoom(50.9, 50.95, 6.95, 7.0)  # ~5 km bbox
    assert isinstance(z, int)
    assert 10 <= z <= 16


# ── _disk_tile_path ───────────────────────────────────────────────────────────


def test_disk_tile_path_uses_zoom_x_y_directory_layout():
    # OSM slippy-tile convention: {cache}/{zoom}/{x}/{y}.png. Mismatching this
    # would silently mis-key the on-disk cache so re-opens go back to network.
    p = _disk_tile_path(zoom=12, tx=2127, ty=1378)
    parts = p.parts
    assert parts[-3:] == ("12", "2127", "1378.png")
