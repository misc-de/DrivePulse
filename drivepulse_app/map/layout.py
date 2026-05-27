"""Map page core layout — map widget + overlays (FAB, zoom, maneuver, speed sign)."""
from __future__ import annotations

from typing import Any

from gi.repository import Gtk

from drivepulse_app.common import _translate
from drivepulse_app.map.layout_css import _install_maneuver_css
from drivepulse_app.map.services import MAP_ICONS, MAP_LABEL_KEYS, MAP_TYPES


class MapLayoutMixin:
    """Map area construction: backend selection (WebKit/Shumate/placeholder),
    floating action button column, zoom controls, maneuver banner, and the
    miscellaneous overlays (route loading spinner, speed sign,
    map-state debug label, tour-controls grid)."""

    # ── Map area ──────────────────────────────────────────────────────────────

    def _build_map(self) -> None:
        from drivepulse_app.map.shumate import SHUMATE_OK
        from drivepulse_app.map.webkit import WEBKIT_OK

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
            overlay.add_overlay(self._build_replay_chart_overlay())
            overlay.add_overlay(self._build_tour_reset_btn())
            overlay.add_overlay(self._build_route_loading_overlay())
            overlay.add_overlay(self._build_route_info_overlay())

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
        # WebKit: align with the replay-chart restore icon (margin_bottom=12)
        # so left and right controls share the same baseline.
        # Shumate: keep the historical 36 px clearance so the FAB sits above
        # the libshumate scale ruler that lives in the bottom-left corner.
        fab.set_margin_bottom(41 if self._backend == "shumate" else 12)
        self._fab = fab

        self._poi_btn = Gtk.ToggleButton(icon_name="mark-location-symbolic")
        self._poi_btn.add_css_class("circular")
        self._poi_btn.add_css_class("osd")
        self._poi_btn.set_active(self._poi_visible)
        self._poi_btn.set_tooltip_text(_translate(self.language, "map.poi"))
        self._poi_btn.connect("toggled", self._on_poi_toggled)

        _initial_layer = (
            MAP_TYPES[self._map_type_idx]
            if 0 <= self._map_type_idx < len(MAP_TYPES) else "map"
        )
        self._layer_btn = Gtk.Button(
            icon_name=MAP_ICONS.get(_initial_layer, "dialog-layers-symbolic")
        )
        self._layer_btn.add_css_class("circular")
        self._layer_btn.add_css_class("osd")
        self._layer_btn.set_tooltip_text(_translate(self.language, MAP_LABEL_KEYS[_initial_layer]))
        self._layer_btn.connect("clicked", self._on_layer_clicked)

        # Heading-up toggle: active = map rotates so the heading is always
        # at the top (arrow stays fixed); inactive = map stays north-up and
        # the arrow rotates with GPS heading. Icon mirrors the navigation
        # marker (same chevron shape) rendered in grey, pointing up.
        nav_arrow = Gtk.DrawingArea()
        nav_arrow.set_size_request(24, 24)
        nav_arrow.set_draw_func(self._draw_nav_arrow_icon, None)
        self._heading_up_btn = Gtk.ToggleButton()
        self._heading_up_btn.set_child(nav_arrow)
        self._heading_up_btn.add_css_class("circular")
        self._heading_up_btn.add_css_class("osd")
        self._heading_up_btn.set_active(bool(getattr(self, "_heading_up", True)))
        self._refresh_heading_up_btn_tooltip()
        self._heading_up_btn.connect("toggled", self._on_heading_up_toggled)

        self._center_btn = Gtk.Button(icon_name="find-location-symbolic")
        self._center_btn.add_css_class("circular")
        self._center_btn.add_css_class("osd")
        self._center_btn.set_tooltip_text(_translate(self.language, "map.center"))
        self._center_btn.connect("clicked", self._on_center_clicked)

        self._tts_btn = Gtk.ToggleButton()
        self._tts_btn.add_css_class("circular")
        self._tts_btn.add_css_class("osd")
        self._tts_btn.set_active(self._tts_enabled)
        self._tts_btn.set_visible(False)
        self._refresh_tts_btn()
        self._tts_btn.connect("toggled", self._on_tts_btn_toggled)

        self._speed_warn_btn = Gtk.ToggleButton(icon_name="alarm-symbolic")
        self._speed_warn_btn.add_css_class("circular")
        self._speed_warn_btn.add_css_class("osd")
        self._speed_warn_btn.set_active(self._speed_warn_enabled)
        self._speed_warn_btn.set_visible(False)
        self._refresh_speed_warn_btn()
        self._speed_warn_btn.connect("toggled", self._on_speed_warn_toggled)

        fab.append(self._poi_btn)
        fab.append(self._layer_btn)
        fab.append(self._heading_up_btn)
        fab.append(self._center_btn)
        fab.append(self._tts_btn)
        fab.append(self._speed_warn_btn)

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

    def _build_route_loading_overlay(self) -> Gtk.Widget:
        """Centred spinner shown while a route is being computed or a tour is
        loading — visible regardless of whether the search bar is up."""
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        wrap.set_halign(Gtk.Align.CENTER)
        wrap.set_valign(Gtk.Align.CENTER)
        wrap.add_css_class("osd")
        wrap.add_css_class("dp-route-loading")
        wrap.set_can_target(False)
        wrap.set_visible(False)

        spinner = Gtk.Spinner()
        spinner.set_size_request(48, 48)
        spinner.set_margin_top(14)
        spinner.set_margin_bottom(14)
        spinner.set_margin_start(14)
        spinner.set_margin_end(14)
        wrap.append(spinner)

        css = Gtk.CssProvider()
        css.load_from_data(b".dp-route-loading { border-radius: 14px; }")
        wrap.connect(
            "realize",
            lambda w: Gtk.StyleContext.add_provider_for_display(
                w.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            ),
        )

        self._route_loading_overlay = wrap
        self._route_loading_spinner = spinner
        return wrap

    def _set_route_loading(self, active: bool) -> None:
        overlay = getattr(self, "_route_loading_overlay", None)
        spinner = getattr(self, "_route_loading_spinner", None)
        if overlay is None or spinner is None:
            return
        if active:
            spinner.start()
            overlay.set_visible(True)
        else:
            spinner.stop()
            overlay.set_visible(False)

    def _draw_nav_arrow_icon(
        self, _area: Gtk.DrawingArea, cr: Any, width: int, height: int, _data: Any
    ) -> None:
        """Render the navigation chevron as a grey, upward-pointing icon for
        the heading-up FAB toggle. Shape matches _draw_car in shumate.py but
        scaled down (24×24) and recoloured (mid-grey instead of blue)."""
        cx, cy = width / 2.0, height / 2.0
        scale = min(width, height) / 44.0  # arrow was tuned for a 44px canvas
        cr.save()
        cr.translate(cx, cy)
        cr.scale(scale, scale)
        cr.move_to(0, -16)
        cr.line_to(11, 13)
        cr.line_to(0, 7)
        cr.line_to(-11, 13)
        cr.close_path()
        cr.set_source_rgb(1.0, 1.0, 1.0)
        cr.fill()
        cr.restore()

    def _build_tour_reset_btn(self) -> Gtk.Widget:
        """Trash button bottom-left, above the Shumate scale ruler.
        Click clears any loaded/planned tour and resets the map to the
        empty state. Visibility is driven by
        _update_left_chrome_visibility() so it disappears whenever the
        replay chart takes over the bottom-left corner."""
        btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        btn.add_css_class("osd")
        btn.add_css_class("circular")
        btn.set_halign(Gtk.Align.START)
        btn.set_valign(Gtk.Align.END)
        btn.set_margin_start(12)
        # Scale ruler sits at margin_bottom=36; the trash icon stacks above
        # it with enough gap for the 40px button + a small breathing space.
        btn.set_margin_bottom(48)
        btn.set_size_request(40, 40)
        btn.set_tooltip_text(_translate(self.language, "map.tour_reset.tooltip"))
        btn.set_visible(False)
        btn.connect("clicked", lambda _b: self._on_tour_reset_clicked())
        self._tour_reset_btn = btn
        return btn

    def _on_tour_reset_clicked(self) -> None:
        # Re-uses the same teardown the search-bar's X already triggers:
        # clears entries, route, tour state, overlays, hides controls.
        if hasattr(self, "_on_clear_clicked"):
            self._on_clear_clicked(None)
        # Job done — force-hide the trash regardless of any lingering state
        # _update_left_chrome_visibility might still consider truthy.
        btn = getattr(self, "_tour_reset_btn", None)
        if btn is not None:
            btn.set_visible(False)
        self._update_left_chrome_visibility()

    def _update_left_chrome_visibility(self) -> None:
        """Decide which of the three bottom-/top-left controls
        (trash, info-restore, chart-restore) get to be visible right now.

          - Trash → only when a tour or plan exists AND the chart is
            not currently expanded (would clash visually).
          - Chart-restore icon → always visible while replay chart data exists.

        Called from set_replay_*_minimized, route load/clear paths, and
        the tour state-machine transitions."""
        chart_overlay = getattr(self, "_replay_chart_overlay", None)
        chart_expanded = bool(chart_overlay is not None and chart_overlay.get_visible())
        steps_panel = getattr(self, "_steps_panel", None)
        steps_expanded = bool(steps_panel is not None and steps_panel.get_visible())

        has_tour = bool(
            getattr(self, "_tour_active", False)
            or getattr(self, "_tour_paused", False)
            or getattr(self, "_tour_plan_active", False)
            or getattr(self, "_loaded_tour_id", None) is not None
            or getattr(self, "_loaded_trip_id", None) is not None
            or (getattr(self, "_tour_coords", None) or [])
            or (getattr(self, "_route_coords", None) or [])
        )

        tour_running = bool(
            getattr(self, "_tour_active", False)
            or getattr(self, "_tour_paused", False)
        )
        btn = getattr(self, "_tour_reset_btn", None)
        if btn is not None:
            btn.set_visible(
                has_tour and not tour_running and not chart_expanded and not steps_expanded
            )

        is_trip_replay = bool(getattr(self, "_loaded_trip_id", None))
        has_navigable_tour = bool(
            getattr(self, "_tour_active", False)
            or getattr(self, "_tour_paused", False)
            or getattr(self, "_tour_plan_active", False)
            or getattr(self, "_loaded_tour_id", None) is not None
            or (not is_trip_replay and (getattr(self, "_tour_coords", None) or []))
            or (not is_trip_replay and (getattr(self, "_route_coords", None) or []))
        )

        tts_btn = getattr(self, "_tts_btn", None)
        if tts_btn is not None:
            tts_btn.set_visible(has_navigable_tour)

        speed_warn_btn = getattr(self, "_speed_warn_btn", None)
        if speed_warn_btn is not None:
            speed_warn_btn.set_visible(has_navigable_tour)

        chart_restore = getattr(self, "_replay_chart_restore_btn", None)
        chart_has_data = bool(getattr(self, "_replay_chart_widget", None))
        if chart_restore is not None:
            chart_restore.set_visible(chart_has_data and not steps_expanded)

    def _build_route_info_overlay(self) -> Gtk.Widget:
        """Route info is now embedded in the tour-controls icon row.
        Return an invisible placeholder so _build_map's overlay.add_overlay()
        call keeps working without any structural change there."""
        dummy = Gtk.Box()
        dummy.set_visible(False)
        dummy.set_can_target(False)
        return dummy

    def _show_route_info(self, duration_s: float | None, distance_m: float | None) -> None:
        """Render duration + distance into the top-left route-info card,
        stacked vertically. Hides the card when both values are missing.
        """
        from drivepulse_app.map.services import format_distance, format_duration
        if getattr(self, "_route_info_overlay", None) is None:
            return
        duration_text = ""
        distance_text = ""
        if duration_s:
            prefix = _translate(self.language, "map.duration_prefix")
            duration_text = f"{prefix}{format_duration(duration_s)}"
        if distance_m:
            distance_prefix = _translate(self.language, "map.distance_prefix")
            distance_text = f"{distance_prefix}{format_distance(distance_m, self.units)}"
        if not duration_text and not distance_text:
            self._hide_route_info()
            return
        if getattr(self, "_route_info_duration_lbl", None) is not None:
            self._route_info_duration_lbl.set_text(duration_text)
            self._route_info_duration_lbl.set_visible(bool(duration_text))
        if getattr(self, "_route_info_distance_lbl", None) is not None:
            self._route_info_distance_lbl.set_text(distance_text)
            self._route_info_distance_lbl.set_visible(bool(distance_text))
        self._route_info_overlay.set_visible(True)

    def _hide_route_info(self) -> None:
        if getattr(self, "_route_info_overlay", None) is not None:
            self._route_info_overlay.set_visible(False)
            for attr in ("_route_info_duration_lbl", "_route_info_distance_lbl"):
                lbl = getattr(self, attr, None)
                if lbl is not None:
                    lbl.set_text("")

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

    def _build_speed_zone_overlay(self) -> Gtk.Widget:
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        wrap.set_halign(Gtk.Align.START)
        wrap.set_valign(Gtk.Align.END)
        wrap.set_margin_start(12)
        # Shumate hosts a scale ruler + license banner in the bottom-left
        # corner; lift the speed-limit sign 50 px higher so it clears them.
        wrap.set_margin_bottom(86 if self._backend == "shumate" else 36)
        wrap.set_can_target(False)
        wrap.set_visible(False)

        sign = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sign.add_css_class("dp-speed-sign")
        sign.set_halign(Gtk.Align.CENTER)
        sign.set_valign(Gtk.Align.CENTER)
        sign.set_size_request(130, 130)

        lbl = Gtk.Label(label="")
        lbl.set_halign(Gtk.Align.CENTER)
        lbl.set_valign(Gtk.Align.CENTER)
        lbl.set_yalign(0.18)
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
        # Grid layout:
        # Row 0, Col 0: start button
        # Row 1, Col 0: icon_row — [info toggle] [notepad_slot]
        #   notepad_slot holds notepad button AND info card (one visible at a time),
        #   so the card opens exactly at the notepad button's position.
        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        grid.set_halign(Gtk.Align.START)
        grid.set_valign(Gtk.Align.START)
        grid.set_margin_start(12)
        grid.set_margin_top(12)
        self._tour_controls_box = grid

        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._tour_btn_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        inner.append(self._tour_btn_icon)
        self._tour_start_lbl = Gtk.Label(label=_translate(self.language, "map.tour_start"))
        inner.append(self._tour_start_lbl)

        self._tour_start_btn = Gtk.Button()
        self._tour_start_btn.set_child(inner)
        self._tour_start_btn.add_css_class("osd")
        self._tour_start_btn.set_halign(Gtk.Align.START)
        self._tour_start_btn.connect("clicked", self._on_tour_start_clicked)
        grid.attach(self._tour_start_btn, 0, 0, 1, 1)

        # Abort button shows only while the tour is paused ("Resume tour"
        # label on the left). Click fully resets the tour via _abort_tour.
        abort_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        abort_inner.append(Gtk.Image.new_from_icon_name("window-close-symbolic"))
        abort_inner.append(Gtk.Label(label=_translate(self.language, "map.tour_abort")))
        self._tour_abort_btn = Gtk.Button()
        self._tour_abort_btn.set_child(abort_inner)
        self._tour_abort_btn.add_css_class("osd")
        self._tour_abort_btn.add_css_class("destructive-action")
        # dp-abort-tour cranks the background back up to near-opaque so
        # the abort affordance pops against the map instead of fading
        # into the OSD-typical translucency.
        self._tour_abort_btn.add_css_class("dp-abort-tour")
        self._tour_abort_btn.set_halign(Gtk.Align.END)
        self._tour_abort_btn.set_visible(False)
        self._tour_abort_btn.connect("clicked", lambda _b: self._abort_tour())
        grid.attach(self._tour_abort_btn, 1, 0, 1, 1)

        # "Nächstes Ziel" button — visible only during active navigation when
        # the car is within 200 m of an intermediate waypoint.  Shown to the
        # right of the pause button; abort and next-wp are mutually exclusive
        # (abort = paused, next-wp = active) so they never overlap.
        next_wp_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        next_wp_inner.append(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        next_wp_inner.append(Gtk.Label(label=_translate(self.language, "map.next_waypoint")))
        self._next_wp_btn = Gtk.Button()
        self._next_wp_btn.set_child(next_wp_inner)
        self._next_wp_btn.add_css_class("osd")
        self._next_wp_btn.set_halign(Gtk.Align.END)
        self._next_wp_btn.set_visible(False)
        self._next_wp_btn.connect("clicked", self._on_next_wp_clicked)
        grid.attach(self._next_wp_btn, 2, 0, 1, 1)

        icon_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        icon_row.set_halign(Gtk.Align.START)
        icon_row.set_valign(Gtk.Align.START)

        steps_icon = Gtk.Image.new_from_icon_name("info-symbolic")
        steps_icon.set_pixel_size(20)
        self._steps_toggle_btn = Gtk.ToggleButton()
        self._steps_toggle_btn.set_child(steps_icon)
        self._steps_toggle_btn.add_css_class("osd")
        self._steps_toggle_btn.add_css_class("circular")
        self._steps_toggle_btn.set_halign(Gtk.Align.START)
        self._steps_toggle_btn.set_valign(Gtk.Align.START)
        self._steps_toggle_btn.set_size_request(40, 40)
        self._steps_toggle_btn.set_tooltip_text(_translate(self.language, "map.steps.toggle"))
        self._steps_toggle_btn.connect("toggled", self._on_steps_toggle)

        # Left column: info toggle on top, chart-restore icon directly below.
        info_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        info_col.set_halign(Gtk.Align.START)
        info_col.set_valign(Gtk.Align.START)
        info_col.append(self._steps_toggle_btn)
        info_col.append(self._build_replay_chart_restore_btn())
        icon_row.append(info_col)

        # notepad_slot: notepad button and info card share the same position.
        # Clicking the notepad button swaps them — the card opens where the
        # button was, and minimising the card brings the button back.
        notepad_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        notepad_slot.set_valign(Gtk.Align.START)
        notepad_slot.append(self._build_replay_info_restore_btn())
        notepad_slot.append(self._build_replay_info_overlay())
        icon_row.append(notepad_slot)

        # Route info (duration + distance) to the right of the notepad icon.
        # Duration and distance stack vertically; the .dp-route-info-osd
        # class gives the same translucent OSD background used by the
        # neighbouring "Start Tour" button so the two cards visually match.
        _install_maneuver_css()
        route_info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        route_info_box.add_css_class("dp-route-info-osd")
        route_info_box.set_valign(Gtk.Align.CENTER)
        route_info_box.set_visible(False)
        route_info_duration_lbl = Gtk.Label(label="")
        route_info_duration_lbl.set_xalign(0.0)
        route_info_duration_lbl.set_halign(Gtk.Align.START)
        route_info_distance_lbl = Gtk.Label(label="")
        route_info_distance_lbl.set_xalign(0.0)
        route_info_distance_lbl.set_halign(Gtk.Align.START)
        route_info_box.append(route_info_duration_lbl)
        route_info_box.append(route_info_distance_lbl)
        icon_row.append(route_info_box)
        self._route_info_overlay = route_info_box
        self._route_info_duration_lbl = route_info_duration_lbl
        self._route_info_distance_lbl = route_info_distance_lbl

        # Let icon_row span all 3 button columns so the route info can use the
        # full available width without being clipped by column boundaries.
        grid.attach(icon_row, 0, 1, 3, 1)

        grid.set_visible(False)
        return grid

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
