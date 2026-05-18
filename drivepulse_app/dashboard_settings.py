"""Settings and dialog callbacks for the dashboard window."""
from __future__ import annotations

from typing import Any

from .app_settings import load_settings, save_settings
from .common import _detect_language, _normalize_language, _translate
from .dashboard import DASHBOARD_THEMES
from .diagnostics import get_logger
from .settings_dialog import SettingsDialog


log = get_logger(__name__)


class DashboardSettingsMixin:
    def _load_settings(self) -> dict[str, Any]:
        return load_settings()

    def _load_units(self) -> str:
        return self._load_settings()["units"]

    def _save_settings(self) -> None:
        try:
            save_settings({
                "units": getattr(self, "units", "metric"),
                "language": getattr(self, "language", _detect_language()),
                "mock_mode": getattr(self, "mock_mode", False),
                "obd_port": getattr(self, "obd_port", None),
                "gauge_theme": getattr(self, "gauge_theme", "cockpit"),
                "sidebar_side": getattr(self, "sidebar_side", "left"),
                "engage_threshold": getattr(self, "engage_threshold", 0.20),
                "theme_mode": getattr(self, "theme_mode", "auto"),
                "force_webkit_map": getattr(self, "force_webkit_map", False),
                "last_update_check": getattr(self, "last_update_check", None),
                "dashcam_camera": getattr(self, "dashcam_camera", "/dev/video0"),
                "dashcam_resolution": getattr(self, "dashcam_resolution", "1280x720"),
                "dashcam_seg_minutes": getattr(self, "dashcam_seg_minutes", 3),
                "dashcam_max_segments": getattr(self, "dashcam_max_segments", 10),
                "dashcam_dim_timeout": getattr(self, "dashcam_dim_timeout", 30),
                "dashcam_rolling_dir": getattr(self, "dashcam_rolling_dir", ""),
                "dashcam_saved_dir": getattr(self, "dashcam_saved_dir", ""),
                "nav_position": getattr(self, "nav_position", "bottom"),
            })
        except Exception:
            log.exception("Could not save dashboard settings")

    def _on_engage_threshold_changed(self, value: float) -> None:
        self.engage_threshold = value
        self._save_settings()

    def _on_acceleration_run_complete(self, results: dict, samples: list) -> None:
        trip_recorder = getattr(self, "trip_recorder", None)
        car_id = trip_recorder.car_id if trip_recorder else None
        if car_id is None:
            return
        try:
            self.db.add_acceleration_run(
                car_id=car_id,
                results=results,
                samples=samples,
                lat=self._last_gps_lat,
                lon=self._last_gps_lon,
            )
            self.cars_page.refresh_if_showing_car(car_id)
        except Exception:
            log.exception("Could not persist acceleration run")

    def _save_units(self) -> None:
        self._save_settings()

    def _open_settings(self, *_args: Any) -> None:
        dialog = SettingsDialog(
            self, self.units, self.language,
            self._set_units, self._set_language,
            self.mock_mode, self._set_mock_mode,
            current_obd_port=self.obd_port,
            on_obd_port_changed=self._set_obd_port,
            current_gauge_theme=self.gauge_theme,
            on_gauge_theme_changed=self._set_gauge_theme,
            current_sidebar_side=self.sidebar_side,
            on_sidebar_side_changed=self._set_sidebar_side,
            current_theme_mode=getattr(self, "theme_mode", "auto"),
            on_theme_mode_changed=self._set_theme_mode,
            current_force_webkit_map=getattr(self, "force_webkit_map", False),
            on_force_webkit_map_changed=self._set_force_webkit_map,
            current_last_check=getattr(self, "last_update_check", None),
            on_last_check_updated=self._set_last_update_check,
            current_dashcam_camera=getattr(self, "dashcam_camera", "/dev/video0"),
            on_dashcam_camera_changed=self._set_dashcam_camera,
            current_dashcam_resolution=getattr(self, "dashcam_resolution", "1280x720"),
            on_dashcam_resolution_changed=self._set_dashcam_resolution,
            current_dashcam_seg_minutes=getattr(self, "dashcam_seg_minutes", 3),
            on_dashcam_seg_minutes_changed=self._set_dashcam_seg_minutes,
            current_dashcam_max_segments=getattr(self, "dashcam_max_segments", 10),
            on_dashcam_max_segments_changed=self._set_dashcam_max_segments,
            current_dashcam_dim_timeout=getattr(self, "dashcam_dim_timeout", 30),
            on_dashcam_dim_timeout_changed=self._set_dashcam_dim_timeout,
            current_dashcam_rolling_dir=getattr(self, "dashcam_rolling_dir", ""),
            on_dashcam_rolling_dir_changed=self._set_dashcam_rolling_dir,
            current_dashcam_saved_dir=getattr(self, "dashcam_saved_dir", ""),
            on_dashcam_saved_dir_changed=self._set_dashcam_saved_dir,
            current_nav_position=getattr(self, "nav_position", "bottom"),
            on_nav_position_changed=self._set_nav_position,
        )
        dialog.present(self)

    def _set_dashcam_camera(self, value: str) -> None:
        self.dashcam_camera = value
        self._save_settings()
        self.dashcam_page.set_camera(value)

    def _set_dashcam_resolution(self, value: str) -> None:
        self.dashcam_resolution = value
        self._save_settings()
        self.dashcam_page.set_resolution(value)

    def _set_dashcam_seg_minutes(self, value: int) -> None:
        self.dashcam_seg_minutes = value
        self._save_settings()
        self.dashcam_page.set_segment_minutes(value)

    def _set_dashcam_max_segments(self, value: int) -> None:
        self.dashcam_max_segments = value
        self._save_settings()
        self.dashcam_page.set_max_segments(value)

    def _set_dashcam_dim_timeout(self, value: int) -> None:
        self.dashcam_dim_timeout = value
        self._save_settings()
        self.dashcam_page.set_dim_timeout(value)

    def _set_dashcam_rolling_dir(self, value: str) -> None:
        self.dashcam_rolling_dir = value
        self._save_settings()
        self.dashcam_page.set_rolling_dir(value)

    def _set_dashcam_saved_dir(self, value: str) -> None:
        self.dashcam_saved_dir = value
        self._save_settings()
        self.dashcam_page.set_saved_dir(value)

    def _set_nav_position(self, position: str) -> None:
        if position == getattr(self, "nav_position", "bottom"):
            return
        self.nav_position = position
        self._save_settings()
        self._apply_nav_position(position)

    def _open_sync(self, *_args: Any) -> None:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk
        from .sync_dialog import SyncDialog

        dialog = Adw.Dialog()
        dialog.set_title(_translate(self.language, "sync.title"))
        dialog.set_content_width(320)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)

        def _launch(mode: str) -> None:
            dialog.close()
            sync_dialog = SyncDialog(
                self, self.language, self.db,
                initial_mode="client" if mode == "client" else None,
                on_sync_complete=lambda: self.cars_page.refresh_profiles(),
            )
            sync_dialog.present(self)
            if mode == "server":
                sync_dialog.start_server_from_user_action()

        client_btn = Gtk.Button(label=_translate(self.language, "sync.choose.client"))
        client_btn.add_css_class("pill")
        client_btn.set_hexpand(True)
        client_btn.connect("clicked", lambda _b: _launch("client"))

        server_btn = Gtk.Button(label=_translate(self.language, "sync.choose.server"))
        server_btn.add_css_class("pill")
        server_btn.set_hexpand(True)
        server_btn.connect("clicked", lambda _b: _launch("server"))

        box.append(client_btn)
        box.append(server_btn)

        toolbar_view.set_content(box)
        dialog.set_child(toolbar_view)
        dialog.present(self)

    def _set_obd_port(self, port: str | None) -> None:
        if port == self.obd_port:
            return
        self.obd_port = port
        self._save_settings()
        self.reader.set_configured_port(port)

    def _set_units(self, units: str) -> None:
        if units == self.units:
            return
        self.units = units
        self._save_units()
        self.dashboard_canvas.set_units(units)

        if self.units == "metric":
            self.speed_gauge.state.unit = "km/h"
            self.speed_gauge.state.max_value = 240
        else:
            self.speed_gauge.state.unit = "mph"
            self.speed_gauge.state.max_value = 150

        if self.last_payload is not None:
            self._update_from_payload(self.last_payload)
        else:
            self.speed_gauge.queue_draw()

    def _set_gauge_theme(self, theme: str) -> None:
        if theme == self.gauge_theme:
            return
        self.gauge_theme = theme
        self._save_settings()
        is_dashboard = theme in DASHBOARD_THEMES
        self.gauge_box.set_visible(not is_dashboard)
        self.dashboard_canvas.set_visible(is_dashboard)
        # Dashboard themes fill the screen edge-to-edge; gauge themes need breathing room
        margin = 0 if is_dashboard else 12
        for setter in (
            self.dashboard_page.set_margin_top,
            self.dashboard_page.set_margin_bottom,
            self.dashboard_page.set_margin_start,
            self.dashboard_page.set_margin_end,
        ):
            setter(margin)
        if is_dashboard:
            self.dashboard_canvas.set_theme(theme)
        else:
            for gauge in (self.rpm_gauge, self.speed_gauge, self.temp_gauge):
                gauge.set_theme(theme)
        self.acceleration_page.set_theme(theme)
        self._apply_window_theme(theme)

    def _set_mock_mode(self, mock_mode: bool) -> None:
        if mock_mode == self.mock_mode:
            return
        self.mock_mode = mock_mode
        self._save_settings()
        self.reader.set_force_mock(mock_mode)

    def _set_sidebar_side(self, side: str) -> None:
        if side == self.sidebar_side:
            return
        self.sidebar_side = side
        self._save_settings()
        self.cars_page.set_sidebar_side(side)

    def _set_theme_mode(self, mode: str) -> None:
        if mode == getattr(self, "theme_mode", "auto"):
            return
        self.theme_mode = mode
        self._save_settings()
        self._apply_theme_mode(mode)
        self._apply_window_theme(self.gauge_theme)

    def _set_force_webkit_map(self, force_webkit: bool) -> None:
        if force_webkit == getattr(self, "force_webkit_map", False):
            return
        self.force_webkit_map = force_webkit
        self._save_settings()

    def _set_last_update_check(self, timestamp: str) -> None:
        self.last_update_check = timestamp
        self._save_settings()

    def _set_language(self, language: str) -> None:
        language = _normalize_language(language)
        if language == self.language:
            return
        self.language = language
        self._save_settings()
        self.rpm_gauge.title = _translate(self.language, "gauge.rpm")
        self.speed_gauge.title = _translate(self.language, "gauge.speed")
        self.temp_gauge.title = _translate(self.language, "gauge.coolant")
        self.title_label.set_text(_translate(self.language, "window.title"))
        self.settings_button.set_tooltip_text(_translate(self.language, "settings.tooltip"))
        self.obd_indicator["label"].set_text(_translate(self.language, "status.obd"))
        self.gps_indicator["label"].set_text(_translate(self.language, "status.gps"))
        self.dashboard_stack_page.set_title(_translate(self.language, "nav.gauges"))
        self.acceleration_stack_page.set_title(_translate(self.language, "nav.acceleration"))
        self.cars_stack_page.set_title(_translate(self.language, "nav.cars"))
        self.map_stack_page.set_title(_translate(self.language, "nav.map"))
        self.dashcam_stack_page.set_title(_translate(self.language, "nav.dashcam"))
        self.acceleration_page.set_language(self.language)
        self.map_page.set_language(self.language)
        self.dashcam_page.set_language(self.language)
        self.dashboard_canvas.set_language(self.language)
        self.cars_page.set_language(self.language)
        if self.last_payload is not None:
            self._update_from_payload(self.last_payload)
        else:
            self.status_label.set_text(_translate(self.language, "status.connecting"))
        for gauge in (self.rpm_gauge, self.speed_gauge, self.temp_gauge):
            gauge.queue_draw()
