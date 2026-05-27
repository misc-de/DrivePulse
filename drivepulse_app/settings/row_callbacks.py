"""Settings dialog: option-row callbacks (selection / toggle / spin).

Each method here is a thin adapter that reads the current row state and
fires the matching ``on_*_changed`` callback that the owning DashboardWindow
wired up at construction time. They share the same dialog instance so MRO
resolves ``self.<row>`` and ``self.on_*`` lookups against the live attributes.
"""
from __future__ import annotations

from typing import Any

from gi.repository import Adw

from drivepulse_app.common import SUPPORTED_LANGUAGES, _translate
from drivepulse_app.obd.devices import scan_obd_devices


class SettingsRowCallbacksMixin:
    # Owning class (SettingsDialog) initializes the dongle-port list with an
    # explicit ``list[str | None]`` because the first entry is the "auto"
    # sentinel. Annotated here so mypy doesn't infer ``list[None]`` from the
    # reset below.
    _obd_port_values: list[str | None]

    # ── I/O config: units, language, mock, dongle ────────────────────────────

    def _on_unit_selected(self, *_args: Any) -> None:
        self.on_units_changed("metric" if self.unit_row.get_selected() == 0 else "imperial")

    def _on_language_selected(self, *_args: Any) -> None:
        idx = self.language_row.get_selected()
        language = SUPPORTED_LANGUAGES[idx] if 0 <= idx < len(SUPPORTED_LANGUAGES) else SUPPORTED_LANGUAGES[0]
        self.on_language_changed(language)

    def _on_mock_changed(self, *_args: Any) -> None:
        if self.on_mock_mode_changed is not None:
            self.on_mock_mode_changed(self.mock_switch.get_active())

    def _refresh_dongle_dropdown(self, selected_port: str | None) -> None:
        """Re-scan OBD devices and rebuild the dongle dropdown, selecting selected_port."""
        # Local import to keep DeviceItem co-located with its sole producer
        # (SettingsDialog) without forcing this mixin to participate in the
        # GObject type registration.
        from drivepulse_app.settings.dialog import DeviceItem

        self._dongle_updating = True
        try:
            obd_devices = scan_obd_devices()
            self._obd_port_values = [None]
            self._dongle_store.remove_all()
            self._dongle_store.append(DeviceItem(
                label=_translate(self.language, "settings.obd_dongle.auto"),
                port=None,
                is_present=False,
                is_connected=(selected_port is None),
            ))
            for lbl, port, is_present in obd_devices:
                self._dongle_store.append(DeviceItem(
                    label=lbl,
                    port=port,
                    is_present=is_present,
                    is_connected=(port == selected_port),
                ))
                self._obd_port_values.append(port)
            selected_idx = 0
            if selected_port in self._obd_port_values:
                selected_idx = self._obd_port_values.index(selected_port)
            self.dongle_row.set_selected(selected_idx)
            self.dongle_row.set_subtitle("")
        finally:
            self._dongle_updating = False

    def _on_dongle_selected(self, *_args: Any) -> None:
        if self._dongle_updating:
            return
        if self.on_obd_port_changed is not None:
            idx = self.dongle_row.get_selected()
            port = self._obd_port_values[idx] if 0 <= idx < len(self._obd_port_values) else None
            self.on_obd_port_changed(port)

    # ── Appearance: gauge theme, sidebar side, theme mode, nav, rotation ─────

    def _on_gauge_theme_selected(self, *_args: Any) -> None:
        if self.on_gauge_theme_changed is not None:
            idx = self.gauge_theme_row.get_selected()
            theme = self._theme_options[idx][0] if 0 <= idx < len(self._theme_options) else "cockpit"
            self.on_gauge_theme_changed(theme)

    def _on_sidebar_side_selected(self, *_args: Any) -> None:
        if self.on_sidebar_side_changed is not None:
            side = "left" if self.sidebar_side_row.get_selected() == 0 else "right"
            self.on_sidebar_side_changed(side)

    def _on_theme_mode_selected(self, *_args: Any) -> None:
        if self.on_theme_mode_changed is not None:
            modes = ["auto", "dark", "light"]
            idx = self.theme_mode_row.get_selected()
            self.on_theme_mode_changed(modes[idx] if 0 <= idx < len(modes) else "auto")

    def _on_nav_position_selected(self, *_args: Any) -> None:
        if self.on_nav_position_changed is not None:
            idx = self.nav_position_row.get_selected()
            pos = self._NAV_POSITIONS[idx] if 0 <= idx < len(self._NAV_POSITIONS) else self._NAV_POSITIONS[0]
            self.on_nav_position_changed(pos)

    def _on_rotation_mode_selected(self, *_args: Any) -> None:
        if self.on_rotation_mode_changed is not None:
            idx = self.rotation_mode_row.get_selected()
            mode = self._ROTATION_MODES[idx] if 0 <= idx < len(self._ROTATION_MODES) else self._ROTATION_MODES[0]
            self.on_rotation_mode_changed(mode)

    # ── Map page: WebKit toggle (with restart prompt) and traffic sources ────

    def _on_force_webkit_map_changed(self, *_args: Any) -> None:
        new_value = self.force_webkit_map_switch.get_active()
        if self.on_force_webkit_map_changed is not None:
            self.on_force_webkit_map_changed(new_value)
        if new_value != self._initial_force_webkit_map:
            self._show_map_backend_restart_dialog()

    def _show_map_backend_restart_dialog(self) -> None:
        dialog = Adw.AlertDialog(
            heading=_translate(self.language, "settings.map.webkit.restart_dialog.title"),
            body=_translate(self.language, "settings.map.webkit.restart_dialog.body"),
        )
        dialog.add_response("no", _translate(self.language, "settings.app.restart_dialog.no"))
        dialog.add_response("yes", _translate(self.language, "settings.app.restart_dialog.yes"))
        dialog.set_response_appearance("yes", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("yes")
        dialog.set_close_response("no")
        dialog.connect("response", self._on_restart_response)
        dialog.present(self.get_root())

    def _on_traffic_bundesweit_changed(self, *_args: Any) -> None:
        if self.on_traffic_bundesweit_changed is not None:
            self.on_traffic_bundesweit_changed(self.traffic_bundesweit_switch.get_active())

    def _on_traffic_nrw_changed(self, *_args: Any) -> None:
        if self.on_traffic_nrw_changed is not None:
            self.on_traffic_nrw_changed(self.traffic_nrw_switch.get_active())

    def _on_sync_access_selected(self, *_args: Any) -> None:
        if self.on_sync_access_changed is None:
            return
        idx = self._sync_access_row.get_selected()
        if 0 <= idx < len(self._SYNC_ACCESS_MODES):
            self.on_sync_access_changed(self._SYNC_ACCESS_MODES[idx])

    # ── Logging + recording toggles + photo thumbnail cache size ─────────────

    def _on_log_app_toggled(self, row: Adw.SwitchRow, _param: Any) -> None:
        if self.on_log_app_enabled_changed is not None:
            self.on_log_app_enabled_changed(row.get_active())

    def _on_log_obd_toggled(self, row: Adw.SwitchRow, _param: Any) -> None:
        if self.on_log_obd_enabled_changed is not None:
            self.on_log_obd_enabled_changed(row.get_active())

    def _on_obd_auto_record_toggled(self, row: Adw.SwitchRow, _param: Any) -> None:
        if self.on_obd_auto_record_changed is not None:
            self.on_obd_auto_record_changed(row.get_active())

    def _on_nhtsa_enabled_toggled(self, row: Adw.SwitchRow, _param: Any) -> None:
        if self.on_nhtsa_enabled_changed is not None:
            self.on_nhtsa_enabled_changed(row.get_active())

    def _on_thumb_cache_changed(self, row: Adw.SpinRow, _param: Any) -> None:
        if self.on_photo_thumb_cache_max_mb_changed is not None:
            self.on_photo_thumb_cache_max_mb_changed(int(row.get_value()))
