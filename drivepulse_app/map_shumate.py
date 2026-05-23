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
    from gi.repository import Shumate
    SHUMATE_OK = True
except (ValueError, ImportError):
    Shumate = None


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
        # SimpleMap renders its own square zoom buttons in the top-right
        # corner — hide them so only our circular OSD zoom controls remain.
        try:
            self._shumate_map.set_show_zoom_buttons(False)
        except (AttributeError, TypeError):
            try:
                self._shumate_map.set_property("show-zoom-buttons", False)
            except Exception:
                pass
        # Park the scale ruler immediately to the left of the FAB's bottom
        # icon (TTS / speaker) and at the same vertical height.  FAB lives at
        # halign=END with margin_end=12 and ~36 px circular buttons; sit the
        # scale on the same baseline (margin_bottom=36) with enough end-margin
        # to clear the FAB column.
        scale = self._shumate_map.get_scale()
        if scale is not None:
            scale.set_halign(Gtk.Align.END)
            scale.set_valign(Gtk.Align.END)
            scale.set_margin_end(60)
            scale.set_margin_bottom(31)
        # Initial scale unit follows the user's settings choice.
        self._shumate_apply_scale_unit(getattr(self, "units", "metric"))

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

        # Trip-replay scrubber position (lat, lon) or None. Drawn on the same
        # Cairo overlay as the replay polyline so it always sits on top of the
        # coloured track — no separate Shumate MarkerLayer needed.
        self._replay_marker_pos: tuple[float, float] | None = None

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
            drawing.set_size_request(44, 44)
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
        cr.arc(cx, cy, 18, 0, 2 * math.pi)
        cr.fill()

        cr.save()
        cr.translate(cx, cy)
        cr.rotate(math.radians(getattr(self, "_gps_heading", 0.0)))
        cr.move_to(0, -18)
        cr.line_to(12, 14)
        cr.line_to(0, 8)
        cr.line_to(-12, 14)
        cr.close_path()
        cr.set_source_rgb(1.0, 1.0, 1.0)
        cr.set_line_width(5.0)
        cr.set_line_join(1)
        cr.stroke_preserve()
        cr.set_source_rgb(0.12, 0.53, 0.90)
        cr.fill()

        cr.move_to(0, -12)
        cr.line_to(6, 8)
        cr.line_to(0, 5)
        cr.line_to(-6, 8)
        cr.close_path()
        cr.set_source_rgb(0.26, 0.65, 0.96)
        cr.fill()
        cr.restore()

    def _shumate_set_guide(self, coords: list[list[float]]) -> None:
        self._shumate_set_path(self._guide_path_layer, coords)

    def _shumate_max_zoom(self) -> float:
        """Maximum zoom level supported by the current Shumate tile source."""
        if self._shumate_map is None:
            return 19.0
        source = self._shumate_map.get_map_source()
        if source is not None and hasattr(source, "get_max_zoom_level"):
            try:
                return float(source.get_max_zoom_level())
            except Exception:
                pass
        return 19.0

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

    def _shumate_show_traffic(self, parsed: list[dict]) -> None:
        if self._traffic_layer is None:
            return
        self._traffic_layer.remove_all()
        for item in parsed:
            self._traffic_layer.add_marker(self._make_traffic_marker(item))

    def _make_traffic_marker(self, item: dict) -> Any:
        kind = item.get("kind", "incidents")
        if kind == "roadworks":
            fill = (0.95, 0.60, 0.0, 1.0)
            border = (0.70, 0.40, 0.0, 1.0)
        else:
            fill = (0.90, 0.20, 0.20, 1.0)
            border = (0.60, 0.10, 0.10, 1.0)
        da = Gtk.DrawingArea()
        da.set_size_request(14, 14)
        da.set_draw_func(self._draw_dot, (fill, border))
        road = item.get("road") or ""
        title = item.get("title") or ""
        da.set_tooltip_text(f"{road}: {title}" if road else title)
        click = Gtk.GestureClick.new()
        click.set_button(1)
        click.connect("released", lambda g, n, x, y, it=item: self._show_traffic_popover(g, it))
        da.add_controller(click)
        marker = Shumate.Marker.new()
        marker.set_child(da)
        marker.set_location(item.get("lat", 0.0), item.get("lon", 0.0))
        return marker

    def _show_traffic_popover(self, gesture: Any, item: dict) -> None:
        widget = gesture.get_widget()
        if widget is None:
            return
        popover = Gtk.Popover()
        popover.set_has_arrow(True)
        popover.set_autohide(True)
        popover.set_parent(widget)
        popover.set_child(self._build_traffic_detail_widget(item))
        popover.popup()

    def _shumate_set_poi_visible(self, visible: bool) -> None:
        if self._poi_layer is None:
            return
        self._poi_layer.set_visible(visible)

    def _build_shumate_replay_overlay(self, overlay: Gtk.Overlay) -> None:
        """Attach a single Cairo DrawingArea on top of the Shumate map for the
        replay polyline.

        One overlay widget paints all segments in a single draw_func — matches
        the Fahrtenbuch's performance (one Cairo pass, full colour granularity)
        and avoids the per-bin ``Shumate.PathLayer`` explosion that made
        pan/zoom unusable.
        """
        area = Gtk.DrawingArea()
        area.set_hexpand(True)
        area.set_vexpand(True)
        area.set_can_target(False)  # don't intercept clicks on the map below
        area.set_visible(False)
        area.set_draw_func(self._draw_shumate_replay_overlay)
        self._replay_track_area = area
        self._replay_track_points: list[tuple[float, float, float | None]] = []

        viewport = self._shumate_map.get_viewport()
        for prop in ("latitude", "longitude", "zoom-level"):
            viewport.connect(f"notify::{prop}", lambda *_a: area.queue_draw())

        # Insert beneath the other overlay controls (FAB, zoom, etc.) so
        # buttons keep their hit area on top of the painted track.
        overlay.add_overlay(area)

    def _draw_shumate_replay_overlay(
        self, area: Gtk.DrawingArea, cr: Any, _w: int, _h: int
    ) -> None:
        from .cars_trip_visuals import speed_to_rgb

        viewport = self._shumate_map.get_viewport()
        points = getattr(self, "_replay_track_points", None) or []

        if len(points) >= 2:
            speeds = [s for _, _, s in points if s is not None]
            vmax = max(speeds) if speeds else 0.0

            proj: list[tuple[float, float] | None] = []
            for lat, lon, _spd in points:
                try:
                    x, y = viewport.location_to_widget_coords(area, lat, lon)
                    proj.append((x, y))
                except Exception:
                    proj.append(None)

            cr.set_line_cap(1)   # ROUND
            cr.set_line_join(1)  # ROUND

            # Black case underneath for contrast against light/dark tiles.
            cr.set_source_rgba(0.0, 0.0, 0.0, 0.35)
            cr.set_line_width(7.0)
            prev = proj[0]
            for i in range(1, len(proj)):
                cur = proj[i]
                if prev is not None and cur is not None:
                    cr.move_to(*prev)
                    cr.line_to(*cur)
                prev = cur
            cr.stroke()

            # Coloured segments — one stroke per segment, all in a single pass.
            cr.set_line_width(4.5)
            prev = proj[0]
            for i in range(1, len(proj)):
                cur = proj[i]
                if prev is not None and cur is not None:
                    _lat, _lon, spd = points[i]
                    r, g, b = speed_to_rgb(spd, vmax)
                    cr.set_source_rgba(r, g, b, 0.95)
                    cr.move_to(*prev)
                    cr.line_to(*cur)
                    cr.stroke()
                prev = cur

        # Scrubber marker — drawn last so it always sits on top of the track.
        marker_pos = getattr(self, "_replay_marker_pos", None)
        if marker_pos is not None:
            try:
                mx, my = viewport.location_to_widget_coords(
                    area, marker_pos[0], marker_pos[1]
                )
            except Exception:
                return
            # White halo for contrast against any colour.
            cr.set_source_rgba(1.0, 1.0, 1.0, 1.0)
            cr.arc(mx, my, 8.5, 0, 2 * math.pi)
            cr.set_line_width(2.5)
            cr.stroke()
            # Blue fill — matches the webkit marker.
            cr.set_source_rgba(0.12, 0.53, 0.90, 1.0)
            cr.arc(mx, my, 7.0, 0, 2 * math.pi)
            cr.fill()

    def _shumate_show_colored_track(
        self, latlon_speed: list[tuple[float, float, float | None]]
    ) -> None:
        """Render a speed-coloured polyline for trip replay via Cairo overlay.

        Stores the points + adjusts the viewport bbox; the actual drawing
        happens in :meth:`_draw_shumate_replay_overlay`, which a single
        Gtk.DrawingArea repaints on every viewport change.
        """
        if not latlon_speed:
            self._shumate_clear_colored_track()
            return

        self._replay_track_points = list(latlon_speed)
        area = getattr(self, "_replay_track_area", None)
        if area is not None:
            area.set_visible(True)
            area.queue_draw()

        viewport = self._shumate_map.get_viewport()
        lats = [p[0] for p in latlon_speed]
        lons = [p[1] for p in latlon_speed]
        clat = (min(lats) + max(lats)) / 2.0
        clon = (min(lons) + max(lons)) / 2.0
        alloc = self._shumate_map.get_allocation()
        px_w = max(alloc.width, 400)
        px_h = max(alloc.height, 600)
        zoom = zoom_for_bbox(min(lats), min(lons), max(lats), max(lons), px_w, px_h)
        self._setting_pos = True
        viewport.set_zoom_level(zoom)
        viewport.set_location(clat, clon)
        self._setting_pos = False

    def _shumate_clear_colored_track(self) -> None:
        self._replay_track_points = []
        area = getattr(self, "_replay_track_area", None)
        if area is not None:
            area.set_visible(False)
            area.queue_draw()

    def _shumate_apply_scale_unit(self, units: str) -> None:
        """Mirror the user's units setting on the shumate scale ruler."""
        smap = getattr(self, "_shumate_map", None)
        if smap is None or Shumate is None:
            return
        scale = smap.get_scale()
        if scale is None:
            return
        if units == "imperial":
            unit = getattr(Shumate, "Unit", None)
            target = getattr(unit, "IMPERIAL", None) if unit is not None else None
        else:
            unit = getattr(Shumate, "Unit", None)
            target = getattr(unit, "METRIC", None) if unit is not None else None
        if target is not None:
            try:
                scale.set_unit(target)
            except Exception:
                # Older Shumate APIs may not support set_unit — silently skip.
                pass

    def _shumate_set_scale_offset(self, offset_px: int) -> None:
        """Push the bottom-left scale ruler to the right of the replay chart.

        When the replay chart overlay covers the bottom-left corner the
        scale ruler is hidden behind it — pointless. Bumping its
        ``margin_start`` parks it just to the right of the chart instead.
        """
        smap = getattr(self, "_shumate_map", None)
        if smap is None:
            return
        scale = smap.get_scale()
        if scale is not None:
            scale.set_margin_start(offset_px)

    def _shumate_set_scale_visible(self, visible: bool) -> None:
        smap = getattr(self, "_shumate_map", None)
        if smap is None:
            return
        scale = smap.get_scale()
        if scale is not None:
            scale.set_visible(visible)

    def _shumate_set_replay_marker(self, lat: float, lon: float) -> None:
        self._replay_marker_pos = (lat, lon)
        area = getattr(self, "_replay_track_area", None)
        if area is not None:
            area.queue_draw()

    def _shumate_clear_replay_marker(self) -> None:
        self._replay_marker_pos = None
        area = getattr(self, "_replay_track_area", None)
        if area is not None:
            area.queue_draw()

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
