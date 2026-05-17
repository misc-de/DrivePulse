"""Map page — OpenStreetMap navigation with GPS tracking and routing."""
from __future__ import annotations

import json
import math
import threading
import urllib.parse
import urllib.request
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

_SHUMATE_OK = False
try:
    gi.require_version("Shumate", "1.0")
    from gi.repository import Shumate  # type: ignore[attr-defined]
    _SHUMATE_OK = True
except (ValueError, ImportError):
    Shumate = None  # type: ignore[assignment]

from .common import SOURCE_LANGUAGE, _normalize_language, _translate
from .diagnostics import get_logger

log = get_logger(__name__)

# ── Map type cycle ────────────────────────────────────────────────────────────

_MAP_TYPES = ["map", "satellite", "dark"]
_MAP_LABEL_KEYS = {
    "map":       "map.type.map",
    "satellite": "map.type.satellite",
    "dark":      "map.type.dark",
}
_MAP_ICONS = {
    "map":       "map-symbolic",
    "satellite": "image-x-generic-symbolic",
    "dark":      "night-light-symbolic",
}
_TILE_URLS = {
    "map": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "satellite": (
        "https://server.arcgisonline.com/ArcGIS/rest/services"
        "/World_Imagery/MapServer/tile/{z}/{y}/{x}"
    ),
    "dark": "https://cartodb-basemaps-a.global.ssl.fastly.net/dark_all/{z}/{x}/{y}.png",
}

# ── Routing modes ─────────────────────────────────────────────────────────────

_ROUTING_MODES = ["car", "bicycle", "motorcycle"]
_OSRM_PROFILE = {"car": "driving", "bicycle": "cycling", "motorcycle": "driving"}

# ── Network helpers (background threads) ──────────────────────────────────────

_UA = {"User-Agent": "DrivePulse/1.0"}


def _http_get(url: str) -> Any:
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as exc:
        log.warning("HTTP GET failed %s — %s", url, exc)
        return None


def _geocode(query: str) -> tuple[float, float] | None:
    url = (
        "https://nominatim.openstreetmap.org/search"
        f"?q={urllib.parse.quote(query)}&format=json&limit=1"
    )
    data = _http_get(url)
    if data:
        return float(data[0]["lat"]), float(data[0]["lon"])
    return None


def _osrm_route(
    start: tuple[float, float],
    end: tuple[float, float],
    mode: str,
) -> tuple[list[list[float]], float] | None:
    profile = _OSRM_PROFILE.get(mode, "driving")
    coords = f"{start[1]},{start[0]};{end[1]},{end[0]}"
    url = (
        f"https://router.project-osrm.org/route/v1/{profile}/{coords}"
        "?overview=full&geometries=geojson"
    )
    data = _http_get(url)
    if data and data.get("code") == "Ok" and data.get("routes"):
        route = data["routes"][0]
        return route["geometry"]["coordinates"], float(route.get("duration", 0))
    return None


def _format_duration(seconds: float) -> str:
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    if h > 0:
        return f"{h}h {m}min"
    return f"{m}min"


# ── Cairo helpers ─────────────────────────────────────────────────────────────

def _rounded_rect(cr: Any, x: float, y: float, w: float, h: float, r: float) -> None:
    cr.new_sub_path()
    cr.arc(x + w - r, y + r,     r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0,            math.pi / 2)
    cr.arc(x + r,     y + h - r, r, math.pi / 2,  math.pi)
    cr.arc(x + r,     y + r,     r, math.pi,       3 * math.pi / 2)
    cr.close_path()


def _zoom_for_bbox(
    lat1: float, lon1: float,
    lat2: float, lon2: float,
    px_w: int = 400, px_h: int = 600,
) -> float:
    TILE = 256
    ZOOM_MAX = 17

    def lat_rad(lat: float) -> float:
        s = math.sin(math.radians(lat))
        return math.log((1 + s) / (1 - s)) / 2

    dlat = max(abs(lat_rad(lat2) - lat_rad(lat1)) / math.pi, 1e-9)
    dlon = max(abs(lon2 - lon1) / 360.0, 1e-9)
    z_lat = math.floor(math.log2(px_h / TILE / dlat))
    z_lon = math.floor(math.log2(px_w / TILE / dlon))
    return float(max(1, min(ZOOM_MAX, z_lat, z_lon) - 1))  # -1 padding


# ── MapPage widget ────────────────────────────────────────────────────────────

class MapPage(Gtk.Box):
    """OpenStreetMap navigation page with GPS tracking and routing."""
    __gtype_name__ = "MapPage"

    def __init__(self, language: str = SOURCE_LANGUAGE) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.language = _normalize_language(language)

        self._gps_lat: float | None = None
        self._gps_lon: float | None = None
        self._gps_heading: float = 0.0
        self._follow_gps: bool = True
        self._map_type_idx: int = 0
        self._routing_mode: str = "car"
        self._start_coord: tuple[float, float] | None = None
        self._end_coord: tuple[float, float] | None = None

        # Shumate state
        self._shumate_map: Any = None
        self._inner_map: Any = None   # underlying ShumateMap inside SimpleMap
        self._car_marker: Any = None
        self._marker_layer: Any = None
        self._path_layer: Any = None
        self._wp_layer: Any = None
        self._sources: dict[str, Any] = {}
        self._setting_pos: bool = False  # suppress follow-disable on programmatic moves

        # FAB buttons (None when Shumate unavailable)
        self._follow_btn: Gtk.ToggleButton | None = None
        self._center_btn: Gtk.Button | None = None
        self._layer_btn: Gtk.Button | None = None

        self._build_search_bar()
        self._build_map()

        # Prevent search entries from auto-grabbing focus when the tab becomes visible
        self.connect("map", self._on_mapped)

    def _on_mapped(self, _widget: Any) -> None:
        GLib.idle_add(self._drop_focus)

    def _drop_focus(self) -> bool:
        root = self.get_root()
        if root is not None:
            root.set_focus(None)
        return False

    # ── Search / route bar ────────────────────────────────────────────────────

    def _build_search_bar(self) -> None:
        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bar.set_margin_top(8)
        bar.set_margin_bottom(4)
        bar.set_margin_start(8)
        bar.set_margin_end(8)

        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        self._start_entry = Gtk.Entry()
        self._start_entry.set_placeholder_text(_translate(self.language, "map.search.start"))
        self._start_entry.set_hexpand(True)
        self._start_entry.connect("activate", self._on_route_clicked)

        arrow = Gtk.Label(label="→")
        arrow.add_css_class("dim-label")

        self._end_entry = Gtk.Entry()
        self._end_entry.set_placeholder_text(_translate(self.language, "map.search.end"))
        self._end_entry.set_hexpand(True)
        self._end_entry.connect("activate", self._on_route_clicked)

        self._route_btn = Gtk.Button(icon_name="map-symbolic")
        self._route_btn.add_css_class("suggested-action")
        self._route_btn.set_tooltip_text(_translate(self.language, "map.route"))
        self._route_btn.connect("clicked", self._on_route_clicked)

        self._clear_btn = Gtk.Button(icon_name="edit-clear-symbolic")
        self._clear_btn.set_tooltip_text(_translate(self.language, "map.clear"))
        self._clear_btn.set_visible(False)
        self._clear_btn.connect("clicked", self._on_clear_clicked)

        for w in (self._start_entry, arrow, self._end_entry, self._route_btn, self._clear_btn):
            row1.append(w)

        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

        self._status_lbl = Gtk.Label(label="")
        self._status_lbl.add_css_class("dim-label")
        self._status_lbl.set_hexpand(True)
        self._status_lbl.set_halign(Gtk.Align.END)
        row2.append(self._status_lbl)

        bar.append(row1)
        bar.append(row2)
        self.append(bar)

    # ── Shumate map ───────────────────────────────────────────────────────────

    def _build_map(self) -> None:
        if not _SHUMATE_OK:
            placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            placeholder.set_hexpand(True)
            placeholder.set_vexpand(True)
            placeholder.set_halign(Gtk.Align.CENTER)
            placeholder.set_valign(Gtk.Align.CENTER)
            icon = Gtk.Image.new_from_icon_name("map-symbolic")
            icon.set_pixel_size(64)
            icon.add_css_class("dim-label")
            label = Gtk.Label(
                label="Map not available.\nInstall gir1.2-shumate-1.0 to enable."
            )
            label.set_justify(Gtk.Justification.CENTER)
            label.add_css_class("dim-label")
            placeholder.append(icon)
            placeholder.append(label)
            self.append(placeholder)
            return

        # SimpleMap handles tile loading, caching and network setup automatically —
        # unlike bare Shumate.Map which needs explicit ShumateFileTileSource wiring.
        self._shumate_map = Shumate.SimpleMap()
        self._shumate_map.set_hexpand(True)
        self._shumate_map.set_vexpand(True)

        viewport = self._shumate_map.get_viewport()
        viewport.set_zoom_level(13.0)
        viewport.set_location(48.137, 11.576)

        # Street map: always use the built-in registry source — same one GNOME Maps uses.
        registry = Shumate.MapSourceRegistry.new_with_defaults()
        osm_id = getattr(Shumate, "MAP_SOURCE_OSM_MAPNIK", "osm-mapnik")
        osm_source = registry.get_by_id(osm_id)
        for key in _TILE_URLS:
            self._sources[key] = osm_source  # default fallback for all layers

        # Satellite + dark: RasterRenderer/TileDownloader available on Shumate >= 1.1.
        if hasattr(Shumate, "RasterRenderer") and hasattr(Shumate, "TileDownloader"):
            for key, url in (("satellite", _TILE_URLS["satellite"]), ("dark", _TILE_URLS["dark"])):
                try:
                    self._sources[key] = Shumate.RasterRenderer.new(
                        Shumate.TileDownloader.new(url)
                    )
                except Exception:
                    log.warning("Could not create tile source for %s — using OSM fallback", key)

        self._shumate_map.set_map_source(self._sources["map"])

        # Layers are added to the underlying ShumateMap inside SimpleMap.
        self._inner_map = self._shumate_map.get_map() if hasattr(self._shumate_map, "get_map") else self._shumate_map
        _inner = self._inner_map

        self._path_layer = Shumate.PathLayer.new(viewport)
        route_color = Gdk.RGBA()
        route_color.red, route_color.green, route_color.blue, route_color.alpha = (
            0.20, 0.60, 0.86, 0.85
        )
        self._path_layer.set_stroke_color(route_color)
        self._path_layer.set_stroke_width(5.0)
        _inner.add_layer(self._path_layer)

        self._wp_layer = Shumate.MarkerLayer.new(viewport)
        _inner.add_layer(self._wp_layer)

        self._marker_layer = Shumate.MarkerLayer.new(viewport)
        _inner.add_layer(self._marker_layer)

        # Detect manual pan → disable follow
        viewport.connect("notify::latitude", self._on_viewport_moved)

        # Floating action buttons
        fab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        fab.set_halign(Gtk.Align.END)
        fab.set_valign(Gtk.Align.END)
        fab.set_margin_end(12)
        fab.set_margin_bottom(12)

        self._follow_btn = Gtk.ToggleButton(icon_name="find-location-symbolic")
        self._follow_btn.add_css_class("circular")
        self._follow_btn.add_css_class("osd")
        self._follow_btn.set_active(True)
        self._follow_btn.set_tooltip_text(_translate(self.language, "map.follow"))
        self._follow_btn.connect("toggled", self._on_follow_toggled)

        self._center_btn = Gtk.Button(icon_name="find-location-symbolic")
        self._center_btn.add_css_class("circular")
        self._center_btn.add_css_class("osd")
        self._center_btn.set_tooltip_text(_translate(self.language, "map.center"))
        self._center_btn.connect("clicked", self._on_center_clicked)

        self._layer_btn = Gtk.Button(icon_name="map-symbolic")
        self._layer_btn.add_css_class("circular")
        self._layer_btn.add_css_class("osd")
        self._layer_btn.set_tooltip_text(_translate(self.language, _MAP_LABEL_KEYS["map"]))
        self._layer_btn.connect("clicked", self._on_layer_clicked)

        fab.append(self._follow_btn)
        fab.append(self._center_btn)
        fab.append(self._layer_btn)

        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        overlay.set_child(self._shumate_map)
        overlay.add_overlay(fab)
        self.append(overlay)

    # ── GPS position updates ──────────────────────────────────────────────────

    def update_gps(
        self,
        lat: float | None,
        lon: float | None,
        heading: float | None,
    ) -> None:
        if not _SHUMATE_OK or self._shumate_map is None:
            return
        if lat is None or lon is None:
            return
        self._gps_lat = lat
        self._gps_lon = lon
        self._gps_heading = heading or 0.0

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

        if self._follow_gps:
            self._goto(lat, lon)

    def _goto(self, lat: float, lon: float) -> None:
        self._setting_pos = True
        viewport = self._shumate_map.get_viewport()
        viewport.set_location(lat, lon)
        self._setting_pos = False

    # ── Car Cairo drawing ─────────────────────────────────────────────────────

    def _draw_car(self, _da: Any, cr: Any, width: int, height: int, _data: Any) -> None:
        cx, cy = width / 2.0, height / 2.0
        cr.save()
        cr.translate(cx, cy)
        cr.rotate(math.radians(self._gps_heading))
        cr.translate(-cx, -cy)

        # Shadow
        cr.set_source_rgba(0, 0, 0, 0.20)
        cr.arc(cx, cy + 2, 13, 0, 2 * math.pi)
        cr.fill()

        # Body (front = top when heading = 0 = north)
        cr.set_source_rgb(0.16, 0.50, 0.73)
        _rounded_rect(cr, cx - 11, cy - 17, 22, 34, 6)
        cr.fill()

        # Roof / windows
        cr.set_source_rgb(0.53, 0.81, 0.98)
        _rounded_rect(cr, cx - 7, cy - 11, 14, 20, 4)
        cr.fill()

        # Front headlights
        cr.set_source_rgb(0.99, 0.91, 0.28)
        cr.rectangle(cx - 10, cy - 19, 4, 3)
        cr.fill()
        cr.rectangle(cx + 6,  cy - 19, 4, 3)
        cr.fill()

        # Rear lights
        cr.set_source_rgb(0.90, 0.30, 0.24)
        cr.rectangle(cx - 10, cy + 16, 4, 3)
        cr.fill()
        cr.rectangle(cx + 6,  cy + 16, 4, 3)
        cr.fill()

        cr.restore()

    # ── Follow / viewport ─────────────────────────────────────────────────────

    def _on_viewport_moved(self, _viewport: Any, _pspec: Any) -> None:
        if not self._setting_pos and self._follow_gps:
            self._set_follow(False)

    def _set_follow(self, active: bool) -> None:
        self._follow_gps = active
        if self._follow_btn is not None:
            self._follow_btn.handler_block_by_func(self._on_follow_toggled)
            self._follow_btn.set_active(active)
            self._follow_btn.handler_unblock_by_func(self._on_follow_toggled)

    def _on_follow_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._follow_gps = btn.get_active()
        if self._follow_gps and self._gps_lat is not None and self._gps_lon is not None:
            self._goto(self._gps_lat, self._gps_lon)

    def _on_center_clicked(self, _btn: Gtk.Button) -> None:
        if self._gps_lat is None or self._shumate_map is None:
            return
        viewport = self._shumate_map.get_viewport()
        self._setting_pos = True
        viewport.set_location(self._gps_lat, self._gps_lon)  # type: ignore[arg-type]
        if viewport.get_zoom_level() < 15.0:
            viewport.set_zoom_level(15.0)
        self._setting_pos = False

    def _on_layer_clicked(self, _btn: Gtk.Button) -> None:
        self._map_type_idx = (self._map_type_idx + 1) % len(_MAP_TYPES)
        layer = _MAP_TYPES[self._map_type_idx]
        self._shumate_map.set_map_source(self._sources[layer])
        if self._layer_btn is not None:
            self._layer_btn.set_icon_name(_MAP_ICONS.get(layer, "map-symbolic"))
            self._layer_btn.set_tooltip_text(_translate(self.language, _MAP_LABEL_KEYS[layer]))

    # ── Mode toggle ───────────────────────────────────────────────────────────

    # ── Route ─────────────────────────────────────────────────────────────────

    def _on_route_clicked(self, _widget: Any) -> None:
        end_text = self._end_entry.get_text().strip()
        if not end_text:
            return
        start_text = self._start_entry.get_text().strip()
        self._status_lbl.set_text(_translate(self.language, "map.routing.searching"))
        self._route_btn.set_sensitive(False)
        threading.Thread(
            target=self._compute_route,
            args=(start_text, end_text),
            daemon=True,
        ).start()

    def _on_clear_clicked(self, _btn: Gtk.Button) -> None:
        self._start_entry.set_text("")
        self._end_entry.set_text("")
        self._start_coord = None
        self._end_coord = None
        self._clear_btn.set_visible(False)
        self._status_lbl.set_text("")
        if self._path_layer is not None:
            self._path_layer.remove_all()
        if self._wp_layer is not None:
            self._wp_layer.remove_all()

    def _compute_route(self, start_text: str, end_text: str) -> None:
        if start_text:
            start = _geocode(start_text)
        elif self._gps_lat is not None and self._gps_lon is not None:
            start = (self._gps_lat, self._gps_lon)
        else:
            start = None

        end = _geocode(end_text)

        if start is None or end is None:
            GLib.idle_add(self._route_error)
            return

        result = _osrm_route(start, end, self._routing_mode)
        GLib.idle_add(self._route_result, start, end, result)

    def _route_error(self) -> bool:
        self._status_lbl.set_text(_translate(self.language, "map.routing.error"))
        self._route_btn.set_sensitive(True)
        return False

    def _route_result(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        result: tuple[list[list[float]], float] | None,
    ) -> bool:
        self._route_btn.set_sensitive(True)
        if result is None:
            self._status_lbl.set_text(_translate(self.language, "map.routing.error"))
            return False

        coords, duration_s = result
        self._start_coord = start
        self._end_coord = end
        self._clear_btn.set_visible(True)
        self._status_lbl.set_text(_format_duration(duration_s))

        if self._path_layer is not None:
            self._path_layer.remove_all()
            for lon, lat in coords:  # OSRM returns [lon, lat]
                self._path_layer.add_node(Shumate.Coordinate.new_full(lat, lon))

        if self._wp_layer is not None:
            self._wp_layer.remove_all()
            self._wp_layer.add_marker(self._make_wp_marker(start[0], start[1], True))
            self._wp_layer.add_marker(self._make_wp_marker(end[0], end[1], False))

        if self._shumate_map is not None and coords:
            lats = [c[1] for c in coords]
            lons = [c[0] for c in coords]
            clat = (min(lats) + max(lats)) / 2.0
            clon = (min(lons) + max(lons)) / 2.0

            alloc = self._shumate_map.get_allocation()
            px_w = alloc.width  if alloc.width  > 100 else 400
            px_h = alloc.height if alloc.height > 100 else 600
            zoom = _zoom_for_bbox(min(lats), min(lons), max(lats), max(lons), px_w, px_h)

            self._set_follow(False)
            # go_to_full animates pan + zoom smoothly; fall back to instant viewport set
            target = self._inner_map if self._inner_map is not None else self._shumate_map
            if hasattr(target, "go_to_full"):
                target.go_to_full(clat, clon, zoom)
            else:
                self._setting_pos = True
                self._shumate_map.get_viewport().set_location(clat, clon)
                self._shumate_map.get_viewport().set_zoom_level(zoom)
                self._setting_pos = False

        return False

    def _make_wp_marker(self, lat: float, lon: float, is_start: bool) -> Any:
        da = Gtk.DrawingArea()
        da.set_size_request(14, 14)
        fill   = (0.18, 0.80, 0.44, 1.0) if is_start else (0.91, 0.30, 0.24, 1.0)
        border = (0.10, 0.54, 0.27, 1.0) if is_start else (0.60, 0.15, 0.10, 1.0)
        da.set_draw_func(self._draw_dot, (fill, border))
        m = Shumate.Marker.new()
        m.set_child(da)
        m.set_location(lat, lon)
        return m

    def _draw_dot(self, _da: Any, cr: Any, w: int, h: int, data: tuple) -> None:
        fill, border = data
        cr.set_source_rgba(*fill)
        cr.arc(w / 2, h / 2, w / 2 - 1.5, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(*border)
        cr.set_line_width(2.5)
        cr.arc(w / 2, h / 2, w / 2 - 1.5, 0, 2 * math.pi)
        cr.stroke()

    # ── Language ──────────────────────────────────────────────────────────────

    def set_language(self, language: str) -> None:
        self.language = _normalize_language(language)
        self._start_entry.set_placeholder_text(_translate(self.language, "map.search.start"))
        self._end_entry.set_placeholder_text(_translate(self.language, "map.search.end"))
        self._route_btn.set_tooltip_text(_translate(self.language, "map.route"))
        self._clear_btn.set_tooltip_text(_translate(self.language, "map.clear"))
        if self._shumate_map is not None:
            if self._center_btn is not None:
                self._center_btn.set_tooltip_text(_translate(self.language, "map.center"))
            if self._follow_btn is not None:
                self._follow_btn.set_tooltip_text(_translate(self.language, "map.follow"))
            if self._layer_btn is not None:
                layer = _MAP_TYPES[self._map_type_idx]
                self._layer_btn.set_tooltip_text(_translate(self.language, _MAP_LABEL_KEYS[layer]))
