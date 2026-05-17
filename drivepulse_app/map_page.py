"""Map page — OpenStreetMap navigation with GPS tracking and routing.

Backend priority:
  1. WebKit (MapLibre GL JS) — 3D vector tiles, pitch, bearing-follow
  2. Shumate (native GTK4)  — 2D raster tiles, offline-friendly
  3. Placeholder             — neither library available
"""
from __future__ import annotations

import concurrent.futures
import json
import math
import os
import threading
import urllib.parse
import urllib.request
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

# ── Optional backends ─────────────────────────────────────────────────────────

_WEBKIT_OK = False
_WebKit: Any = None
try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit as _WebKit  # type: ignore[attr-defined]
    _WEBKIT_OK = True
except (ValueError, ImportError):
    try:
        gi.require_version("WebKit2", "4.1")
        from gi.repository import WebKit2 as _WebKit  # type: ignore[attr-defined]
        _WEBKIT_OK = True
    except (ValueError, ImportError):
        pass

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

_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "map.html")

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

# ── Bundesautobahn API ────────────────────────────────────────────────────────

_BAB_BASE = "https://autobahn.api.bund.dev/v0/details"


def _bab_fetch_road(road: str) -> list[dict]:
    items: list[dict] = []
    for kind in ("roadworks", "incidents"):
        data = _http_get(f"{_BAB_BASE}/{kind}/{urllib.parse.quote(road, safe='')}")
        if data:
            for entry in data.get(kind, []):
                entry["_kind"] = kind
                entry["_road"] = road
                items.append(entry)
    return items


def _bab_fetch_all() -> list[dict]:
    roads_resp = _http_get(f"{_BAB_BASE}/roads")
    if not roads_resp:
        return []
    roads: list[str] = roads_resp.get("roads", [])
    all_items: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for result in pool.map(_bab_fetch_road, roads):
            all_items.extend(result)
    return all_items


# ── Routing modes ─────────────────────────────────────────────────────────────

_OSRM_PROFILE = {"car": "driving", "bicycle": "cycling", "motorcycle": "driving"}

# ── Network helpers ───────────────────────────────────────────────────────────

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
    waypoints: list[tuple[float, float]],
    mode: str,
) -> tuple[list[list[float]], float] | None:
    profile = _OSRM_PROFILE.get(mode, "driving")
    coord_str = ";".join(f"{lon},{lat}" for lat, lon in waypoints)
    url = (
        f"https://router.project-osrm.org/route/v1/{profile}/{coord_str}"
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


# ── Cairo helpers (Shumate backend only) ──────────────────────────────────────

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
    z_lat = math.floor(math.log2(px_h * 0.88 / TILE / dlat))
    z_lon = math.floor(math.log2(px_w * 0.88 / TILE / dlon))
    return float(max(1, min(ZOOM_MAX, z_lat, z_lon)))


# ── MapPage widget ────────────────────────────────────────────────────────────

class MapPage(Gtk.Box):
    """OpenStreetMap navigation page — WebKit/MapLibre (3D) or Shumate (2D)."""
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

        # Backend: "webkit" | "shumate" | "none"
        self._backend: str = "none"

        # WebKit state
        self._webview: Any = None

        # Shumate state
        self._shumate_map: Any = None
        self._inner_map: Any = None
        self._car_marker: Any = None
        self._marker_layer: Any = None
        self._path_layer: Any = None
        self._wp_layer: Any = None
        self._sources: dict[str, Any] = {}
        self._setting_pos: bool = False

        # FAB buttons (None when backend unavailable)
        self._follow_btn: Gtk.ToggleButton | None = None
        self._center_btn: Gtk.Button | None = None
        self._layer_btn: Gtk.Button | None = None
        self._traffic_btn: Gtk.ToggleButton | None = None

        # Traffic layer (Shumate only)
        self._traffic_layer: Any = None
        self._traffic_loaded: bool = False

        # Entry rows: flat list of (row_box, entry, remove_btn)
        self._entry_rows: list[tuple[Gtk.Box, Gtk.Entry, Gtk.Button]] = []
        self._entries_container: Gtk.Box | None = None
        self._search_bar: Gtk.Box | None = None

        self._build_search_bar()
        self._build_map()

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
        self._search_bar = bar
        bar.set_margin_top(8)
        bar.set_margin_bottom(4)
        bar.set_margin_start(8)
        bar.set_margin_end(8)

        self._entries_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bar.append(self._entries_container)

        # Two initial rows: start + end
        self._entry_rows = []
        for _ in range(2):
            self._entries_container.append(self._make_entry_row())
        self._update_placeholders()
        self._update_remove_sensitivity()

        # Action row: [route] [clear] ... [status→]
        action = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        self._route_btn = Gtk.Button(icon_name="xsi-search")
        self._route_btn.add_css_class("suggested-action")
        self._route_btn.set_tooltip_text(_translate(self.language, "map.route"))
        self._route_btn.connect("clicked", self._on_route_clicked)

        self._status_lbl = Gtk.Label(label="")
        self._status_lbl.add_css_class("dim-label")
        self._status_lbl.set_hexpand(True)
        self._status_lbl.set_halign(Gtk.Align.END)

        for w in (self._status_lbl, self._route_btn):
            action.append(w)
        bar.append(action)
        self.append(bar)

    def _make_entry_row(self) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        entry = Gtk.Entry()
        entry.set_hexpand(True)
        entry.connect("activate", self._on_route_clicked)

        add_btn = Gtk.Button(label="+")
        add_btn.add_css_class("flat")
        add_btn.set_valign(Gtk.Align.CENTER)
        add_btn.connect("clicked", lambda _b, r=row: self._insert_entry_after(r))

        rem_btn = Gtk.Button(label="−")
        rem_btn.add_css_class("flat")
        rem_btn.set_valign(Gtk.Align.CENTER)
        rem_btn.connect("clicked", lambda _b, r=row: self._remove_entry(r))

        row.append(entry)
        row.append(add_btn)
        row.append(rem_btn)

        self._entry_rows.append((row, entry, rem_btn))
        return row

    def _insert_entry_after(self, after_row: Gtk.Box) -> None:
        idx = next(i for i, (r, _, __) in enumerate(self._entry_rows) if r is after_row)
        new_row = self._make_entry_row()
        # _make_entry_row appended to list; move it to correct position
        triple = self._entry_rows.pop()
        self._entry_rows.insert(idx + 1, triple)
        if self._entries_container is not None:
            self._entries_container.insert_child_after(new_row, after_row)
        self._update_placeholders()
        self._update_remove_sensitivity()
        triple[1].grab_focus()

    def _remove_entry(self, row: Gtk.Box) -> None:
        idx = next(i for i, (r, _, __) in enumerate(self._entry_rows) if r is row)
        if len(self._entry_rows) <= 2:
            self._entry_rows[idx][1].set_text("")
            return
        self._entry_rows.pop(idx)
        if self._entries_container is not None:
            self._entries_container.remove(row)
        self._update_placeholders()
        self._update_remove_sensitivity()

    def _update_placeholders(self) -> None:
        n = len(self._entry_rows)
        for i, (_, entry, __) in enumerate(self._entry_rows):
            if i == 0:
                key = "map.search.start"
            elif i == n - 1:
                key = "map.search.end"
            else:
                key = "map.search.waypoint"
            entry.set_placeholder_text(_translate(self.language, key))

    def _update_remove_sensitivity(self) -> None:
        for _, __, rem_btn in self._entry_rows:
            rem_btn.set_sensitive(True)

    # ── Map area ──────────────────────────────────────────────────────────────

    def _build_map(self) -> None:
        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)

        if _SHUMATE_OK:
            self._backend = "shumate"
            content = self._setup_shumate()
        elif _WEBKIT_OK:
            self._backend = "webkit"
            content = self._setup_webview()
        else:
            self._backend = "none"
            content = self._build_placeholder()

        overlay.set_child(content)

        if self._backend != "none":
            overlay.add_overlay(self._build_fab())

        self.append(overlay)

    def _build_placeholder(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_hexpand(True)
        box.set_vexpand(True)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        icon = Gtk.Image.new_from_icon_name("map-symbolic")
        icon.set_pixel_size(64)
        icon.add_css_class("dim-label")
        label = Gtk.Label(
            label="Map not available.\nInstall gir1.2-shumate-1.0 or webkit2gtk to enable."
        )
        label.set_justify(Gtk.Justification.CENTER)
        label.add_css_class("dim-label")
        box.append(icon)
        box.append(label)
        return box

    def _build_fab(self) -> Gtk.Box:
        fab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        fab.set_halign(Gtk.Align.END)
        fab.set_valign(Gtk.Align.END)
        fab.set_margin_end(12)
        fab.set_margin_bottom(36)

        self._traffic_btn = Gtk.ToggleButton(icon_name="emblem-important-symbolic")
        self._traffic_btn.add_css_class("circular")
        self._traffic_btn.add_css_class("osd")
        self._traffic_btn.set_tooltip_text(_translate(self.language, "map.traffic"))
        self._traffic_btn.connect("toggled", self._on_traffic_toggled)

        self._layer_btn = Gtk.Button(icon_name="dialog-layers-symbolic")
        self._layer_btn.add_css_class("circular")
        self._layer_btn.add_css_class("osd")
        self._layer_btn.set_tooltip_text(_translate(self.language, _MAP_LABEL_KEYS["map"]))
        self._layer_btn.connect("clicked", self._on_layer_clicked)

        self._follow_btn = Gtk.ToggleButton(icon_name="kstars_satellites-symbolic")
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

        fab.append(self._traffic_btn)
        fab.append(self._layer_btn)
        fab.append(self._follow_btn)
        fab.append(self._center_btn)
        return fab

    # ── WebKit backend ────────────────────────────────────────────────────────

    def _setup_webview(self) -> Gtk.Widget:
        self._webview = _WebKit.WebView()
        self._webview.set_hexpand(True)
        self._webview.set_vexpand(True)

        settings = self._webview.get_settings()
        for prop in ("allow-file-access-from-file-urls", "allow-universal-access-from-file-urls"):
            try:
                settings.set_property(prop, True)
            except Exception:
                pass

        ucm = self._webview.get_user_content_manager()
        try:
            ucm.register_script_message_handler("drivepulse")
        except TypeError:
            try:
                ucm.register_script_message_handler("drivepulse", None)
            except Exception:
                pass
        ucm.connect("script-message-received::drivepulse", self._on_js_message)

        try:
            with open(_HTML_PATH, encoding="utf-8") as fh:
                html = fh.read()
            self._webview.load_html(html, "file:///")
        except OSError as exc:
            log.error("Could not load map.html: %s", exc)

        return self._webview

    def _js(self, code: str) -> None:
        if self._webview is None:
            return
        try:
            if hasattr(self._webview, "evaluate_javascript"):
                self._webview.evaluate_javascript(code, -1, None, None, None, None, None)
            else:
                self._webview.run_javascript(code, None, None, None)
        except Exception as exc:
            log.debug("JS call failed: %s", exc)

    def _on_js_message(self, _ucm: Any, *args: Any) -> None:
        try:
            msg = args[-1]
            js_val = msg.get_js_value()
            data = json.loads(js_val.to_json(0))
            if data.get("action") == "follow_off":
                GLib.idle_add(self._set_follow, False)
        except Exception as exc:
            log.debug("JS message error: %s", exc)

    # ── Shumate backend ───────────────────────────────────────────────────────

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
        for key in _TILE_URLS:
            self._sources[key] = osm_source

        if hasattr(Shumate, "RasterRenderer") and hasattr(Shumate, "TileDownloader"):
            for key, url in (("satellite", _TILE_URLS["satellite"]), ("dark", _TILE_URLS["dark"])):
                try:
                    self._sources[key] = Shumate.RasterRenderer.new(
                        Shumate.TileDownloader.new(url)
                    )
                except Exception:
                    log.warning("Could not create tile source for %s — using OSM fallback", key)

        self._shumate_map.set_map_source(self._sources["map"])

        self._shumate_map.get_scale().set_margin_bottom(24)

        self._inner_map = (
            self._shumate_map.get_map()
            if hasattr(self._shumate_map, "get_map")
            else self._shumate_map
        )
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

        self._traffic_layer = Shumate.MarkerLayer.new(viewport)
        self._traffic_layer.set_visible(False)
        _inner.add_layer(self._traffic_layer)

        viewport.connect("notify::latitude", self._on_viewport_moved)
        return self._shumate_map

    # ── GPS position updates ──────────────────────────────────────────────────

    def update_gps(
        self,
        lat: float | None,
        lon: float | None,
        heading: float | None,
    ) -> None:
        if lat is None or lon is None:
            return
        self._gps_lat = lat
        self._gps_lon = lon
        self._gps_heading = heading or 0.0

        if self._backend == "webkit":
            self._js(f"mapSetCar({lat}, {lon}, {self._gps_heading})")
        elif self._backend == "shumate" and self._shumate_map is not None:
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
        if self._backend == "webkit":
            self._js(f"mapSetCar({lat}, {lon}, {self._gps_heading})")
        elif self._shumate_map is not None:
            self._setting_pos = True
            self._shumate_map.get_viewport().set_location(lat, lon)
            self._setting_pos = False

    # ── Car Cairo drawing (Shumate only) ──────────────────────────────────────

    def _draw_car(self, _da: Any, cr: Any, width: int, height: int, _data: Any) -> None:
        cx, cy = width / 2.0, height / 2.0
        cr.save()
        cr.translate(cx, cy)
        cr.rotate(math.radians(self._gps_heading))
        cr.translate(-cx, -cy)

        cr.set_source_rgba(0, 0, 0, 0.20)
        cr.arc(cx, cy + 2, 13, 0, 2 * math.pi)
        cr.fill()

        cr.set_source_rgb(0.16, 0.50, 0.73)
        _rounded_rect(cr, cx - 11, cy - 17, 22, 34, 6)
        cr.fill()

        cr.set_source_rgb(0.53, 0.81, 0.98)
        _rounded_rect(cr, cx - 7, cy - 11, 14, 20, 4)
        cr.fill()

        cr.set_source_rgb(0.99, 0.91, 0.28)
        cr.rectangle(cx - 10, cy - 19, 4, 3)
        cr.fill()
        cr.rectangle(cx + 6,  cy - 19, 4, 3)
        cr.fill()

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

    def _set_follow(self, active: bool) -> bool:
        self._follow_gps = active
        if self._follow_btn is not None:
            self._follow_btn.handler_block_by_func(self._on_follow_toggled)
            self._follow_btn.set_active(active)
            self._follow_btn.handler_unblock_by_func(self._on_follow_toggled)
        if self._backend == "webkit":
            val = "true" if active else "false"
            self._js(f"mapSetFollow({val})")
        return False

    def _on_follow_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._follow_gps = btn.get_active()
        if self._backend == "webkit":
            val = "true" if self._follow_gps else "false"
            self._js(f"mapSetFollow({val})")
        if self._follow_gps and self._gps_lat is not None and self._gps_lon is not None:
            self._goto(self._gps_lat, self._gps_lon)

    def _on_center_clicked(self, _btn: Gtk.Button) -> None:
        if self._gps_lat is None:
            return
        if self._backend == "webkit":
            self._js(f"mapGoTo({self._gps_lat}, {self._gps_lon}, 15)")
        elif self._shumate_map is not None:
            viewport = self._shumate_map.get_viewport()
            self._setting_pos = True
            viewport.set_location(self._gps_lat, self._gps_lon)
            if viewport.get_zoom_level() < 15.0:
                viewport.set_zoom_level(15.0)
            self._setting_pos = False

    def _on_layer_clicked(self, _btn: Gtk.Button) -> None:
        self._map_type_idx = (self._map_type_idx + 1) % len(_MAP_TYPES)
        layer = _MAP_TYPES[self._map_type_idx]
        if self._backend == "webkit":
            self._js(f"mapSetStyle('{layer}')")
        elif self._shumate_map is not None:
            self._shumate_map.set_map_source(self._sources[layer])
        if self._layer_btn is not None:
            self._layer_btn.set_icon_name(_MAP_ICONS.get(layer, "map-symbolic"))
            self._layer_btn.set_tooltip_text(_translate(self.language, _MAP_LABEL_KEYS[layer]))

    # ── Traffic layer (Bundesautobahn API) ────────────────────────────────────

    def _on_traffic_toggled(self, btn: Gtk.ToggleButton) -> None:
        visible = btn.get_active()
        if self._backend == "webkit":
            val = "true" if visible else "false"
            self._js(f"mapSetTrafficVisible({val})")
        elif self._traffic_layer is not None:
            self._traffic_layer.set_visible(visible)
        if visible and not self._traffic_loaded:
            self._traffic_loaded = True
            self._status_lbl.set_text(_translate(self.language, "map.traffic.loading"))
            threading.Thread(target=self._load_traffic_thread, daemon=True).start()

    def _load_traffic_thread(self) -> None:
        items = _bab_fetch_all()
        GLib.idle_add(self._show_traffic, items)

    def _parse_traffic_items(
        self, items: list[dict]
    ) -> list[tuple[float, float, str, str]]:
        result = []
        for item in items:
            coord = item.get("coordinate") or {}
            try:
                lat = float(str(coord.get("lat", "0")).replace(",", "."))
                lon = float(str(coord.get("long", "0")).replace(",", "."))
            except (ValueError, TypeError):
                continue
            if lat == 0.0 and lon == 0.0:
                continue
            kind = item.get("_kind", "incidents")
            title = item.get("title") or ""
            if not title:
                desc = item.get("description") or []
                title = desc[0] if desc else kind
            road = item.get("_road", "")
            tooltip = f"{road}: {title}" if road else title
            result.append((lat, lon, kind, tooltip))
        return result

    def _show_traffic(self, items: list[dict]) -> bool:
        parsed = self._parse_traffic_items(items)
        count = len(parsed)

        if self._backend == "webkit":
            js_items = [
                {"lat": lat, "lon": lon, "kind": kind, "title": title}
                for lat, lon, kind, title in parsed
            ]
            self._js(f"mapSetTraffic({json.dumps(js_items)})")
            if self._traffic_btn is not None and self._traffic_btn.get_active():
                self._js("mapSetTrafficVisible(true)")
        elif self._traffic_layer is not None:
            self._traffic_layer.remove_all()
            for lat, lon, kind, tooltip in parsed:
                m = self._make_traffic_marker(kind, tooltip, lat, lon)
                self._traffic_layer.add_marker(m)

        if self._traffic_btn is not None and self._traffic_btn.get_active():
            self._status_lbl.set_text(
                _translate(self.language, "map.traffic.count").format(count=count)
            )
        return False

    def _make_traffic_marker(self, kind: str, tooltip: str, lat: float, lon: float) -> Any:
        if kind == "roadworks":
            fill   = (0.95, 0.60, 0.0,  1.0)
            border = (0.70, 0.40, 0.0,  1.0)
        else:
            fill   = (0.90, 0.20, 0.20, 1.0)
            border = (0.60, 0.10, 0.10, 1.0)
        da = Gtk.DrawingArea()
        da.set_size_request(14, 14)
        da.set_draw_func(self._draw_dot, (fill, border))
        da.set_tooltip_text(tooltip)
        m = Shumate.Marker.new()
        m.set_child(da)
        m.set_location(lat, lon)
        return m

    # ── Route ─────────────────────────────────────────────────────────────────

    def _on_route_clicked(self, _widget: Any) -> None:
        texts = [e.get_text().strip() for _, e, __ in self._entry_rows]
        if not texts[-1]:
            return
        start_text = texts[0]
        wp_texts = texts[1:-1]
        end_text = texts[-1]
        self._status_lbl.set_text(_translate(self.language, "map.routing.searching"))
        self._route_btn.set_sensitive(False)
        threading.Thread(
            target=self._compute_route,
            args=(start_text, wp_texts, end_text),
            daemon=True,
        ).start()

    def _on_clear_clicked(self, _btn: Gtk.Button) -> None:
        # Remove all rows except first two (start + end)
        while len(self._entry_rows) > 2:
            row, _, __ = self._entry_rows.pop()
            if self._entries_container is not None:
                self._entries_container.remove(row)
        # Clear text in remaining entries
        for _, entry, __ in self._entry_rows:
            entry.set_text("")
        self._update_placeholders()
        self._update_remove_sensitivity()
        self._status_lbl.set_text("")
        if self._backend == "webkit":
            self._js("mapClearRoute()")
        else:
            if self._path_layer is not None:
                self._path_layer.remove_all()
            if self._wp_layer is not None:
                self._wp_layer.remove_all()

    def set_nav_visible(self, visible: bool) -> None:
        if self._search_bar is not None:
            self._search_bar.set_visible(visible)
        if self._backend == "webkit":
            v = "true" if visible else "false"
            self._js(f"mapSetRouteVisible({v})")
        else:
            if self._path_layer is not None:
                self._path_layer.set_visible(visible)
            if self._wp_layer is not None:
                self._wp_layer.set_visible(visible)

    def _compute_route(self, start_text: str, wp_texts: list[str], end_text: str) -> None:
        if start_text:
            start = _geocode(start_text)
        elif self._gps_lat is not None and self._gps_lon is not None:
            start = (self._gps_lat, self._gps_lon)
        else:
            start = None
        if start is None:
            GLib.idle_add(self._route_error)
            return

        via: list[tuple[float, float]] = []
        for txt in wp_texts:
            if not txt:
                continue
            pt = _geocode(txt)
            if pt is None:
                GLib.idle_add(self._route_error)
                return
            via.append(pt)

        end = _geocode(end_text)
        if end is None:
            GLib.idle_add(self._route_error)
            return

        all_points = [start] + via + [end]
        result = _osrm_route(all_points, self._routing_mode)
        GLib.idle_add(self._route_result, all_points, result)

    def _route_error(self) -> bool:
        self._status_lbl.set_text(_translate(self.language, "map.routing.error"))
        self._route_btn.set_sensitive(True)
        return False

    def _route_result(
        self,
        all_points: list[tuple[float, float]],
        result: tuple[list[list[float]], float] | None,
    ) -> bool:
        self._route_btn.set_sensitive(True)
        if result is None:
            self._status_lbl.set_text(_translate(self.language, "map.routing.error"))
            return False

        coords, duration_s = result
        self._start_coord = all_points[0]
        self._end_coord = all_points[-1]
        self._status_lbl.set_text(_format_duration(duration_s))

        if coords:
            lats = [c[1] for c in coords]
            lons = [c[0] for c in coords]
            self._set_follow(False)

        if self._backend == "webkit":
            self._js(f"mapSetRoute({json.dumps(coords)})")
            pts_js = json.dumps([[p[0], p[1]] for p in all_points])
            self._js(f"mapSetWaypoints({pts_js})")
            if coords:
                min_lat, max_lat = min(lats), max(lats)
                min_lon, max_lon = min(lons), max(lons)
                self._js(f"mapFitBounds({min_lat},{min_lon},{max_lat},{max_lon})")
        elif self._shumate_map is not None:
            if self._path_layer is not None:
                self._path_layer.remove_all()
                for lon, lat in coords:
                    self._path_layer.add_node(Shumate.Coordinate.new_full(lat, lon))

            if self._wp_layer is not None:
                self._wp_layer.remove_all()
                for i, pt in enumerate(all_points):
                    role = "start" if i == 0 else ("end" if i == len(all_points) - 1 else "via")
                    self._wp_layer.add_marker(self._make_wp_marker(pt[0], pt[1], role))

            if coords:
                clat = (min(lats) + max(lats)) / 2.0
                clon = (min(lons) + max(lons)) / 2.0
                alloc = self._shumate_map.get_allocation()
                px_w = max(alloc.width,  400)
                px_h = max(alloc.height, 600)
                zoom = _zoom_for_bbox(min(lats), min(lons), max(lats), max(lons), px_w, px_h)
                viewport = self._shumate_map.get_viewport()
                self._setting_pos = True
                viewport.set_zoom_level(zoom)
                viewport.set_location(clat, clon)
                self._setting_pos = False

        return False

    # ── Waypoint markers (Shumate only) ───────────────────────────────────────

    def _make_wp_marker(self, lat: float, lon: float, role: str) -> Any:
        if role == "start":
            fill, border = (0.18, 0.80, 0.44, 1.0), (0.10, 0.54, 0.27, 1.0)
        elif role == "end":
            fill, border = (0.91, 0.30, 0.24, 1.0), (0.60, 0.15, 0.10, 1.0)
        else:  # via
            fill, border = (0.95, 0.65, 0.10, 1.0), (0.70, 0.45, 0.00, 1.0)
        da = Gtk.DrawingArea()
        da.set_size_request(14, 14)
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
        self._update_placeholders()
        self._route_btn.set_tooltip_text(_translate(self.language, "map.route"))
        layer = _MAP_TYPES[self._map_type_idx]
        if self._layer_btn is not None:
            self._layer_btn.set_tooltip_text(_translate(self.language, _MAP_LABEL_KEYS[layer]))
        if self._follow_btn is not None:
            self._follow_btn.set_tooltip_text(_translate(self.language, "map.follow"))
        if self._center_btn is not None:
            self._center_btn.set_tooltip_text(_translate(self.language, "map.center"))
        if self._traffic_btn is not None:
            self._traffic_btn.set_tooltip_text(_translate(self.language, "map.traffic"))
