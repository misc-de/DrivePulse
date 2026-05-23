"""Map page UI construction mixin — _build_* methods, CSS, step list."""
from __future__ import annotations

import math
import time as _time
from typing import Any

from gi.repository import Adw, Gdk, GLib, GObject, Gtk

from .common import _translate
from .map_services import (
    MAP_ICONS,
    MAP_LABEL_KEYS,
    MAP_TYPES,
    format_distance,
    maneuver_icon,
    maneuver_text_key,
)

# Inline CSS for the in-tour navigation banner.  Adwaita's ".osd"/".card" classes
# on a Box don't reliably paint a dark translucent background under the labels —
# we inject our own so the white text always reads against the map underneath.
_MANEUVER_CSS = b"""
.dp-maneuver-banner {
  background-color: rgba(20, 24, 32, 0.82);
  color: #ffffff;
  border-radius: 18px;
  padding: 16px 26px;
  box-shadow: 0 6px 16px rgba(0, 0, 0, 0.40);
}
.dp-maneuver-banner label { color: #ffffff; }
/* Symbolic icons recolor via the widget's CSS color - tint the arrows light
   blue so they pop against the dark banner without inheriting the label white. */
.dp-maneuver-banner image { color: #8FCFFF; }
.dp-maneuver-banner .dp-maneuver-distance {
  font-size: 32px;
  font-weight: 800;
}
.dp-maneuver-banner .dp-maneuver-instr {
  font-size: 20px;
  font-weight: 500;
  opacity: 0.95;
}
.dp-map-state {
  background-color: rgba(50, 50, 50, 0.80);
  color: #ffffff;
  border-radius: 8px;
  padding: 6px 10px;
  font-family: monospace;
  font-size: 13px;
}
.dp-map-state label { color: #ffffff; }
.dp-steps-panel {
  background-color: rgba(20, 24, 32, 0.82);
  border-radius: 14px;
  padding: 6px;
  box-shadow: 0 6px 16px rgba(0, 0, 0, 0.40);
}
.dp-steps-panel, .dp-steps-panel label { color: #f5f7fa; }
.dp-steps-panel list,
.dp-steps-panel list > row { background: transparent; }
.dp-steps-row { padding: 8px 10px; border-radius: 10px; }
.dp-steps-row image { color: #B6DEFF; }
.dp-steps-row-active { background-color: rgba(143, 207, 255, 0.30); }
.dp-steps-row-active label { color: #ffffff; }
.dp-steps-row-done { opacity: 0.65; }
.dp-steps-distance { font-weight: 700; color: #ffffff; }
.dp-steps-instr { opacity: 1.0; }
.dark .dp-steps-panel {
  background-color: rgba(8, 10, 14, 0.96);
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
}
.dp-tour-topnav { padding: 2px 4px; }
.dp-tour-topnav button label { font-size: 11px; }
/* Lane guidance row inside the maneuver banner */
.dp-lane-row { padding: 4px 12px 2px 12px; }
.dp-lane {
  border-radius: 8px;
  padding: 6px 8px;
  min-width: 42px;
  min-height: 42px;
}
.dp-lane image { color: rgba(255,255,255,0.28); }
.dp-lane-valid {
  background-color: rgba(30, 136, 229, 0.55);
}
.dp-lane-valid image { color: #ffffff; }
/* Speed-limit sign - classic European round white/red circle */
.dp-speed-sign {
  background-color: #ffffff;
  border: 6px solid #cc0000;
  border-radius: 9999px;
  min-width: 88px;
  min-height: 88px;
  box-shadow: 0 3px 10px rgba(0,0,0,0.45);
}
.dp-speed-sign label {
  color: #111111;
  font-size: 27px;
  font-weight: 900;
}
"""
_maneuver_css_installed = False


def _install_maneuver_css() -> None:
    global _maneuver_css_installed
    if _maneuver_css_installed:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_MANEUVER_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _maneuver_css_installed = True


class MapLayoutMixin:
    """Map page UI construction — _build_* methods, CSS, search bar, step list."""

    def _build_tour_topnav(self) -> None:
        _install_maneuver_css()
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        bar.add_css_class("dp-tour-topnav")
        bar.set_margin_start(4)
        bar.set_margin_end(4)
        bar.set_margin_top(4)
        bar.set_margin_bottom(4)
        self._tour_topnav = bar

        def _child(icon_name: str, label_key: str) -> Gtk.Box:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_halign(Gtk.Align.CENTER)
            img = Gtk.Image.new_from_icon_name(icon_name)
            img.set_pixel_size(22)
            lbl = Gtk.Label(label=_translate(self.language, label_key))
            lbl.add_css_class("caption")
            box.append(img)
            box.append(lbl)
            return box

        load_btn = Gtk.Button()
        load_btn.set_child(_child("document-open-symbolic", "map.topnav.load"))
        load_btn.add_css_class("flat")
        load_btn.set_hexpand(True)
        load_btn.connect("clicked", self._on_tour_load_clicked)
        self._tour_load_btn = load_btn

        plan_btn = Gtk.ToggleButton()
        plan_btn.set_child(_child("dp-tour-plan-symbolic", "map.topnav.plan"))
        plan_btn.add_css_class("flat")
        plan_btn.set_hexpand(True)
        plan_btn.connect("toggled", self._on_tour_plan_toggled)
        self._tour_plan_btn = plan_btn

        save_btn = Gtk.Button()
        save_btn.set_child(_child("document-save-symbolic", "map.topnav.save"))
        save_btn.add_css_class("flat")
        save_btn.set_hexpand(True)
        save_btn.connect("clicked", self._on_tour_save_clicked)
        save_btn.set_visible(False)
        self._tour_save_btn = save_btn

        history_btn = Gtk.Button()
        history_btn.set_child(_child("document-open-recent-symbolic", "map.topnav.history"))
        history_btn.add_css_class("flat")
        history_btn.set_hexpand(True)
        history_btn.connect("clicked", self._on_tour_history_clicked)
        self._tour_history_btn = history_btn

        # "Letzte Touren" sits on the far left as a view-only entry point;
        # the tour-planning actions (load / plan / save) follow on the right.
        for btn in (history_btn, load_btn, plan_btn, save_btn):
            bar.append(btn)

        self._map_content_box.append(bar)

    def _on_tour_plan_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._tour_plan_active = btn.get_active()
        if self._search_bar is not None:
            self._search_bar.set_visible(self._tour_plan_active)
        if self._tour_plan_active:
            # Drop any trip-replay polyline / overlays so the planning UI starts clean.
            self._clear_replay_overlays()
        GLib.idle_add(self._nudge_map_resize)

    def _on_tour_load_clicked(self, _btn: object) -> None:
        nav_view = getattr(self, "_nav_view", None)
        if nav_view is None:
            return
        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        page_box.set_hexpand(True)
        page_box.set_vexpand(True)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("navigation-sidebar")
        scrolled.set_child(listbox)
        page_box.append(scrolled)

        self._tour_listbox = listbox

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(Adw.HeaderBar())
        toolbar_view.set_content(page_box)

        page = Adw.NavigationPage(title=_translate(self.language, "map.topnav.load"))
        page.set_child(toolbar_view)
        nav_view.push(page)
        self._rebuild_tour_list()

    # Pull this many history rows per fetch. Small enough to keep the first
    # render snappy, large enough that you don't trigger pagination on every
    # tiny scroll.
    _TOUR_HISTORY_PAGE_SIZE = 30

    def _on_tour_history_clicked(self, _btn: object) -> None:
        nav_view = getattr(self, "_nav_view", None)
        if nav_view is None:
            return

        self._tour_history_offset = 0
        self._tour_history_loading = False
        self._tour_history_exhausted = False
        self._tour_history_empty_row: Gtk.ListBoxRow | None = None

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        listbox.set_valign(Gtk.Align.START)
        self._tour_history_listbox = listbox

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        inner.set_margin_top(12)
        inner.set_margin_bottom(12)
        inner.set_margin_start(12)
        inner.set_margin_end(12)
        inner.append(listbox)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        scrolled.set_child(inner)
        scrolled.connect("edge-reached", self._on_tour_history_edge_reached)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(Adw.HeaderBar())
        toolbar_view.set_content(scrolled)

        page = Adw.NavigationPage(title=_translate(self.language, "map.topnav.history"))
        page.set_child(toolbar_view)
        nav_view.push(page)

        self._load_more_tour_history()

    def _on_tour_history_edge_reached(
        self, _sw: Gtk.ScrolledWindow, pos: Gtk.PositionType
    ) -> None:
        if pos == Gtk.PositionType.BOTTOM:
            self._load_more_tour_history()

    def _load_more_tour_history(self) -> None:
        if getattr(self, "_tour_history_loading", False):
            return
        if getattr(self, "_tour_history_exhausted", False):
            return
        db = getattr(self, "_map_db", None)
        if db is None:
            return

        self._tour_history_loading = True
        try:
            page_size = self._TOUR_HISTORY_PAGE_SIZE
            rows = db.list_tour_history(page_size, self._tour_history_offset)
            for row in rows:
                self._append_tour_history_row(row)
            if len(rows) < page_size:
                self._tour_history_exhausted = True
            self._tour_history_offset += len(rows)
            if self._tour_history_offset == 0:
                self._show_tour_history_empty()
        finally:
            self._tour_history_loading = False

    def _show_tour_history_empty(self) -> None:
        listbox = getattr(self, "_tour_history_listbox", None)
        if listbox is None or self._tour_history_empty_row is not None:
            return
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)
        lbl = Gtk.Label(label=_translate(self.language, "map.history.empty"))
        lbl.add_css_class("dim-label")
        lbl.set_margin_top(18)
        lbl.set_margin_bottom(18)
        lbl.set_wrap(True)
        row.set_child(lbl)
        listbox.append(row)
        self._tour_history_empty_row = row

    @staticmethod
    def _format_history_ts(ts: str) -> str:
        if not ts:
            return ""
        # ISO timestamps stored as "2026-05-23T07:32:11+00:00" — keep the
        # date+time portion, drop the seconds/timezone for the row label.
        return ts[:16].replace("T", " ")

    @staticmethod
    def _format_history_duration(seconds: float | None) -> str:
        if not seconds or seconds <= 0:
            return ""
        s = int(seconds)
        h, rem = divmod(s, 3600)
        m, _ = divmod(rem, 60)
        if h > 0:
            return f"{h}h {m:02d}min"
        return f"{m}min"

    def _format_history_title(self, row: Any) -> str:
        kind = row["kind"]
        if kind == "tour":
            return row["trip_label"] or _translate(self.language, "map.history.kind_tour")
        # trip
        car_label = row["car_label"] or row["car_brand"] or ""
        if not car_label and row["car_vin"]:
            car_label = f"VIN …{row['car_vin'][-5:]}"
        trip_label = row["trip_label"] or ""
        if car_label and trip_label:
            return f"{car_label} · {trip_label}"
        return car_label or trip_label or _translate(self.language, "map.history.kind_trip")

    def _format_history_subtitle(self, row: Any) -> str:
        ts = self._format_history_ts(row["ts"] or "")
        kind = row["kind"]
        if kind == "tour":
            return ts
        parts = [ts] if ts else []
        dist = row["distance_km"]
        if dist:
            parts.append(f"{dist:.1f} km")
        dur = self._format_history_duration(row["duration_s"])
        if dur:
            parts.append(dur)
        return "  ·  ".join(parts)

    def _append_tour_history_row(self, data: Any) -> None:
        listbox = getattr(self, "_tour_history_listbox", None)
        if listbox is None:
            return
        action_row = Adw.ActionRow()
        action_row.set_title(GLib.markup_escape_text(self._format_history_title(data)))
        action_row.set_subtitle(GLib.markup_escape_text(self._format_history_subtitle(data)))
        icon_name = (
            "dp-tour-plan-symbolic" if data["kind"] == "tour" else "driving-symbolic"
        )
        icon = Gtk.Image.new_from_icon_name(icon_name)
        action_row.add_prefix(icon)
        chev = Gtk.Image.new_from_icon_name("go-next-symbolic")
        action_row.add_suffix(chev)
        action_row.set_activatable(True)
        # Snapshot the metadata so the click handler doesn't need to re-query.
        action_row._dp_history_meta = {
            "kind": data["kind"],
            "id": int(data["id"]),
            "ts": data["ts"],
            "distance_km": data["distance_km"],
            "duration_s": data["duration_s"],
            "trip_label": data["trip_label"],
            "car_brand": data["car_brand"],
            "car_label": data["car_label"],
            "car_vin": data["car_vin"],
        }
        action_row.connect("activated", self._on_history_row_activated)
        listbox.append(action_row)

    def _on_history_row_activated(self, row: Adw.ActionRow) -> None:
        meta = getattr(row, "_dp_history_meta", None)
        if not meta:
            return
        if meta["kind"] == "trip":
            self._show_trip_replay(meta)
        elif meta["kind"] == "tour":
            db = getattr(self, "_map_db", None)
            if db is None:
                return
            tour = db.get_saved_tour(int(meta["id"]))
            if tour is None:
                return
            # _load_saved_tour pops the nav view and routes via _on_route_clicked
            self._load_saved_tour(dict(tour))

    # ── Trip / tour replay overlays ───────────────────────────────────────────

    def _build_replay_info_overlay(self) -> Gtk.Widget:
        """Top-left card showing the replayed trip's metadata."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("osd")
        box.add_css_class("dp-replay-info")
        box.set_halign(Gtk.Align.START)
        box.set_valign(Gtk.Align.START)
        box.set_margin_start(12)
        # Default to the desktop margin; set_form_factor() pushes the card
        # further down on mobile where the top-left info button would
        # otherwise overlap the card.
        box.set_margin_top(12)
        box.set_visible(False)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._replay_title_lbl = Gtk.Label(xalign=0.0)
        self._replay_title_lbl.add_css_class("heading")
        self._replay_title_lbl.set_hexpand(True)
        head.append(self._replay_title_lbl)

        close_btn = Gtk.Button(icon_name="window-close-symbolic")
        close_btn.add_css_class("flat")
        close_btn.add_css_class("circular")
        close_btn.set_tooltip_text(_translate(self.language, "map.replay.close"))
        close_btn.connect("clicked", lambda _b: self._clear_replay_overlays())
        head.append(close_btn)
        box.append(head)

        self._replay_meta_grid = Gtk.Grid(column_spacing=12, row_spacing=2)
        box.append(self._replay_meta_grid)

        css = Gtk.CssProvider()
        css.load_from_data(
            b".dp-replay-info { border-radius: 10px; padding: 10px 14px; min-width: 220px; }"
        )
        box.connect(
            "realize",
            lambda w: Gtk.StyleContext.add_provider_for_display(
                w.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            ),
        )

        self._replay_info_overlay = box
        return box

    def _build_replay_chart_overlay(self) -> Gtk.Widget:
        """Bottom-left container for the speed chart shown during replay."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.add_css_class("osd")
        box.add_css_class("dp-replay-chart")
        box.set_halign(Gtk.Align.START)
        box.set_valign(Gtk.Align.END)
        box.set_margin_start(12)
        box.set_margin_bottom(12)
        box.set_size_request(340, -1)
        box.set_visible(False)

        css = Gtk.CssProvider()
        css.load_from_data(b".dp-replay-chart { border-radius: 10px; padding: 6px; }")
        box.connect(
            "realize",
            lambda w: Gtk.StyleContext.add_provider_for_display(
                w.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            ),
        )

        self._replay_chart_overlay = box
        self._replay_chart_widget: Gtk.Widget | None = None
        return box

    def _populate_replay_info(self, meta: dict, ended_at: str | None) -> None:
        """Fill the top-left info card with car + trip metadata."""
        grid = self._replay_meta_grid
        # Clear previous rows
        child = grid.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            grid.remove(child)
            child = nxt

        car_title = (
            meta.get("car_label")
            or meta.get("car_brand")
            or (f"VIN …{meta['car_vin'][-5:]}" if meta.get("car_vin") else "")
        )
        if meta["kind"] == "tour":
            car_title = meta.get("trip_label") or _translate(self.language, "map.history.kind_tour")
        self._replay_title_lbl.set_label(car_title or _translate(self.language, "map.history.kind_trip"))

        def _row(idx: int, key: str, value: str) -> None:
            k = Gtk.Label(label=_translate(self.language, key), xalign=0.0)
            k.add_css_class("dim-label")
            k.add_css_class("caption")
            v = Gtk.Label(label=value, xalign=0.0)
            v.add_css_class("caption")
            grid.attach(k, 0, idx, 1, 1)
            grid.attach(v, 1, idx, 1, 1)

        started_disp = self._format_history_ts(meta.get("ts") or "")
        ended_disp = self._format_history_ts(ended_at or "")
        _row(0, "map.replay.started", started_disp or "—")
        if ended_disp:
            _row(1, "map.replay.ended", ended_disp)
        dist = meta.get("distance_km")
        if dist:
            _row(2, "map.replay.distance", f"{dist:.1f} km")
        dur = self._format_history_duration(meta.get("duration_s"))
        if dur:
            _row(3, "map.replay.duration", dur)

    def _populate_replay_chart(self, samples: list) -> None:
        """Build a metric chart + dropdown for the replayed trip.

        The chart cursor stays in sync with a marker on the live map: scrubbing
        the chart moves a circle along the polyline so the user sees which
        point on the route corresponds to the highlighted value.
        """
        from .cars_trip_visuals import _build_chart_widget, build_trip_metric_data

        # Tear down anything from a previous replay.
        child = self._replay_chart_overlay.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._replay_chart_overlay.remove(child)
            child = nxt
        self._replay_chart_widget = None
        self._replay_chart_area: Gtk.Widget | None = None

        metric_data, avail = build_trip_metric_data(samples, self.language)
        if not avail:
            return

        # Default to speed if present, otherwise first available.
        def_key = "speed_kmh" if "speed_kmh" in metric_data else avail[0][0]
        def_entry = next(m for m in avail if m[0] == def_key)
        chart_state: dict[str, Any] = {
            "pts": metric_data[def_key],
            "unit": def_entry[2],
            "color": def_entry[3],
            "fmt": def_entry[4],
            "key": def_key,
        }
        cursor_state: dict[str, Any] = {"idx": -1}

        dropdown: Gtk.DropDown | None = None
        if len(avail) > 1:
            str_model = Gtk.StringList()
            for label in (m[1] for m in avail):
                str_model.append(label)
            dropdown = Gtk.DropDown.new(str_model, None)
            dropdown.set_halign(Gtk.Align.START)
            dropdown.set_valign(Gtk.Align.CENTER)
            dropdown.set_margin_start(4)
            dropdown.set_margin_bottom(5)
            init_sel = next(
                (i for i, m in enumerate(avail) if m[0] == def_key), 0
            )
            dropdown.set_selected(init_sel)
            self._replay_chart_overlay.append(dropdown)

        # When the chart cursor moves, update the marker on the live map at
        # the GPS coord of the highlighted sample.
        def _on_cursor_change() -> None:
            idx = cursor_state.get("idx", -1)
            pts = chart_state.get("pts") or []
            if 0 <= idx < len(pts):
                _ts, _val, clat, clon = pts[idx]
                if clat is not None and clon is not None:
                    self._map_set_replay_marker(clat, clon)
            else:
                self._map_clear_replay_marker()
            if self._replay_chart_area is not None:
                self._replay_chart_area.queue_draw()

        area = _build_chart_widget(chart_state, cursor_state, _on_cursor_change, height=140)
        self._replay_chart_area = area
        self._replay_chart_widget = area
        self._replay_chart_overlay.append(area)

        if dropdown is not None:

            def _on_metric_selected(dd: Gtk.DropDown, _pspec: Any) -> None:
                sel = dd.get_selected()
                if 0 <= sel < len(avail):
                    key, _lbl, unit, color, fmt = avail[sel]
                    chart_state["pts"] = metric_data[key]
                    chart_state["unit"] = unit
                    chart_state["color"] = color
                    chart_state["fmt"] = fmt
                    chart_state["key"] = key
                    cursor_state["idx"] = -1
                    self._map_clear_replay_marker()
                    if self._replay_chart_area is not None:
                        self._replay_chart_area.queue_draw()

            dropdown.connect("notify::selected", _on_metric_selected)

    def _map_set_replay_marker(self, lat: float, lon: float) -> None:
        if self._backend == "webkit":
            self._js(f"mapSetReplayMarker({lat},{lon})")
        elif getattr(self, "_shumate_map", None) is not None:
            self._shumate_set_replay_marker(lat, lon)

    def _map_clear_replay_marker(self) -> None:
        if self._backend == "webkit":
            self._js("mapClearReplayMarker()")
        elif getattr(self, "_shumate_map", None) is not None:
            self._shumate_clear_replay_marker()

    def _show_trip_replay(self, meta: dict) -> None:
        """Render a recorded trip's polyline + metadata on the live map."""
        db = getattr(self, "_map_db", None)
        if db is None:
            return
        nav_view = getattr(self, "_nav_view", None)
        if nav_view is not None:
            nav_view.pop()

        trip_id = int(meta["id"])
        samples = list(db.samples_for_trip(trip_id))
        # Drop samples without a real fix (lat=0, lon=0 means the receiver
        # hadn't acquired one yet) — otherwise the polyline shoots across the
        # globe to (0,0) and Shumate ends up centred on the Atlantic with
        # nothing in cache.
        latlon_speed: list[tuple[float, float, float | None]] = [
            (s["lat"], s["lon"], s["speed_kmh"]) for s in samples
            if s["lat"] is not None and s["lon"] is not None
            and not (s["lat"] == 0.0 and s["lon"] == 0.0)
        ]
        if not latlon_speed:
            return
        latlon = [(lat, lon) for lat, lon, _ in latlon_speed]

        # Get the trip row for the actual ended_at (history meta only has ts=started_at)
        try:
            trip_row = self._map_db._conn.execute(
                "SELECT ended_at FROM trips WHERE id=?", (trip_id,)
            ).fetchone()
            ended_at = trip_row["ended_at"] if trip_row else None
        except Exception:
            ended_at = None

        self._map_show_track(latlon_speed)
        self._populate_replay_info(meta, ended_at)
        self._populate_replay_chart(samples)
        self._replay_info_overlay.set_visible(True)
        if self._replay_chart_widget is not None:
            self._replay_chart_overlay.set_visible(True)
            # On shumate, the scale ruler sits in the same bottom-left
            # corner and gets hidden under the chart. Push it past the
            # chart's right edge so it stays visible alongside.
            if self._backend == "shumate" and hasattr(self, "_shumate_set_scale_offset"):
                self._shumate_set_scale_offset(360)

    def _map_show_track(self, latlon_speed: list[tuple[float, float, float | None]]) -> None:
        """Draw a speed-coloured polyline on the live map for replayed samples."""
        import json as _json
        from .cars_trip_visuals import speed_to_rgb

        latlon = [(lat, lon) for lat, lon, _ in latlon_speed]
        if not latlon:
            return

        if self._backend == "webkit":
            speeds = [s for _, _, s in latlon_speed if s is not None]
            vmax = max(speeds) if speeds else 0.0
            features = []
            for i in range(1, len(latlon_speed)):
                lat1, lon1, _spd_prev = latlon_speed[i - 1]
                lat2, lon2, spd = latlon_speed[i]
                r, g, b = speed_to_rgb(spd, vmax)
                color = "#{:02x}{:02x}{:02x}".format(
                    int(r * 255), int(g * 255), int(b * 255)
                )
                features.append({
                    "type": "Feature",
                    "properties": {"color": color},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[lon1, lat1], [lon2, lat2]],
                    },
                })
            self._js(f"mapSetColoredTrack({_json.dumps(features)})")
            lats = [p[0] for p in latlon]
            lons = [p[1] for p in latlon]
            self._js(f"mapFitBounds({min(lats)},{min(lons)},{max(lats)},{max(lons)})")
            self._set_follow(False)
        elif getattr(self, "_shumate_map", None) is not None:
            self._shumate_show_colored_track(latlon_speed)
            self._set_follow(False)

    def _clear_replay_overlays(self) -> None:
        """Hide the replay info + chart overlays and clear the polyline + marker."""
        if getattr(self, "_replay_info_overlay", None) is not None:
            self._replay_info_overlay.set_visible(False)
        if getattr(self, "_replay_chart_overlay", None) is not None:
            self._replay_chart_overlay.set_visible(False)
        self._map_clear_replay_marker()
        if self._backend == "webkit":
            self._js("mapClearColoredTrack()")
            self._js("mapClearRoute()")
        elif getattr(self, "_shumate_map", None) is not None:
            self._shumate_clear_colored_track()
            self._shumate_clear_route_layers()
            if hasattr(self, "_shumate_set_scale_offset"):
                self._shumate_set_scale_offset(0)

    def _on_tour_save_clicked(self, _btn: object) -> None:
        import json as _json
        from datetime import datetime, timezone
        db = getattr(self, "_map_db", None)
        if db is None:
            return

        waypoints = [e.get_text().strip() for _, e, __ in self._entry_rows]
        names = [w for w in waypoints if w]
        if len(names) >= 2:
            default_name = f"{names[0]} → {names[-1]}"
        elif names:
            default_name = names[0]
        else:
            default_name = datetime.now().strftime("%d.%m.%Y")

        _raw_loaded_id: int | None = getattr(self, "_loaded_tour_id", None)
        loaded_id: int | None = (
            _raw_loaded_id
            if _raw_loaded_id is not None and db is not None
            and db.get_saved_tour(_raw_loaded_id) is not None
            else None
        )

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_box.set_margin_top(6)
        content_box.set_margin_bottom(6)

        name_entry = Gtk.Entry()
        name_entry.set_text(default_name)
        name_entry.set_activates_default(True)
        content_box.append(name_entry)

        save_new_check: Gtk.CheckButton | None = None
        if loaded_id is not None:
            save_new_check = Gtk.CheckButton(
                label=_translate(self.language, "map.tours.save_as_new")
            )
            save_new_check.set_active(False)
            content_box.append(save_new_check)

        dialog = Adw.AlertDialog(heading=_translate(self.language, "map.topnav.save"))
        dialog.set_extra_child(content_box)
        dialog.add_response("cancel", _translate(self.language, "map.tours.cancel"))
        dialog.add_response("save", _translate(self.language, "map.tours.do_save"))
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.AlertDialog, resp: str) -> None:
            if resp != "save":
                return
            name = name_entry.get_text().strip() or default_name
            as_new = save_new_check is None or save_new_check.get_active()
            wp_json = _json.dumps(waypoints)
            now = datetime.now(timezone.utc).isoformat()
            if not as_new and loaded_id is not None:
                db.update_saved_tour(loaded_id, name, wp_json)
            else:
                tid = db.save_tour(name, wp_json, now)
                self._loaded_tour_id = tid

        dialog.connect("response", _on_response)
        dialog.present(self.get_root())

    def _rebuild_tour_list(self) -> None:
        if self._tour_listbox is None:
            return
        child = self._tour_listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._tour_listbox.remove(child)
            child = nxt

        db = getattr(self, "_map_db", None)
        tours = db.list_saved_tours() if db is not None else []

        if not tours:
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            row.set_selectable(False)
            lbl = Gtk.Label(label=_translate(self.language, "map.tours.empty"))
            lbl.add_css_class("dim-label")
            lbl.set_margin_top(14)
            lbl.set_margin_bottom(14)
            row.set_child(lbl)
            self._tour_listbox.append(row)
            return

        import json as _json
        for tour in tours:
            tour_id = int(tour["id"])
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(tour["created_at"])
                date_str = dt.strftime("%d.%m.%Y %H:%M")
            except Exception:
                date_str = str(tour["created_at"])[:16]

            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            row_box.set_margin_start(4)
            row_box.set_margin_end(4)
            row_box.set_margin_top(2)
            row_box.set_margin_bottom(2)

            text_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text_col.set_hexpand(True)
            text_col.set_valign(Gtk.Align.CENTER)
            name_lbl = Gtk.Label(label=str(tour["name"]), xalign=0.0)
            name_lbl.add_css_class("dp-steps-instr")
            name_lbl.set_max_width_chars(24)
            name_lbl.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
            date_lbl = Gtk.Label(label=date_str, xalign=0.0)
            date_lbl.add_css_class("dim-label")
            date_lbl.add_css_class("caption")
            text_col.append(name_lbl)
            text_col.append(date_lbl)

            load_btn = Gtk.Button()
            load_btn.add_css_class("flat")
            load_btn.set_hexpand(True)
            load_btn.set_child(text_col)
            tour_data = dict(tour)
            load_btn.connect("clicked", lambda _b, td=tour_data: self._load_saved_tour(td))

            sync_getter = getattr(self, "get_sync_client", None)
            sync_active = callable(sync_getter) and sync_getter() is not None
            if sync_active:
                share_btn = Gtk.Button(icon_name="share-alt-symbolic")
                share_btn.add_css_class("flat")
                share_btn.add_css_class("circular")
                share_btn.set_valign(Gtk.Align.CENTER)
                share_btn.connect("clicked", lambda _b, td=tour_data: self._share_saved_tour(td))
            else:
                share_btn = None

            del_btn = Gtk.Button(icon_name="user-trash-symbolic")
            del_btn.add_css_class("flat")
            del_btn.add_css_class("circular")
            del_btn.set_valign(Gtk.Align.CENTER)
            del_btn.connect("clicked", lambda _b, tid=tour_id: self._delete_saved_tour(tid))

            row_box.append(load_btn)
            if share_btn is not None:
                row_box.append(share_btn)
            row_box.append(del_btn)

            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            row.set_selectable(False)
            row.set_child(row_box)
            self._tour_listbox.append(row)

    def _load_saved_tour(self, tour: dict) -> None:
        import json as _json
        waypoints: list[str] = _json.loads(tour["waypoints_json"])

        # Adjust entry row count to match saved waypoints (min 2)
        target = max(2, len(waypoints))
        while len(self._entry_rows) < target:
            self._insert_entry_after(self._entry_rows[-1][0])
        while len(self._entry_rows) > target:
            self._remove_entry(self._entry_rows[-1][0])

        for (_, entry, __), text in zip(self._entry_rows, waypoints):
            entry.set_text(text)
        self._update_placeholders()

        self._loaded_tour_id = int(tour["id"])
        nav_view = getattr(self, "_nav_view", None)
        if nav_view is not None:
            nav_view.pop()
        self._on_route_clicked(None)

    def _delete_saved_tour(self, tour_id: int) -> None:
        dialog = Adw.AlertDialog(
            heading=_translate(self.language, "map.tours.delete_heading"),
            body=_translate(self.language, "map.tours.delete_body"),
        )
        dialog.add_response("cancel", _translate(self.language, "map.tours.cancel"))
        dialog.add_response("delete", _translate(self.language, "map.tours.delete_confirm"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.AlertDialog, resp: str) -> None:
            if resp != "delete":
                return
            db = getattr(self, "_map_db", None)
            if db is not None:
                db.delete_saved_tour(tour_id)
            self._rebuild_tour_list()

        dialog.connect("response", _on_response)
        dialog.present(self.get_root())

    def _share_saved_tour(self, tour: dict) -> None:
        dialog = Adw.AlertDialog(
            heading=_translate(self.language, "share.tour_confirm_title"),
            body=_translate(self.language, "share.tour_confirm_body"),
        )
        dialog.add_response("cancel", _translate(self.language, "share.cancel"))
        dialog.add_response("send", _translate(self.language, "share.send"))
        dialog.set_response_appearance("send", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("send")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.AlertDialog, resp: str) -> None:
            if resp != "send":
                return
            from .share_flow import ShareFlow
            ShareFlow(self, self._map_db, self.language, getattr(self, "get_sync_client", None)).share_tour(tour)

        dialog.connect("response", _on_response)
        dialog.present(self.get_root())

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
        bar.set_visible(False)
        self._map_content_box.append(bar)

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
        self, _tgt: Gtk.DropTarget, _val: object, _x: float, _y: float, dst_row: Gtk.Box
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
        # no grab_focus — avoids keyboard popup on mobile

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
        from .map_shumate import SHUMATE_OK
        from .map_webkit import WEBKIT_OK

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
            self._install_map_tap_controller(content)
            # Shumate-only: a single Cairo overlay paints the replay polyline.
            # Added first so the UI controls below sit on top of it.
            if self._backend == "shumate":
                self._build_shumate_replay_overlay(overlay)
            overlay.add_overlay(self._build_fab())
            overlay.add_overlay(self._build_zoom_controls())
            overlay.add_overlay(self._build_tour_controls())
            overlay.add_overlay(self._build_steps_panel())
            overlay.add_overlay(self._build_maneuver_overlay())
            overlay.add_overlay(self._build_speed_zone_overlay())
            overlay.add_overlay(self._build_map_state_overlay())
            overlay.add_overlay(self._build_replay_info_overlay())
            overlay.add_overlay(self._build_replay_chart_overlay())

        self._map_content_box.append(overlay)

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

        self._tts_btn = Gtk.ToggleButton()
        self._tts_btn.add_css_class("circular")
        self._tts_btn.add_css_class("osd")
        self._tts_btn.set_active(self._tts_enabled)
        self._refresh_tts_btn()
        self._tts_btn.connect("toggled", self._on_tts_btn_toggled)

        fab.append(self._poi_btn)
        fab.append(self._layer_btn)
        fab.append(self._center_btn)
        fab.append(self._tts_btn)

        # 3D/2D toggle — text label instead of icon so the current mode is
        # always readable at a glance.  WebKit-only (Shumate is flat).
        if self._backend == "webkit":
            self._3d_btn = Gtk.Button()
            self._3d_btn.add_css_class("circular")
            self._3d_btn.add_css_class("osd")
            self._3d_btn.connect("clicked", self._on_3d_clicked)
            self._refresh_3d_btn()
            fab.append(self._3d_btn)
        return fab

    def _build_map_state_overlay(self) -> Gtk.Widget:
        wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        wrap.set_halign(Gtk.Align.START)
        wrap.set_valign(Gtk.Align.END)
        wrap.set_margin_start(8)
        # Sit above the MapLibre scale bar (~24 px tall).
        wrap.set_margin_bottom(36)
        wrap.set_can_target(False)
        wrap.set_visible(False)

        self._map_state_lbl = Gtk.Label(label="")
        self._map_state_lbl.add_css_class("dp-map-state")
        self._map_state_lbl.set_xalign(0.0)
        wrap.append(self._map_state_lbl)

        self._map_state_overlay = wrap
        return wrap

    def _install_map_tap_controller(self, widget: Gtk.Widget) -> None:
        """Attach a capture-phase legacy controller to the map widget so a short
        tap toggles navigation visibility — the same as on all other pages.
        Adding it to the map content widget (not the overlay) means FAB button
        taps do not trigger the toggle."""
        _press: list = [0.0, 0.0, 0.0]  # monotonic, start_x, start_y
        _cur: list = [0.0, 0.0]  # last tracked x, y (TOUCH_UPDATE fallback)

        def _on_event(_ctrl: Gtk.EventControllerLegacy, event: Gdk.Event) -> bool:
            if event is None:
                return False
            etype = event.get_event_type()
            if etype in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.TOUCH_BEGIN):
                ok, x, y = event.get_position()
                if ok:
                    _press[0] = _time.monotonic()
                    _press[1] = x
                    _press[2] = y
                    _cur[0] = x
                    _cur[1] = y
            elif etype == Gdk.EventType.TOUCH_UPDATE:
                ok, x, y = event.get_position()
                if ok:
                    _cur[0] = x
                    _cur[1] = y
            elif etype in (Gdk.EventType.BUTTON_RELEASE, Gdk.EventType.TOUCH_END):
                if _press[0] == 0.0:
                    return False
                now = _time.monotonic()
                ok, x, y = event.get_position()
                duration = now - _press[0]
                _press[0] = 0.0
                if not ok:
                    x, y = _cur[0], _cur[1]
                moved = math.hypot(x - _press[1], y - _press[2])
                if duration <= 0.30 and moved <= 14.0:
                    cb = getattr(self, "_on_map_tapped", None)
                    if cb is not None:
                        GLib.idle_add(cb)
            return False  # never consume — map interaction stays intact

        ctrl = Gtk.EventControllerLegacy()
        ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        ctrl.connect("event", _on_event)
        widget.add_controller(ctrl)

    def _build_speed_zone_overlay(self) -> Gtk.Widget:
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        wrap.set_halign(Gtk.Align.START)
        wrap.set_valign(Gtk.Align.END)
        wrap.set_margin_start(12)
        wrap.set_margin_bottom(36)
        wrap.set_can_target(False)
        wrap.set_visible(False)

        sign = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sign.add_css_class("dp-speed-sign")
        sign.set_halign(Gtk.Align.CENTER)
        sign.set_valign(Gtk.Align.CENTER)
        sign.set_size_request(88, 88)

        lbl = Gtk.Label(label="")
        lbl.set_halign(Gtk.Align.CENTER)
        lbl.set_valign(Gtk.Align.CENTER)
        lbl.set_hexpand(True)
        lbl.set_vexpand(True)
        lbl.set_justify(Gtk.Justification.CENTER)
        sign.append(lbl)
        wrap.append(sign)

        self._speed_zone_overlay = wrap
        self._speed_zone_lbl = lbl
        return wrap

    def _build_zoom_controls(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_halign(Gtk.Align.END)
        box.set_valign(Gtk.Align.START)
        box.set_margin_top(12)
        box.set_margin_end(12)

        zoom_in = Gtk.Button(icon_name="zoom-in-symbolic")
        zoom_in.add_css_class("circular")
        zoom_in.add_css_class("osd")
        zoom_in.set_tooltip_text(_translate(self.language, "map.zoom_in"))
        zoom_in.connect("clicked", lambda _b: self._zoom_step(+1))

        zoom_out = Gtk.Button(icon_name="zoom-out-symbolic")
        zoom_out.add_css_class("circular")
        zoom_out.add_css_class("osd")
        zoom_out.set_tooltip_text(_translate(self.language, "map.zoom_out"))
        zoom_out.connect("clicked", lambda _b: self._zoom_step(-1))

        self._zoom_in_btn = zoom_in
        self._zoom_out_btn = zoom_out
        box.append(zoom_in)
        box.append(zoom_out)
        return box

    def _view_3d_tooltip(self, active: bool) -> str:
        return _translate(
            self.language,
            "map.view.switch_to_2d" if active else "map.view.switch_to_3d",
        )

    def _refresh_3d_btn(self) -> None:
        if self._3d_btn is None:
            return
        # Show what you'd switch TO — "2D" while we're in 3D, "3D" while flat.
        self._3d_btn.set_label("2D" if self._map_3d_view else "3D")
        self._3d_btn.set_tooltip_text(self._view_3d_tooltip(self._map_3d_view))

    def _build_maneuver_overlay(self) -> Gtk.Widget:
        _install_maneuver_css()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_halign(Gtk.Align.CENTER)
        outer.set_valign(Gtk.Align.START)
        # Sits visibly in the upper third of the map area, below the search bar.
        outer.set_margin_top(72)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        outer.set_can_target(False)
        outer.set_visible(False)

        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=22)
        card.add_css_class("dp-maneuver-banner")

        self._maneuver_icon = Gtk.Image.new_from_icon_name("dp-nav-straight-symbolic")
        self._maneuver_icon.set_pixel_size(96)
        card.append(self._maneuver_icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        text_box.set_valign(Gtk.Align.CENTER)

        self._maneuver_distance_lbl = Gtk.Label(label="")
        self._maneuver_distance_lbl.add_css_class("dp-maneuver-distance")
        self._maneuver_distance_lbl.set_halign(Gtk.Align.START)

        self._maneuver_instr_lbl = Gtk.Label(label="")
        self._maneuver_instr_lbl.add_css_class("dp-maneuver-instr")
        self._maneuver_instr_lbl.set_halign(Gtk.Align.START)
        self._maneuver_instr_lbl.set_max_width_chars(28)
        self._maneuver_instr_lbl.set_wrap(True)

        text_box.append(self._maneuver_distance_lbl)
        text_box.append(self._maneuver_instr_lbl)
        card.append(text_box)

        outer.append(card)

        lane_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lane_row.add_css_class("dp-lane-row")
        lane_row.set_halign(Gtk.Align.CENTER)
        lane_row.set_visible(False)
        outer.append(lane_row)
        self._lane_row = lane_row

        self._maneuver_overlay = outer
        return outer

    def _build_tour_controls(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_halign(Gtk.Align.START)
        box.set_valign(Gtk.Align.START)
        box.set_margin_start(12)
        box.set_margin_top(12)
        self._tour_controls_box = box

        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._tour_btn_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        inner.append(self._tour_btn_icon)
        self._tour_start_lbl = Gtk.Label(label=_translate(self.language, "map.tour_start"))
        inner.append(self._tour_start_lbl)

        self._tour_start_btn = Gtk.Button()
        self._tour_start_btn.set_child(inner)
        self._tour_start_btn.add_css_class("osd")
        self._tour_start_btn.connect("clicked", self._on_tour_start_clicked)
        box.append(self._tour_start_btn)

        steps_icon = Gtk.Image.new_from_icon_name("info-symbolic")
        steps_icon.set_pixel_size(20)
        self._steps_toggle_btn = Gtk.ToggleButton()
        self._steps_toggle_btn.set_child(steps_icon)
        self._steps_toggle_btn.add_css_class("osd")
        self._steps_toggle_btn.add_css_class("circular")
        self._steps_toggle_btn.set_halign(Gtk.Align.START)
        self._steps_toggle_btn.set_size_request(40, 40)
        self._steps_toggle_btn.set_tooltip_text(_translate(self.language, "map.steps.toggle"))
        self._steps_toggle_btn.connect("toggled", self._on_steps_toggle)
        box.append(self._steps_toggle_btn)

        box.set_visible(False)
        return box

    def _build_steps_panel(self) -> Gtk.Widget:
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        wrap.add_css_class("dp-steps-panel")
        wrap.set_halign(Gtk.Align.START)
        wrap.set_valign(Gtk.Align.FILL)
        wrap.set_margin_start(12)
        # Sit below the stacked tour-controls column
        # (start button + info button + spacing + top margin ≈ 105 px).
        wrap.set_margin_top(105)
        wrap.set_margin_bottom(12)
        wrap.set_size_request(280, -1)
        wrap.set_visible(False)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("navigation-sidebar")
        scrolled.set_child(listbox)
        wrap.append(scrolled)

        self._steps_panel = wrap
        self._steps_listbox = listbox
        self._steps_scrolled = scrolled
        return wrap

    def _on_steps_toggle(self, btn: Gtk.ToggleButton) -> None:
        if self._steps_panel is None:
            return
        show = btn.get_active() and bool(self._tour_steps)
        if show:
            self._rebuild_steps_list()
        self._steps_panel.set_visible(show)
        if show:
            self._scroll_steps_to_active()

    def _rebuild_steps_list(self) -> None:
        if self._steps_listbox is None:
            return
        child = self._steps_listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._steps_listbox.remove(child)
            child = nxt
        self._steps_row_widgets = []
        self._steps_row_listbox_rows = []

        for idx, step in enumerate(self._tour_steps):
            m_type = step.get("type", "")
            m_modifier = step.get("modifier", "")
            name = step.get("name", "") or ""
            icon_name = maneuver_icon(m_type, m_modifier)
            text = _translate(self.language, maneuver_text_key(m_type, m_modifier))
            if name and m_type not in {"arrive", "depart"}:
                text += _translate(self.language, "map.maneuver.on_street").format(name=name)

            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row_box.add_css_class("dp-steps-row")

            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.set_pixel_size(28)
            row_box.append(icon)

            text_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text_col.set_hexpand(True)
            instr_lbl = Gtk.Label(label=text, xalign=0.0)
            instr_lbl.add_css_class("dp-steps-instr")
            instr_lbl.set_wrap(True)
            instr_lbl.set_max_width_chars(24)
            text_col.append(instr_lbl)
            dist_m = float(step.get("distance") or 0.0)
            if dist_m > 0:
                dist_lbl = Gtk.Label(label=format_distance(dist_m, self.units), xalign=0.0)
                dist_lbl.add_css_class("dp-steps-distance")
                dist_lbl.add_css_class("dim-label")
                text_col.append(dist_lbl)
            row_box.append(text_col)

            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            row.set_selectable(False)
            row.set_child(row_box)
            self._steps_listbox.append(row)
            self._steps_row_widgets.append(row_box)
            self._steps_row_listbox_rows.append(row)

        self._highlight_active_step()

    def _highlight_active_step(self) -> None:
        active = self._tour_step_idx if (self._tour_active or self._tour_paused) else -1
        for idx, row_box in enumerate(self._steps_row_widgets):
            row_box.remove_css_class("dp-steps-row-active")
            row_box.remove_css_class("dp-steps-row-done")
            if active < 0:
                continue
            if idx < active:
                row_box.add_css_class("dp-steps-row-done")
            elif idx == active:
                row_box.add_css_class("dp-steps-row-active")
        if active >= 0:
            self._scroll_steps_to_active()

    def _scroll_steps_to_active(self) -> None:
        """Auto-scroll the steps panel so the active step is near the top
        — gives the driver a preview of upcoming maneuvers below it."""
        if (
            self._steps_scrolled is None
            or self._steps_panel is None
            or not self._steps_panel.get_visible()
            or not self._steps_row_listbox_rows
        ):
            return
        idx = self._tour_step_idx
        if idx < 0 or idx >= len(self._steps_row_listbox_rows):
            return
        # Defer until after GTK has allocated the rows, otherwise heights are 0.
        GLib.idle_add(self._do_scroll_to_active, idx)

    def _do_scroll_to_active(self, idx: int) -> bool:
        if self._steps_scrolled is None or idx >= len(self._steps_row_listbox_rows):
            return False
        adj = self._steps_scrolled.get_vadjustment()
        if adj is None:
            return False
        upper = adj.get_upper()
        page = adj.get_page_size()
        if upper <= page:
            return False  # all rows already fit
        n = max(1, len(self._steps_row_listbox_rows))
        row_h = upper / n
        # Pin the active step ~one row down from the top of the viewport.
        target = max(0.0, min(upper - page, idx * row_h - row_h))
        adj.set_value(target)
        return False

    def _shumate_initial_render(self) -> bool:
        if self._shumate_map is None:
            return False
        viewport = self._shumate_map.get_viewport()
        self._setting_pos = True
        viewport.set_zoom_level(viewport.get_zoom_level())
        viewport.set_location(viewport.get_latitude(), viewport.get_longitude())
        self._setting_pos = False
        self._shumate_map.queue_resize()
        return False
