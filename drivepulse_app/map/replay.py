"""Trip-replay overlays for the map page.

Hosts the top-left metadata card, the bottom-left metric chart, the restore
buttons for both of those, the replay-marker plumbing on the live map, and
the orchestration that loads a recorded trip onto the live map. Extracted
from ``map_layout.py`` because it's a self-contained feature with no
overlap with the search bar, tour-planning, or step-list code there.
"""
from __future__ import annotations

from typing import Any

from gi.repository import Gtk

from drivepulse_app.common import _translate


class MapReplayMixin:
    """Trip-replay info card, metric chart, polyline + marker on the live map."""

    # Owning class (MapPage) initializes this in __init__ as int | None.
    # Annotated here so mypy doesn't infer ``int`` from the assignment in
    # ``_show_trip_replay`` further below.
    _loaded_trip_id: int | None

    def _build_replay_info_overlay(self) -> Gtk.Widget:
        """Card showing the replayed trip's metadata (lives inside the tour-controls grid)."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("osd")
        box.add_css_class("dp-replay-info")
        box.set_valign(Gtk.Align.START)
        box.set_visible(False)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._replay_title_lbl = Gtk.Label(xalign=0.0)
        self._replay_title_lbl.add_css_class("heading")
        self._replay_title_lbl.set_hexpand(True)
        head.append(self._replay_title_lbl)

        min_btn = Gtk.Button(icon_name="window-minimize-symbolic")
        min_btn.add_css_class("flat")
        min_btn.add_css_class("circular")
        min_btn.set_tooltip_text(_translate(self.language, "map.replay.minimize"))
        # Minimise hides the card and shows the compact notepad-icon restore button.
        min_btn.connect("clicked", lambda _b: self._set_replay_info_minimized(True))
        head.append(min_btn)
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

    def _build_replay_info_restore_btn(self) -> Gtk.Widget:
        """Notepad button — identical style to the info toggle, shows trip data card."""
        notepad_icon = Gtk.Image.new_from_icon_name("notepad-symbolic")
        notepad_icon.set_pixel_size(20)
        btn = Gtk.Button()
        btn.set_child(notepad_icon)
        btn.add_css_class("osd")
        btn.add_css_class("circular")
        btn.set_size_request(40, 40)
        btn.set_tooltip_text(_translate(self.language, "map.replay.restore"))
        btn.set_visible(False)
        btn.connect("clicked", lambda _b: self._set_replay_info_minimized(False))
        self._replay_info_restore_btn = btn
        return btn

    def _set_replay_info_minimized(self, minimized: bool) -> None:
        if getattr(self, "_replay_info_overlay", None) is not None:
            self._replay_info_overlay.set_visible(not minimized)
        if getattr(self, "_replay_info_restore_btn", None) is not None:
            self._replay_info_restore_btn.set_visible(minimized)
        # Info card and chart-restore icon share the upper-left column;
        # let the central helper decide which one wins right now.
        if hasattr(self, "_update_left_chrome_visibility"):
            self._update_left_chrome_visibility()

    def _build_replay_chart_overlay(self) -> Gtk.Widget:
        """Bottom-left container for the speed chart shown during replay."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.add_css_class("osd")
        box.add_css_class("dp-replay-chart")
        box.set_halign(Gtk.Align.START)
        box.set_valign(Gtk.Align.END)
        box.set_margin_start(12)
        # Shumate: lift the chart up to share the TTS button's baseline so the
        # left+right bottom controls sit on the same line.
        box.set_margin_bottom(36 if self._backend == "shumate" else 12)
        box.set_size_request(340, -1)
        box.set_visible(False)

        # Header row: metric-selector dropdown on the left, minimize button
        # on the right.  Kept across _populate_replay_chart re-renders so the
        # button doesn't get torn down when the chart contents change.
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        head.set_valign(Gtk.Align.CENTER)
        dropdown_slot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        dropdown_slot.set_valign(Gtk.Align.CENTER)
        head.append(dropdown_slot)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        head.append(spacer)
        min_btn = Gtk.Button(icon_name="window-minimize-symbolic")
        min_btn.add_css_class("flat")
        min_btn.add_css_class("circular")
        min_btn.set_valign(Gtk.Align.CENTER)
        min_btn.set_tooltip_text(_translate(self.language, "map.replay.chart_minimize"))
        min_btn.connect("clicked", lambda _b: self._set_replay_chart_minimized(True))
        head.append(min_btn)
        box.append(head)
        self._replay_chart_header = head
        self._replay_chart_min_btn = min_btn
        self._replay_chart_dropdown_slot = dropdown_slot

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.append(content)
        self._replay_chart_content = content

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
        self._replay_chart_minimized: bool = False
        return box

    def _build_replay_chart_restore_btn(self) -> Gtk.Widget:
        """Icon to restore a minimised chart overlay — same shape as the info
        and notepad buttons in the top-left, placed directly under the info."""
        icon = Gtk.Image.new_from_icon_name("integral3-symbolic")
        icon.set_pixel_size(20)
        btn = Gtk.Button()
        btn.set_child(icon)
        btn.add_css_class("osd")
        btn.add_css_class("circular")
        btn.set_halign(Gtk.Align.START)
        btn.set_valign(Gtk.Align.START)
        btn.set_size_request(40, 40)
        btn.set_tooltip_text(_translate(self.language, "map.replay.chart_restore"))
        btn.set_visible(False)
        btn.connect(
            "clicked",
            lambda _b: self._set_replay_chart_minimized(
                not bool(getattr(self, "_replay_chart_minimized", False))
            ),
        )
        self._replay_chart_restore_btn = btn
        return btn

    def _set_replay_chart_minimized(self, minimized: bool) -> None:
        self._replay_chart_minimized = minimized
        if getattr(self, "_replay_chart_overlay", None) is not None:
            self._replay_chart_overlay.set_visible(not minimized)
        # Chart-restore icon stays visible throughout replay; only the
        # _update_left_chrome_visibility gate (chart_has_data) can hide it.
        if self._backend == "shumate" and hasattr(self, "_shumate_set_scale_visible"):
            self._shumate_set_scale_visible(minimized)
        self._refresh_fab_visibility()
        # Expanded chart hides the trash icon; minimised chart restores it.
        if hasattr(self, "_update_left_chrome_visibility"):
            self._update_left_chrome_visibility()

    def _refresh_fab_visibility(self) -> None:
        """Map options FAB column is always visible; the previous coord-chip
        suppression no longer applies."""
        fab = getattr(self, "_fab", None)
        if fab is None:
            return
        fab.set_visible(True)

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

    def _populate_trip_route_info(
        self,
        label: str | None,
        distance_km: float | None,
        duration_s: float | None,
    ) -> None:
        """Populate the replay info card with a recorded trip's metadata.

        Used by `load_trip_as_route` so the trip's distance/duration sits
        in the top-left card under the info icon, instead of getting buried
        in the search-bar status label.
        """
        if getattr(self, "_replay_info_overlay", None) is None:
            return
        grid = self._replay_meta_grid
        child = grid.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            grid.remove(child)
            child = nxt

        self._replay_title_lbl.set_label(
            label or _translate(self.language, "map.history.kind_trip")
        )

        # Stack each metric as its own full-width row (label on top, value
        # underneath) so duration and distance read vertically rather than
        # competing for space side-by-side.
        def _block(idx: int, key: str, value: str) -> None:
            k = Gtk.Label(label=_translate(self.language, key), xalign=0.0)
            k.add_css_class("dim-label")
            k.add_css_class("caption")
            v = Gtk.Label(label=value, xalign=0.0)
            v.add_css_class("caption")
            v.add_css_class("heading")
            grid.attach(k, 0, idx, 2, 1)
            grid.attach(v, 0, idx + 1, 2, 1)

        idx = 0
        if distance_km is not None:
            _block(idx, "map.replay.distance", f"{float(distance_km):.1f} km")
            idx += 2
        if duration_s:
            dur = self._format_history_duration(float(duration_s))
            if dur:
                _block(idx, "map.replay.duration", dur)

        # Start minimized — only the notepad icon shows; user opens the card on demand.
        self._set_replay_info_minimized(True)

    def _populate_replay_chart(self, samples: list) -> None:
        """Build a metric chart + dropdown for the replayed trip.

        The chart cursor stays in sync with a marker on the live map: scrubbing
        the chart moves a circle along the polyline so the user sees which
        point on the route corresponds to the highlighted value.
        """
        from drivepulse_app.cars.trip_visuals import _build_chart_widget, build_trip_metric_data

        # Tear down anything from a previous replay (only the content box —
        # the header with the minimize button is kept across re-renders).
        content = getattr(self, "_replay_chart_content", None) or self._replay_chart_overlay
        child = content.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            content.remove(child)
            child = nxt
        # Also clear the dropdown slot in the header so a previous metric
        # selector doesn't stack up next to the minimize button.
        slot = getattr(self, "_replay_chart_dropdown_slot", None)
        if slot is not None:
            child = slot.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                slot.remove(child)
                child = nxt
        self._replay_chart_widget = None
        self._replay_chart_area: Gtk.Widget | None = None
        # Fresh replay starts un-minimized.
        self._set_replay_chart_minimized(False)

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
            init_sel = next(
                (i for i, m in enumerate(avail) if m[0] == def_key), 0
            )
            dropdown.set_selected(init_sel)
            from drivepulse_app.cars.trip_visuals import lift_dropdown_popover
            lift_dropdown_popover(dropdown)
            # Park the metric selector in the header next to the minimize
            # button so they share a single row.
            if slot is not None:
                slot.append(dropdown)
            else:
                content.append(dropdown)

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
        content.append(area)

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
        self._step_preview_row = None

    def _show_trip_replay(self, meta: dict) -> None:
        """Render a recorded trip's polyline + metadata on the live map."""
        db = getattr(self, "_map_db", None)
        if db is None:
            return
        self._clear_replay_overlays()
        nav_view = getattr(self, "_nav_view", None)
        if nav_view is not None:
            nav_view.pop()

        trip_id = int(meta["id"])
        # Mark this trip as currently displayed so the Recent-Tours list
        # can highlight it with the green emblem on the next rebuild.
        self._loaded_trip_id = trip_id
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
        # The info card lives inside the tour-controls grid, so the grid must
        # be visible for the card to appear.
        self._set_tour_controls_visible(True)
        # Store coords so "Tour berechnen" can trigger load_trip_as_route on click.
        lonlat_coords = [[lon, lat] for lat, lon, _ in latlon_speed]
        if len(lonlat_coords) >= 2:
            self._replay_nav_coords: list | None = lonlat_coords
            self._replay_nav_meta: dict | None = meta
        self._set_tour_button("calculate")
        # Start minimized — only the notepad icon shows; user opens the card on demand.
        self._set_replay_info_minimized(True)
        if self._replay_chart_widget is not None:
            # Start minimized — only the restore icon shows; user opens the chart on demand.
            self._set_replay_chart_minimized(True)
        self._refresh_fab_visibility()

    def _map_show_track(self, latlon_speed: list[tuple[float, float, float | None]]) -> None:
        """Draw a speed-coloured polyline on the live map for replayed samples."""
        import json as _json

        from drivepulse_app.cars.trip_visuals import speed_to_rgb

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
                color = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
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
        self._loaded_trip_id = None
        self._replay_nav_coords = None
        self._replay_nav_meta = None
        if getattr(self, "_replay_info_overlay", None) is not None:
            self._replay_info_overlay.set_visible(False)
        if getattr(self, "_replay_info_restore_btn", None) is not None:
            self._replay_info_restore_btn.set_visible(False)
        # If neither a tour plan nor a tour is active, the controls were only
        # shown to host the info card — hide them again.
        if not getattr(self, "_tour_plan_active", False) and not getattr(self, "_tour_active", False) and not getattr(self, "_tour_paused", False):
            self._set_tour_controls_visible(False)
        if getattr(self, "_replay_chart_overlay", None) is not None:
            self._replay_chart_overlay.set_visible(False)
        if getattr(self, "_replay_chart_restore_btn", None) is not None:
            self._replay_chart_restore_btn.set_visible(False)
        self._replay_chart_minimized = False
        self._refresh_fab_visibility()
        self._map_clear_replay_marker()
        if hasattr(self, "_hide_route_info"):
            self._hide_route_info()
        if self._backend == "webkit":
            self._js("mapClearColoredTrack()")
            self._js("mapClearRoute()")
        elif getattr(self, "_shumate_map", None) is not None:
            self._shumate_clear_colored_track()
            self._shumate_clear_route_layers()
            if hasattr(self, "_shumate_set_scale_visible"):
                self._shumate_set_scale_visible(True)
        if hasattr(self, "_update_left_chrome_visibility"):
            self._update_left_chrome_visibility()
