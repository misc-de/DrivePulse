"""Main DrivePulse application window."""
from __future__ import annotations

import atexit
import math
import threading
import time
from typing import Any, cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from drivepulse_app.cars.page import CarsPage
from drivepulse_app.common import (
    DB_FILE,
    _detect_language,
    _make_label_responsive,
    _translate,
)
from drivepulse_app.dashboard.conflicts import DashboardConflictsMixin
from drivepulse_app.dashboard.layout import DashboardLayoutMixin
from drivepulse_app.dashboard.map_lifecycle import DashboardMapLifecycleMixin
from drivepulse_app.dashboard.nav_routing import DashboardNavRoutingMixin
from drivepulse_app.dashboard.page import DASHBOARD_THEMES, DashboardCanvas
from drivepulse_app.dashboard.piper_overlay import DashboardPiperOverlayMixin
from drivepulse_app.dashboard.settings import DashboardSettingsMixin
from drivepulse_app.dashboard.telemetry import DashboardTelemetryMixin
from drivepulse_app.dashboard.theming import DashboardThemingMixin
from drivepulse_app.dashcam.page import DashcamPage
from drivepulse_app.db import DriveDB
from drivepulse_app.diagnostics import get_logger, set_log_enabled
from drivepulse_app.map.page import MapPage
from drivepulse_app.stopwatch.page import StopWatchPage
from drivepulse_app.ui.gauge import Gauge

log = get_logger(__name__)
from drivepulse_app.mock.tour import MockTourSimulator
from drivepulse_app.obd.reader import ObdReader
from drivepulse_app.sensors.gps import GpsReader
from drivepulse_app.sensors.orientation import OrientationReader
from drivepulse_app.sensors.rotation import RotationProvider
from drivepulse_app.sensors.rotation import Source as RotationSource
from drivepulse_app.trip_recorder import TripRecorder
from drivepulse_app.ui.rotated_container import RotatedContainer
from drivepulse_app.ui.scaled_container import ScaledContainer


class DashboardWindow(
    DashboardSettingsMixin,
    DashboardLayoutMixin,
    DashboardTelemetryMixin,
    DashboardPiperOverlayMixin,
    DashboardThemingMixin,
    DashboardMapLifecycleMixin,
    DashboardConflictsMixin,
    DashboardNavRoutingMixin,
    Adw.ApplicationWindow,
):
    __gtype_name__ = "DashboardWindow"

    PAGE_DASHBOARD = "dashboard"
    PAGE_STOPWATCH = "stopwatch"
    PAGE_CARS = "cars"
    PAGE_MAP = "map"
    PAGE_DASHCAM = "dashcam"

    # Fensterbreite, unterhalb derer die Autos-Detailansicht ihre Kategorienleiste
    # auf Icon-only umschaltet (Phosh/Mobian-typische Portrait-Breiten 360–540 px).
    CARS_NARROW_BREAKPOINT = 500

    # Below this width (in scalable pixels) the window switches to "mobile"
    # form factor: bottom switcher, rotation follows the IIO sensor, and
    # touch gestures (swipe-page, tap-to-hide-nav) are active.
    MOBILE_FORM_FACTOR_MAX_WIDTH = 720

    # Seconds to keep GPS shown as "available" after the last valid fix.
    # Must be well above the GPS update interval (~1 s for GeoClue) so that OBD
    # polls (every 0.5 s) between GPS updates don't falsely detect GPS as gone.
    GPS_UNAVAIL_HOLDOVER = 5.0

    # How long the OBD link indicator stays green after the last healthy read
    # before falling back to "searching". A few poll cycles (0.5 s each) so a
    # single hiccup doesn't flicker, but a dongle that left range turns the icon
    # grey within a couple of seconds instead of lingering green.
    OBD_LINK_HOLDOVER = 4.0

    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title=_translate(_detect_language(), "window.title"))
        # Desktop default; phosh / mobile compositors maximise to screen and
        # ignore this anyway. The 720sp breakpoint flips to mobile mode for
        # narrow windows regardless of this size.
        self.set_default_size(1280, 800)
        self.settings = self._load_settings()
        self._apply_settings()

        # Form factor (mobile vs desktop) is driven by an Adw.Breakpoint added
        # at the end of __init__. Default to "desktop" since set_default_size is
        # wider than MOBILE_FORM_FACTOR_MAX_WIDTH; the breakpoint will flip it
        # to "mobile" on phosh/small windows during the first size allocation.
        self.form_factor: str = "desktop"

        # Rotation state: pages can bind to either "follow_sensor"
        # (compensates for the compositor transform) or "follow_system"
        # (lets the compositor handle rotation). See drivepulse_app/rotation.py.
        # On desktop we lock to follow_system so no IIO-sensor wobble reaches
        # the UI even if a sensor happens to be present.
        _initial_rotation_mode = (
            self.rotation_mode if self.form_factor == "mobile" else "follow_system"
        )
        self.rotation = RotationProvider(mode=_initial_rotation_mode)

        self._init_database()

        # Live-Trip-Statistik (min/max) für das Dashboard
        self._live_trip_id: int | None = None   # letzter bekannter trip_id
        self._live_rpm_min: float | None = None
        self._live_rpm_max: float | None = None
        self._live_coolant_min: float | None = None
        self._live_coolant_max: float | None = None
        self._live_speed_max: float | None = None

        dashboard_scroller = self._build_dashboard_page()
        stopwatch_scroller = self._build_stopwatch_page()

        self._init_cars_page()

        self.mock_tour_sim = MockTourSimulator(self._update_from_payload)
        self._pending_sim_start_id: int | None = None
        # MapPage is created lazily when the map tab is first opened.
        self.map_page: MapPage | None = None
        self._map_unload_timer_id: int | None = None
        self._map_suspended_zoom: float | None = None
        self._map_suspended_follow: bool = True
        self._init_tts_service()
        self._map_rotator = RotatedContainer()
        self._map_rotator.set_hexpand(True)
        self._map_rotator.set_vexpand(True)

        self._init_dashcam_page()

        self._assemble_chrome(dashboard_scroller, stopwatch_scroller)
        self._start_io()

    def _apply_settings(self) -> None:
        """Unpack persisted settings into typed instance attributes."""
        self.units = self.settings["units"]
        self.language = self.settings["language"]
        self.mock_mode = self.settings["mock_mode"]
        self.obd_port: str | None = self.settings.get("obd_port")
        self.gauge_theme: str = self.settings.get("gauge_theme", "cockpit")
        self.sidebar_side: str = self.settings.get("sidebar_side", "left")
        self.theme_mode: str = self.settings.get("theme_mode", "auto")
        self.force_webkit_map: bool = bool(self.settings.get("force_webkit_map", False))
        self.sync_access: str = str(self.settings.get("sync_access", "lan_only"))
        # POIs are deliberately not persisted — they're a performance hit, so
        # the map always starts without POI loading until the user toggles it.
        self.map_traffic_visible: bool = bool(self.settings.get("map_traffic_visible", False))
        self.map_traffic_bundesweit: bool = bool(self.settings.get("map_traffic_bundesweit", True))
        self.map_traffic_nrw: bool = bool(self.settings.get("map_traffic_nrw", False))
        self.map_3d_view: bool = bool(self.settings.get("map_3d_view", True))
        self.map_layer: str = str(self.settings.get("map_layer", "map"))
        self.map_heading_up: bool = bool(self.settings.get("map_heading_up", True))
        self.last_update_check: str | None = self.settings.get("last_update_check")
        self.dashcam_camera: str = self.settings.get("dashcam_camera", "/dev/video0")
        self.dashcam_resolution: str = self.settings.get("dashcam_resolution", "1280x720")
        self.dashcam_codec: str = str(self.settings.get("dashcam_codec", "vp8"))
        self.dashcam_fps: int = int(self.settings.get("dashcam_fps", 25))
        self.dashcam_seg_minutes: int = int(self.settings.get("dashcam_seg_minutes", 3))
        self.dashcam_max_segments: int = int(self.settings.get("dashcam_max_segments", 10))
        self.dashcam_dim_timeout: int = int(self.settings.get("dashcam_dim_timeout", 30))
        self.dashcam_rolling_dir: str = self.settings.get("dashcam_rolling_dir", "")
        self.dashcam_saved_dir: str = self.settings.get("dashcam_saved_dir", "")
        self.nav_position: str = self.settings.get("nav_position", "bottom")
        self.ui_scale: int = int(self.settings.get("ui_scale", 100))
        self.dashcam_gps_osd: bool = bool(self.settings.get("dashcam_gps_osd", False))
        self.dashcam_speed_osd: bool = bool(self.settings.get("dashcam_speed_osd", False))
        # Validated at load time in app_settings.py against {"follow_sensor", "follow_system"}.
        self.rotation_mode: RotationSource = cast(
            RotationSource, self.settings.get("rotation_mode", "follow_sensor")
        )
        self.tts_enabled: bool = bool(self.settings.get("tts_enabled", True))
        self.speed_limit_warn: bool = bool(self.settings.get("speed_limit_warn", True))
        self.tts_backend: str = self.settings.get("tts_backend", "espeak")
        self.tts_language: str = self.settings.get("tts_language", "auto")
        self.tts_voice: str = self.settings.get("tts_voice", "female")
        self.tts_quality: str = self.settings.get("tts_quality", "high")
        self.tts_volume_pct: int = int(self.settings.get("tts_volume_pct") or 100)
        self.tts_duck_pct: int = int(self.settings.get("tts_duck_pct") or 0)
        self.tts_duck_pre_ms: int = int(self.settings.get("tts_duck_pre_ms") or 0)
        self.log_app_enabled: bool = bool(self.settings.get("log_app_enabled", True))
        self.log_obd_enabled: bool = bool(self.settings.get("log_obd_enabled", True))
        self.obd_auto_record: bool = bool(self.settings.get("obd_auto_record", True))
        self.nhtsa_enabled: bool = bool(self.settings.get("nhtsa_enabled", True))
        self.vindecoder_api_key: str = self.settings.get("vindecoder_api_key") or ""
        self.vindecoder_secret_key: str = self.settings.get("vindecoder_secret_key") or ""
        self.autodev_api_key: str = self.settings.get("autodev_api_key") or ""
        self.autodev_month: str = self.settings.get("autodev_month") or ""
        self.autodev_month_count: int = max(0, int(self.settings.get("autodev_month_count") or 0))
        self.autodev_usage_used: int = max(0, int(self.settings.get("autodev_usage_used") or 0))
        self.autodev_usage_limit: int = max(0, int(self.settings.get("autodev_usage_limit") or 0))
        self.autodev_usage_remaining: int = max(0, int(self.settings.get("autodev_usage_remaining") or 0))
        self.autodev_usage_paid: int = max(0, int(self.settings.get("autodev_usage_paid") or 0))
        self.autodev_usage_plan: str = self.settings.get("autodev_usage_plan") or ""
        self.autodev_usage_updated: str = self.settings.get("autodev_usage_updated") or ""
        self.photo_thumb_cache_max_mb: int = int(self.settings.get("photo_thumb_cache_max_mb") or 200)
        self.last_cars_source: str | None = self.settings.get("last_cars_source") or None
        self.last_cars_category: str | None = self.settings.get("last_cars_category") or None
        _raw_scan_id = self.settings.get("last_cars_scan_id")
        self.last_cars_scan_id: int | None = int(_raw_scan_id) if _raw_scan_id is not None else None
        self.last_payload: dict[str, Any] | None = None
        self._gps_last_seen: float = 0.0
        self._obd_last_healthy: float = 0.0
        self._last_gps_lat: float | None = None
        self._last_gps_lon: float | None = None
        self._last_gps_speed_kmh: float | None = None
        self._gps_was_connected: bool = False

    def _init_database(self) -> None:
        """Open the trip DB, reconcile stale live cars and (un)seed mock data."""
        # Persistente Fahrten-Datenbank (cars/trips/samples) — vor allen Pages,
        # weil CarsPage sie injiziert bekommt.
        self.db = DriveDB(DB_FILE)
        # Beim App-Start: nicht promotete Live-Fahrzeuge reconcilen.
        # Leere Live-Cars werden geloescht; welche mit angehaengten Daten
        # (Trips/Scans/Runs/Fotos) werden zu permanenten Fahrzeugen promoted,
        # damit sie in der Liste auftauchen und der Anwender entscheiden kann.
        try:
            result = self.db.recover_stale_live_cars()
            if result["promoted"]:
                log.warning(
                    "Promoted %d stale live car(s) with attached data: %s",
                    len(result["promoted"]),
                    result["promoted"],
                )
            if result["purged"]:
                log.info("Purged %d empty live car(s): %s",
                         len(result["purged"]), result["purged"])
        except Exception:
            log.exception("Could not reconcile stale live cars on startup")
        if self.mock_mode:
            try:
                from drivepulse_app.mock.seed import seed_mock_data
                seed_mock_data(self.db)
            except Exception:
                log.exception("Could not seed mock vehicle data")
        else:
            # Mock mode was switched off (or never enabled) — strip any
            # previously seeded mock cars so they don't linger across restarts.
            try:
                from drivepulse_app.mock.seed import remove_mock_data
                remove_mock_data(self.db)
            except Exception:
                log.exception("Could not remove mock vehicle data on startup")
        self.trip_recorder = TripRecorder(self.db)
        atexit.register(self._shutdown_db)

    def _build_dashboard_page(self) -> Gtk.Widget:
        """Build the gauge dashboard page and return its scroller."""
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
        return dashboard_scroller

    def _build_stopwatch_page(self) -> Gtk.Widget:
        """Build the stopwatch/performance page and return its scroller."""
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
        return stopwatch_scroller

    def _init_cars_page(self) -> None:
        """Construct the cars page and wire its callbacks back to the window."""
        self.cars_page = CarsPage(
            self.language,
            db=self.db,
            sidebar_side=self.sidebar_side,
            vindecoder_api_key=self.vindecoder_api_key or None,
            vindecoder_secret_key=self.vindecoder_secret_key or None,
            autodev_api_key=self.autodev_api_key or None,
            nhtsa_enabled=self.nhtsa_enabled,
            on_autodev_call=self._increment_autodev_count,
            initial_source=self.last_cars_source,
            initial_category=self.last_cars_category,
            initial_scan_id=self.last_cars_scan_id,
            on_state_changed=self._on_cars_state_changed,
        )
        self.cars_page.mock_mode = bool(self.mock_mode)
        self.cars_page.on_back_swipe = self._on_cars_back_swipe
        self.cars_page.on_forward_swipe = self._on_cars_forward_swipe
        self.cars_page.on_live_vehicle_add = self._promote_live_vehicle_from_identity
        self.cars_page.get_sync_client = self._get_active_sync_client
        self.cars_page.on_load_stopwatch_run = self._load_persisted_run_into_stopwatch
        self.cars_page.on_open_trip_as_route = self._open_trip_as_route_on_map
        self.cars_page.on_show_trip_replay_on_map = self._show_trip_replay_on_map_from_cars
        self.cars_page.on_clear_dtcs = self._clear_obd_dtcs
        self.cars_page.on_carlab_discover = self._carlab_discover
        self.cars_page.on_carlab_sweep = self._carlab_sweep
        self.cars_page.on_carlab_snapshot = self._carlab_snapshot
        self.cars_page.on_carlab_mock_toggle = self._carlab_mock_toggle
        self.cars_page.on_carlab_scan = self._carlab_scan
        self._cars_rotator = RotatedContainer()
        self._cars_rotator.set_child(self.cars_page)
        self._cars_rotator.set_hexpand(True)
        self._cars_rotator.set_vexpand(True)

    def _init_tts_service(self) -> None:
        """Apply persisted TTS settings to the shared service and prefetch piper models."""
        from drivepulse_app.tts import service as _tts_svc
        _tts_svc.set_backend(self.tts_backend)
        _tts_svc.set_volume_pct(self.tts_volume_pct)
        _tts_svc.set_duck(self.tts_duck_pct, self.tts_duck_pre_ms)
        _tts_svc.set_download_callback(self._on_piper_dl_progress)
        self._piper_dl_current_model: str | None = None
        if self.tts_backend == "piper":
            _tts_svc.ensure_models(self.tts_language, self.tts_voice, self.tts_quality)

    def _init_dashcam_page(self) -> None:
        """Construct the dashcam page from persisted camera/recording settings."""
        self.dashcam_page = DashcamPage(self.language)
        self.dashcam_page.set_camera(self.dashcam_camera)
        self.dashcam_page.set_resolution(self.dashcam_resolution)
        self.dashcam_page.set_codec(self.dashcam_codec)
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

    def _assemble_chrome(self, dashboard_scroller: Gtk.Widget, stopwatch_scroller: Gtk.Widget) -> None:
        """Build the view stack, header bar, switchers and nav chrome, then mount the content."""
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

        # Desktop alternative to the top/bottom switcher bars: a vertical
        # navigation sidebar on the left. Built once; visibility toggled
        # together with switcher_top/_bar from _apply_nav_position.
        self.left_nav = self._build_left_nav()
        self.left_nav_separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        stack_overlay.set_hexpand(True)
        content_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        content_row.append(self.left_nav)
        content_row.append(self.left_nav_separator)
        content_row.append(stack_overlay)

        self._apply_nav_position(self.nav_position)
        toolbar_view.set_content(content_row)

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
        # Wrap the whole UI in a scaler so the Display-size setting can shrink
        # the entire app (icons, spacing, images and text) with real reflow.
        self._scale_container = ScaledContainer()
        self._scale_container.set_child(self.nav_view)
        self._scale_container.set_scale(self.ui_scale / 100)
        self.set_content(self._scale_container)
        self._install_form_factor_breakpoint()
        # Initial sync: breakpoint apply/unapply only fires on transitions, so
        # apply current form_factor state to dependents that were constructed
        # before the breakpoint existed (cars_page split-view, etc.).
        self._apply_form_factor_state()
        # First-run materialisation of nav_position: defer to idle so the
        # breakpoint condition has been evaluated against the actual window
        # size (phosh / mobile compositors only resolve this after present()).
        if self.nav_position == "auto":
            GLib.idle_add(self._materialise_auto_nav_position)
        self.connect("notify::default-width", self._on_size_changed)
        self.connect("notify::default-height", self._on_size_changed)
        self.add_tick_callback(self._layout_tick)
        # Decay the OBD/GPS link icons to grey when the reader goes silent
        # (dropped Bluetooth bridge); otherwise the last icon state sticks.
        GLib.timeout_add_seconds(1, self._link_indicator_tick)
        GLib.idle_add(self._on_size_changed)

        self._nav_rotation_css = Gtk.CssProvider()
        self._theme_css_provider = Gtk.CssProvider()
        self._light_palette_css = Gtk.CssProvider()
        self.connect("realize", self._on_realize_install_css)

    def _start_io(self) -> None:
        """Start the OBD/GPS/orientation readers and kick off async data loads."""
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
        if hasattr(self, "left_nav"):
            left_visible = visible and getattr(self, "_left_nav_intended_visible", False)
            self.left_nav.set_visible(left_visible)
            self.left_nav_separator.set_visible(left_visible)

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

    def _set_link_indicator(
        self,
        indicator: dict[str, Any],
        connected: bool,
        connecting: bool = False,
        degraded: bool = False,
    ) -> None:
        box = indicator["box"]
        spinner = indicator["spinner"]
        image = indicator["image"]
        box.remove_css_class("dim-label")
        box.remove_css_class("success")
        box.remove_css_class("warning")
        if connected and degraded:
            # Linked but no live data (e.g. engine off): amber, not green.
            box.add_css_class("warning")
        elif connected:
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
        # Tap-to-hide-nav is a mobile pattern; on desktop the header/switcher
        # stay permanently visible, so a stray mouse click must not toggle them.
        if getattr(self, "form_factor", "mobile") == "desktop":
            return
        now = time.monotonic()
        # Reject if a swipe just fired — its release event still reaches the click gesture
        if now - self._last_swipe_time < 0.35:
            return
        # Reject if the touch lasted too long (long-press) or moved too far (swipe/drag)
        duration = now - self._tap_press_time
        moved = math.hypot(x - self._tap_press_x, y - self._tap_press_y)
        if duration > self._TAP_MAX_DURATION_S or moved > self._TAP_MAX_MOVE_PX:
            return
        page = self.view_stack.get_visible_child_name()
        # Karte hat ihren eigenen Tap-Controller — hier nicht doppelt schalten.
        if page == self.PAGE_MAP:
            return
        self._set_nav_visible(not self._nav_visible)

    def _set_nav_visible(self, visible: bool) -> None:
        self._nav_visible = visible
        self.header.set_visible(visible)
        self.switcher_bar.set_visible(visible)
        self.footer.set_visible(visible)
        if self.view_stack.get_visible_child_name() == self.PAGE_MAP and self.map_page is not None:
            self.map_page.set_nav_visible(visible)

    _GPS_REQUIRED_PAGES = frozenset(["dashboard", "stopwatch", "map"])

    def _on_visible_page_changed(self, _stack: Adw.ViewStack, _pspec: Any) -> None:
        page = self.view_stack.get_visible_child_name()
        if page in (self.PAGE_CARS, self.PAGE_MAP) and not self._nav_visible:
            self._set_nav_visible(True)
        if page == self.PAGE_MAP:
            self._cancel_map_unload()
            self._ensure_map_page()
            if self.map_page is not None:
                GLib.timeout_add(50, self.map_page.on_shown)
        if page in self._GPS_REQUIRED_PAGES:
            self.gps_reader.ensure_active()

        # Dashcam preview is started lazily and torn down when the user leaves
        # the tab — except the recorder keeps running across tab switches so a
        # tour is recorded end-to-end regardless of which tab is in front.
        prev = getattr(self, "_last_visible_page", None)
        if prev == self.PAGE_MAP and page != self.PAGE_MAP:
            self._schedule_map_unload()
        if page == self.PAGE_DASHCAM:
            self.dashcam_page.on_shown()
        elif prev == self.PAGE_DASHCAM:
            self.dashcam_page.on_hidden()
        self._last_visible_page = page

        # Keep the left desktop nav in sync with the active tab.
        self._sync_left_nav_selection()

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
        maneuver_m: list[float] | None = None,
    ) -> None:
        self._cancel_pending_sim_start()
        if not self.mock_mode:
            return
        self._pending_sim_start_id = GLib.timeout_add(
            self._TOUR_SETTLE_MS,
            self._start_mock_sim_delayed,
            list(coords),
            list(speed_zones),
            list(maneuver_m) if maneuver_m else [],
        )

    def _start_mock_sim_delayed(
        self,
        coords: list[list[float]],
        speed_zones: list[tuple[float, float]],
        maneuver_m: list[float],
    ) -> bool:
        self._pending_sim_start_id = None
        if self.mock_mode:
            self.mock_tour_sim.start(coords, speed_zones, maneuver_m)
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

    def _clear_obd_dtcs(self, on_done: Any) -> None:
        """Run OBD Mode 04 in a worker thread and report back via on_done(ok).

        Done off the GTK thread because the OBD round-trip can take a
        moment, and we don't want the confirmation dialog to freeze the UI.
        """
        def _worker() -> None:
            ok = False
            try:
                ok = self.reader.clear_dtcs()
            finally:
                GLib.idle_add(on_done, ok)
            if ok:
                # Trigger a fresh vehicle scan so the cleared state is
                # reflected the next time the user opens the diagnostics
                # category. Without this the cached DTC list lingers until
                # the periodic rescan kicks in.
                try:
                    self.reader._run_vehicle_scan(force_rescan=True)
                except Exception:
                    pass

        threading.Thread(target=_worker, name="obd-clear-dtc", daemon=True).start()

    def _carlab_discover(self, tx: str, rx: str, on_done: Any) -> None:
        """Run a read-only module discovery off the GTK thread (Car Lab)."""
        def _worker() -> None:
            result: dict[str, Any] = {}
            try:
                result = self.reader.discover_module(tx, rx)
            finally:
                GLib.idle_add(on_done, result)

        threading.Thread(target=_worker, name="carlab-discover", daemon=True).start()

    def _carlab_sweep(self, tx: str, rx: str, on_done: Any) -> None:
        """Run a deep read-only DID sweep off the GTK thread (Car Lab)."""
        def _worker() -> None:
            result: dict[str, Any] = {}
            try:
                result = self.reader.sweep_module(tx, rx)
            finally:
                GLib.idle_add(on_done, result)

        threading.Thread(target=_worker, name="carlab-sweep", daemon=True).start()

    def _carlab_snapshot(self, tx: str, rx: str, dids: list[int], on_done: Any) -> None:
        """Read a single DID snapshot from a module off the GTK thread (Car Lab)."""
        def _worker() -> None:
            result: dict[int, str] = {}
            try:
                result = self.reader.uds_snapshot(tx, rx, dids)
            finally:
                GLib.idle_add(on_done, result)

        threading.Thread(target=_worker, name="carlab-snapshot", daemon=True).start()

    def _carlab_scan(self, on_done: Any) -> None:
        """Probe known module addresses off the GTK thread (Car Lab)."""
        def _worker() -> None:
            result: list[dict[str, str]] = []
            try:
                result = self.reader.scan_modules()
            finally:
                GLib.idle_add(on_done, result)

        threading.Thread(target=_worker, name="carlab-scan", daemon=True).start()

    def _carlab_mock_toggle(self) -> None:
        """Mock only: flip the simulated coding bit so a capture shows a diff."""
        self.reader.mock_uds_toggle()

    def _apply_nav_position(self, position: str) -> None:
        effective = position
        if effective == "auto":
            effective = "left" if self.form_factor == "desktop" else "bottom"
        # The vertical sidebar is too wide for phone-style windows; fall back
        # to a bottom bar there, even when "left" is the persisted choice.
        if effective == "left" and self.form_factor == "mobile":
            effective = "bottom"
        is_left = effective == "left"
        is_top = effective == "top"
        is_bottom = effective == "bottom"
        self._left_nav_intended_visible = is_left
        self.left_nav.set_visible(is_left)
        self.left_nav_separator.set_visible(is_left)
        self.switcher_top.set_reveal(is_top)
        self.switcher_bar.set_reveal(is_bottom)

    def _build_left_nav(self) -> Gtk.Widget:
        """Vertical desktop navigation sidebar mirroring the view_stack."""
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        listbox.add_css_class("navigation-sidebar")
        listbox.set_vexpand(True)
        self._left_nav_listbox = listbox
        self._left_nav_rows: dict[str, Gtk.ListBoxRow] = {}
        self._left_nav_syncing = False

        pages = self.view_stack.get_pages()
        for i in range(pages.get_n_items()):
            page = pages.get_item(i)
            name = page.get_name()
            row = Gtk.ListBoxRow()
            row.page_name = name
            row.set_tooltip_text(page.get_title() or name)
            img = Gtk.Image.new_from_icon_name(page.get_icon_name() or "")
            img.set_pixel_size(22)
            img.set_margin_top(12)
            img.set_margin_bottom(12)
            img.set_margin_start(14)
            img.set_margin_end(14)
            row.set_child(img)
            listbox.append(row)
            self._left_nav_rows[name] = row

        listbox.connect("row-selected", self._on_left_nav_row_selected)
        self._sync_left_nav_selection()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_size_request(56, -1)
        box.append(listbox)
        return box

    def _on_left_nav_row_selected(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None or self._left_nav_syncing:
            return
        page_name = getattr(row, "page_name", None)
        if page_name and self.view_stack.get_visible_child_name() != page_name:
            self.view_stack.set_visible_child_name(page_name)

    def _sync_left_nav_selection(self) -> None:
        rows = getattr(self, "_left_nav_rows", None)
        listbox = getattr(self, "_left_nav_listbox", None)
        if not rows or listbox is None:
            return
        page = self.view_stack.get_visible_child_name()
        row = rows.get(page) if page else None
        if row is None or listbox.get_selected_row() is row:
            return
        self._left_nav_syncing = True
        try:
            listbox.select_row(row)
        finally:
            self._left_nav_syncing = False

    def _materialise_auto_nav_position(self) -> bool:
        """One-shot: convert the implicit "auto" default to a concrete choice.

        Fired once on first start so the settings dialog shows a real option
        (Links / Unten) selected instead of the hidden "auto" sentinel.
        """
        if self.nav_position != "auto":
            return False
        effective = "left" if self.form_factor == "desktop" else "bottom"
        self.nav_position = effective
        self._apply_nav_position(effective)
        self._save_settings()
        return False

    def _on_cars_state_changed(self, source: str | None, category: str | None, scan_id: int | None = None) -> None:
        """Persist the last viewed source + category + scan from the Cars page."""
        if (source == self.last_cars_source
                and category == self.last_cars_category
                and scan_id == self.last_cars_scan_id):
            return
        self.last_cars_source = source
        self.last_cars_category = category
        self.last_cars_scan_id = scan_id
        self._save_settings()

    def _install_form_factor_breakpoint(self) -> None:
        """Drive ``self.form_factor`` from an Adw.Breakpoint on window width.

        Default state is ``"desktop"`` (set in ``__init__``). The breakpoint
        flips to ``"mobile"`` when the window is at or below
        ``MOBILE_FORM_FACTOR_MAX_WIDTH`` scalable pixels, which is the common
        phosh / portrait-phone range.
        """
        try:
            condition = Adw.BreakpointCondition.parse(
                f"max-width: {self.MOBILE_FORM_FACTOR_MAX_WIDTH}sp"
            )
            bp = Adw.Breakpoint.new(condition)
        except Exception:
            # libadwaita too old or BreakpointCondition unavailable — stay on
            # the default form factor. The rest of the UI still works.
            return
        bp.connect("apply", lambda *_a: self._set_form_factor("mobile"))
        bp.connect("unapply", lambda *_a: self._set_form_factor("desktop"))
        self.add_breakpoint(bp)

    def _set_form_factor(self, ff: str) -> None:
        if ff not in ("mobile", "desktop") or ff == self.form_factor:
            return
        self.form_factor = ff
        self._apply_form_factor_state()

    def _apply_form_factor_state(self) -> None:
        """Apply form_factor to dependent UI without the equality guard.

        Called by _set_form_factor on transitions and once at end of __init__
        so the initial state propagates even when the breakpoint condition
        doesn't fire (e.g. desktop windows that never cross the threshold).
        """
        ff = self.form_factor
        # Rotation: desktop locks to 0; mobile restores the user's choice.
        if ff == "desktop":
            self.rotation.set_mode("follow_system")
        else:
            self.rotation.set_mode(self.rotation_mode)
        # Nav position: re-evaluate, so "auto" follows the new form factor.
        self._apply_nav_position(self.nav_position)
        # Cars page: split view collapses to push/pop on mobile, expands to
        # list+detail side-by-side on desktop.
        if hasattr(self, "cars_page") and hasattr(self.cars_page, "set_collapsed"):
            self.cars_page.set_collapsed(ff == "mobile")
        # Map page: nudges replay-info overlay below the top-left info button.
        if self.map_page is not None and hasattr(self.map_page, "set_form_factor"):
            self.map_page.set_form_factor(ff)
        # Dashcam page: groups the control buttons left-aligned on desktop.
        if hasattr(self, "dashcam_page") and hasattr(self.dashcam_page, "set_form_factor"):
            self.dashcam_page.set_form_factor(ff)

    def close(self) -> bool:
        self._cancel_map_unload()
        self.reader.stop()
        self.gps_reader.stop()
        self.orientation_reader.stop()
        return super().close()

    def _on_swipe(self, _gesture: Gtk.GestureSwipe, velocity_x: float, velocity_y: float) -> None:
        # Swipes (page change, theme cycle) are touch-only; on desktop the
        # equivalent mouse drag would cycle themes by accident.
        if getattr(self, "form_factor", "mobile") == "desktop":
            return
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

    def _get_active_sync_client(self) -> Any:
        from drivepulse_app.sync.client import SyncClient
        dialog = getattr(self, "_active_sync_dialog", None)
        if dialog is None:
            return None
        client = getattr(dialog, "_active_client", None)
        if isinstance(client, SyncClient):
            return client
        server = getattr(dialog, "_server", None)
        if server is not None and server._paired:
            from drivepulse_app.sync.dialog import ServerShareClient
            return ServerShareClient(server)
        return None

