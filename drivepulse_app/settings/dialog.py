"""Settings dialog for DrivePulse."""
from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from datetime import datetime

from gi.repository import (
    Adw,
    Gio,
    GLib,
    GObject,
    Gtk,
)

from drivepulse_app import updater
from drivepulse_app.common import SUPPORTED_LANGUAGES, _normalize_language, _translate, language_name
from drivepulse_app.obd.devices import scan_obd_devices
from drivepulse_app.settings.bluetooth import SettingsBluetoothMixin
from drivepulse_app.settings.dashcam import SettingsDashcamMixin
from drivepulse_app.settings.row_callbacks import SettingsRowCallbacksMixin
from drivepulse_app.settings.tts import SettingsTtsMixin
from drivepulse_app.settings.updates import SettingsUpdatesMixin
from drivepulse_app.settings.vin_decoder import SettingsVinDecoderMixin
from drivepulse_app.tts import service as tts_service
from drivepulse_app.ui.gauge import all_theme_options


class DeviceItem(GObject.Object):
    __gtype_name__ = "DrivePulseDeviceItem"

    def __init__(self, label: str, port: str | None, is_present: bool = False, is_connected: bool = False) -> None:
        super().__init__()
        self._label = label
        self._port = port
        self._is_present = is_present
        self._is_connected = is_connected


class _BtExpander:
    """Expander row for BT device lists, backed by Adw.ExpanderRow."""

    def __init__(self) -> None:
        self._expander = Adw.ExpanderRow()
        self.widget = self._expander

    def set_title(self, title: str) -> None:
        self._expander.set_title(title)

    def set_subtitle(self, subtitle: str) -> None:
        self._expander.set_subtitle(subtitle)

    def add_action(self, widget: Gtk.Widget) -> None:
        self._expander.add_action(widget)

    def add_row(self, row: Gtk.Widget) -> None:
        self._expander.add_row(row)

    def remove(self, row: Gtk.Widget) -> None:
        self._expander.remove(row)

    def get_expanded(self) -> bool:
        return self._expander.get_expanded()

    def set_expanded(self, value: bool) -> None:
        self._expander.set_expanded(value)


class SettingsDialog(
    SettingsBluetoothMixin,
    SettingsDashcamMixin,
    SettingsRowCallbacksMixin,
    SettingsTtsMixin,
    SettingsUpdatesMixin,
    SettingsVinDecoderMixin,
    Adw.NavigationPage,
):
    __gtype_name__ = "SettingsDialog"

    def __init__(
        self,
        parent: Gtk.Window,
        current_units: str,
        current_language: str,
        on_units_changed: Callable[[str], None],
        on_language_changed: Callable[[str], None],
        current_mock_mode: bool = False,
        on_mock_mode_changed: Callable[[bool], None] | None = None,
        current_obd_port: str | None = None,
        on_obd_port_changed: Callable[[str | None], None] | None = None,
        current_gauge_theme: str = "cockpit",
        on_gauge_theme_changed: Callable[[str], None] | None = None,
        current_sidebar_side: str = "left",
        on_sidebar_side_changed: Callable[[str], None] | None = None,
        current_theme_mode: str = "auto",
        on_theme_mode_changed: Callable[[str], None] | None = None,
        current_force_webkit_map: bool = False,
        on_force_webkit_map_changed: Callable[[bool], None] | None = None,
        current_traffic_bundesweit: bool = True,
        on_traffic_bundesweit_changed: Callable[[bool], None] | None = None,
        current_traffic_nrw: bool = False,
        on_traffic_nrw_changed: Callable[[bool], None] | None = None,
        current_last_check: str | None = None,
        on_last_check_updated: Callable[[str], None] | None = None,
        current_dashcam_camera: str = "/dev/video0",
        on_dashcam_camera_changed: Callable[[str], None] | None = None,
        current_dashcam_resolution: str = "1280x720",
        on_dashcam_resolution_changed: Callable[[str], None] | None = None,
        current_dashcam_codec: str = "vp8",
        on_dashcam_codec_changed: Callable[[str], None] | None = None,
        current_dashcam_fps: int = 25,
        on_dashcam_fps_changed: Callable[[int], None] | None = None,
        current_dashcam_seg_minutes: int = 3,
        on_dashcam_seg_minutes_changed: Callable[[int], None] | None = None,
        current_dashcam_max_segments: int = 10,
        on_dashcam_max_segments_changed: Callable[[int], None] | None = None,
        current_dashcam_dim_timeout: int = 30,
        on_dashcam_dim_timeout_changed: Callable[[int], None] | None = None,
        current_dashcam_rolling_dir: str = "",
        on_dashcam_rolling_dir_changed: Callable[[str], None] | None = None,
        current_dashcam_saved_dir: str = "",
        on_dashcam_saved_dir_changed: Callable[[str], None] | None = None,
        current_dashcam_gps_osd: bool = False,
        on_dashcam_gps_osd_changed: Callable[[bool], None] | None = None,
        current_dashcam_speed_osd: bool = False,
        on_dashcam_speed_osd_changed: Callable[[bool], None] | None = None,
        current_nav_position: str = "bottom",
        on_nav_position_changed: Callable[[str], None] | None = None,
        current_ui_scale: int = 100,
        on_ui_scale_changed: Callable[[int], None] | None = None,
        current_rotation_mode: str = "follow_sensor",
        on_rotation_mode_changed: Callable[[str], None] | None = None,
        current_tts_enabled: bool = False,
        on_tts_enabled_changed: Callable[[bool], None] | None = None,
        current_tts_backend: str = "espeak",
        on_tts_backend_changed: Callable[[str], None] | None = None,
        current_tts_language: str = "auto",
        on_tts_language_changed: Callable[[str], None] | None = None,
        current_tts_voice: str = "female",
        on_tts_voice_changed: Callable[[str], None] | None = None,
        current_tts_quality: str = "high",
        on_tts_quality_changed: Callable[[str], None] | None = None,
        current_tts_volume_pct: int = 100,
        on_tts_volume_pct_changed: Callable[[int], None] | None = None,
        current_tts_duck_pct: int = 0,
        on_tts_duck_pct_changed: Callable[[int], None] | None = None,
        current_tts_duck_pre_ms: int = 0,
        on_tts_duck_pre_ms_changed: Callable[[int], None] | None = None,
        current_log_app_enabled: bool = True,
        on_log_app_enabled_changed: Callable[[bool], None] | None = None,
        current_log_obd_enabled: bool = True,
        on_log_obd_enabled_changed: Callable[[bool], None] | None = None,
        current_obd_auto_record: bool = True,
        on_obd_auto_record_changed: Callable[[bool], None] | None = None,
        current_nhtsa_enabled: bool = True,
        on_nhtsa_enabled_changed: Callable[[bool], None] | None = None,
        current_vindecoder_api_key: str = "",
        on_vindecoder_api_key_changed: Callable[[str], None] | None = None,
        current_vindecoder_secret_key: str = "",
        on_vindecoder_secret_key_changed: Callable[[str], None] | None = None,
        current_autodev_api_key: str = "",
        on_autodev_api_key_changed: Callable[[str], None] | None = None,
        current_autodev_month: str = "",
        current_autodev_month_count: int = 0,
        current_autodev_usage_used: int = 0,
        current_autodev_usage_limit: int = 0,
        current_autodev_usage_paid: int = 0,
        current_autodev_usage_plan: str = "",
        current_photo_thumb_cache_max_mb: int = 200,
        on_photo_thumb_cache_max_mb_changed: Callable[[int], None] | None = None,
        current_sync_access: str = "lan_only",
        on_sync_access_changed: Callable[[str], None] | None = None,
        obd_status_provider: Callable[[], dict | None] | None = None,
    ) -> None:
        super().__init__(tag="settings")
        self.language = _normalize_language(current_language)
        self.on_units_changed = on_units_changed
        self.on_language_changed = on_language_changed
        self.on_mock_mode_changed = on_mock_mode_changed
        self.on_obd_port_changed = on_obd_port_changed
        self.on_gauge_theme_changed = on_gauge_theme_changed
        self.on_sidebar_side_changed = on_sidebar_side_changed
        self.on_theme_mode_changed = on_theme_mode_changed
        self.on_force_webkit_map_changed = on_force_webkit_map_changed
        self._initial_force_webkit_map = bool(current_force_webkit_map)
        self.on_traffic_bundesweit_changed = on_traffic_bundesweit_changed
        self.on_traffic_nrw_changed = on_traffic_nrw_changed
        self.on_last_check_updated = on_last_check_updated
        self.on_dashcam_camera_changed = on_dashcam_camera_changed
        self.on_dashcam_resolution_changed = on_dashcam_resolution_changed
        self.on_dashcam_codec_changed = on_dashcam_codec_changed
        self._current_dashcam_codec = current_dashcam_codec
        self.on_dashcam_fps_changed = on_dashcam_fps_changed
        self._current_dashcam_fps = current_dashcam_fps
        self.on_dashcam_seg_minutes_changed = on_dashcam_seg_minutes_changed
        self.on_dashcam_max_segments_changed = on_dashcam_max_segments_changed
        self.on_dashcam_dim_timeout_changed = on_dashcam_dim_timeout_changed
        self.on_dashcam_rolling_dir_changed = on_dashcam_rolling_dir_changed
        self.on_dashcam_saved_dir_changed = on_dashcam_saved_dir_changed
        self._current_dashcam_rolling_dir = current_dashcam_rolling_dir
        self._current_dashcam_saved_dir = current_dashcam_saved_dir
        self.on_dashcam_gps_osd_changed = on_dashcam_gps_osd_changed
        self._current_dashcam_gps_osd = current_dashcam_gps_osd
        self.on_dashcam_speed_osd_changed = on_dashcam_speed_osd_changed
        self._current_dashcam_speed_osd = current_dashcam_speed_osd
        self.on_nav_position_changed = on_nav_position_changed
        self.on_ui_scale_changed = on_ui_scale_changed
        self.on_rotation_mode_changed = on_rotation_mode_changed
        self.on_tts_enabled_changed = on_tts_enabled_changed
        self.on_tts_backend_changed = on_tts_backend_changed
        self.on_tts_language_changed = on_tts_language_changed
        self.on_tts_voice_changed = on_tts_voice_changed
        self.on_tts_quality_changed = on_tts_quality_changed
        self.on_tts_volume_pct_changed = on_tts_volume_pct_changed
        self.on_tts_duck_pct_changed = on_tts_duck_pct_changed
        self.on_tts_duck_pre_ms_changed = on_tts_duck_pre_ms_changed
        self._current_tts_volume_pct = current_tts_volume_pct
        self._current_tts_duck_pct = current_tts_duck_pct
        self._current_tts_duck_pre_ms = current_tts_duck_pre_ms
        self.on_log_app_enabled_changed = on_log_app_enabled_changed
        self.on_log_obd_enabled_changed = on_log_obd_enabled_changed
        self.on_obd_auto_record_changed = on_obd_auto_record_changed
        self.on_nhtsa_enabled_changed = on_nhtsa_enabled_changed
        self.on_vindecoder_api_key_changed = on_vindecoder_api_key_changed
        self.on_vindecoder_secret_key_changed = on_vindecoder_secret_key_changed
        self.on_autodev_api_key_changed = on_autodev_api_key_changed
        self._autodev_month = current_autodev_month
        self._autodev_month_count = current_autodev_month_count
        self._autodev_usage_used = current_autodev_usage_used
        self._autodev_usage_limit = current_autodev_usage_limit
        self._autodev_usage_paid = current_autodev_usage_paid
        self._autodev_usage_plan = current_autodev_usage_plan
        self.on_photo_thumb_cache_max_mb_changed = on_photo_thumb_cache_max_mb_changed
        self._current_photo_thumb_cache_max_mb = current_photo_thumb_cache_max_mb
        self.on_sync_access_changed = on_sync_access_changed
        self._current_sync_access = current_sync_access if current_sync_access in {"off", "lan_only", "any"} else "lan_only"
        self._remote_version: str | None = None
        self._closing = False
        self._obd_status_provider = obd_status_provider
        # Snapshot the configured port + open subscriptions for the OBD-dongle
        # subpage's live-state poller (see ``_obd_subpage_poll_tick``).
        self.current_obd_port: str | None = current_obd_port
        self._obd_subpage_poll_id: int = 0
        self._obd_last_seen_connected: bool = False
        self._bt_nearby_last_devices: list[tuple[str, str]] | None = None
        # The outer NavigationView the settings page lives in — used to push the
        # OBD-dongle subpage. Mirrors the pattern in sync/dialog.py.
        self._outer_nav: Adw.NavigationView | None = getattr(parent, "nav_view", None)
        self.set_title(_translate(self.language, "settings.title"))
        self.connect("hiding", self._on_hiding)

        # ── Build all option rows (assigned to pages further below) ──────────
        self.unit_row = Adw.ComboRow(title=_translate(self.language, "settings.speed"))
        model = Gtk.StringList()
        model.append(_translate(self.language, "settings.metric"))
        model.append(_translate(self.language, "settings.imperial"))
        self.unit_row.set_model(model)
        self.unit_row.set_selected(0 if current_units == "metric" else 1)
        self.unit_row.connect("notify::selected", self._on_unit_selected)

        self.language_row = Adw.ComboRow(title=_translate(self.language, "settings.language"))
        language_model = Gtk.StringList()
        for code in SUPPORTED_LANGUAGES:
            language_model.append(language_name(code))
        self.language_row.set_model(language_model)
        self.language_row.set_selected(SUPPORTED_LANGUAGES.index(self.language))
        self.language_row.connect("notify::selected", self._on_language_selected)

        self.mock_switch = Gtk.Switch()
        self.mock_switch.set_active(current_mock_mode)
        self.mock_switch.set_valign(Gtk.Align.CENTER)
        self.mock_switch.connect("notify::active", self._on_mock_changed)
        self.mock_row = Adw.ActionRow(
            title=_translate(self.language, "settings.mock_mode"),
            subtitle=_translate(self.language, "settings.mock_mode.subtitle"),
        )
        self.mock_row.add_suffix(self.mock_switch)
        self.mock_row.set_activatable_widget(self.mock_switch)

        self._theme_options = all_theme_options(self.language)
        theme_model = Gtk.StringList()
        for _, label in self._theme_options:
            theme_model.append(label)
        self.gauge_theme_row = Adw.ComboRow(title=_translate(self.language, "settings.gauge_theme"))
        self.gauge_theme_row.set_model(theme_model)
        theme_ids = [tid for tid, _ in self._theme_options]
        selected_idx = theme_ids.index(current_gauge_theme) if current_gauge_theme in theme_ids else 0
        self.gauge_theme_row.set_selected(selected_idx)
        self.gauge_theme_row.connect("notify::selected", self._on_gauge_theme_selected)

        sidebar_side_model = Gtk.StringList()
        sidebar_side_model.append(_translate(self.language, "settings.sidebar_side.left"))
        sidebar_side_model.append(_translate(self.language, "settings.sidebar_side.right"))
        self.sidebar_side_row = Adw.ComboRow(title=_translate(self.language, "settings.sidebar_side"))
        self.sidebar_side_row.set_model(sidebar_side_model)
        self.sidebar_side_row.set_selected(0 if current_sidebar_side == "left" else 1)
        self.sidebar_side_row.connect("notify::selected", self._on_sidebar_side_selected)

        _THEME_MODES = ["auto", "dark", "light"]
        theme_mode_model = Gtk.StringList()
        for key in _THEME_MODES:
            theme_mode_model.append(_translate(self.language, f"settings.theme_mode.{key}"))
        self.theme_mode_row = Adw.ComboRow(title=_translate(self.language, "settings.theme_mode"))
        self.theme_mode_row.set_model(theme_mode_model)
        selected_mode = current_theme_mode if current_theme_mode in _THEME_MODES else "auto"
        self.theme_mode_row.set_selected(_THEME_MODES.index(selected_mode))
        self.theme_mode_row.connect("notify::selected", self._on_theme_mode_selected)

        self.force_webkit_map_switch = Gtk.Switch()
        self.force_webkit_map_switch.set_active(current_force_webkit_map)
        self.force_webkit_map_switch.set_valign(Gtk.Align.CENTER)
        self.force_webkit_map_switch.connect("notify::active", self._on_force_webkit_map_changed)
        self.force_webkit_map_row = Adw.ActionRow(
            title=_translate(self.language, "settings.map.webkit"),
            subtitle=_translate(self.language, "settings.map.webkit.subtitle"),
        )
        self.force_webkit_map_row.add_suffix(self.force_webkit_map_switch)
        self.force_webkit_map_row.set_activatable_widget(self.force_webkit_map_switch)

        self.traffic_bundesweit_switch = Gtk.Switch()
        self.traffic_bundesweit_switch.set_active(current_traffic_bundesweit)
        self.traffic_bundesweit_switch.set_valign(Gtk.Align.CENTER)
        self.traffic_bundesweit_switch.connect("notify::active", self._on_traffic_bundesweit_changed)
        self.traffic_bundesweit_row = Adw.ActionRow(
            title=_translate(self.language, "settings.traffic.bundesweit"),
            subtitle=_translate(self.language, "settings.traffic.bundesweit.subtitle"),
        )
        self.traffic_bundesweit_row.add_suffix(self.traffic_bundesweit_switch)
        self.traffic_bundesweit_row.set_activatable_widget(self.traffic_bundesweit_switch)

        self.traffic_nrw_switch = Gtk.Switch()
        self.traffic_nrw_switch.set_active(current_traffic_nrw)
        self.traffic_nrw_switch.set_valign(Gtk.Align.CENTER)
        self.traffic_nrw_switch.connect("notify::active", self._on_traffic_nrw_changed)
        self.traffic_nrw_row = Adw.ActionRow(
            title=_translate(self.language, "settings.traffic.nrw"),
            subtitle=_translate(self.language, "settings.traffic.nrw.subtitle"),
        )
        self.traffic_nrw_row.add_suffix(self.traffic_nrw_switch)
        self.traffic_nrw_row.set_activatable_widget(self.traffic_nrw_switch)

        self._NAV_POSITIONS = ["bottom", "top", "left"]
        nav_pos_model = Gtk.StringList()
        for key in self._NAV_POSITIONS:
            nav_pos_model.append(_translate(self.language, f"settings.nav_position.{key}"))
        self.nav_position_row = Adw.ComboRow(title=_translate(self.language, "settings.nav_position"))
        self.nav_position_row.set_model(nav_pos_model)
        sel_nav = self._NAV_POSITIONS.index(current_nav_position) if current_nav_position in self._NAV_POSITIONS else 0
        self.nav_position_row.set_selected(sel_nav)
        self.nav_position_row.connect("notify::selected", self._on_nav_position_selected)

        # Display size: 100 % native down to 25 % (50 % ≈ double the content).
        self._UI_SCALES = [100, 75, 50, 25]
        ui_scale_model = Gtk.StringList()
        for pct in self._UI_SCALES:
            ui_scale_model.append(_translate(self.language, f"settings.ui_scale.{pct}"))
        self.ui_scale_row = Adw.ComboRow(title=_translate(self.language, "settings.ui_scale"))
        self.ui_scale_row.set_model(ui_scale_model)
        sel_scale = self._UI_SCALES.index(current_ui_scale) if current_ui_scale in self._UI_SCALES else 0
        self.ui_scale_row.set_selected(sel_scale)
        self.ui_scale_row.connect("notify::selected", self._on_ui_scale_selected)

        self._ROTATION_MODES = ["follow_sensor", "follow_system"]
        rotation_model = Gtk.StringList()
        for key in self._ROTATION_MODES:
            rotation_model.append(_translate(self.language, f"settings.rotation_mode.{key}"))
        self.rotation_mode_row = Adw.ComboRow(title=_translate(self.language, "settings.rotation_mode"))
        self.rotation_mode_row.set_model(rotation_model)
        sel_rot = self._ROTATION_MODES.index(current_rotation_mode) if current_rotation_mode in self._ROTATION_MODES else 0
        self.rotation_mode_row.set_selected(sel_rot)
        self.rotation_mode_row.connect("notify::selected", self._on_rotation_mode_selected)

        # TTS rows
        self._TTS_LANGUAGES = ["auto", "en", "de"]
        self._TTS_VOICES = ["male", "female"]
        self._TTS_QUALITIES = ["low", "medium", "high"]
        # Backend list: always include espeak; add piper only when available.
        self._TTS_BACKENDS = ["espeak"] + (["piper"] if tts_service.PIPER_AVAILABLE else [])

        self.tts_enabled_row = Adw.SwitchRow(
            title=_translate(self.language, "settings.tts.enabled"),
            subtitle=_translate(self.language, "settings.tts.enabled.subtitle"),
        )
        self.tts_enabled_row.set_active(current_tts_enabled)
        self.tts_enabled_row.connect("notify::active", self._on_tts_enabled_toggled)

        tts_backend_model = Gtk.StringList()
        for key in self._TTS_BACKENDS:
            tts_backend_model.append(_translate(self.language, f"settings.tts.backend.{key}"))
        self.tts_backend_row = Adw.ComboRow(title=_translate(self.language, "settings.tts.backend"))
        self.tts_backend_row.set_model(tts_backend_model)
        _safe_backend = current_tts_backend if current_tts_backend in self._TTS_BACKENDS else "espeak"
        self.tts_backend_row.set_selected(self._TTS_BACKENDS.index(_safe_backend))
        self.tts_backend_row.connect("notify::selected", self._on_tts_backend_selected)

        tts_lang_model = Gtk.StringList()
        for key in self._TTS_LANGUAGES:
            tts_lang_model.append(_translate(self.language, f"settings.tts.language.{key}"))
        self.tts_language_row = Adw.ComboRow(title=_translate(self.language, "settings.tts.language"))
        self.tts_language_row.set_model(tts_lang_model)
        sel_tts_lang = self._TTS_LANGUAGES.index(current_tts_language) if current_tts_language in self._TTS_LANGUAGES else 0
        self.tts_language_row.set_selected(sel_tts_lang)
        self.tts_language_row.connect("notify::selected", self._on_tts_language_selected)

        tts_voice_model = Gtk.StringList()
        for key in self._TTS_VOICES:
            tts_voice_model.append(_translate(self.language, f"settings.tts.voice.{key}"))
        self.tts_voice_row = Adw.ComboRow(title=_translate(self.language, "settings.tts.voice"))
        self.tts_voice_row.set_model(tts_voice_model)
        sel_tts_voice = self._TTS_VOICES.index(current_tts_voice) if current_tts_voice in self._TTS_VOICES else 1
        self.tts_voice_row.set_selected(sel_tts_voice)
        self.tts_voice_row.connect("notify::selected", self._on_tts_voice_selected)

        tts_quality_model = Gtk.StringList()
        for key in self._TTS_QUALITIES:
            tts_quality_model.append(_translate(self.language, f"settings.tts.quality.{key}"))
        self.tts_quality_row = Adw.ComboRow(title=_translate(self.language, "settings.tts.quality"))
        self.tts_quality_row.set_model(tts_quality_model)
        _safe_quality = current_tts_quality if current_tts_quality in self._TTS_QUALITIES else "high"
        self.tts_quality_row.set_selected(self._TTS_QUALITIES.index(_safe_quality))
        self.tts_quality_row.connect("notify::selected", self._on_tts_quality_selected)

        # Language/voice/quality are piper-only options — hide them for espeak.
        _piper_selected = _safe_backend == "piper"
        self.tts_language_row.set_visible(_piper_selected)
        self.tts_voice_row.set_visible(_piper_selected)
        self.tts_quality_row.set_visible(_piper_selected)

        # Volume + music-ducking controls — work for both backends and
        # rely on paplay (PulseAudio/PipeWire). When neither is around
        # the ducking-only rows still appear, they'll just be no-ops.
        self.tts_volume_row = Adw.SpinRow.new_with_range(1, 200, 5)
        self.tts_volume_row.set_title(_translate(self.language, "settings.tts.volume"))
        self.tts_volume_row.set_subtitle(_translate(self.language, "settings.tts.volume.subtitle"))
        self.tts_volume_row.set_value(self._current_tts_volume_pct)
        self.tts_volume_row.connect("notify::value", self._on_tts_volume_changed)

        self.tts_duck_row = Adw.SpinRow.new_with_range(0, 90, 5)
        self.tts_duck_row.set_title(_translate(self.language, "settings.tts.duck_pct"))
        self.tts_duck_row.set_subtitle(_translate(self.language, "settings.tts.duck_pct.subtitle"))
        self.tts_duck_row.set_value(self._current_tts_duck_pct)
        self.tts_duck_row.connect("notify::value", self._on_tts_duck_pct_changed)

        self.tts_duck_pre_row = Adw.SpinRow.new_with_range(0, 2000, 50)
        self.tts_duck_pre_row.set_title(_translate(self.language, "settings.tts.duck_pre_ms"))
        self.tts_duck_pre_row.set_subtitle(_translate(self.language, "settings.tts.duck_pre_ms.subtitle"))
        self.tts_duck_pre_row.set_value(self._current_tts_duck_pre_ms)
        self.tts_duck_pre_row.connect("notify::value", self._on_tts_duck_pre_ms_changed)

        # Download progress row — shown directly below voice options when a download runs.
        self._piper_dl_row = Adw.ActionRow()
        self._piper_dl_row.set_visible(False)
        self._piper_dl_bar = Gtk.ProgressBar()
        self._piper_dl_bar.set_valign(Gtk.Align.CENTER)
        self._piper_dl_bar.set_hexpand(True)
        self._piper_dl_bar.set_show_text(True)
        _dl_cancel_btn = Gtk.Button(icon_name="process-stop-symbolic")
        _dl_cancel_btn.set_valign(Gtk.Align.CENTER)
        _dl_cancel_btn.add_css_class("flat")
        _dl_cancel_btn.set_tooltip_text(_translate(self.language, "settings.tts.dl.cancel"))
        _dl_cancel_btn.connect("clicked", self._on_piper_dl_cancel)
        _dl_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        _dl_box.set_hexpand(True)
        _dl_box.set_valign(Gtk.Align.CENTER)
        _dl_box.append(self._piper_dl_bar)
        _dl_box.append(_dl_cancel_btn)
        self._piper_dl_row.add_suffix(_dl_box)

        # Register callback and show any already-running download immediately.
        tts_service.set_download_callback(self._on_piper_dl_progress)
        for model in tts_service.active_downloads():
            self._on_piper_dl_progress(model, 0.0)

        self.connect("destroy", lambda _w: tts_service.set_download_callback(None))

        # Logging rows
        self.log_app_row = Adw.SwitchRow(
            title=_translate(self.language, "settings.log_app"),
            subtitle=_translate(self.language, "settings.log_app.subtitle"),
        )
        self.log_app_row.set_active(current_log_app_enabled)
        self.log_app_row.connect("notify::active", self._on_log_app_toggled)

        self.log_obd_row = Adw.SwitchRow(
            title=_translate(self.language, "settings.log_obd"),
            subtitle=_translate(self.language, "settings.log_obd.subtitle"),
        )
        self.log_obd_row.set_active(current_log_obd_enabled)
        self.log_obd_row.connect("notify::active", self._on_log_obd_toggled)

        self.obd_auto_record_row = Adw.SwitchRow(
            title=_translate(self.language, "settings.obd_auto_record"),
            subtitle=_translate(self.language, "settings.obd_auto_record.subtitle"),
        )
        self.obd_auto_record_row.set_active(current_obd_auto_record)
        self.obd_auto_record_row.connect("notify::active", self._on_obd_auto_record_toggled)

        # OBD hardware group
        obd_devices = scan_obd_devices()  # (label, port, is_present)
        self._obd_port_values: list[str | None] = [None]

        # Live connection truth for the green "present" marker. A configured
        # dongle that is not actually connected (asleep / removed / left in
        # another car) must NOT render green: the dropdown used to hard-code
        # presence for the selected port, so a missing MX+ looked connected even
        # while "Connected Dongle:" said otherwise.
        _status: dict | None = None
        if self._obd_status_provider is not None:
            try:
                _status = self._obd_status_provider()
            except Exception:
                _status = None
        _live_connected = bool(_status and _status.get("connected"))

        self._dongle_store = Gio.ListStore(item_type=DeviceItem)
        dongle_store = self._dongle_store
        # is_connected gates the green ✓ icon next to a row. It must follow
        # the *live* reader state, not just the saved port preference —
        # otherwise the dropdown shows "configured + paired-but-offline"
        # dongles as connected (the BT entry stays green even when the
        # bottom "Verbundener Dongle" panel correctly says nothing is up).
        # The "auto" row inherits the live state when no port is configured.
        dongle_store.append(DeviceItem(
            label=_translate(self.language, "settings.obd_dongle.auto"),
            port=None,
            is_present=False,
            is_connected=(current_obd_port is None and _live_connected),
        ))
        for lbl, port, is_present in obd_devices:
            dongle_store.append(DeviceItem(
                label=lbl,
                port=port,
                is_present=is_present,
                is_connected=(port == current_obd_port and _live_connected),
            ))
            self._obd_port_values.append(port)
        # Surface the configured port (bt:ADDR Bluetooth dongle / /dev/pts
        # bridge) when the serial scan didn't list it, so the dropdown shows the
        # actual selected dongle instead of silently falling back to "auto".
        if current_obd_port is not None and current_obd_port not in self._obd_port_values:
            dongle_store.append(DeviceItem(
                label=self.dongle_label_for(current_obd_port),
                port=current_obd_port,
                is_present=_live_connected,
                is_connected=_live_connected,
            ))
            self._obd_port_values.append(current_obd_port)

        def _setup_header(_fac: object, li: Gtk.ListItem) -> None:
            li.set_child(Gtk.Label(xalign=0, hexpand=True))

        # Green colouring is reserved for the *currently connected* dongle,
        # not every paired-or-known entry. Painting every historic device green
        # made the dropdown feel like "everything is fine" while the reader
        # was actually offline — one green row for the live dongle is the
        # clean signal.
        def _bind_header(_fac: object, li: Gtk.ListItem) -> None:
            label_widget: Gtk.Label = li.get_child()
            dev: DeviceItem = li.get_item()
            label_widget.set_text(dev._label)
            if dev._is_connected:
                label_widget.add_css_class("success")
            else:
                label_widget.remove_css_class("success")

        def _setup_list(_fac: object, li: Gtk.ListItem) -> None:
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            box.append(Gtk.Label(xalign=0, hexpand=True))
            box.append(Gtk.Image.new_from_icon_name("object-select-symbolic"))
            li.set_child(box)

        def _bind_list(_fac: object, li: Gtk.ListItem) -> None:
            box = li.get_child()
            label_widget: Gtk.Label = box.get_first_child()
            icon: Gtk.Image = label_widget.get_next_sibling()
            dev: DeviceItem = li.get_item()
            label_widget.set_text(dev._label)
            if dev._is_connected:
                label_widget.add_css_class("success")
            else:
                label_widget.remove_css_class("success")
            icon.set_visible(dev._is_connected)

        header_fac = Gtk.SignalListItemFactory()
        header_fac.connect("setup", _setup_header)
        header_fac.connect("bind", _bind_header)

        list_fac = Gtk.SignalListItemFactory()
        list_fac.connect("setup", _setup_list)
        list_fac.connect("bind", _bind_list)

        obd_group = Adw.PreferencesGroup(title=_translate(self.language, "settings.obd"))
        self.dongle_row = Adw.ComboRow(title=_translate(self.language, "settings.obd_dongle"))
        self.dongle_row.set_model(dongle_store)
        self.dongle_row.set_factory(header_fac)
        self.dongle_row.set_list_factory(list_fac)
        if not obd_devices:
            self.dongle_row.set_subtitle(_translate(self.language, "settings.obd_dongle.none_found"))
        selected_idx = 0
        if current_obd_port in self._obd_port_values:
            selected_idx = self._obd_port_values.index(current_obd_port)
        self.dongle_row.set_selected(selected_idx)
        self.dongle_row.connect("notify::selected", self._on_dongle_selected)
        self._dongle_updating = False
        obd_group.add(self.dongle_row)

        # ── App page ──────────────────────────────────────────────────────────
        app_page = Adw.PreferencesPage(
            title=_translate(self.language, "settings.page.app"),
        )

        app_group = Adw.PreferencesGroup(title=_translate(self.language, "settings.app.group"))

        # Version / update row
        current_version = updater.get_current_version()
        if current_last_check:
            try:
                dt = datetime.fromisoformat(current_last_check)
                check_str = _translate(self.language, "settings.app.last_check.prefix") + \
                            dt.strftime("%d.%m.%Y %H:%M")
            except ValueError:
                check_str = _translate(self.language, "settings.app.last_check.never")
        else:
            check_str = _translate(self.language, "settings.app.last_check.never")

        self._update_row = Adw.ActionRow(
            title=_translate(self.language, "settings.app.version_row"),
            subtitle=f"v{current_version}  ·  {check_str}",
        )

        self._update_btn = Gtk.Button(
            label=_translate(self.language, "settings.app.check_btn"),
            valign=Gtk.Align.CENTER,
        )
        self._update_btn.connect("clicked", self._on_check_update)
        self._update_row.add_suffix(self._update_btn)
        app_group.add(self.language_row)
        app_group.add(self.mock_row)
        app_group.add(self._update_row)
        app_page.add(app_group)

        logging_group = Adw.PreferencesGroup(title=_translate(self.language, "settings.logging"))
        logging_group.add(self.log_app_row)
        logging_group.add(self.log_obd_row)
        logging_group.add(self.obd_auto_record_row)
        app_page.add(logging_group)

        self._thumb_cache_row = Adw.SpinRow.new_with_range(10, 2000, 50)
        self._thumb_cache_row.set_title(_translate(self.language, "settings.photos.thumb_cache_max_mb"))
        self._thumb_cache_row.set_subtitle(_translate(self.language, "settings.photos.thumb_cache_max_mb.desc"))
        self._thumb_cache_row.set_value(float(self._current_photo_thumb_cache_max_mb))
        self._thumb_cache_row.connect("notify::value", self._on_thumb_cache_changed)
        photos_group = Adw.PreferencesGroup(title=_translate(self.language, "settings.photos.group"))
        photos_group.add(self._thumb_cache_row)
        app_page.add(photos_group)

        # VIN decoder — one group per provider so each option has its own
        # description (per-option blurb instead of a collective intro).
        self._nhtsa_row = Adw.SwitchRow(
            title=_translate(self.language, "settings.vin_decoder.nhtsa_enable"),
        )
        self._nhtsa_row.set_active(bool(current_nhtsa_enabled))
        self._nhtsa_row.connect("notify::active", self._on_nhtsa_enabled_toggled)
        nhtsa_group = Adw.PreferencesGroup(
            title=_translate(self.language, "settings.vin_decoder.nhtsa"),
            description=_translate(self.language, "settings.vin_decoder.nhtsa.desc"),
        )
        nhtsa_group.add(self._nhtsa_row)

        # PasswordEntryRow gives bullets + the built-in eye-toggle next to
        # the edit pencil, so the user can peek at the key when needed.
        self._autodev_row = Adw.PasswordEntryRow(
            title=_translate(self.language, "settings.vin_decoder.autodev_key"),
        )
        self._autodev_row.set_text(current_autodev_api_key or "")
        self._autodev_row.connect("changed", self._on_autodev_key_changed)
        autodev_group = Adw.PreferencesGroup(
            title=_translate(self.language, "settings.vin_decoder.autodev"),
            description=_translate(self.language, "settings.vin_decoder.autodev.desc"),
        )
        autodev_group.add(self._autodev_row)
        autodev_group.add(self._build_autodev_counter_row())

        self._vd_api_key_row = Adw.PasswordEntryRow(
            title=_translate(self.language, "settings.vin_decoder.api_key"),
        )
        self._vd_api_key_row.set_text(current_vindecoder_api_key or "")
        self._vd_api_key_row.connect("changed", self._on_vd_api_key_changed)

        self._vd_secret_row = Adw.PasswordEntryRow(
            title=_translate(self.language, "settings.vin_decoder.secret_key"),
        )
        self._vd_secret_row.set_text(current_vindecoder_secret_key or "")
        self._vd_secret_row.connect("changed", self._on_vd_secret_changed)

        vindecoder_group = Adw.PreferencesGroup(
            title=_translate(self.language, "settings.vin_decoder.vindecoder"),
            description=_translate(self.language, "settings.vin_decoder.vindecoder.desc"),
        )
        vindecoder_group.add(self._vd_api_key_row)
        vindecoder_group.add(self._vd_secret_row)

        # ── Sync group ──────────────────────────────────────────────────────
        self._SYNC_ACCESS_MODES = ["off", "lan_only", "any"]
        sync_model = Gtk.StringList()
        for key in self._SYNC_ACCESS_MODES:
            sync_model.append(_translate(self.language, f"settings.sync.access.{key}"))
        self._sync_access_row = Adw.ComboRow(title=_translate(self.language, "settings.sync.access"))
        self._sync_access_row.set_subtitle(_translate(self.language, "settings.sync.access.subtitle"))
        self._sync_access_row.set_model(sync_model)
        self._sync_access_row.set_selected(self._SYNC_ACCESS_MODES.index(self._current_sync_access))
        self._sync_access_row.connect("notify::selected", self._on_sync_access_selected)
        sync_group = Adw.PreferencesGroup(title=_translate(self.language, "settings.sync.group"))
        sync_group.add(self._sync_access_row)
        app_page.add(sync_group)

        # ── OBD-Dongle subpage ───────────────────────────────────────────────
        # Dongle dropdown + unified scan list + live "Connected Dongle:" infos
        # live on a dedicated subpage reached via an activatable row below.
        self._obd_subpage = self._build_obd_subpage(obd_group)

        # Activatable row on the App page that opens the OBD-dongle subpage.
        obd_open_group = Adw.PreferencesGroup()
        self._obd_open_row = Adw.ActionRow(
            title=_translate(self.language, "settings.obd_dongle.open_row"),
            subtitle=_translate(self.language, "settings.obd_dongle.open_row.subtitle"),
        )
        self._obd_open_row.set_activatable(True)
        self._obd_open_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        self._obd_open_row.connect("activated", self._on_obd_open_row_activated)
        obd_open_group.add(self._obd_open_row)
        app_page.add(obd_open_group)

        # ── Display page ──────────────────────────────────────────────────────
        display_page = Adw.PreferencesPage(
            title=_translate(self.language, "settings.page.display"),
        )
        display_group = Adw.PreferencesGroup(title=_translate(self.language, "settings.display"))
        display_group.add(self.theme_mode_row)
        display_group.add(self.sidebar_side_row)
        display_group.add(self.nav_position_row)
        display_group.add(self.ui_scale_row)
        display_group.add(self.rotation_mode_row)
        display_page.add(display_group)

        # ── Tour page ─────────────────────────────────────────────────────────
        tour_page = Adw.PreferencesPage(
            title=_translate(self.language, "settings.page.tour"),
        )
        tour_group = Adw.PreferencesGroup(title=_translate(self.language, "settings.page.tour"))
        tour_group.add(self.force_webkit_map_row)
        tour_page.add(tour_group)

        traffic_group = Adw.PreferencesGroup(title=_translate(self.language, "settings.traffic"))
        traffic_group.add(self.traffic_bundesweit_row)
        traffic_group.add(self.traffic_nrw_row)
        tour_page.add(traffic_group)

        tts_group = Adw.PreferencesGroup(title=_translate(self.language, "settings.tts"))
        tts_group.add(self.tts_enabled_row)
        tts_group.add(self.tts_backend_row)
        tts_group.add(self.tts_language_row)
        tts_group.add(self.tts_voice_row)
        tts_group.add(self.tts_quality_row)
        tts_group.add(self.tts_volume_row)
        tts_group.add(self.tts_duck_row)
        tts_group.add(self.tts_duck_pre_row)
        tts_group.add(self._piper_dl_row)
        tour_page.add(tts_group)

        # ── Tacho page ────────────────────────────────────────────────────────
        tacho_page = Adw.PreferencesPage(
            title=_translate(self.language, "settings.page.tacho"),
        )
        tacho_group = Adw.PreferencesGroup(title=_translate(self.language, "settings.page.tacho"))
        tacho_group.add(self.gauge_theme_row)
        tacho_group.add(self.unit_row)
        tacho_page.add(tacho_group)

        # ── Dashcam page ──────────────────────────────────────────────────────
        dc_page = Adw.PreferencesPage(
            title=_translate(self.language, "settings.page.dashcam"),
        )
        dc_group = Adw.PreferencesGroup(title=_translate(self.language, "dashcam.settings.title"))

        # Camera device — editable entry + scan-button popover
        self._dc_cam_entry = Adw.EntryRow(
            title=_translate(self.language, "dashcam.settings.camera"),
        )
        self._dc_cam_entry.set_text(current_dashcam_camera)
        self._dc_cam_entry.connect("changed", self._on_dc_camera_entry_changed)
        self._dc_cam_entry.connect("apply", self._on_dc_camera_entry_apply)

        self._dc_cam_popover = Gtk.Popover()
        self._dc_cam_popover.set_position(Gtk.PositionType.BOTTOM)
        self._dc_cam_list_box = Gtk.ListBox()
        self._dc_cam_list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._dc_cam_list_box.add_css_class("boxed-list")
        pop_scroll = Gtk.ScrolledWindow()
        pop_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        pop_scroll.set_max_content_height(240)
        pop_scroll.set_propagate_natural_height(True)
        pop_scroll.set_child(self._dc_cam_list_box)
        pop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        pop_box.set_margin_top(6)
        pop_box.set_margin_bottom(6)
        pop_box.set_margin_start(6)
        pop_box.set_margin_end(6)
        pop_box.append(pop_scroll)
        self._dc_cam_popover.set_child(pop_box)

        scan_btn = Gtk.MenuButton()
        scan_btn.set_icon_name("view-refresh-symbolic")
        scan_btn.set_tooltip_text(_translate(self.language, "dashcam.settings.camera_scan"))
        scan_btn.set_valign(Gtk.Align.CENTER)
        scan_btn.add_css_class("flat")
        scan_btn.set_popover(self._dc_cam_popover)
        self._dc_cam_popover.connect("show", self._on_dc_cam_popover_show)
        self._dc_cam_entry.add_suffix(scan_btn)
        dc_group.add(self._dc_cam_entry)

        # Resolution — populated dynamically; falls back to RESOLUTIONS on no v4l2-ctl
        self._dc_res_row = Adw.ComboRow(
            title=_translate(self.language, "dashcam.settings.resolution"),
        )
        self._dc_res_row.connect("notify::selected", self._on_dc_resolution_changed)
        dc_group.add(self._dc_res_row)

        # FPS — populated when resolution changes
        self._dc_fps_row = Adw.ComboRow(
            title=_translate(self.language, "dashcam.settings.fps"),
        )
        self._dc_fps_row.connect("notify::selected", self._on_dc_fps_changed)
        dc_group.add(self._dc_fps_row)

        # Codec — VP8 / VP9 / AV1 (Flathub-friendly royalty-free codecs)
        self._dc_codec_ids = ["vp8", "vp9", "av1"]
        codec_model = Gtk.StringList()
        for cid in self._dc_codec_ids:
            codec_model.append(cid.upper())
        self._dc_codec_row = Adw.ComboRow(
            title=_translate(self.language, "dashcam.settings.codec"),
        )
        self._dc_codec_row.set_subtitle(
            _translate(self.language, "dashcam.settings.codec_sub"),
        )
        self._dc_codec_row.set_model(codec_model)
        codec_idx = (
            self._dc_codec_ids.index(self._current_dashcam_codec)
            if self._current_dashcam_codec in self._dc_codec_ids else 0
        )
        self._dc_codec_row.set_selected(codec_idx)
        self._dc_codec_row.connect("notify::selected", self._on_dc_codec_changed)
        dc_group.add(self._dc_codec_row)

        # Populate resolution+fps from camera query (or static fallback)
        self._dc_cam_modes: dict[str, list[int]] = {}
        self._dc_current_res = current_dashcam_resolution
        self._dc_current_fps = current_dashcam_fps
        self._populate_modes(current_dashcam_camera)

        # Segment length
        seg_adj = Gtk.Adjustment(value=current_dashcam_seg_minutes, lower=1, upper=30, step_increment=1)
        self._dc_seg_spin = Gtk.SpinButton(adjustment=seg_adj, climb_rate=1, digits=0)
        self._dc_seg_spin.connect("value-changed", self._on_dc_seg_minutes_changed)
        seg_row = Adw.ActionRow(
            title=_translate(self.language, "dashcam.settings.seg_len"),
            subtitle=_translate(self.language, "dashcam.settings.seg_len_sub"),
        )
        seg_row.add_suffix(self._dc_seg_spin)
        seg_row.set_activatable_widget(self._dc_seg_spin)
        dc_group.add(seg_row)

        # Max segments
        max_adj = Gtk.Adjustment(value=current_dashcam_max_segments, lower=2, upper=60, step_increment=1)
        self._dc_max_spin = Gtk.SpinButton(adjustment=max_adj, climb_rate=1, digits=0)
        self._dc_max_spin.connect("value-changed", self._on_dc_max_segments_changed)
        max_row = Adw.ActionRow(
            title=_translate(self.language, "dashcam.settings.max_seg"),
            subtitle=_translate(self.language, "dashcam.settings.max_seg_sub"),
        )
        max_row.add_suffix(self._dc_max_spin)
        max_row.set_activatable_widget(self._dc_max_spin)
        dc_group.add(max_row)

        # Screen dim timeout
        dim_adj = Gtk.Adjustment(value=current_dashcam_dim_timeout, lower=0, upper=300, step_increment=5)
        self._dc_dim_spin = Gtk.SpinButton(adjustment=dim_adj, climb_rate=1, digits=0)
        self._dc_dim_spin.connect("value-changed", self._on_dc_dim_timeout_changed)
        dim_row = Adw.ActionRow(
            title=_translate(self.language, "dashcam.settings.dim_timeout"),
            subtitle=_translate(self.language, "dashcam.settings.dim_timeout_sub"),
        )
        dim_row.add_suffix(self._dc_dim_spin)
        dim_row.set_activatable_widget(self._dc_dim_spin)
        dc_group.add(dim_row)

        dc_page.add(dc_group)

        # Storage folder group
        storage_group = Adw.PreferencesGroup(title=_translate(self.language, "dashcam.settings.storage"))
        self._dc_rolling_row = self._make_folder_row(
            title=_translate(self.language, "dashcam.settings.rolling_dir"),
            current=current_dashcam_rolling_dir,
            callback=self._on_dc_rolling_dir_chosen,
        )
        self._dc_saved_row = self._make_folder_row(
            title=_translate(self.language, "dashcam.settings.saved_dir"),
            current=current_dashcam_saved_dir,
            callback=self._on_dc_saved_dir_chosen,
        )
        storage_group.add(self._dc_rolling_row)
        storage_group.add(self._dc_saved_row)
        dc_page.add(storage_group)

        # GPS / OSD group
        gps_group = Adw.PreferencesGroup(title=_translate(self.language, "dashcam.settings.gps"))
        gps_osd_row = Adw.SwitchRow(
            title=_translate(self.language, "dashcam.settings.gps_osd"),
            subtitle=_translate(self.language, "dashcam.settings.gps_osd_sub"),
        )
        gps_osd_row.set_active(current_dashcam_gps_osd)
        gps_osd_row.connect("notify::active", self._on_dc_gps_osd_toggled)
        gps_group.add(gps_osd_row)

        speed_osd_row = Adw.SwitchRow(
            title=_translate(self.language, "dashcam.settings.speed_osd"),
            subtitle=_translate(self.language, "dashcam.settings.speed_osd_sub"),
        )
        speed_osd_row.set_active(current_dashcam_speed_osd)
        speed_osd_row.connect("notify::active", self._on_dc_speed_osd_toggled)
        gps_group.add(speed_osd_row)
        dc_page.add(gps_group)

        # ── ViewStack ─────────────────────────────────────────────────────────────
        view_stack = Adw.ViewStack()
        view_stack.add_titled_with_icon(
            app_page, "app",
            _translate(self.language, "settings.page.app"),
            "applications-system-symbolic",
        )
        view_stack.add_titled_with_icon(
            display_page, "display",
            _translate(self.language, "settings.page.display"),
            "video-display-symbolic",
        )
        view_stack.add_titled_with_icon(
            tour_page, "tour",
            _translate(self.language, "settings.page.tour"),
            "navigate-north-symbolic",
        )
        view_stack.add_titled_with_icon(
            dc_page, "dashcam",
            _translate(self.language, "settings.page.dashcam"),
            "camera-video-symbolic",
        )
        view_stack.add_titled_with_icon(
            tacho_page, "tacho",
            _translate(self.language, "settings.page.tacho"),
            "speedometer4-symbolic",
        )

        # ── Accounts page ─────────────────────────────────────────────────────
        accounts_page = Adw.PreferencesPage(
            title=_translate(self.language, "settings.page.accounts"),
        )
        accounts_page.add(nhtsa_group)
        accounts_page.add(autodev_group)
        accounts_page.add(vindecoder_group)
        view_stack.add_titled_with_icon(
            accounts_page, "accounts",
            _translate(self.language, "settings.page.accounts"),
            "avatar-default-symbolic",
        )

        # ── Top navigation (ViewSwitcher inside the header) ───────────────────
        view_switcher = Adw.ViewSwitcher()
        view_switcher.set_stack(view_stack)
        view_switcher.set_policy(Adw.ViewSwitcherPolicy.NARROW)

        # ── Header: NavigationView injects the back-arrow automatically ──────
        dlg_header = Adw.HeaderBar()
        dlg_header.set_show_start_title_buttons(False)
        dlg_header.set_show_end_title_buttons(False)
        dlg_header.set_title_widget(view_switcher)

        # Fallback switcher bar for narrow widths
        switcher_bar = Adw.ViewSwitcherBar()
        switcher_bar.set_stack(view_stack)
        switcher_bar.set_reveal(False)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(dlg_header)
        toolbar_view.add_bottom_bar(switcher_bar)
        toolbar_view.set_content(view_stack)

        def _clear_focus(*_args: object) -> bool:
            root = self.get_root()
            if root:
                root.set_focus(None)
            return False

        view_stack.connect("notify::visible-child", lambda *_: GLib.idle_add(_clear_focus))
        self.connect("map", lambda *_: GLib.idle_add(_clear_focus))

        self.set_child(toolbar_view)

    # ── OBD-Dongle subpage ────────────────────────────────────────────────────

    def _build_obd_subpage(self, obd_group: Adw.PreferencesGroup) -> Adw.NavigationPage:
        """Assemble the OBD-dongle subpage: dropdown + unified scan + live infos."""
        page = Adw.PreferencesPage(
            title=_translate(self.language, "settings.obd_dongle.page"),
        )

        # Dongle dropdown (moved off the App page).
        page.add(obd_group)

        # Unified scan list — a single discovery scan that shows both already
        # paired and freshly found OBD devices. "Connect" pairs first when the
        # device isn't bonded yet (handled in the BT mixin).
        scan_group = Adw.PreferencesGroup(
            title=_translate(self.language, "settings.bt_obd"),
            description=_translate(self.language, "settings.bt_obd.desc"),
        )
        self._bt_nearby_expander = _BtExpander()
        self._bt_nearby_expander.set_title(_translate(self.language, "settings.obd_dongle.scan_title"))
        self._bt_nearby_expander.set_subtitle(_translate(self.language, "settings.obd_dongle.scan_subtitle"))
        self._bt_nearby_rows: list[Adw.ActionRow] = []

        _nearby_scan_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        _nearby_scan_btn.set_valign(Gtk.Align.CENTER)
        _nearby_scan_btn.add_css_class("flat")
        _nearby_scan_btn.set_tooltip_text(_translate(self.language, "settings.bt_obd.nearby.scan"))
        _nearby_scan_btn.connect("clicked", self._on_bt_nearby_scan_clicked)
        self._bt_nearby_expander.add_action(_nearby_scan_btn)
        self._bt_nearby_scan_btn = _nearby_scan_btn
        scan_group.add(self._bt_nearby_expander.widget)
        page.add(scan_group)

        # Live "Connected Dongle:" infos, snapshotted at construction time.
        page.add(self._build_connected_dongle_group())

        # Wrap the page in a ToolbarView with a HeaderBar so the NavigationView
        # shows a back arrow (a bare PreferencesPage child has no header, which is
        # why this subpage had no way back). Mirrors the main settings page.
        header = Adw.HeaderBar()
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(page)
        nav_page = Adw.NavigationPage(
            title=_translate(self.language, "settings.obd_dongle.page"),
            child=toolbar,
            tag="settings-obd-dongle",
        )
        # Auto-trigger the nearby scan when the subpage becomes visible.
        # Users found "open Settings → see candidates listed" the natural
        # flow; clicking a refresh icon to populate an empty list felt like
        # an obstacle. Idempotent: the click handler bails out if a scan is
        # already running.
        nav_page.connect("shown", self._on_obd_subpage_shown)
        nav_page.connect("hidden", self._on_obd_subpage_hidden)
        return nav_page

    def _on_obd_subpage_shown(self, _page: Adw.NavigationPage) -> None:
        """Kick off a nearby BT scan + live state polling when the subpage opens."""
        # Auto-scan once on first arrival.
        if not getattr(self, "_bt_nearby_scan_active", False):
            btn = getattr(self, "_bt_nearby_scan_btn", None)
            if btn is not None:
                self._on_bt_nearby_scan_clicked(btn)
        # Start a 1.5 s poll that re-renders the dropdown and Nearby list when
        # the live OBD connection state flips. Without this the user unplugs
        # the dongle, the top-bar indicator turns grey, but Settings keeps
        # showing the dongle as connected until they back out and re-enter.
        self._obd_last_seen_connected = self._obd_status_is_connected()
        if getattr(self, "_obd_subpage_poll_id", 0):
            return
        self._obd_subpage_poll_id = GLib.timeout_add(1500, self._obd_subpage_poll_tick)

    def _on_obd_subpage_hidden(self, _page: Adw.NavigationPage) -> None:
        """Stop the live-state poll when the user leaves the subpage."""
        poll_id = getattr(self, "_obd_subpage_poll_id", 0)
        if poll_id:
            try:
                GLib.source_remove(poll_id)
            except Exception:
                pass
            self._obd_subpage_poll_id = 0

    def _obd_status_is_connected(self) -> bool:
        """Best-effort snapshot of the reader's current connection state."""
        if self._obd_status_provider is None:
            return False
        try:
            status = self._obd_status_provider() or {}
            return bool(status.get("connected"))
        except Exception:
            return False

    def _obd_subpage_poll_tick(self) -> bool:
        """Periodic check while the subpage is visible — refresh on state flip."""
        now_connected = self._obd_status_is_connected()
        if now_connected != getattr(self, "_obd_last_seen_connected", False):
            self._obd_last_seen_connected = now_connected
            # Dropdown ✓ rendering + Nearby row buttons depend on the live
            # state — rebuild both so the page mirrors what the reader is
            # actually doing right now.
            try:
                self._refresh_dongle_dropdown(self.current_obd_port)
            except Exception:
                log.debug("dropdown refresh on state flip failed", exc_info=True)
            cached_devs = getattr(self, "_bt_nearby_last_devices", None)
            if cached_devs is not None:
                try:
                    self._bt_nearby_scan_done(cached_devs)
                except Exception:
                    log.debug("nearby re-render on state flip failed", exc_info=True)
            # Bottom "Verbundener Dongle:" panel also depends on the live
            # state — without rebuilding it the panel keeps showing whatever
            # the first build saw (a half-second after the subpage opened).
            try:
                self._refresh_connected_dongle_group()
            except Exception:
                log.debug("connected-dongle panel refresh failed", exc_info=True)
        return True  # keep polling while the subpage is visible

    def _refresh_connected_dongle_group(self) -> None:
        """Re-populate the "Verbundener Dongle:" panel with the current snapshot."""
        group = getattr(self, "_connected_dongle_group", None)
        if group is None:
            return
        # Adw.PreferencesGroup has no public clear() — remove children one by
        # one via the GTK widget API. The group's internal listbox is a child.
        child = group.get_first_child()
        # The first child is the internal header; the rows live in a list under
        # it. Easiest reliable path: remove every Adw.ActionRow we appended.
        for row in list(getattr(self, "_connected_dongle_rows", [])):
            try:
                group.remove(row)
            except Exception:
                pass
        self._connected_dongle_rows = []
        self._populate_connected_dongle_group(group)

    def _build_connected_dongle_group(self) -> Adw.PreferencesGroup:
        """Group showing a snapshot of the live OBD connection state.

        Built once; the contents are repopulated by ``_refresh_connected_dongle_group``
        when the subpage poller detects a connect/disconnect.
        """
        group = Adw.PreferencesGroup(
            title=_translate(self.language, "settings.obd_dongle.connected_heading"),
        )
        self._connected_dongle_group = group
        self._connected_dongle_rows: list[Adw.ActionRow] = []
        self._populate_connected_dongle_group(group)
        return group

    def _populate_connected_dongle_group(self, group: Adw.PreferencesGroup) -> None:
        """Refresh the panel's rows in-place from the current status snapshot."""
        status: dict | None = None
        if self._obd_status_provider is not None:
            try:
                status = self._obd_status_provider()
            except Exception:
                status = None

        connected = bool(status and status.get("connected"))
        if not connected:
            row = Adw.ActionRow(
                title=_translate(self.language, "settings.obd_dongle.not_connected"),
            )
            row.set_activatable(False)
            group.add(row)
            self._connected_dongle_rows.append(row)
            return

        assert status is not None  # narrowed by `connected`

        def _info_row(title: str, value: str) -> Adw.ActionRow:
            row = Adw.ActionRow(title=title)
            row.set_activatable(False)
            label = Gtk.Label(label=value)
            label.add_css_class("dim-label")
            label.set_valign(Gtk.Align.CENTER)
            label.set_selectable(True)
            row.add_suffix(label)
            return row

        rows = [
            _info_row(
                _translate(self.language, "settings.obd_dongle.status"),
                _translate(self.language, "settings.obd_dongle.status.connected"),
            ),
        ]
        port = str(status.get("port") or "")
        if port:
            rows.append(_info_row(
                _translate(self.language, "settings.obd_dongle.address"), port,
            ))
        adapter = str(status.get("adapter") or "")
        if adapter:
            rows.append(_info_row(
                _translate(self.language, "settings.obd_dongle.adapter"), adapter,
            ))
        for row in rows:
            group.add(row)
            self._connected_dongle_rows.append(row)

    def _on_obd_open_row_activated(self, _row: Adw.ActionRow) -> None:
        if self._outer_nav is None:
            return
        if self._outer_nav.find_page("settings-obd-dongle") is not None:
            return
        self._outer_nav.push(self._obd_subpage)

    # ── Page lifecycle (NavigationPage signals) ───────────────────────────────

    def _on_hiding(self, _page: SettingsDialog) -> None:
        self._closing = True
        tts_service.set_download_callback(None)
        self._cancel_no_update_reset()
