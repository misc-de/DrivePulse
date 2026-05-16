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
                "auto_rotate": getattr(self, "auto_rotate", True),
                "sidebar_side": getattr(self, "sidebar_side", "left"),
                "engage_threshold": getattr(self, "engage_threshold", 0.20),
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
            current_auto_rotate=self.auto_rotate,
            on_auto_rotate_changed=self._set_auto_rotate,
            current_sidebar_side=self.sidebar_side,
            on_sidebar_side_changed=self._set_sidebar_side,
        )
        dialog.present(self)

    def _open_sync(self, *_args: Any) -> None:
        import gi
        gi.require_version("Adw", "1")
        from gi.repository import Adw
        from .sync_dialog import SyncDialog

        alert = Adw.AlertDialog(
            heading=_translate(self.language, "sync.title"),
            body="",
        )
        alert.add_response("server", _translate(self.language, "sync.choose.server"))
        alert.add_response("client", _translate(self.language, "sync.choose.client"))
        alert.add_response("cancel", _translate(self.language, "sync.choose.cancel"))
        alert.set_response_appearance("server", Adw.ResponseAppearance.SUGGESTED)
        alert.set_response_appearance("cancel", Adw.ResponseAppearance.SUGGESTED)
        alert.set_default_response("cancel")
        alert.set_close_response("cancel")

        def _on_response(_dlg: Any, response: str) -> None:
            if response in ("server", "client"):
                SyncDialog(
                    self, self.language, self.db,
                    initial_mode=response,
                    on_sync_complete=lambda: self.cars_page.refresh_profiles(),
                ).present(self)

        alert.connect("response", _on_response)
        alert.present(self)

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

    def _set_auto_rotate(self, enabled: bool) -> None:
        if enabled == self.auto_rotate:
            return
        self.auto_rotate = enabled
        self._save_settings()
        self.orientation_reader.set_enabled(enabled)

    def _set_sidebar_side(self, side: str) -> None:
        if side == self.sidebar_side:
            return
        self.sidebar_side = side
        self._save_settings()
        self.cars_page.set_sidebar_side(side)

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
        self.acceleration_page.set_language(self.language)
        self.dashboard_canvas.set_language(self.language)
        self.cars_page.set_language(self.language)
        if self.last_payload is not None:
            self._update_from_payload(self.last_payload)
        else:
            self.status_label.set_text(_translate(self.language, "status.connecting"))
        for gauge in (self.rpm_gauge, self.speed_gauge, self.temp_gauge):
            gauge.queue_draw()

