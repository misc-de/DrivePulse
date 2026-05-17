"""Map page — OpenStreetMap navigation with GPS tracking and routing."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.parse
import urllib.request
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk  # noqa: E402

_WEBKIT_OK = False
try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit  # type: ignore[attr-defined]
    _WEBKIT_OK = True
except (ValueError, ImportError):
    WebKit = None  # type: ignore[assignment]

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

# ── Routing modes ─────────────────────────────────────────────────────────────

_ROUTING_MODES = ["car", "bicycle", "motorcycle"]
_OSRM_PROFILE = {"car": "driving", "bicycle": "cycling", "motorcycle": "driving"}

# ── Network helpers (run in background threads) ───────────────────────────────

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
) -> list[list[float]] | None:
    profile = _OSRM_PROFILE.get(mode, "driving")
    coords = f"{start[1]},{start[0]};{end[1]},{end[0]}"
    url = (
        f"https://router.project-osrm.org/route/v1/{profile}/{coords}"
        "?overview=full&geometries=geojson"
    )
    data = _http_get(url)
    if data and data.get("code") == "Ok" and data.get("routes"):
        return data["routes"][0]["geometry"]["coordinates"]  # [[lon, lat], ...]
    return None


# ── Embedded Leaflet map HTML ─────────────────────────────────────────────────

_MAP_HTML = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
html,body { width:100%; height:100%; }
#map { width:100%; height:100%; }

.car-wrap { width:36px; height:36px; transform-origin:center center; display:flex;
            align-items:center; justify-content:center; }
.car-wrap svg { display:block; }

.wp-start,.wp-end { width:14px; height:14px; border-radius:50%; box-shadow:0 1px 4px rgba(0,0,0,.45); }
.wp-start { background:#2ecc71; border:2.5px solid #1a8a4a; }
.wp-end   { background:#e74c3c; border:2.5px solid #a93226; }
</style>
</head>
<body>
<div id="map"></div>
<script>
'use strict';

// ── Map + tile layers ─────────────────────────────────────────────────────────
var map = L.map('map', { zoomControl: true }).setView([48.137, 11.576], 13);

var layers = {
  map: L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19
  }),
  satellite: L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    { attribution: 'Tiles &copy; Esri', maxZoom: 19 }
  ),
  dark: L.tileLayer(
    'https://cartodb-basemaps-a.global.ssl.fastly.net/dark_all/{z}/{x}/{y}.png',
    { attribution: '&copy; OpenStreetMap contributors &copy; CARTO', maxZoom: 19 }
  )
};
var curLayer = 'map';
layers.map.addTo(map);

// ── Car SVG marker ────────────────────────────────────────────────────────────
var CAR_SVG =
  '<svg width="36" height="36" viewBox="0 0 36 36" xmlns="http://www.w3.org/2000/svg">' +
  /* shadow */ '<ellipse cx="18" cy="31" rx="10" ry="3" fill="rgba(0,0,0,.22)"/>' +
  /* body */  '<rect x="4" y="15" width="28" height="14" rx="5" fill="#2980b9" stroke="#1a5276" stroke-width="1.5"/>' +
  /* roof */  '<rect x="8" y="8" width="20" height="12" rx="5" fill="#5dade2" stroke="#2471a3" stroke-width="1"/>' +
  /* front headlights */ '<rect x="24" y="13" width="5" height="3" rx="1" fill="#f9e547"/>' +
  /* rear lights */      '<rect x="7"  y="13" width="5" height="3" rx="1" fill="#e67e22"/>' +
  /* left wheel */       '<circle cx="10" cy="29" r="4" fill="#1c2833" stroke="#717d7e" stroke-width="1"/>' +
  /* right wheel */      '<circle cx="26" cy="29" r="4" fill="#1c2833" stroke="#717d7e" stroke-width="1"/>' +
  '</svg>';

var carIcon = L.divIcon({
  html: '<div class="car-wrap" id="car-wrap">' + CAR_SVG + '</div>',
  iconSize: [36, 36],
  iconAnchor: [18, 18],
  className: ''
});

var carMarker = null;
var carLat = null, carLon = null;
var followGps = true;

// ── Route + waypoints ─────────────────────────────────────────────────────────
var routeLine  = null;
var wpStart    = null;
var wpEnd      = null;

function _wpIcon(cls) {
  return L.divIcon({ html: '<div class="' + cls + '"></div>',
                     iconSize: [14, 14], iconAnchor: [7, 7], className: '' });
}

// ── Python → JS API ───────────────────────────────────────────────────────────

window.dpUpdatePos = function(lat, lon, hdg) {
  carLat = lat; carLon = lon;
  if (!carMarker) {
    carMarker = L.marker([lat, lon], { icon: carIcon, zIndexOffset: 1000 }).addTo(map);
  } else {
    carMarker.setLatLng([lat, lon]);
  }
  var el = carMarker.getElement();
  if (el && hdg !== null) {
    var wrap = el.querySelector('.car-wrap');
    if (wrap) wrap.style.transform = 'rotate(' + hdg + 'deg)';
  }
  if (followGps) map.panTo([lat, lon], { animate: true, duration: 0.4 });
};

window.dpSetLayer = function(name) {
  if (layers[curLayer]) map.removeLayer(layers[curLayer]);
  curLayer = name;
  if (layers[name]) layers[name].addTo(map);
};

window.dpShowRoute = function(coordsJson) {
  if (routeLine) { map.removeLayer(routeLine); routeLine = null; }
  var pts = JSON.parse(coordsJson).map(function(c) { return [c[1], c[0]]; });
  if (!pts.length) return;
  routeLine = L.polyline(pts, { color: '#3498db', weight: 5, opacity: 0.85 }).addTo(map);
  map.fitBounds(routeLine.getBounds(), { padding: [32, 32] });
};

window.dpSetWaypoints = function(slat, slon, elat, elon) {
  if (wpStart) { map.removeLayer(wpStart); wpStart = null; }
  if (wpEnd)   { map.removeLayer(wpEnd);   wpEnd   = null; }
  if (slat !== null) wpStart = L.marker([slat, slon], { icon: _wpIcon('wp-start') }).addTo(map);
  if (elat !== null) wpEnd   = L.marker([elat, elon], { icon: _wpIcon('wp-end')   }).addTo(map);
};

window.dpClearRoute = function() {
  if (routeLine) { map.removeLayer(routeLine); routeLine = null; }
  if (wpStart)   { map.removeLayer(wpStart);   wpStart   = null; }
  if (wpEnd)     { map.removeLayer(wpEnd);     wpEnd     = null; }
};

window.dpCenter = function() {
  if (carLat !== null) map.setView([carLat, carLon], Math.max(map.getZoom(), 15), { animate: true });
};

window.dpSetFollow = function(on) {
  followGps = on;
  if (on && carLat !== null) map.panTo([carLat, carLon]);
};

// ── JS → Python: disable follow on manual drag ────────────────────────────────
map.on('dragstart', function() {
  followGps = false;
  try { window.webkit.messageHandlers.dp.postMessage('follow_off'); } catch(e) {}
});
</script>
</body>
</html>
"""


# ── MapPage widget ────────────────────────────────────────────────────────────

class MapPage(Gtk.Box):
    """OpenStreetMap navigation page with GPS tracking and routing."""
    __gtype_name__ = "MapPage"

    def __init__(self, language: str = SOURCE_LANGUAGE) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.language = _normalize_language(language)

        self._gps_lat: float | None = None
        self._gps_lon: float | None = None
        self._gps_heading: float | None = None
        self._follow_gps: bool = True
        self._map_type_idx: int = 0
        self._routing_mode: str = "car"
        self._start_coord: tuple[float, float] | None = None
        self._end_coord: tuple[float, float] | None = None
        self._html_tmp: Any = None

        self._build_search_bar()
        self._build_map_overlay()

    # ── Search / route bar ────────────────────────────────────────────────────

    def _build_search_bar(self) -> None:
        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bar.set_margin_top(8)
        bar.set_margin_bottom(4)
        bar.set_margin_start(8)
        bar.set_margin_end(8)

        # ── Row 1: start → end + route button ────────────────────────────────
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

        # ── Row 2: mode toggles + status ─────────────────────────────────────
        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

        self._mode_btns: dict[str, Gtk.ToggleButton] = {}
        first: Gtk.ToggleButton | None = None
        for mode in _ROUTING_MODES:
            btn = Gtk.ToggleButton(label=_translate(self.language, f"map.routing.{mode}"))
            btn.add_css_class("flat")
            if first is None:
                btn.set_active(True)
                first = btn
            else:
                btn.set_group(first)
            btn.connect("toggled", self._on_mode_toggled, mode)
            self._mode_btns[mode] = btn
            row2.append(btn)

        self._status_lbl = Gtk.Label(label="")
        self._status_lbl.add_css_class("dim-label")
        self._status_lbl.set_hexpand(True)
        self._status_lbl.set_halign(Gtk.Align.END)
        row2.append(self._status_lbl)

        bar.append(row1)
        bar.append(row2)
        self.append(bar)

    # ── Map WebView + floating buttons ────────────────────────────────────────

    def _build_map_overlay(self) -> None:
        if not _WEBKIT_OK:
            self._webview = None
            placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            placeholder.set_hexpand(True)
            placeholder.set_vexpand(True)
            placeholder.set_halign(Gtk.Align.CENTER)
            placeholder.set_valign(Gtk.Align.CENTER)
            icon = Gtk.Image.new_from_icon_name("map-symbolic")
            icon.set_pixel_size(64)
            icon.add_css_class("dim-label")
            label = Gtk.Label(label="Map not available.\nInstall webkitgtk-6.0 to enable.")
            label.set_justify(Gtk.Justification.CENTER)
            label.add_css_class("dim-label")
            placeholder.append(icon)
            placeholder.append(label)
            self.append(placeholder)
            return

        ctx = WebKit.WebContext.get_default()
        self._webview = WebKit.WebView.new_with_context(ctx)

        # Allow the local file:// page to load HTTPS tile/CDN resources
        s = self._webview.get_settings()
        s.set_allow_universal_access_from_file_urls(True)
        s.set_allow_file_access_from_file_urls(True)
        s.set_javascript_can_open_windows_automatically(False)

        # JS → Python messaging
        mgr = self._webview.get_user_content_manager()
        mgr.connect("script-message-received::dp", self._on_js_message)
        mgr.register_script_message_handler("dp")

        self._webview.set_hexpand(True)
        self._webview.set_vexpand(True)

        # Floating action buttons (bottom-right)
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
        self._layer_btn.set_tooltip_text(_translate(self.language, "map.type.map"))
        self._layer_btn.connect("clicked", self._on_layer_clicked)

        fab.append(self._follow_btn)
        fab.append(self._center_btn)
        fab.append(self._layer_btn)

        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        overlay.set_child(self._webview)
        overlay.add_overlay(fab)
        self.append(overlay)

        # Write HTML to temp file and load via file:// so WebKit grants origin
        self._html_tmp = tempfile.NamedTemporaryFile(
            suffix=".html", delete=False, mode="w", encoding="utf-8"
        )
        self._html_tmp.write(_MAP_HTML)
        self._html_tmp.flush()
        self._webview.load_uri(f"file://{self._html_tmp.name}")

    # ── GPS position updates (called from telemetry mixin) ────────────────────

    def update_gps(
        self,
        lat: float | None,
        lon: float | None,
        heading: float | None,
    ) -> None:
        if self._webview is None or lat is None or lon is None:
            return
        self._gps_lat = lat
        self._gps_lon = lon
        self._gps_heading = heading
        hdg = f"{heading:.1f}" if heading is not None else "null"
        self._js(f"dpUpdatePos({lat},{lon},{hdg});")

    # ── JS bridge ─────────────────────────────────────────────────────────────

    def _js(self, script: str) -> None:
        if self._webview is None:
            return
        self._webview.evaluate_javascript(script, -1, None, None, None, None, None)

    def _on_js_message(self, _mgr: Any, result: Any) -> None:
        try:
            msg = result.get_js_value().to_string()
            if msg == "follow_off" and self._follow_btn.get_active():
                # Suppress the toggled signal to avoid feedback loop
                self._follow_gps = False
                self._follow_btn.handler_block_by_func(self._on_follow_toggled)
                self._follow_btn.set_active(False)
                self._follow_btn.handler_unblock_by_func(self._on_follow_toggled)
        except Exception:
            pass

    # ── FAB callbacks ─────────────────────────────────────────────────────────

    def _on_center_clicked(self, _btn: Gtk.Button) -> None:
        self._js("dpCenter();")

    def _on_follow_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._follow_gps = btn.get_active()
        val = "true" if self._follow_gps else "false"
        self._js(f"dpSetFollow({val});")

    def _on_layer_clicked(self, _btn: Gtk.Button) -> None:
        self._map_type_idx = (self._map_type_idx + 1) % len(_MAP_TYPES)
        layer = _MAP_TYPES[self._map_type_idx]
        self._js(f"dpSetLayer('{layer}');")
        self._layer_btn.set_icon_name(_MAP_ICONS.get(layer, "map-symbolic"))
        self._layer_btn.set_tooltip_text(
            _translate(self.language, _MAP_LABEL_KEYS[layer])
        )

    # ── Mode toggle ───────────────────────────────────────────────────────────

    def _on_mode_toggled(self, btn: Gtk.ToggleButton, mode: str) -> None:
        if btn.get_active():
            self._routing_mode = mode

    # ── Route button / clear ──────────────────────────────────────────────────

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
        self._js("dpClearRoute();")

    # ── Routing (background thread) ───────────────────────────────────────────

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

        coords = _osrm_route(start, end, self._routing_mode)
        GLib.idle_add(self._route_result, start, end, coords)

    def _route_error(self) -> bool:
        self._status_lbl.set_text(_translate(self.language, "map.routing.error"))
        self._route_btn.set_sensitive(True)
        return False

    def _route_result(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        coords: list[list[float]] | None,
    ) -> bool:
        self._route_btn.set_sensitive(True)
        if coords is None:
            self._status_lbl.set_text(_translate(self.language, "map.routing.error"))
            return False
        self._start_coord = start
        self._end_coord = end
        self._clear_btn.set_visible(True)
        self._status_lbl.set_text("")
        # Escape the JSON so it's safe inside a single-quoted JS string
        coords_json = json.dumps(coords).replace("\\", "\\\\").replace("'", "\\'")
        self._js(
            f"dpSetWaypoints({start[0]},{start[1]},{end[0]},{end[1]});"
            f"dpShowRoute('{coords_json}');"
        )
        return False

    # ── Language ──────────────────────────────────────────────────────────────

    def set_language(self, language: str) -> None:
        self.language = _normalize_language(language)
        self._start_entry.set_placeholder_text(_translate(self.language, "map.search.start"))
        self._end_entry.set_placeholder_text(_translate(self.language, "map.search.end"))
        self._route_btn.set_tooltip_text(_translate(self.language, "map.route"))
        self._clear_btn.set_tooltip_text(_translate(self.language, "map.clear"))
        if self._webview is not None:
            self._center_btn.set_tooltip_text(_translate(self.language, "map.center"))
            self._follow_btn.set_tooltip_text(_translate(self.language, "map.follow"))
            layer = _MAP_TYPES[self._map_type_idx]
            self._layer_btn.set_tooltip_text(_translate(self.language, _MAP_LABEL_KEYS[layer]))
        for mode, btn in self._mode_btns.items():
            btn.set_label(_translate(self.language, f"map.routing.{mode}"))

    def __del__(self) -> None:
        if self._html_tmp is not None:
            try:
                os.unlink(self._html_tmp.name)
            except Exception:
                pass
