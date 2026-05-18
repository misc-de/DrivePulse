"""Native Shumate map backend helpers for the map page."""
from __future__ import annotations

import math
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from .diagnostics import get_logger
from .map_services import TILE_URLS, zoom_for_bbox

log = get_logger(__name__)

SHUMATE_OK = False
try:
    gi.require_version("Shumate", "1.0")
    from gi.repository import Shumate  # type: ignore[attr-defined]
    SHUMATE_OK = True
except (ValueError, ImportError):
    Shumate = None  # type: ignore[assignment]


class MapShumateMixin:
    """Shumate-specific setup, layers and marker drawing."""

    def _setup_shumate(self) -> Gtk.Widget:
        self._shumate_map = Shumate.SimpleMap()
        self._shumate_map.set_hexpand(True)
        self._shumate_map.set_vexpand(True)

        viewport = self._shumate_map.get_viewport()
        viewport.set_zoom_level(13.0)
        viewport.set_location(48.137, 11.576)

        registry = Shumate.MapSourceRegistry.new_with_defaults()
        osm_id = getattr(Shumate, "MAP_SOURCE_OSM_MAPNIK", "osm-mapnik")
        osm_source = registry.get_by_id(osm_id)
        for key in TILE_URLS:
            self._sources[key] = osm_source

        if hasattr(Shumate, "RasterRenderer") and hasattr(Shumate, "TileDownloader"):
            for key, url in (("satellite", TILE_URLS["satellite"]), ("dark", TILE_URLS["dark"])):
                try:
                    self._sources[key] = Shumate.RasterRenderer.new(
                        Shumate.TileDownloader.new(url)
                    )
                except Exception:
                    log.warning("Could not create tile source for %s - using OSM fallback", key)

        self._shumate_map.set_map_source(self._sources["map"])
        self._shumate_map.get_scale().set_margin_bottom(24)

        self._inner_map = (
            self._shumate_map.get_map()
            if hasattr(self._shumate_map, "get_map")
            else self._shumate_map
        )
        inner = self._inner_map

        self._guide_path_layer = Shumate.PathLayer.new(viewport)
        guide_color = Gdk.RGBA()
        guide_color.red, guide_color.green, guide_color.blue, guide_color.alpha = (
            0.96, 0.65, 0.14, 0.85
        )
        self._guide_path_layer.set_stroke_color(guide_color)
        self._guide_path_layer.set_stroke_width(4.0)
        inner.add_layer(self._guide_path_layer)

        self._path_layer = Shumate.PathLayer.new(viewport)
        route_color = Gdk.RGBA()
        route_color.red, route_color.green, route_color.blue, route_color.alpha = (
            0.20, 0.60, 0.86, 0.85
        )
        self._path_layer.set_stroke_color(route_color)
        self._path_layer.set_stroke_width(5.0)
        inner.add_layer(self._path_layer)

        self._wp_layer = Shumate.MarkerLayer.new(viewport)
        inner.add_layer(self._wp_layer)

        self._marker_layer = Shumate.MarkerLayer.new(viewport)
        inner.add_layer(self._marker_layer)

        self._traffic_layer = Shumate.MarkerLayer.new(viewport)
        self._traffic_layer.set_visible(False)
        inner.add_layer(self._traffic_layer)

        self._poi_layer = Shumate.MarkerLayer.new(viewport)
        self._poi_layer.set_visible(False)
        inner.add_layer(self._poi_layer)

        viewport.connect("notify::latitude", self._on_viewport_moved)
        return self._shumate_map

    def _update_shumate_gps(self, lat: float, lon: float) -> None:
        if self._car_marker is None:
            drawing = Gtk.DrawingArea()
            drawing.set_size_request(40, 40)
            drawing.set_draw_func(self._draw_car, None)
            self._car_marker = Shumate.Marker.new()
            self._car_marker.set_child(drawing)
            self._car_marker.set_location(lat, lon)
            self._marker_layer.add_marker(self._car_marker)
        else:
            self._car_marker.set_location(lat, lon)
            child = self._car_marker.get_child()
            if child is not None:
                child.queue_draw()

    def _draw_car(self, _da: Any, cr: Any, width: int, height: int, _data: Any) -> None:
        cx, cy = width / 2.0, height / 2.0
        cr.set_source_rgba(0.16, 0.50, 0.73, 0.20)
        cr.arc(cx, cy, 16, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgb(1.0, 1.0, 1.0)
        cr.arc(cx, cy, 9, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgb(0.16, 0.50, 0.73)
        cr.arc(cx, cy, 7, 0, 2 * math.pi)
        cr.fill()

    def _shumate_set_guide(self, coords: list[list[float]]) -> None:
        self._shumate_set_path(self._guide_path_layer, coords)

    def _shumate_set_path(self, layer: Any, coords: list[list[float]]) -> None:
        if layer is None:
            return
        layer.remove_all()
        for lon, lat in coords:
            layer.add_node(Shumate.Coordinate.new_full(lat, lon))

    def _shumate_clear_route_layers(self) -> None:
        for layer in (self._guide_path_layer, self._path_layer, self._wp_layer):
            if layer is not None:
                layer.remove_all()

    def _shumate_show_route(
        self,
        all_points: list[tuple[float, float]],
        coords: list[list[float]],
    ) -> None:
        self._shumate_set_path(self._path_layer, coords)
        if self._wp_layer is not None:
            self._wp_layer.remove_all()
            for i, pt in enumerate(all_points):
                role = "start" if i == 0 else ("end" if i == len(all_points) - 1 else "via")
                self._wp_layer.add_marker(self._make_wp_marker(pt[0], pt[1], role))
        if coords and self._shumate_map is not None:
            lats = [c[1] for c in coords]
            lons = [c[0] for c in coords]
            clat = (min(lats) + max(lats)) / 2.0
            clon = (min(lons) + max(lons)) / 2.0
            alloc = self._shumate_map.get_allocation()
            px_w = max(alloc.width, 400)
            px_h = max(alloc.height, 600)
            zoom = zoom_for_bbox(min(lats), min(lons), max(lats), max(lons), px_w, px_h)
            viewport = self._shumate_map.get_viewport()
            self._setting_pos = True
            viewport.set_zoom_level(zoom)
            viewport.set_location(clat, clon)
            self._setting_pos = False

    def _shumate_set_traffic_visible(self, visible: bool) -> None:
        if self._traffic_layer is not None:
            self._traffic_layer.set_visible(visible)

    def _shumate_show_traffic(self, parsed: list[tuple[float, float, str, str]]) -> None:
        if self._traffic_layer is None:
            return
        self._traffic_layer.remove_all()
        for lat, lon, kind, tooltip in parsed:
            self._traffic_layer.add_marker(self._make_traffic_marker(kind, tooltip, lat, lon))

    def _make_traffic_marker(self, kind: str, tooltip: str, lat: float, lon: float) -> Any:
        if kind == "roadworks":
            fill = (0.95, 0.60, 0.0, 1.0)
            border = (0.70, 0.40, 0.0, 1.0)
        else:
            fill = (0.90, 0.20, 0.20, 1.0)
            border = (0.60, 0.10, 0.10, 1.0)
        da = Gtk.DrawingArea()
        da.set_size_request(14, 14)
        da.set_draw_func(self._draw_dot, (fill, border))
        da.set_tooltip_text(tooltip)
        marker = Shumate.Marker.new()
        marker.set_child(da)
        marker.set_location(lat, lon)
        return marker

    def _shumate_set_poi_visible(self, visible: bool) -> None:
        if self._poi_layer is None:
            return
        self._poi_layer.set_visible(visible)

    def _make_wp_marker(self, lat: float, lon: float, role: str) -> Any:
        if role == "start":
            fill, border = (0.18, 0.80, 0.44, 1.0), (0.10, 0.54, 0.27, 1.0)
        elif role == "end":
            fill, border = (0.91, 0.30, 0.24, 1.0), (0.60, 0.15, 0.10, 1.0)
        else:
            fill, border = (0.95, 0.65, 0.10, 1.0), (0.70, 0.45, 0.00, 1.0)
        da = Gtk.DrawingArea()
        da.set_size_request(14, 14)
        da.set_draw_func(self._draw_dot, (fill, border))
        marker = Shumate.Marker.new()
        marker.set_child(da)
        marker.set_location(lat, lon)
        return marker

    def _draw_dot(self, _da: Any, cr: Any, w: int, h: int, data: tuple) -> None:
        fill, border = data
        cr.set_source_rgba(*fill)
        cr.arc(w / 2, h / 2, w / 2 - 1.5, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(*border)
        cr.set_line_width(2.5)
        cr.arc(w / 2, h / 2, w / 2 - 1.5, 0, 2 * math.pi)
        cr.stroke()
