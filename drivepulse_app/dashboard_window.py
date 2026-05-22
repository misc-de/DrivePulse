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
from .stopwatch import StopWatchPage
from .cars import CarsPage
from .map_page import MapPage
from .dashcam_page import DashcamPage
from .dashboard_telemetry import DashboardTelemetryMixin
from .db import DriveDB
from .dashboard_settings import DashboardSettingsMixin
from .diagnostics import set_log_enabled
from .gps_reader import GpsReader
from .mock_tour import MockTourSimulator
from .orientation_reader import OrientationReader
from .obd_reader import ObdReader
from .rotation import RotationProvider
from .rotated_container import RotatedContainer
from .trip_recorder import TripRecorder


class DashboardWindow(DashboardSettingsMixin, DashboardLayoutMixin, DashboardTelemetryMixin, Adw.ApplicationWindow):
    __gtype_name__ = "DashboardWindow"

    PAGE_DASHBOARD = "dashboard"
    PAGE_STOPWATCH = "stopwatch"
    PAGE_CARS = "cars"
    PAGE_MAP = "map"
    PAGE_DASHCAM = "dashcam"

    # Fensterbreite, unterhalb derer die Autos-Detailansicht ihre Kategorienleiste
    # auf Icon-only umschaltet (Phosh/Mobian-typische Portrait-Breiten 360–540 px).
    CARS_NARROW_BREAKPOINT = 500

    # Seconds to keep GPS shown as "available" after the last valid fix.
    # Must be well above the GPS update interval (~1 s for GeoClue) so that OBD
    # polls (every 0.5 s) between GPS updates don't falsely detect GPS as gone.
    GPS_UNAVAIL_HOLDOVER = 5.0

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
        self.map_traffic_bundesweit: bool = bool(self.settings.get("map_traffic_bundesweit", True))
        self.map_traffic_nrw: bool = bool(self.settings.get("map_traffic_nrw", False))
        self.map_3d_view: bool = bool(self.settings.get("map_3d_view", True))
        self.last_update_check: str | None = self.settings.get("last_update_check")
        self.dashcam_camera: str = self.settings.get("dashcam_camera", "/dev/video0")
        self.dashcam_resolution: str = self.settings.get("dashcam_resolution", "1280x720")
        self.dashcam_fps: int = int(self.settings.get("dashcam_fps", 25))
        self.dashcam_seg_minutes: int = int(self.settings.get("dashcam_seg_minutes", 3))
        self.dashcam_max_segments: int = int(self.settings.get("dashcam_max_segments", 10))
        self.dashcam_dim_timeout: int = int(self.settings.get("dashcam_dim_timeout", 30))
        self.dashcam_rolling_dir: str = self.settings.get("dashcam_rolling_dir", "")
        self.dashcam_saved_dir: str = self.settings.get("dashcam_saved_dir", "")
        self.nav_position: str = self.settings.get("nav_position", "bottom")
        self.dashcam_gps_osd: bool = bool(self.settings.get("dashcam_gps_osd", False))
        self.dashcam_speed_osd: bool = bool(self.settings.get("dashcam_speed_osd", False))
        self.rotation_mode: str = self.settings.get("rotation_mode", "follow_sensor")
        self.tts_enabled: bool = bool(self.settings.get("tts_enabled", True))
        self.tts_backend: str = self.settings.get("tts_backend", "espeak")
        self.tts_language: str = self.settings.get("tts_language", "auto")
        self.tts_voice: str = self.settings.get("tts_voice", "female")
        self.log_app_enabled: bool = bool(self.settings.get("log_app_enabled", True))
        self.log_obd_enabled: bool = bool(self.settings.get("log_obd_enabled", True))
        self.vindecoder_api_key: str = self.settings.get("vindecoder_api_key") or ""
        self.vindecoder_secret_key: str = self.settings.get("vindecoder_secret_key") or ""
        self.autodev_api_key: str = self.settings.get("autodev_api_key") or ""
        self.last_payload: dict[str, Any] | None = None
        self._gps_last_seen: float = 0.0
        self._last_gps_lat: float | None = None
        self._last_gps_lon: float | None = None
        self._last_gps_speed_kmh: float | None = None
        self._gps_was_connected: bool = False

        # Rotation state: pages can bind to either "follow_sensor"
        # (compensates for the compositor transform) or "follow_system"
        # (lets the compositor handle rotation). See drivepulse_app/rotation.py.
        self.rotation = RotationProvider(mode=self.rotation_mode)

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

        self._gauge_rotator = RotatedContainer()
        self._gauge_rotator.set_child(self.dashboard_page)
        self._gauge_rotator.set_hexpand(True)
        self._gauge_rotator.set_vexpand(True)
        dashboard_scroller = Gtk.ScrolledWindow()
        dashboard_scroller.add_css_class("dp-gauge-bg")
        dashboard_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        dashboard_scroller.set_propagate_natural_width(False)
        dashboard_scroller.set_propagate_natural_height(False)
        dashboard_scroller.set_child(self._gauge_rotator)

        self.stopwatch_page = StopWatchPage(self.language)
        self.stopwatch_page.set_theme(self.gauge_theme)
        self.stopwatch_page.set_engage_threshold(self.settings.get("engage_threshold", 0.20))
        self.stopwatch_page.on_engage_threshold_changed = self._on_engage_threshold_changed
        self.stopwatch_page.on_run_complete = self._on_stopwatch_run_complete
        self._stopwatch_rotator = RotatedContainer()
        self._stopwatch_rotator.set_child(self.stopwatch_page)
        self._stopwatch_rotator.set_hexpand(True)
        self._stopwatch_rotator.set_vexpand(True)
        stopwatch_scroller = Gtk.ScrolledWindow()
        stopwatch_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        stopwatch_scroller.set_propagate_natural_width(False)
        stopwatch_scroller.set_propagate_natural_height(False)
        stopwatch_scroller.set_hexpand(True)
        stopwatch_scroller.set_vexpand(True)
        stopwatch_scroller.set_child(self._stopwatch_rotator)

        self.cars_page = CarsPage(
            self.language,
            db=self.db,
            sidebar_side=self.sidebar_side,
            vindecoder_api_key=self.vindecoder_api_key or None,
            vindecoder_secret_key=self.vindecoder_secret_key or None,
        )
        self.cars_page._autodev_api_key = self.autodev_api_key or None
        self.cars_page.on_back_swipe = self._on_cars_back_swipe
        self.cars_page.on_forward_swipe = self._on_cars_forward_swipe
        self.cars_page.on_live_vehicle_add = self._add_live_vehicle_from_identity
        self.cars_page.get_sync_client = self._get_active_sync_client
        self._cars_rotator = RotatedContainer()
        self._cars_rotator.set_child(self.cars_page)
        self._cars_rotator.set_hexpand(True)
        self._cars_rotator.set_vexpand(True)

        self.mock_tour_sim = MockTourSimulator(self._update_from_payload)
        self._pending_sim_start_id: int | None = None
        self.map_page = MapPage(
            self.language,
            force_webkit=self.force_webkit_map,
            units=self.units,
            mock_mode=self.mock_mode,
            poi_visible=False,
            traffic_visible=self.map_traffic_visible,
            traffic_bundesweit=self.map_traffic_bundesweit,
            traffic_nrw=self.map_traffic_nrw,
            map_3d_view=self.map_3d_view,
            on_traffic_visible_changed=self._set_map_traffic_visible,
            on_3d_view_changed=self._set_map_3d_view,
            on_tour_started=self._on_tour_started,
            on_tour_stopped=self._on_tour_stopped,
            on_tour_resumed=self._on_tour_resumed,
            on_tts_enabled_changed=self._set_tts_enabled,
            db=self.db,
        )
        from . import tts_service as _tts_svc
        _tts_svc.set_backend(self.tts_backend)
        _tts_svc.set_download_callback(self._on_piper_dl_progress)
        self._piper_dl_current_model: str | None = None
        if self.tts_backend == "piper":
            _tts_svc.ensure_models(self.tts_language, self.tts_voice)
        self.map_page.set_tts_enabled(self.tts_enabled)
        self.map_page.set_tts_language(self.tts_language)
        self.map_page.set_tts_voice(self.tts_voice)
        self._map_rotator = RotatedContainer()
        self._map_rotator.set_child(self.map_page)
        self._map_rotator.set_hexpand(True)
        self._map_rotator.set_vexpand(True)

        self.dashcam_page = DashcamPage(self.language)
        self.dashcam_page.set_camera(self.dashcam_camera)
        self.dashcam_page.set_resolution(self.dashcam_resolution)
        self.dashcam_page.set_fps(self.dashcam_fps)
        self.dashcam_page.set_segment_minutes(self.dashcam_seg_minutes)
        self.dashcam_page.set_max_segments(self.dashcam_max_segments)
        self.dashcam_page.set_dim_timeout(self.dashcam_dim_timeout)
        self.dashcam_page.set_rolling_dir(self.dashcam_rolling_dir)
        self.dashcam_page.set_saved_dir(self.dashcam_saved_dir)
        self.dashcam_page.set_gps_osd(bool(self.settings.get("dashcam_gps_osd", False)))
        self.dashcam_page.set_speed_osd(bool(self.settings.get("dashcam_speed_osd", False)))
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
            self._cars_rotator,
            self.PAGE_CARS,
            _translate(self.language, "nav.cars"),
            "driving-symbolic",
        )
        self.map_stack_page = self.view_stack.add_titled_with_icon(
            self._map_rotator,
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
        self.stopwatch_stack_page = self.view_stack.add_titled_with_icon(
            stopwatch_scroller,
            self.PAGE_STOPWATCH,
            _translate(self.language, "nav.stopwatch"),
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

        self._sync_btn = Gtk.Button(icon_name="arrows-loop-symbolic")
        self._sync_btn.set_tooltip_text(_translate(self.language, "sync.tooltip"))
        self._sync_btn.connect("clicked", self._open_sync)

        self.obd_indicator["box"].set_margin_start(10)
        header.pack_start(self.obd_indicator["box"])
        header.pack_start(self.gps_indicator["box"])

        self._dashcam_rec_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._dashcam_rec_box.set_visible(False)
        _rec_dot = Gtk.Label(label="●")
        _rec_dot.add_css_class("error")
        self._dashcam_rec_box.append(_rec_dot)
        _rec_lbl = Gtk.Label(label="REC")
        _rec_lbl.add_css_class("caption-heading")
        self._dashcam_rec_box.append(_rec_lbl)
        header.pack_start(self._dashcam_rec_box)

        self._conflict_btn = Gtk.Button(icon_name="dialog-warning-symbolic")
        self._conflict_btn.add_css_class("flat")
        self._conflict_btn.set_tooltip_text(_translate(self.language, "share.conflicts_tooltip"))
        self._conflict_btn.set_visible(False)
        self._conflict_btn.connect("clicked", self._open_conflict_page)

        header.pack_end(settings_button)
        header.pack_end(self._sync_btn)
        header.pack_end(self._conflict_btn)

        switcher_top = Adw.ViewSwitcherBar()
        switcher_top.set_stack(self.view_stack)

        stack_overlay = Gtk.Overlay()
        stack_overlay.set_child(self.view_stack)
        stack_overlay.add_overlay(self._build_piper_dl_overlay())

        self.header            = header
        self.switcher_bar      = switcher_bar        # bottom bar (default)
        self.switcher_top      = switcher_top
        self._current_rotation  = 0
        self._last_sensor_angle = 0
        self.toolbar_view      = toolbar_view
        toolbar_view.add_top_bar(header)
        toolbar_view.add_top_bar(switcher_top)
        toolbar_view.add_bottom_bar(switcher_bar)
        self._apply_nav_position(self.nav_position)
        toolbar_view.set_content(stack_overlay)

        self._nav_visible = True
        self._dashcam_is_recording = False
        self._last_swipe_time = 0.0
        self._tap_press_time = 0.0
        self._tap_press_x = 0.0
        self._tap_press_y = 0.0
        tap = Gtk.GestureClick()
        tap.connect("pressed", self._on_content_press)
        tap.connect("released", self._on_content_tap)
        stack_overlay.add_controller(tap)

        main_page = Adw.NavigationPage(tag="main")
        main_page.set_child(toolbar_view)
        self.nav_view = Adw.NavigationView()
        self.nav_view.add(main_page)
        self.set_content(self.nav_view)
        self.connect("notify::default-width", self._on_size_changed)
        self.connect("notify::default-height", self._on_size_changed)
        self.add_tick_callback(self._layout_tick)
        GLib.idle_add(self._on_size_changed)

        self._nav_rotation_css = Gtk.CssProvider()
        self._theme_css_provider = Gtk.CssProvider()
        self.connect("realize", self._on_realize_install_css)

        self._obd_active = False

        GLib.idle_add(self._load_initial_scan_data)

        self.reader = ObdReader(self._update_from_payload, force_mock=self.mock_mode)
        self.reader._configured_port = self.obd_port
        self.reader.set_obd_log_enabled(self.log_obd_enabled)
        set_log_enabled(self.log_app_enabled)
        self.stopwatch_page.on_mock_start = self.reader.trigger_mock_stopwatch
        self.reader.start()
        self.gps_reader = GpsReader(self._update_from_payload, mock_mode=self.mock_mode)
        self.gps_reader.start()
        self.orientation_reader = OrientationReader(self._on_orientation_changed)
        self.orientation_reader.on_gforce = self.stopwatch_page.update_gforce_raw
        self.rotation.bind(self._apply_page_rotation)

    def _build_piper_dl_overlay(self) -> Gtk.Box:
        """Build the Piper download-progress overlay widget (initially hidden)."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.START)
        box.set_margin_top(12)
        box.add_css_class("osd")
        box.add_css_class("piper-dl-overlay")

        # Custom CSS for rounded corners + padding — applied lazily on first show.
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(b".piper-dl-overlay { border-radius: 14px; padding: 10px 16px; }")
        self._piper_dl_css_provider = css_provider
        self._piper_dl_css_installed = False

        icon = Gtk.Image(icon_name="emblem-downloads-symbolic")
        box.append(icon)

        self._piper_dl_label = Gtk.Label(label="Piper: …")
        box.append(self._piper_dl_label)

        self._piper_dl_bar = Gtk.ProgressBar()
        self._piper_dl_bar.set_size_request(140, -1)
        box.append(self._piper_dl_bar)

        self._piper_dl_cancel_btn = Gtk.Button(icon_name="process-stop-symbolic")
        self._piper_dl_cancel_btn.add_css_class("flat")
        self._piper_dl_cancel_btn.set_tooltip_text("Download abbrechen")
        box.append(self._piper_dl_cancel_btn)

        box.set_visible(False)
        self._piper_dl_overlay = box
        return box

    def _on_piper_dl_progress(self, model_name: str, fraction: float) -> bool:
        """Handle Piper download progress updates (called via GLib.idle_add from bg thread)."""
        from . import tts_service as _tts_svc
        if fraction >= 0.0 and fraction <= 1.0:
            # Active download progress
            self._piper_dl_current_model = model_name
            self._piper_dl_label.set_text(f"Piper: {model_name}")
            self._piper_dl_bar.set_fraction(fraction)
            # Re-wire cancel button to current model
            try:
                self._piper_dl_cancel_btn.disconnect_by_func(self._piper_dl_cancel_clicked)
            except Exception:
                pass
            self._piper_dl_cancel_btn.connect("clicked", self._piper_dl_cancel_clicked)
            self._piper_dl_overlay.set_visible(True)
            # Install CSS on first show if not yet realized
            if not getattr(self, "_piper_dl_css_installed", False):
                try:
                    display = self.get_display()
                    Gtk.StyleContext.add_provider_for_display(
                        display,
                        self._piper_dl_css_provider,
                        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
                    )
                    self._piper_dl_css_installed = True
                except Exception:
                    pass
        elif fraction == -1.0:
            # Cancelled or error
            self._piper_dl_overlay.set_visible(False)
            self._piper_dl_current_model = None
        elif fraction == 2.0:
            # Done
            self._piper_dl_overlay.set_visible(False)
            self._piper_dl_current_model = None
            try:
                self.add_toast(Adw.Toast(title="Piper bereit ✓"))
            except Exception:
                pass
        return False

    def _piper_dl_cancel_clicked(self, _btn: Gtk.Button) -> None:
        """Cancel the current Piper model download."""
        from . import tts_service as _tts_svc
        model = self._piper_dl_current_model
        if model:
            _tts_svc.cancel_download(model)

    def _on_dashcam_recording_changed(self, recording: bool) -> None:
        self._dashcam_is_recording = recording
        self._dashcam_rec_box.set_visible(recording)
        self.dashcam_stack_page.set_needs_attention(recording)
        self._refresh_dashcam_nav()

    def _refresh_dashcam_nav(self) -> None:
        on_cam = self.view_stack.get_visible_child_name() == self.PAGE_DASHCAM
        hide = self._dashcam_is_recording and on_cam
        visible = self._nav_visible and not hide
        self.header.set_visible(visible)
        self.switcher_bar.set_visible(visible)
        self.switcher_top.set_visible(visible)

    def _apply_page_rotation(self, angle: int) -> None:
        self._current_rotation = angle
        self._gauge_rotator.set_rotation(angle)
        self._stopwatch_rotator.set_rotation(angle)
        self._cars_rotator.set_rotation(angle)
        self._map_rotator.set_rotation(angle)
        self.dashcam_page.update_ui_rotation(angle)
        self._apply_nav_rotation(angle)
        GLib.idle_add(self._on_size_changed)

    def _apply_nav_rotation(self, angle: int) -> None:
        if angle == 0:
            css = b""
        else:
            css = (
                f".dp-nav-rotated button {{ padding: 0; }}"
                f".dp-nav-rotated button > * {{ margin: auto; }}"
                f".dp-nav-rotated button image {{ transform: rotate({angle}deg); margin: auto; padding: 10px; }}"
                f".dp-nav-rotated button label {{ opacity: 0; font-size: 0; min-width: 0; min-height: 0; margin: 0; padding: 0; }}"
            ).encode()
        self._nav_rotation_css.load_from_data(css)
        for bar in (self.switcher_bar, self.switcher_top):
            if angle != 0:
                bar.add_css_class("dp-nav-rotated")
            else:
                bar.remove_css_class("dp-nav-rotated")

    def _on_orientation_changed(self, _name: str, angle: int, is_landscape: bool) -> None:
        self._last_sensor_angle = angle
        self.rotation.set_sensor(angle)
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

    _GPS_REQUIRED_PAGES = frozenset(["dashboard", "stopwatch", "map"])

    def _on_visible_page_changed(self, _stack: Adw.ViewStack, _pspec: Any) -> None:
        page = self.view_stack.get_visible_child_name()
        if page == self.PAGE_CARS:
            if not self._nav_visible:
                self._set_nav_visible(True)
        if page == self.PAGE_MAP:
            GLib.timeout_add(50, self.map_page.on_shown)
        if page in self._GPS_REQUIRED_PAGES:
            self.gps_reader.ensure_active()

        # Dashcam preview is started lazily and torn down when the user leaves
        # the tab — except the recorder keeps running across tab switches so a
        # tour is recorded end-to-end regardless of which tab is in front.
        prev = getattr(self, "_last_visible_page", None)
        if page == self.PAGE_DASHCAM:
            self.dashcam_page.on_shown()
        elif prev == self.PAGE_DASHCAM:
            self.dashcam_page.on_hidden()
        self._last_visible_page = page

        # Hide nav on dashcam tab while recording; restore when leaving.
        self._refresh_dashcam_nav()

    # Hold the simulated drive for this long after the tour starts, matching
    # mapStartTour's camera settle window in map.html so the car doesn't pull
    # away while the user is still reading the freshly opened navigation card.
    _TOUR_SETTLE_MS = 3000

    def _on_tour_started(
        self,
        coords: list[list[float]],
        speed_zones: list[tuple[float, float]],
    ) -> None:
        self._cancel_pending_sim_start()
        if not self.mock_mode:
            return
        self._pending_sim_start_id = GLib.timeout_add(
            self._TOUR_SETTLE_MS,
            self._start_mock_sim_delayed,
            list(coords),
            list(speed_zones),
        )

    def _start_mock_sim_delayed(
        self,
        coords: list[list[float]],
        speed_zones: list[tuple[float, float]],
    ) -> bool:
        self._pending_sim_start_id = None
        if self.mock_mode:
            self.mock_tour_sim.start(coords, speed_zones)
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
        """Vom Autos-Tab (Übersicht) per Wisch nach rechts → StopWatch.

        Cars ist der erste Tab; ein Wisch nach rechts hätte sonst kein Ziel.
        Statt der Endlosschleife des ViewSwitchers springen wir direkt zum
        gegenüberliegenden Ende (StopWatch), damit die Geste nicht ins
        Leere läuft.
        """
        if self.view_stack.get_visible_child_name() == self.PAGE_CARS:
            self.view_stack.set_visible_child_name(self.PAGE_STOPWATCH)
            self._last_swipe_time = time.monotonic()

    def _on_cars_forward_swipe(self) -> None:
        """Vom Autos-Tab (Liste) per Wisch nach links zur Karte."""
        if self.view_stack.get_visible_child_name() == self.PAGE_CARS:
            self.view_stack.set_visible_child_name(self.PAGE_MAP)
            self._last_swipe_time = time.monotonic()

    def _on_realize_install_css(self, *_args: Any) -> None:
        from gi.repository import Adw
        display = self.get_display()
        Gtk.StyleContext.add_provider_for_display(
            display, self._theme_css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        Gtk.StyleContext.add_provider_for_display(
            display, self._nav_rotation_css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        _global_css = Gtk.CssProvider()
        _global_css.load_from_data(
            b".dp-table-row { border-radius: 0; }"
            b".dp-sync-online { color: #33d17a; }"
        )
        Gtk.StyleContext.add_provider_for_display(
            display, _global_css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self._apply_theme_mode(self.theme_mode)
        self._apply_window_theme(self.gauge_theme)
        Adw.StyleManager.get_default().connect("notify::dark", self._on_system_dark_changed)

    def _apply_theme_mode(self, mode: str) -> None:
        from gi.repository import Adw
        manager = Adw.StyleManager.get_default()
        if mode == "dark":
            manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        elif mode == "light":
            manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        else:
            manager.set_color_scheme(Adw.ColorScheme.DEFAULT)
        if hasattr(self, "stopwatch_page"):
            effective = "dark" if manager.get_dark() else "light"
            self.stopwatch_page.set_theme_mode(effective)

    def _on_system_dark_changed(self, _manager: Any, _param: Any) -> None:
        if getattr(self, "theme_mode", "auto") == "auto":
            self._apply_window_theme(self.gauge_theme)
            if hasattr(self, "stopwatch_page"):
                from gi.repository import Adw
                effective = "dark" if Adw.StyleManager.get_default().get_dark() else "light"
                self.stopwatch_page.set_theme_mode(effective)

    def _apply_nav_position(self, position: str) -> None:
        at_top = position == "top"
        self.switcher_top.set_reveal(at_top)
        self.switcher_bar.set_reveal(not at_top)

    def _apply_window_theme(self, theme: str) -> None:
        from gi.repository import Adw
        for cls in list(self.get_css_classes()):
            if cls.startswith("dp-theme-"):
                self.remove_css_class(cls)
        safe = theme.replace(":", "-").replace("_", "-")
        self.add_css_class(f"dp-theme-{safe}")
        mode = getattr(self, "theme_mode", "auto")
        is_dark = mode == "dark" or (mode == "auto" and Adw.StyleManager.get_default().get_dark())
        # Theme CSS contains broad window/toolbarview/scrolledwindow selectors that
        # override the entire app background.  Only load it when the gauge theme
        # variant matches the app colour scheme — a light gauge theme in a dark app
        # (or vice versa) would otherwise repaint the whole UI the wrong colour.
        # The gauge's Cairo drawing controls its own colours regardless of this CSS.
        is_light_theme = "_light" in theme
        # Dark themes always apply their background CSS — the user explicitly chose a dark
        # theme and expects a dark canvas regardless of the system colour scheme.
        # Light themes are suppressed in dark mode to avoid painting a white background
        # over the dark UI.
        load_css = not is_light_theme or not is_dark
        if load_css:
            css = get_theme_css(theme)
            self._theme_css_provider.load_from_data(css.encode() if css else b"")
        else:
            self._theme_css_provider.load_from_data(b"")

    def close(self) -> bool:
        self.reader.stop()
        self.gps_reader.stop()
        self.orientation_reader.stop()
        return super().close()

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
        pages = [self.PAGE_CARS, self.PAGE_MAP, self.PAGE_DASHCAM, self.PAGE_DASHBOARD, self.PAGE_STOPWATCH]
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

    def _get_active_sync_client(self) -> Any:
        from .sync_client import SyncClient
        dialog = getattr(self, "_active_sync_dialog", None)
        if dialog is None:
            return None
        client = getattr(dialog, "_active_client", None)
        return client if isinstance(client, SyncClient) else None

    def _update_conflict_badge(self) -> None:
        try:
            n = self.db.count_share_conflicts()
        except Exception:
            n = 0
        btn = getattr(self, "_conflict_btn", None)
        if btn is not None:
            btn.set_visible(n > 0)

    def _open_conflict_page(self, *_args: Any) -> None:
        if self.nav_view.find_page("share-conflicts") is not None:
            return
        t = lambda key: _translate(self.language, key)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=t("share.conflicts_title")))
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        toolbar_view.add_top_bar(header)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        outer.set_margin_top(16)
        outer.set_margin_bottom(16)
        outer.set_margin_start(16)
        outer.set_margin_end(16)

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_box.add_css_class("boxed-list")
        list_box.set_valign(Gtk.Align.START)
        outer.append(list_box)

        def _refresh() -> None:
            while True:
                child = list_box.get_first_child()
                if child is None:
                    break
                list_box.remove(child)
            try:
                conflicts = self.db.list_share_conflicts()
            except Exception:
                conflicts = []
            for c in conflicts:
                row = Adw.ActionRow()
                row.set_title(f"{c['type']} #{c['local_id']}")
                ts = c["received_at"][:16] if c["received_at"] else ""
                row.set_subtitle(ts)

                cid = int(c["id"])

                discard_btn = Gtk.Button(label=t("share.conflict_discard"))
                discard_btn.add_css_class("flat")
                discard_btn.set_valign(Gtk.Align.CENTER)

                def _discard(_btn: Gtk.Button, conflict_id: int = cid) -> None:
                    try:
                        self.db.discard_conflict(conflict_id)
                    except Exception:
                        pass
                    _refresh()
                    self._update_conflict_badge()

                discard_btn.connect("clicked", _discard)
                row.add_suffix(discard_btn)

                apply_btn = Gtk.Button(label=t("share.conflict_apply"))
                apply_btn.add_css_class("suggested-action")
                apply_btn.set_valign(Gtk.Align.CENTER)

                def _apply(_btn: Gtk.Button, conflict_id: int = cid) -> None:
                    try:
                        self.db.resolve_conflict(conflict_id)
                    except Exception:
                        pass
                    _refresh()
                    self._update_conflict_badge()
                    self.cars_page.refresh_profiles()

                apply_btn.connect("clicked", _apply)
                row.add_suffix(apply_btn)
                list_box.append(row)

        _refresh()

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(outer)
        toolbar_view.set_content(scroll)

        page = Adw.NavigationPage()
        page.set_tag("share-conflicts")
        page.set_title(t("share.conflicts_title"))
        page.set_child(toolbar_view)
        self.nav_view.push(page)
