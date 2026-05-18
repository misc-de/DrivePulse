"""Main DrivePulse application window."""
from __future__ import annotations

import atexit
import math
import time
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .common import (
    DB_FILE,
    _detect_language,
    _make_label_responsive,
    _translate,
)
from .gauge import Gauge, GAUGE_THEMES, all_theme_options, get_theme_css
from .dashboard import DashboardCanvas, DASHBOARD_THEMES
from .dashboard_layout import DashboardLayoutMixin
from .acceleration import AccelerationPage
from .cars import CarsPage
from .map_page import MapPage
from .dashcam_page import DashcamPage
from .dashboard_telemetry import DashboardTelemetryMixin
from .db import DriveDB
from .dashboard_settings import DashboardSettingsMixin
from .gps_reader import GpsReader
from .mock_tour import MockTourSimulator
from .orientation_reader import OrientationReader
from .obd_reader import ObdReader
from .trip_recorder import TripRecorder


class DashboardWindow(DashboardSettingsMixin, DashboardLayoutMixin, DashboardTelemetryMixin, Adw.ApplicationWindow):
    __gtype_name__ = "DashboardWindow"

    PAGE_DASHBOARD = "dashboard"
    PAGE_ACCELERATION = "acceleration"
    PAGE_CARS = "cars"
    PAGE_MAP = "map"
    PAGE_DASHCAM = "dashcam"

    # Fensterbreite, unterhalb derer die Autos-Detailansicht ihre Kategorienleiste
    # auf Icon-only umschaltet (Phosh/Mobian-typische Portrait-Breiten 360–540 px).
    CARS_NARROW_BREAKPOINT = 500

    # Seconds to keep GPS shown as "available" after the last valid fix
    GPS_UNAVAIL_HOLDOVER = 1.0

    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title=_translate(_detect_language(), "window.title"))
        self.set_default_size(980, 520)
        self.settings = self._load_settings()
        self.units = self.settings["units"]
        self.language = self.settings["language"]
        self.mock_mode = self.settings["mock_mode"]
        self.obd_port: str | None = self.settings.get("obd_port")
        self.gauge_theme: str = self.settings.get("gauge_theme", "cockpit")
        self.sidebar_side: str = self.settings.get("sidebar_side", "left")
        self.theme_mode: str = self.settings.get("theme_mode", "auto")
        self.force_webkit_map: bool = bool(self.settings.get("force_webkit_map", False))
        # POIs are deliberately not persisted — they're a performance hit, so
        # the map always starts without POI loading until the user toggles it.
        self.map_traffic_visible: bool = bool(self.settings.get("map_traffic_visible", False))
        self.last_update_check: str | None = self.settings.get("last_update_check")
        self.dashcam_camera: str = self.settings.get("dashcam_camera", "/dev/video0")
        self.dashcam_resolution: str = self.settings.get("dashcam_resolution", "1280x720")
        self.dashcam_seg_minutes: int = int(self.settings.get("dashcam_seg_minutes", 3))
        self.dashcam_max_segments: int = int(self.settings.get("dashcam_max_segments", 10))
        self.dashcam_dim_timeout: int = int(self.settings.get("dashcam_dim_timeout", 30))
        self.dashcam_rolling_dir: str = self.settings.get("dashcam_rolling_dir", "")
        self.dashcam_saved_dir: str = self.settings.get("dashcam_saved_dir", "")
        self.nav_position: str = self.settings.get("nav_position", "bottom")
        self.dashcam_gps_osd: bool = bool(self.settings.get("dashcam_gps_osd", False))
        self.last_payload: dict[str, Any] | None = None
        self._gps_last_seen: float = 0.0
        self._last_gps_lat: float | None = None
        self._last_gps_lon: float | None = None

        # Lock screen auto-rotation for the session so the display doesn't
        # spin when the phone vibrates while mounted in the car.
        self._saved_rotation_setting: tuple[str, str, str] | None = None
        self._lock_screen_rotation()
        atexit.register(self._unlock_screen_rotation)

        # Persistente Fahrten-Datenbank (cars/trips/samples) — vor allen Pages,
        # weil CarsPage sie injiziert bekommt.
        self.db = DriveDB(DB_FILE)
        self.trip_recorder = TripRecorder(self.db)
        atexit.register(self._shutdown_db)

        # Live-Trip-Statistik (min/max) für das Dashboard
        self._live_trip_id: int | None = None   # letzter bekannter trip_id
        self._live_rpm_min: float | None = None
        self._live_rpm_max: float | None = None
        self._live_coolant_min: float | None = None
        self._live_coolant_max: float | None = None
        self._live_speed_max: float | None = None

        self.rpm_gauge = Gauge(_translate(self.language, "gauge.rpm"), "rpm", 0, 7000, (0.34, 0.62, 0.86), self.gauge_theme)
        speed_unit = "km/h" if self.units == "metric" else "mph"
        speed_max = 240 if self.units == "metric" else 150
        self.speed_gauge = Gauge(_translate(self.language, "gauge.speed"), speed_unit, 0, speed_max, (0.50, 0.72, 0.92), self.gauge_theme)
        self.temp_gauge = Gauge(_translate(self.language, "gauge.coolant"), "°C", 40, 130, (0.72, 0.32, 0.48), self.gauge_theme)

        self.status_label = _make_label_responsive(Gtk.Label(label=_translate(self.language, "status.connecting")), 36, 0.5)
        self.status_label.add_css_class("dim-label")
        self.gauge_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self.gauge_box.add_css_class("dp-gauge-bg")
        self.gauge_box.set_halign(Gtk.Align.FILL)
        self.gauge_box.set_valign(Gtk.Align.FILL)
        self.gauge_box.set_hexpand(True)
        self.gauge_box.set_vexpand(True)

        for gauge in (self.rpm_gauge, self.speed_gauge, self.temp_gauge):
            gauge.set_hexpand(True)
            gauge.set_vexpand(True)
            gauge.set_halign(Gtk.Align.FILL)
            gauge.set_valign(Gtk.Align.FILL)
            self.gauge_box.append(gauge)

        self.scan_bar = Gtk.ProgressBar()
        self.scan_bar.set_show_text(True)
        self.scan_bar.set_hexpand(True)
        self.scan_bar.set_visible(False)

        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        footer.set_halign(Gtk.Align.FILL)
        footer.set_hexpand(True)
        footer.append(self.scan_bar)

        self.footer = footer

        self.dashboard_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.dashboard_page.set_margin_top(12)
        self.dashboard_page.set_margin_bottom(12)
        self.dashboard_page.set_margin_start(12)
        self.dashboard_page.set_margin_end(12)
        self.dashboard_page.add_css_class("dp-gauge-bg")

        self.dashboard_canvas = DashboardCanvas(self.gauge_theme, self.units, self.language)
        self.dashboard_canvas.set_hexpand(True)
        self.dashboard_canvas.set_vexpand(True)
        self.dashboard_canvas.set_halign(Gtk.Align.FILL)
        self.dashboard_canvas.set_valign(Gtk.Align.FILL)

        _is_dash = self.gauge_theme in DASHBOARD_THEMES
        self.gauge_box.set_visible(not _is_dash)
        self.dashboard_canvas.set_visible(_is_dash)
        if _is_dash:
            for setter in (
                self.dashboard_page.set_margin_top,
                self.dashboard_page.set_margin_bottom,
                self.dashboard_page.set_margin_start,
                self.dashboard_page.set_margin_end,
            ):
                setter(0)

        self.dashboard_page.append(self.gauge_box)
        self.dashboard_page.append(self.dashboard_canvas)
        self.dashboard_page.append(footer)

        dashboard_scroller = Gtk.ScrolledWindow()
        dashboard_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        dashboard_scroller.set_propagate_natural_width(False)
        dashboard_scroller.set_propagate_natural_height(False)
        dashboard_scroller.set_child(self.dashboard_page)

        self.acceleration_page = AccelerationPage(self.language)
        self.acceleration_page.set_theme(self.gauge_theme)
        self.acceleration_page.set_engage_threshold(self.settings.get("engage_threshold", 0.20))
        self.acceleration_page.on_engage_threshold_changed = self._on_engage_threshold_changed
        self.acceleration_page.on_run_complete = self._on_acceleration_run_complete
        acceleration_scroller = Gtk.ScrolledWindow()
        acceleration_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        acceleration_scroller.set_propagate_natural_width(False)
        acceleration_scroller.set_propagate_natural_height(False)
        acceleration_scroller.set_hexpand(True)
        acceleration_scroller.set_vexpand(True)
        acceleration_scroller.set_child(self.acceleration_page)

        self.cars_page = CarsPage(self.language, db=self.db, sidebar_side=self.sidebar_side)
        self.cars_page.on_back_swipe = self._on_cars_back_swipe
        self.cars_page.on_forward_swipe = self._on_cars_forward_swipe
        self.cars_page.on_live_vehicle_add = self._add_live_vehicle_from_identity
        self.cars_page.set_header_trash_fn = self.set_ctx_trash

        self.mock_tour_sim = MockTourSimulator(self._update_from_payload)
        self._pending_sim_start_id: int | None = None
        self.map_page = MapPage(
            self.language,
            force_webkit=self.force_webkit_map,
            units=self.units,
            poi_visible=False,
            traffic_visible=self.map_traffic_visible,
            on_traffic_visible_changed=self._set_map_traffic_visible,
            on_tour_started=self._on_tour_started,
            on_tour_stopped=self._on_tour_stopped,
            on_tour_resumed=self._on_tour_resumed,
        )
        self.dashcam_page = DashcamPage(self.language)
        self.dashcam_page.set_camera(self.dashcam_camera)
        self.dashcam_page.set_resolution(self.dashcam_resolution)
        self.dashcam_page.set_segment_minutes(self.dashcam_seg_minutes)
        self.dashcam_page.set_max_segments(self.dashcam_max_segments)
        self.dashcam_page.set_dim_timeout(self.dashcam_dim_timeout)
        self.dashcam_page.set_rolling_dir(self.dashcam_rolling_dir)
        self.dashcam_page.set_saved_dir(self.dashcam_saved_dir)
        self.dashcam_page.set_gps_osd(
            bool(self.settings.get("dashcam_gps_osd", False))
        )
        self.dashcam_page.set_units(self.units)
        self.dashcam_page.on_recording_changed = self._on_dashcam_recording_changed

        self.view_stack = Adw.ViewStack()
        self.view_stack.set_vexpand(True)
        self.view_stack.set_hexpand(True)
        self.view_stack.set_hhomogeneous(False)
        self.view_stack.set_vhomogeneous(False)
        self.view_stack.set_enable_transitions(True)
        self.view_stack.set_transition_duration(240)
        self.cars_stack_page = self.view_stack.add_titled_with_icon(
            self.cars_page,
            self.PAGE_CARS,
            _translate(self.language, "nav.cars"),
            "driving-symbolic",
        )
        self.map_stack_page = self.view_stack.add_titled_with_icon(
            self.map_page,
            self.PAGE_MAP,
            _translate(self.language, "nav.map"),
            "navigate-north",
        )
        self.dashcam_stack_page = self.view_stack.add_titled_with_icon(
            self.dashcam_page,
            self.PAGE_DASHCAM,
            _translate(self.language, "nav.dashcam"),
            "camera-video-symbolic",
        )
        self.dashboard_stack_page = self.view_stack.add_titled_with_icon(
            dashboard_scroller,
            self.PAGE_DASHBOARD,
            _translate(self.language, "nav.gauges"),
            "speedometer4-symbolic",
        )
        self.acceleration_stack_page = self.view_stack.add_titled_with_icon(
            acceleration_scroller,
            self.PAGE_ACCELERATION,
            _translate(self.language, "nav.acceleration"),
            "stopwatch-symbolic",
        )

        self.view_stack.connect("notify::visible-child-name", self._on_visible_page_changed)

        swipe = Gtk.GestureSwipe()
        swipe.connect("swipe", self._on_swipe)
        self.view_stack.add_controller(swipe)

        switcher_bar = Adw.ViewSwitcherBar()
        switcher_bar.set_stack(self.view_stack)
        switcher_bar.set_reveal(True)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title_label = Gtk.Label(label=_translate(self.language, "window.title"))
        header.set_title_widget(self.title_label)

        self.obd_indicator = self._build_link_indicator("network-wired-symbolic", _translate(self.language, "status.obd"))
        self.gps_indicator = self._build_link_indicator("find-location-symbolic", _translate(self.language, "status.gps"))
        settings_button = Gtk.Button(icon_name="emblem-system-symbolic")
        self.settings_button = settings_button
        settings_button.set_tooltip_text(_translate(self.language, "settings.tooltip"))
        settings_button.connect("clicked", self._open_settings)

        self._ctx_trash_btn = Gtk.Button(icon_name="user-trash-symbolic")
        self._ctx_trash_btn.add_css_class("flat")
        self._ctx_trash_btn.set_visible(False)
        self._ctx_trash_handler: int | None = None

        self._sync_btn = Gtk.Button(icon_name="share-alt-symbolic")
        self._sync_btn.set_tooltip_text(_translate(self.language, "sync.tooltip"))
        self._sync_btn.connect("clicked", self._open_sync)

        # REC indicator — shown when dashcam is recording in the background
        self._dashcam_rec_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._dashcam_rec_box.set_visible(False)
        _rec_dot = Gtk.Label(label="●")
        _rec_dot.add_css_class("error")
        self._dashcam_rec_box.append(_rec_dot)
        _rec_lbl = Gtk.Label(label="REC")
        _rec_lbl.add_css_class("caption-heading")
        self._dashcam_rec_box.append(_rec_lbl)

        header.pack_start(self.obd_indicator["box"])
        header.pack_start(self.gps_indicator["box"])
        header.pack_start(self._dashcam_rec_box)
        header.pack_end(settings_button)
        header.pack_end(self._sync_btn)
        header.pack_end(self._ctx_trash_btn)

        switcher_top = Adw.ViewSwitcherBar()
        switcher_top.set_stack(self.view_stack)

        self.header        = header
        self.switcher_bar  = switcher_bar        # bottom bar (default)
        self.switcher_top  = switcher_top
        self.toolbar_view  = toolbar_view
        toolbar_view.add_top_bar(header)
        toolbar_view.add_top_bar(switcher_top)
        toolbar_view.add_bottom_bar(switcher_bar)
        self._apply_nav_position(self.nav_position)
        toolbar_view.set_content(self.view_stack)

        self._nav_visible = True
        self._last_swipe_time = 0.0
        self._tap_press_time = 0.0
        self._tap_press_x = 0.0
        self._tap_press_y = 0.0
        tap = Gtk.GestureClick()
        tap.connect("pressed", self._on_content_press)
        tap.connect("released", self._on_content_tap)
        self.view_stack.add_controller(tap)

        self.set_content(toolbar_view)
        self.connect("notify::default-width", self._on_size_changed)
        self.connect("notify::default-height", self._on_size_changed)
        self.add_tick_callback(self._layout_tick)
        GLib.idle_add(self._on_size_changed)

        self._theme_css_provider = Gtk.CssProvider()
        self.connect("realize", self._on_realize_install_css)

        self._obd_active = False

        GLib.idle_add(self._load_initial_scan_data)

        self.reader = ObdReader(self._update_from_payload, force_mock=self.mock_mode)
        self.reader._configured_port = self.obd_port
        self.acceleration_page.on_mock_start = self.reader.trigger_mock_acceleration
        self.reader.start()
        self.gps_reader = GpsReader(self._update_from_payload)
        self.gps_reader.start()
        self.orientation_reader = OrientationReader(self._on_orientation_changed)
        self.orientation_reader.on_gforce = self.acceleration_page.update_gforce_raw

    def _on_dashcam_recording_changed(self, recording: bool) -> None:
        self._dashcam_rec_box.set_visible(recording)

    def _on_orientation_changed(self, _name: str, angle: int, is_landscape: bool) -> None:
        self.dashcam_page.update_orientation(angle, is_landscape)

        # Idle-Erkennung + WAL-Checkpoint alle 30 s
        GLib.timeout_add_seconds(30, self._db_periodic_tick)

    def _build_link_indicator(self, icon_name: str, label_text: str) -> dict[str, Any]:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.add_css_class("dim-label")
        image = Gtk.Image(icon_name=icon_name)
        spinner = Gtk.Spinner()
        spinner.set_visible(False)
        label = Gtk.Label(label=label_text)
        box.append(spinner)
        box.append(image)
        box.append(label)
        return {"box": box, "image": image, "spinner": spinner, "label": label}

    def _set_link_indicator(self, indicator: dict[str, Any], connected: bool, connecting: bool = False) -> None:
        box = indicator["box"]
        spinner = indicator["spinner"]
        image = indicator["image"]
        box.remove_css_class("dim-label")
        box.remove_css_class("success")
        if connected:
            box.add_css_class("success")
        else:
            box.add_css_class("dim-label")
        spinner.set_visible(connecting)
        image.set_visible(not connecting)
        if connecting:
            spinner.start()
        else:
            spinner.stop()

    # Maximum duration / movement that still counts as a "short tap"
    _TAP_MAX_DURATION_S = 0.30
    _TAP_MAX_MOVE_PX = 14.0

    def _on_content_press(self, _gesture: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
        self._tap_press_time = time.monotonic()
        self._tap_press_x = x
        self._tap_press_y = y

    def _on_content_tap(self, _gesture: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
        now = time.monotonic()
        # Reject if a swipe just fired — its release event still reaches the click gesture
        if now - self._last_swipe_time < 0.35:
            return
        # Reject if the touch lasted too long (long-press) or moved too far (swipe/drag)
        duration = now - self._tap_press_time
        moved = math.hypot(x - self._tap_press_x, y - self._tap_press_y)
        if duration > self._TAP_MAX_DURATION_S or moved > self._TAP_MAX_MOVE_PX:
            return
        # Auf der Autos-Seite muss die Navigation jederzeit erreichbar bleiben,
        # damit der Anwender zurück zu Tachos/Beschleunigung kommt.
        if self.view_stack.get_visible_child_name() == self.PAGE_CARS:
            self._set_nav_visible(True)
            return
        self._set_nav_visible(not self._nav_visible)

    def _set_nav_visible(self, visible: bool) -> None:
        self._nav_visible = visible
        self.header.set_visible(visible)
        self.switcher_bar.set_visible(visible)
        self.footer.set_visible(visible)
        if self.view_stack.get_visible_child_name() == self.PAGE_MAP:
            self.map_page.set_nav_visible(visible)

    def _on_visible_page_changed(self, _stack: Adw.ViewStack, _pspec: Any) -> None:
        page = self.view_stack.get_visible_child_name()
        if page == self.PAGE_CARS:
            if not self._nav_visible:
                self._set_nav_visible(True)
        else:
            self.set_ctx_trash(None)
        if page == self.PAGE_MAP:
            GLib.timeout_add(50, self.map_page.on_shown)

        # Dashcam preview is started lazily and torn down when the user leaves
        # the tab — except the recorder keeps running across tab switches so a
        # tour is recorded end-to-end regardless of which tab is in front.
        prev = getattr(self, "_last_visible_page", None)
        if page == self.PAGE_DASHCAM:
            self.dashcam_page.on_shown()
        elif prev == self.PAGE_DASHCAM:
            self.dashcam_page.on_hidden()
        self._last_visible_page = page

    # Hold the simulated drive for this long after the tour starts, matching
    # mapStartTour's camera settle window in map.html so the car doesn't pull
    # away while the user is still reading the freshly opened navigation card.
    _TOUR_SETTLE_MS = 3000

    def _on_tour_started(self, coords: list[list[float]]) -> None:
        self._cancel_pending_sim_start()
        if not self.mock_mode:
            return
        coords_copy = list(coords)
        self._pending_sim_start_id = GLib.timeout_add(
            self._TOUR_SETTLE_MS, self._start_mock_sim_delayed, coords_copy
        )

    def _start_mock_sim_delayed(self, coords: list[list[float]]) -> bool:
        self._pending_sim_start_id = None
        if self.mock_mode:
            self.mock_tour_sim.start(coords)
        return False  # one-shot

    def _cancel_pending_sim_start(self) -> None:
        if self._pending_sim_start_id is not None:
            GLib.source_remove(self._pending_sim_start_id)
            self._pending_sim_start_id = None

    def _on_tour_stopped(self) -> None:
        self._cancel_pending_sim_start()
        self.mock_tour_sim.stop()

    def _on_tour_resumed(self) -> None:
        if self.mock_mode:
            self.mock_tour_sim.resume()

    def _on_cars_back_swipe(self) -> None:
        """Vom Autos-Tab (Liste) per Wisch nach rechts — kein Tab (Cars ist erster Tab)."""
        pass

    def _on_cars_forward_swipe(self) -> None:
        """Vom Autos-Tab (Liste) per Wisch nach links zur Karte."""
        if self.view_stack.get_visible_child_name() == self.PAGE_CARS:
            self.view_stack.set_visible_child_name(self.PAGE_MAP)
            self._last_swipe_time = time.monotonic()

    def _on_realize_install_css(self, *_args: Any) -> None:
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), self._theme_css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self._apply_theme_mode(self.theme_mode)
        self._apply_window_theme(self.gauge_theme)

    def _apply_theme_mode(self, mode: str) -> None:
        from gi.repository import Adw
        manager = Adw.StyleManager.get_default()
        if mode == "dark":
            manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        elif mode == "light":
            manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        else:
            manager.set_color_scheme(Adw.ColorScheme.DEFAULT)

    def _lock_screen_rotation(self) -> None:
        """Disable compositor auto-rotation for this session (Phosh/GNOME)."""
        import subprocess
        candidates = [
            ("org.gnome.settings-daemon.plugins.orientation", "active", "false"),
            ("org.gnome.desktop.interface", "orientation-lock", "true"),
        ]
        for schema, key, lock_value in candidates:
            try:
                r = subprocess.run(
                    ["gsettings", "get", schema, key],
                    capture_output=True, text=True, timeout=2,
                )
                if r.returncode != 0:
                    continue
                previous = r.stdout.strip()
                subprocess.run(
                    ["gsettings", "set", schema, key, lock_value],
                    timeout=2, capture_output=True,
                )
                self._saved_rotation_setting = (schema, key, previous)
                log.info("Screen rotation locked via %s %s", schema, key)
                return
            except Exception:
                pass

    def _unlock_screen_rotation(self) -> None:
        """Restore auto-rotation setting saved at startup."""
        if not self._saved_rotation_setting:
            return
        schema, key, previous = self._saved_rotation_setting
        import subprocess
        try:
            subprocess.run(
                ["gsettings", "set", schema, key, previous],
                timeout=2, capture_output=True,
            )
        except Exception:
            pass

    def _apply_nav_position(self, position: str) -> None:
        at_top = position == "top"
        self.switcher_top.set_reveal(at_top)
        self.switcher_bar.set_reveal(not at_top)

    def _apply_window_theme(self, theme: str) -> None:
        for cls in list(self.get_css_classes()):
            if cls.startswith("dp-theme-"):
                self.remove_css_class(cls)
        safe = theme.replace(":", "-").replace("_", "-")
        self.add_css_class(f"dp-theme-{safe}")
        # In light mode, don't override Libadwaita's natural light colours with
        # the gauge theme's hardcoded dark backgrounds.
        if getattr(self, "theme_mode", "auto") == "light":
            self._theme_css_provider.load_from_data(b"")
        else:
            css = get_theme_css(theme)
            self._theme_css_provider.load_from_data(css.encode() if css else b"")

    def close(self) -> bool:
        self.reader.stop()
        self.gps_reader.stop()
        self.orientation_reader.stop()
        return super().close()

    def set_ctx_trash(self, action_fn: Any) -> None:
        """Show/hide the context trash button in the header and wire up its action."""
        btn = self._ctx_trash_btn
        if self._ctx_trash_handler is not None:
            btn.disconnect(self._ctx_trash_handler)
            self._ctx_trash_handler = None
        if action_fn is not None:
            self._ctx_trash_handler = btn.connect("clicked", lambda _b: action_fn())
            btn.set_visible(True)
        else:
            btn.set_visible(False)


    def _on_swipe(self, _gesture: Gtk.GestureSwipe, velocity_x: float, velocity_y: float) -> None:
        ax, ay = abs(velocity_x), abs(velocity_y)

        # Vertical swipe on the gauge/dashboard page → cycle through themes
        if ay > 220 and ay > ax and self.view_stack.get_visible_child_name() == self.PAGE_DASHBOARD:
            self._last_swipe_time = time.monotonic()
            self._cycle_theme(up=velocity_y < 0)
            return

        # Horizontal swipe → switch page
        if ax < 220 or ax <= ay:
            return
        self._last_swipe_time = time.monotonic()
        current = self.view_stack.get_visible_child_name()
        # Wenn das Auto-Detail offen ist, übernimmt Adw.NavigationView den
        # Zurück-Swipe (Detail → Liste). Wir schalten dann nicht zusätzlich den Tab um.
        if current == self.PAGE_CARS and velocity_x > 0 and self.cars_page.is_detail_open():
            return
        pages = [self.PAGE_CARS, self.PAGE_MAP, self.PAGE_DASHCAM, self.PAGE_DASHBOARD, self.PAGE_ACCELERATION]
        try:
            index = pages.index(current)
        except ValueError:
            index = 0
        if velocity_x < 0:
            self.view_stack.set_visible_child_name(pages[(index + 1) % len(pages)])
        elif velocity_x > 0 and index > 0:
            self.view_stack.set_visible_child_name(pages[index - 1])

    def _cycle_theme(self, up: bool) -> None:
        """Cycle to the next/previous theme via vertical swipe."""
        options = [tid for tid, _ in all_theme_options(self.language)]
        if not options:
            return
        try:
            idx = options.index(self.gauge_theme)
        except ValueError:
            idx = 0
        idx = (idx + (1 if up else -1)) % len(options)
        self._set_gauge_theme(options[idx])
