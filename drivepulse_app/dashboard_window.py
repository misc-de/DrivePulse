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
from .dashboard_telemetry import DashboardTelemetryMixin
from .db import DriveDB
from .dashboard_settings import DashboardSettingsMixin
from .gps_reader import GpsReader
from .orientation_reader import OrientationReader
from .obd_reader import ObdReader
from .trip_recorder import TripRecorder


class DashboardWindow(DashboardSettingsMixin, DashboardLayoutMixin, DashboardTelemetryMixin, Adw.ApplicationWindow):
    __gtype_name__ = "DashboardWindow"

    PAGE_DASHBOARD = "dashboard"
    PAGE_ACCELERATION = "acceleration"
    PAGE_CARS = "cars"
    PAGE_MAP = "map"

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
        self.last_update_check: str | None = self.settings.get("last_update_check")
        self.last_payload: dict[str, Any] | None = None
        self._gps_last_seen: float = 0.0
        self._last_gps_lat: float | None = None
        self._last_gps_lon: float | None = None

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

        self.map_page = MapPage(self.language)

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

        header.pack_start(self.obd_indicator["box"])
        header.pack_start(self.gps_indicator["box"])
        header.pack_end(settings_button)
        header.pack_end(self._sync_btn)
        header.pack_end(self._ctx_trash_btn)

        self.header = header
        self.switcher_bar = switcher_bar
        toolbar_view.add_top_bar(header)
        toolbar_view.add_bottom_bar(switcher_bar)
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
        self.orientation_reader = OrientationReader(lambda *_: None)
        self.orientation_reader.on_gforce = self.acceleration_page.update_gforce_raw

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

    def _apply_window_theme(self, theme: str) -> None:
        for cls in list(self.get_css_classes()):
            if cls.startswith("dp-theme-"):
                self.remove_css_class(cls)
        safe = theme.replace(":", "-").replace("_", "-")
        self.add_css_class(f"dp-theme-{safe}")
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
        pages = [self.PAGE_CARS, self.PAGE_MAP, self.PAGE_DASHBOARD, self.PAGE_ACCELERATION]
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
