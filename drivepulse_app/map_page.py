"""Map page — OpenStreetMap navigation with GPS tracking and routing.

Backend priority:
  1. WebKit (MapLibre GL JS) — 3D vector tiles, pitch, bearing-follow
  2. Shumate (native GTK4)  — 2D raster tiles, offline-friendly
  3. Placeholder             — neither library available
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, GLib, GObject, Gtk  # noqa: E402

from .common import SOURCE_LANGUAGE, _normalize_language, _translate
from .diagnostics import get_logger
from .map_shumate import SHUMATE_OK, MapShumateMixin
from .map_webkit import WEBKIT_OK, MapWebKitMixin
from .map_services import (
    MAP_ICONS,
    MAP_LABEL_KEYS,
    MAP_TYPES,
    format_distance,
    bab_fetch_all,
    format_duration,
    geocode,
    haversine,
    maneuver_icon,
    maneuver_text_key,
    osrm_route,
    resolve_route_points,
)

log = get_logger(__name__)

# ── MapPage widget ────────────────────────────────────────────────────────────

class MapPage(MapWebKitMixin, MapShumateMixin, Gtk.Box):
    """OpenStreetMap navigation page — WebKit/MapLibre (3D) or Shumate (2D)."""
    __gtype_name__ = "MapPage"

    def __init__(
        self,
        language: str = SOURCE_LANGUAGE,
        force_webkit: bool = False,
        poi_visible: bool = False,
        traffic_visible: bool = False,
        on_poi_visible_changed: Callable[[bool], None] | None = None,
        on_traffic_visible_changed: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_halign(Gtk.Align.FILL)
        self.set_valign(Gtk.Align.FILL)
        self.language = _normalize_language(language)
        self.force_webkit = force_webkit
        self._on_poi_visible_changed = on_poi_visible_changed
        self._on_traffic_visible_changed = on_traffic_visible_changed

        self._gps_lat: float | None = None
        self._gps_lon: float | None = None
        self._gps_heading: float = 0.0
        self._follow_gps: bool = True
        self._last_map_js: float = 0.0   # throttle: last time mapSetCar was sent
        # Route coords [[lon, lat], ...] — kept for traffic proximity filtering
        self._route_coords: list[list[float]] = []
        self._map_type_idx: int = 0
        self._routing_mode: str = "car"
        self._start_coord: tuple[float, float] | None = None
        self._end_coord: tuple[float, float] | None = None
        self._tour_active: bool = False
        self._tour_steps: list[dict] = []
        self._tour_step_idx: int = 0
        self._dnd_src_idx: int = -1

        # Maneuver overlay widgets
        self._maneuver_overlay: Gtk.Box | None = None
        self._maneuver_icon: Gtk.Image | None = None
        self._maneuver_distance_lbl: Gtk.Label | None = None
        self._maneuver_instr_lbl: Gtk.Label | None = None

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
        self._guide_path_layer: Any = None
        self._wp_layer: Any = None
        self._sources: dict[str, Any] = {}
        self._setting_pos: bool = False

        # FAB buttons (None when backend unavailable)
        self._follow_btn: Gtk.ToggleButton | None = None
        self._center_btn: Gtk.Button | None = None
        self._layer_btn: Gtk.Button | None = None
        self._traffic_btn: Gtk.ToggleButton | None = None
        self._tour_start_btn: Gtk.Button | None = None
        self._tour_start_lbl: Gtk.Label | None = None
        self._tour_btn_icon: Gtk.Image | None = None

        # Traffic layer (Shumate only)
        self._traffic_layer: Any = None
        self._traffic_loaded: bool = False

        # POI layer
        self._poi_btn: Gtk.ToggleButton | None = None
        self._poi_layer: Any = None
        self._poi_visible: bool = bool(poi_visible)
        self._traffic_visible: bool = bool(traffic_visible)

        # Entry rows: flat list of (row_box, entry, remove_btn)
        self._entry_rows: list[tuple[Gtk.Box, Gtk.Entry, Gtk.Button]] = []
        self._entries_container: Gtk.Box | None = None
        self._search_bar: Gtk.Box | None = None

        self._build_search_bar()
        self._build_map()

        self.connect("map", self._on_mapped)

    def _on_mapped(self, _widget: Any) -> None:
        GLib.idle_add(self._drop_focus)

    def _apply_initial_overlay_state(self) -> None:
        """Sync POI/traffic visibility from settings to the active backend."""
        if self._backend == "webkit":
            poi = "true" if self._poi_visible else "false"
            traffic = "true" if self._traffic_visible else "false"
            self._js(f"mapSetPoiVisible({poi})")
            self._js(f"mapSetTrafficVisible({traffic})")
        elif self._backend == "shumate":
            self._shumate_set_poi_visible(self._poi_visible)
            self._shumate_set_traffic_visible(self._traffic_visible)
        if self._traffic_visible and not self._traffic_loaded:
            self._traffic_loaded = True
            self._status_lbl.set_text(_translate(self.language, "map.traffic.loading"))
            threading.Thread(target=self._load_traffic_thread, daemon=True).start()

    def _on_webview_load_changed(self, wv: Any, load_event: Any) -> None:
        super()._on_webview_load_changed(wv, load_event)
        if int(load_event) == 3:
            GLib.timeout_add(200, self._apply_initial_overlay_state_after_load)

    def _apply_initial_overlay_state_after_load(self) -> bool:
        self._apply_initial_overlay_state()
        return False

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

        # Action row: [route-btn] [status]
        action = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        self._route_btn = Gtk.Button()
        self._route_btn.set_label(_translate(self.language, "map.route"))
        self._route_btn.add_css_class("suggested-action")
        self._route_btn.connect("clicked", self._on_route_clicked)

        self._status_lbl = Gtk.Label(label="")
        self._status_lbl.add_css_class("dim-label")
        self._status_lbl.set_hexpand(True)
        self._status_lbl.set_halign(Gtk.Align.START)

        for w in (self._status_lbl, self._route_btn):
            action.append(w)
        bar.append(action)
        self.append(bar)

    def _make_entry_row(self) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)

        # Drag handle
        handle = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
        handle.add_css_class("dim-label")
        handle.set_cursor(Gdk.Cursor.new_from_name("grab"))
        handle.set_margin_start(2)
        handle.set_margin_end(2)

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

        row.append(handle)
        row.append(entry)
        row.append(add_btn)
        row.append(rem_btn)

        # DnD: drag source on handle
        drag_src = Gtk.DragSource.new()
        drag_src.set_actions(Gdk.DragAction.MOVE)
        drag_src.connect("prepare", lambda src, x, y, r=row: self._drag_prepare(src, x, y, r))
        handle.add_controller(drag_src)

        # DnD: drop target on the whole row
        drop_tgt = Gtk.DropTarget.new(GObject.TYPE_INT, Gdk.DragAction.MOVE)
        drop_tgt.connect("drop", lambda tgt, val, x, y, r=row: self._drag_drop(tgt, val, x, y, r))
        drop_tgt.connect("motion", lambda tgt, x, y: Gdk.DragAction.MOVE)
        row.add_controller(drop_tgt)

        self._entry_rows.append((row, entry, rem_btn))
        return row

    def _drag_prepare(
        self, _src: Gtk.DragSource, _x: float, _y: float, row: Gtk.Box
    ) -> Gdk.ContentProvider | None:
        idx = next((i for i, (r, _, __) in enumerate(self._entry_rows) if r is row), -1)
        if idx < 0:
            return None
        self._dnd_src_idx = idx
        gval = GObject.Value()
        gval.init(GObject.TYPE_INT)
        gval.set_int(idx)
        return Gdk.ContentProvider.new_for_value(gval)

    def _drag_drop(
        self, _tgt: Gtk.DropTarget, _val: Any, _x: float, _y: float, dst_row: Gtk.Box
    ) -> bool:
        src_idx = self._dnd_src_idx
        dst_idx = next(
            (i for i, (r, _, __) in enumerate(self._entry_rows) if r is dst_row), -1
        )
        if src_idx < 0 or dst_idx < 0 or src_idx == dst_idx:
            return False
        self._reorder_row(src_idx, dst_idx)
        return True

    def _reorder_row(self, src_idx: int, dst_idx: int) -> None:
        triple = self._entry_rows.pop(src_idx)
        self._entry_rows.insert(dst_idx, triple)
        row_widget = triple[0]
        if self._entries_container is not None:
            self._entries_container.remove(row_widget)
            if dst_idx == 0:
                self._entries_container.prepend(row_widget)
            else:
                prev_sibling = self._entry_rows[dst_idx - 1][0]
                self._entries_container.insert_child_after(row_widget, prev_sibling)
        self._update_placeholders()

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
        overlay.set_halign(Gtk.Align.FILL)
        overlay.set_valign(Gtk.Align.FILL)

        if self.force_webkit and WEBKIT_OK:
            self._backend = "webkit"
            content = self._setup_webview()
        elif SHUMATE_OK:
            self._backend = "shumate"
            content = self._setup_shumate()
        elif WEBKIT_OK:
            self._backend = "webkit"
            content = self._setup_webview()
        else:
            self._backend = "none"
            content = self._build_placeholder()

        overlay.set_child(content)
        content.set_hexpand(True)
        content.set_vexpand(True)
        content.set_halign(Gtk.Align.FILL)
        content.set_valign(Gtk.Align.FILL)

        if self._backend != "none":
            overlay.add_overlay(self._build_fab())
            overlay.add_overlay(self._build_tour_start_btn())
            overlay.add_overlay(self._build_maneuver_overlay())

        self.append(overlay)

        if self._backend == "shumate":
            self._apply_initial_overlay_state()

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

        self._poi_btn = Gtk.ToggleButton(icon_name="mark-location-symbolic")
        self._poi_btn.add_css_class("circular")
        self._poi_btn.add_css_class("osd")
        self._poi_btn.set_active(self._poi_visible)
        self._poi_btn.set_tooltip_text(_translate(self.language, "map.poi"))
        self._poi_btn.connect("toggled", self._on_poi_toggled)

        self._traffic_btn = Gtk.ToggleButton(icon_name="emblem-important-symbolic")
        self._traffic_btn.add_css_class("circular")
        self._traffic_btn.add_css_class("osd")
        self._traffic_btn.set_active(self._traffic_visible)
        self._traffic_btn.set_tooltip_text(_translate(self.language, "map.traffic"))
        self._traffic_btn.connect("toggled", self._on_traffic_toggled)

        self._layer_btn = Gtk.Button(icon_name="dialog-layers-symbolic")
        self._layer_btn.add_css_class("circular")
        self._layer_btn.add_css_class("osd")
        self._layer_btn.set_tooltip_text(_translate(self.language, MAP_LABEL_KEYS["map"]))
        self._layer_btn.connect("clicked", self._on_layer_clicked)

        self._center_btn = Gtk.Button(icon_name="find-location-symbolic")
        self._center_btn.add_css_class("circular")
        self._center_btn.add_css_class("osd")
        self._center_btn.set_tooltip_text(_translate(self.language, "map.center"))
        self._center_btn.connect("clicked", self._on_center_clicked)

        fab.append(self._poi_btn)
        fab.append(self._traffic_btn)
        fab.append(self._layer_btn)
        fab.append(self._center_btn)
        return fab

    def _build_maneuver_overlay(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_halign(Gtk.Align.CENTER)
        outer.set_valign(Gtk.Align.START)
        outer.set_margin_top(48)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        outer.set_can_target(False)
        outer.set_visible(False)

        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        card.add_css_class("osd")
        card.add_css_class("card")
        card.set_margin_top(0)

        self._maneuver_icon = Gtk.Image.new_from_icon_name("go-up-symbolic")
        self._maneuver_icon.set_pixel_size(56)
        self._maneuver_icon.set_margin_start(14)
        self._maneuver_icon.set_margin_end(4)
        self._maneuver_icon.set_margin_top(10)
        self._maneuver_icon.set_margin_bottom(10)
        card.append(self._maneuver_icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_valign(Gtk.Align.CENTER)
        text_box.set_margin_end(16)
        text_box.set_margin_top(8)
        text_box.set_margin_bottom(8)

        self._maneuver_distance_lbl = Gtk.Label(label="")
        self._maneuver_distance_lbl.add_css_class("title-2")
        self._maneuver_distance_lbl.set_halign(Gtk.Align.START)

        self._maneuver_instr_lbl = Gtk.Label(label="")
        self._maneuver_instr_lbl.set_halign(Gtk.Align.START)
        self._maneuver_instr_lbl.set_max_width_chars(32)
        self._maneuver_instr_lbl.set_wrap(True)

        text_box.append(self._maneuver_distance_lbl)
        text_box.append(self._maneuver_instr_lbl)
        card.append(text_box)

        outer.append(card)
        self._maneuver_overlay = outer
        return outer

    def _build_tour_start_btn(self) -> Gtk.Widget:
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._tour_btn_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        inner.append(self._tour_btn_icon)
        self._tour_start_lbl = Gtk.Label(label=_translate(self.language, "map.tour_start"))
        inner.append(self._tour_start_lbl)

        self._tour_start_btn = Gtk.Button()
        self._tour_start_btn.set_child(inner)
        self._tour_start_btn.add_css_class("osd")
        self._tour_start_btn.set_halign(Gtk.Align.START)
        self._tour_start_btn.set_valign(Gtk.Align.START)
        self._tour_start_btn.set_margin_start(12)
        self._tour_start_btn.set_margin_top(12)
        self._tour_start_btn.set_visible(False)
        self._tour_start_btn.connect("clicked", self._on_tour_start_clicked)
        return self._tour_start_btn

    def _on_tour_start_clicked(self, _btn: Gtk.Button) -> None:
        if self._start_coord is None:
            return
        if self._tour_active:
            self._stop_tour()
            return
        lat, lon = self._start_coord
        self._tour_active = True
        self._tour_step_idx = 0
        self._update_maneuver_overlay()
        if self._tour_start_lbl is not None:
            self._tour_start_lbl.set_label(_translate(self.language, "map.tour_stop"))
        if self._tour_btn_icon is not None:
            self._tour_btn_icon.set_from_icon_name("media-playback-stop-symbolic")
        self._set_follow(True)
        if self._backend == "webkit":
            self._js("mapSetTourActive(true)")
        if self._gps_lat is not None and self._gps_lon is not None:
            dist = haversine(self._gps_lat, self._gps_lon, lat, lon)
            if dist > 200:
                gps_lat, gps_lon = self._gps_lat, self._gps_lon
                threading.Thread(
                    target=self._fetch_guide_to_start,
                    args=(gps_lat, gps_lon, lat, lon),
                    daemon=True,
                ).start()

    def _stop_tour(self) -> None:
        self._tour_active = False
        if self._tour_start_lbl is not None:
            self._tour_start_lbl.set_label(_translate(self.language, "map.tour_start"))
        if self._tour_btn_icon is not None:
            self._tour_btn_icon.set_from_icon_name("media-playback-start-symbolic")
        if self._backend == "webkit":
            self._js("mapSetTourActive(false)")
            self._js("mapClearGuideToStart()")
        elif self._guide_path_layer is not None:
            self._guide_path_layer.remove_all()
        if self._maneuver_overlay is not None:
            self._maneuver_overlay.set_visible(False)

    def _fetch_guide_to_start(
        self, gps_lat: float, gps_lon: float, start_lat: float, start_lon: float
    ) -> None:
        result = osrm_route([(gps_lat, gps_lon), (start_lat, start_lon)], self._routing_mode)
        GLib.idle_add(self._guide_result, result)

    def _guide_result(
        self,
        result: tuple[list[list[float]], float, float, list[dict]] | None,
    ) -> bool:
        if result is None:
            return False
        coords = result[0]
        if self._backend == "webkit":
            self._js(f"mapSetGuideToStart({json.dumps(coords)})")
        elif self._shumate_map is not None and self._guide_path_layer is not None:
            self._shumate_set_guide(coords)
        return False

    # ── GPS position updates ──────────────────────────────────────────────────

    # Max rate at which position updates are pushed to the map renderer (seconds)
    _MAP_JS_INTERVAL = 0.25  # 4 Hz

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

        # During an active tour always re-engage follow so the map tracks the driver.
        if self._tour_active and not self._follow_gps:
            self._set_follow(True)

        if self._backend == "webkit":
            now = time.monotonic()
            if now - self._last_map_js >= self._MAP_JS_INTERVAL:
                self._last_map_js = now
                self._js(f"mapSetCar({lat}, {lon}, {self._gps_heading})")
        elif self._backend == "shumate" and self._shumate_map is not None:
            self._update_shumate_gps(lat, lon)
            if self._follow_gps:
                self._goto(lat, lon)

        if self._tour_active:
            self._update_maneuver_overlay()

    # Advance step when the user gets within this many meters of its maneuver point.
    _MANEUVER_ADVANCE_M = 25.0

    def _update_maneuver_overlay(self) -> None:
        if self._maneuver_overlay is None:
            return
        if (
            not self._tour_active
            or not self._tour_steps
            or self._gps_lat is None
            or self._gps_lon is None
        ):
            self._maneuver_overlay.set_visible(False)
            return

        # Advance past "depart" and any maneuver we've already reached.
        while self._tour_step_idx < len(self._tour_steps) - 1:
            cur = self._tour_steps[self._tour_step_idx]
            if cur.get("type") == "depart":
                self._tour_step_idx += 1
                continue
            d_cur = haversine(self._gps_lat, self._gps_lon, cur["lat"], cur["lon"])
            if d_cur <= self._MANEUVER_ADVANCE_M:
                self._tour_step_idx += 1
                continue
            break

        step = self._tour_steps[self._tour_step_idx]
        distance_m = haversine(self._gps_lat, self._gps_lon, step["lat"], step["lon"])
        m_type = step.get("type", "")
        m_modifier = step.get("modifier", "")
        name = step.get("name", "") or ""

        icon = maneuver_icon(m_type, m_modifier)
        text = _translate(self.language, maneuver_text_key(m_type, m_modifier))
        if name and m_type not in {"arrive", "depart"}:
            text += _translate(self.language, "map.maneuver.on_street").format(name=name)

        if self._maneuver_icon is not None:
            self._maneuver_icon.set_from_icon_name(icon)
        if self._maneuver_distance_lbl is not None:
            self._maneuver_distance_lbl.set_text(format_distance(distance_m))
        if self._maneuver_instr_lbl is not None:
            self._maneuver_instr_lbl.set_text(text)
        self._maneuver_overlay.set_visible(True)

    def _goto(self, lat: float, lon: float) -> None:
        if self._backend == "webkit":
            self._js(f"mapSetCar({lat}, {lon}, {self._gps_heading})")
        elif self._shumate_map is not None:
            self._setting_pos = True
            self._shumate_map.get_viewport().set_location(lat, lon)
            self._setting_pos = False

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
        self._set_follow(True)
        if self._backend == "webkit":
            self._js(f"mapGoTo({self._gps_lat}, {self._gps_lon}, 17)")
        elif self._shumate_map is not None:
            viewport = self._shumate_map.get_viewport()
            self._setting_pos = True
            viewport.set_location(self._gps_lat, self._gps_lon)
            viewport.set_zoom_level(17.0)
            self._setting_pos = False

    def _on_layer_clicked(self, _btn: Gtk.Button) -> None:
        self._map_type_idx = (self._map_type_idx + 1) % len(MAP_TYPES)
        layer = MAP_TYPES[self._map_type_idx]
        if self._backend == "webkit":
            self._js(f"mapSetStyle('{layer}')")
        elif self._shumate_map is not None:
            self._shumate_map.set_map_source(self._sources[layer])
        if self._layer_btn is not None:
            self._layer_btn.set_icon_name(MAP_ICONS.get(layer, "map-symbolic"))
            self._layer_btn.set_tooltip_text(_translate(self.language, MAP_LABEL_KEYS[layer]))

    # ── Traffic layer (Bundesautobahn API) ────────────────────────────────────

    def _on_traffic_toggled(self, btn: Gtk.ToggleButton) -> None:
        visible = btn.get_active()
        self._traffic_visible = visible
        if self._backend == "webkit":
            val = "true" if visible else "false"
            self._js(f"mapSetTrafficVisible({val})")
        else:
            self._shumate_set_traffic_visible(visible)
        if visible and not self._traffic_loaded:
            self._traffic_loaded = True
            self._status_lbl.set_text(_translate(self.language, "map.traffic.loading"))
            threading.Thread(target=self._load_traffic_thread, daemon=True).start()
        if self._on_traffic_visible_changed is not None:
            self._on_traffic_visible_changed(visible)

    def _load_traffic_thread(self) -> None:
        items = bab_fetch_all()
        GLib.idle_add(self._show_traffic, items)

    def _parse_traffic_items(
        self, items: list[dict]
    ) -> list[tuple[float, float, str, str]]:
        result = []
        for item in items:
            point = item.get("point") or ""
            try:
                parts = point.split(",")
                lat = float(parts[0].strip())
                lon = float(parts[1].strip())
            except (ValueError, IndexError):
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

        if self._backend == "webkit":
            # WebKit filters by route bounding box inside JS (mapSetTraffic).
            js_items = [
                {"lat": lat, "lon": lon, "kind": kind, "title": title}
                for lat, lon, kind, title in parsed
            ]
            self._js(f"mapSetTraffic({json.dumps(js_items)})")
            if self._traffic_btn is not None and self._traffic_btn.get_active():
                self._js("mapSetTrafficVisible(true)")
        else:
            filtered = self._filter_traffic_by_route(parsed)
            self._shumate_show_traffic(filtered)

        if self._traffic_btn is not None and self._traffic_btn.get_active():
            self._status_lbl.set_text(
                _translate(self.language, "map.traffic.count").format(count=len(parsed))
            )
        return False

    def _filter_traffic_by_route(
        self, items: list[tuple[float, float, str, str]]
    ) -> list[tuple[float, float, str, str]]:
        """Keep only items within ~5 km of the route bounding box."""
        if not self._route_coords:
            return []
        lats = [c[1] for c in self._route_coords]
        lons = [c[0] for c in self._route_coords]
        pad = 0.05  # ~5 km
        min_lat, max_lat = min(lats) - pad, max(lats) + pad
        min_lon, max_lon = min(lons) - pad, max(lons) + pad
        return [
            item for item in items
            if min_lat <= item[0] <= max_lat and min_lon <= item[1] <= max_lon
        ]

    # ── POI layer (Overpass API) ──────────────────────────────────────────────

    def _on_poi_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._poi_visible = btn.get_active()
        if self._backend == "webkit":
            val = "true" if self._poi_visible else "false"
            self._js(f"mapSetPoiVisible({val})")
        else:
            self._shumate_set_poi_visible(self._poi_visible)
        if self._on_poi_visible_changed is not None:
            self._on_poi_visible_changed(self._poi_visible)

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
        self._start_coord = None
        self._end_coord = None
        self._tour_active = False
        self._tour_steps = []
        self._tour_step_idx = 0
        if self._maneuver_overlay is not None:
            self._maneuver_overlay.set_visible(False)
        if self._tour_start_lbl is not None:
            self._tour_start_lbl.set_label(_translate(self.language, "map.tour_start"))
        if self._tour_btn_icon is not None:
            self._tour_btn_icon.set_from_icon_name("media-playback-start-symbolic")
        if self._tour_start_btn is not None:
            self._tour_start_btn.set_visible(False)
        if self._backend == "webkit":
            self._js("mapClearRoute()")
        else:
            self._shumate_clear_route_layers()

    def set_nav_visible(self, visible: bool) -> None:
        if self._search_bar is not None:
            self._search_bar.set_visible(visible)

    def _compute_route(self, start_text: str, wp_texts: list[str], end_text: str) -> None:
        try:
            gps = (
                (self._gps_lat, self._gps_lon)
                if self._gps_lat is not None and self._gps_lon is not None
                else None
            )
            all_points = resolve_route_points(start_text, wp_texts, end_text, gps, geocode)
            if all_points is None:
                GLib.idle_add(self._route_error)
                return
            result = osrm_route(all_points, self._routing_mode)
        except Exception:
            log.exception("Could not compute map route")
            GLib.idle_add(self._route_error)
            return
        GLib.idle_add(self._route_result, all_points, result)

    def _route_error(self) -> bool:
        self._status_lbl.set_text(_translate(self.language, "map.routing.error"))
        self._route_btn.set_sensitive(True)
        return False

    def _route_result(
        self,
        all_points: list[tuple[float, float]],
        result: tuple[list[list[float]], float, float, list[dict]] | None,
    ) -> bool:
        self._route_btn.set_sensitive(True)
        if result is None:
            self._status_lbl.set_text(_translate(self.language, "map.routing.error"))
            return False

        coords, duration_s, distance_m, steps = result
        self._tour_steps = steps
        self._tour_step_idx = 0
        self._start_coord = all_points[0]
        self._end_coord = all_points[-1]
        prefix = _translate(self.language, "map.duration_prefix")
        distance_prefix = _translate(self.language, "map.distance_prefix")
        self._status_lbl.set_text(
            f"{prefix}{format_duration(duration_s)} / "
            f"{distance_prefix}{format_distance(distance_m)}"
        )
        if self._tour_start_btn is not None:
            self._tour_start_btn.set_visible(True)

        if coords:
            lats = [c[1] for c in coords]
            lons = [c[0] for c in coords]
            self._set_follow(False)

        self._route_coords = coords  # store for traffic proximity filter

        if self._backend == "webkit":
            self._js(f"mapSetRoute({json.dumps(coords)})")
            pts_js = json.dumps([[p[0], p[1]] for p in all_points])
            self._js(f"mapSetWaypoints({pts_js})")
            if coords:
                min_lat, max_lat = min(lats), max(lats)
                min_lon, max_lon = min(lons), max(lons)
                self._js(f"mapFitBounds({min_lat},{min_lon},{max_lat},{max_lon})")
        elif self._shumate_map is not None:
            self._shumate_show_route(all_points, coords)

        return False

    # ── Language ──────────────────────────────────────────────────────────────

    def set_language(self, language: str) -> None:
        self.language = _normalize_language(language)
        self._update_placeholders()
        self._route_btn.set_label(_translate(self.language, "map.route"))
        layer = MAP_TYPES[self._map_type_idx]
        if self._layer_btn is not None:
            self._layer_btn.set_tooltip_text(_translate(self.language, MAP_LABEL_KEYS[layer]))
        if self._follow_btn is not None:
            self._follow_btn.set_tooltip_text(_translate(self.language, "map.follow"))
        if self._center_btn is not None:
            self._center_btn.set_tooltip_text(_translate(self.language, "map.center"))
        if self._traffic_btn is not None:
            self._traffic_btn.set_tooltip_text(_translate(self.language, "map.traffic"))
        if self._tour_start_lbl is not None:
            self._tour_start_lbl.set_label(_translate(self.language, "map.tour_start"))
