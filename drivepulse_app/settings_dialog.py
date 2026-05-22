"""Settings dialog for DrivePulse."""
from __future__ import annotations

from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GObject, Gtk  # noqa: E402

import threading
from datetime import datetime

from gi.repository import GLib  # noqa: E402

from .common import SUPPORTED_LANGUAGES, _normalize_language, _translate, language_name
from .gauge import all_theme_options
from .obd_devices import bind_bt_to_rfcomm, probe_bt_rfcomm_socket, scan_bt_nearby_devices, scan_bt_paired_devices, scan_obd_devices
from . import tts_service, updater


class DeviceItem(GObject.Object):
    __gtype_name__ = "DrivePulseDeviceItem"

    def __init__(self, label: str, port: str | None, is_present: bool = False, is_connected: bool = False) -> None:
        super().__init__()
        self._label = label
        self._port = port
        self._is_present = is_present
        self._is_connected = is_connected


class _BtExpander:
    """Expander row for BT device lists using bundled icons (no system icon dependency)."""

    def __init__(self) -> None:
        self._expanded = False

        self._header = Adw.ActionRow()
        self._header.set_activatable(True)
        self._header.connect("activated", self._toggle)

        self._chevron = Gtk.Image.new_from_icon_name("dp-chevron-down-symbolic")
        self._chevron.set_pixel_size(16)
        self._chevron.set_valign(Gtk.Align.CENTER)
        self._header.add_suffix(self._chevron)

        self._rows_box = Gtk.ListBox()
        self._rows_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._rows_box.add_css_class("boxed-list")

        self._revealer = Gtk.Revealer()
        self._revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._revealer.set_transition_duration(200)
        self._revealer.set_reveal_child(False)
        self._revealer.set_child(self._rows_box)

        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.widget.append(self._header)
        self.widget.append(self._revealer)

    def _toggle(self, *_: object) -> None:
        self.set_expanded(not self._expanded)

    def set_expanded(self, value: bool) -> None:
        self._expanded = value
        self._revealer.set_reveal_child(value)
        self._chevron.set_from_icon_name(
            "dp-chevron-up-symbolic" if value else "dp-chevron-down-symbolic"
        )

    def get_expanded(self) -> bool:
        return self._expanded

    def set_title(self, title: str) -> None:
        self._header.set_title(title)

    def set_subtitle(self, subtitle: str) -> None:
        self._header.set_subtitle(subtitle)

    def add_action(self, widget: Gtk.Widget) -> None:
        self._header.add_suffix(widget)

    def add_row(self, row: Gtk.Widget) -> None:
        self._rows_box.append(row)

    def remove(self, row: Gtk.Widget) -> None:
        self._rows_box.remove(row)


class SettingsDialog(Adw.NavigationPage):
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
        current_log_app_enabled: bool = True,
        on_log_app_enabled_changed: Callable[[bool], None] | None = None,
        current_log_obd_enabled: bool = True,
        on_log_obd_enabled_changed: Callable[[bool], None] | None = None,
        current_vindecoder_api_key: str = "",
        on_vindecoder_api_key_changed: Callable[[str], None] | None = None,
        current_vindecoder_secret_key: str = "",
        on_vindecoder_secret_key_changed: Callable[[str], None] | None = None,
        current_autodev_api_key: str = "",
        on_autodev_api_key_changed: Callable[[str], None] | None = None,
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
        self.on_traffic_bundesweit_changed = on_traffic_bundesweit_changed
        self.on_traffic_nrw_changed = on_traffic_nrw_changed
        self.on_last_check_updated = on_last_check_updated
        self.on_dashcam_camera_changed = on_dashcam_camera_changed
        self.on_dashcam_resolution_changed = on_dashcam_resolution_changed
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
        self.on_rotation_mode_changed = on_rotation_mode_changed
        self.on_tts_enabled_changed = on_tts_enabled_changed
        self.on_tts_backend_changed = on_tts_backend_changed
        self.on_tts_language_changed = on_tts_language_changed
        self.on_tts_voice_changed = on_tts_voice_changed
        self.on_tts_quality_changed = on_tts_quality_changed
        self.on_log_app_enabled_changed = on_log_app_enabled_changed
        self.on_log_obd_enabled_changed = on_log_obd_enabled_changed
        self.on_vindecoder_api_key_changed = on_vindecoder_api_key_changed
        self.on_vindecoder_secret_key_changed = on_vindecoder_secret_key_changed
        self.on_autodev_api_key_changed = on_autodev_api_key_changed
        self._remote_version: str | None = None
        self._closing = False
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

        _NAV_POSITIONS = ["bottom", "top"]
        nav_pos_model = Gtk.StringList()
        nav_pos_model.append(_translate(self.language, "settings.nav_position.bottom"))
        nav_pos_model.append(_translate(self.language, "settings.nav_position.top"))
        self.nav_position_row = Adw.ComboRow(title=_translate(self.language, "settings.nav_position"))
        self.nav_position_row.set_model(nav_pos_model)
        sel_nav = _NAV_POSITIONS.index(current_nav_position) if current_nav_position in _NAV_POSITIONS else 0
        self.nav_position_row.set_selected(sel_nav)
        self.nav_position_row.connect("notify::selected", self._on_nav_position_selected)

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

        # OBD hardware group
        obd_devices = scan_obd_devices()  # (label, port, is_present)
        self._obd_port_values: list[str | None] = [None]

        self._dongle_store = Gio.ListStore(item_type=DeviceItem)
        dongle_store = self._dongle_store
        dongle_store.append(DeviceItem(
            label=_translate(self.language, "settings.obd_dongle.auto"),
            port=None,
            is_present=False,
            is_connected=(current_obd_port is None),
        ))
        for lbl, port, is_present in obd_devices:
            dongle_store.append(DeviceItem(
                label=lbl,
                port=port,
                is_present=is_present,
                is_connected=(port == current_obd_port),
            ))
            self._obd_port_values.append(port)

        def _setup_header(_fac: object, li: Gtk.ListItem) -> None:
            li.set_child(Gtk.Label(xalign=0, hexpand=True))

        def _bind_header(_fac: object, li: Gtk.ListItem) -> None:
            label_widget: Gtk.Label = li.get_child()
            dev: DeviceItem = li.get_item()
            label_widget.set_text(dev._label)
            if dev._is_present:
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
            if dev._is_present:
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
        app_page.add(logging_group)

        # VIN decoder group
        self._autodev_row = Adw.EntryRow(
            title=_translate(self.language, "settings.vin_decoder.autodev_key"),
        )
        self._autodev_row.set_text(current_autodev_api_key or "")
        self._autodev_row.connect("changed", self._on_autodev_key_changed)

        self._vd_api_key_row = Adw.EntryRow(
            title=_translate(self.language, "settings.vin_decoder.api_key"),
        )
        self._vd_api_key_row.set_text(current_vindecoder_api_key or "")
        self._vd_api_key_row.connect("changed", self._on_vd_api_key_changed)

        self._vd_secret_row = Adw.EntryRow(
            title=_translate(self.language, "settings.vin_decoder.secret_key"),
        )
        self._vd_secret_row.set_text(current_vindecoder_secret_key or "")
        self._vd_secret_row.connect("changed", self._on_vd_secret_changed)

        vd_group = Adw.PreferencesGroup(
            title=_translate(self.language, "settings.vin_decoder"),
            description=_translate(self.language, "settings.vin_decoder.desc"),
        )
        vd_group.add(self._autodev_row)
        vd_group.add(self._vd_api_key_row)
        vd_group.add(self._vd_secret_row)

        # OBD group
        app_page.add(obd_group)

        # ── Paired BT devices (already bonded to this phone) ─────────────────
        bt_group = Adw.PreferencesGroup(
            title=_translate(self.language, "settings.bt_obd"),
            description=_translate(self.language, "settings.bt_obd.desc"),
        )

        self._bt_expander = _BtExpander()
        self._bt_expander.set_title(_translate(self.language, "settings.bt_obd.scan"))
        self._bt_expander.set_subtitle(_translate(self.language, "settings.bt_obd.scan.subtitle"))
        self._bt_device_rows: list[Adw.ActionRow] = []

        _bt_refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        _bt_refresh_btn.set_valign(Gtk.Align.CENTER)
        _bt_refresh_btn.add_css_class("flat")
        _bt_refresh_btn.set_tooltip_text(_translate(self.language, "settings.bt_obd.refresh"))
        _bt_refresh_btn.connect("clicked", self._on_bt_refresh_clicked)
        self._bt_expander.add_action(_bt_refresh_btn)
        bt_group.add(self._bt_expander.widget)

        # ── Nearby BT devices (discovery scan) ───────────────────────────────
        self._bt_nearby_expander = _BtExpander()
        self._bt_nearby_expander.set_title(_translate(self.language, "settings.bt_obd.nearby"))
        self._bt_nearby_expander.set_subtitle(_translate(self.language, "settings.bt_obd.nearby.subtitle"))
        self._bt_nearby_rows: list[Adw.ActionRow] = []

        _nearby_scan_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        _nearby_scan_btn.set_valign(Gtk.Align.CENTER)
        _nearby_scan_btn.add_css_class("flat")
        _nearby_scan_btn.set_tooltip_text(_translate(self.language, "settings.bt_obd.nearby.scan"))
        _nearby_scan_btn.connect("clicked", self._on_bt_nearby_scan_clicked)
        self._bt_nearby_expander.add_action(_nearby_scan_btn)
        self._bt_nearby_scan_btn = _nearby_scan_btn
        bt_group.add(self._bt_nearby_expander.widget)

        app_page.add(bt_group)

        self._paired_addrs: set[str] = set()

        # Trigger initial paired scan and nearby discovery in background
        self._bt_scan_async()
        self._on_bt_nearby_scan_clicked(self._bt_nearby_scan_btn)

        # ── Display page ──────────────────────────────────────────────────────
        display_page = Adw.PreferencesPage(
            title=_translate(self.language, "settings.page.display"),
        )
        display_group = Adw.PreferencesGroup(title=_translate(self.language, "settings.display"))
        display_group.add(self.theme_mode_row)
        display_group.add(self.sidebar_side_row)
        display_group.add(self.nav_position_row)
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
        from .dashcam_recorder import RESOLUTIONS, list_cameras  # lazy import

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
        accounts_page.add(vd_group)
        view_stack.add_titled_with_icon(
            accounts_page, "accounts",
            _translate(self.language, "settings.page.accounts"),
            "avatar-default-symbolic",
        )

        # ── Bottom navigation bar (standard GNOME pattern) ────────────────────
        switcher_bar = Adw.ViewSwitcherBar()
        switcher_bar.set_stack(view_stack)
        switcher_bar.set_reveal(True)

        # ── Header: NavigationView injects the back-arrow automatically ──────
        dlg_header = Adw.HeaderBar()
        dlg_header.set_show_start_title_buttons(False)
        dlg_header.set_show_end_title_buttons(False)

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

    # ── Page lifecycle (NavigationPage signals) ───────────────────────────────

    def _on_hiding(self, _page: "SettingsDialog") -> None:
        self._closing = True
        tts_service.set_download_callback(None)

    # ── Dashcam callbacks ─────────────────────────────────────────────────────

    def _make_folder_row(
        self, title: str, current: str, callback: Callable[[str], None]
    ) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title, subtitle=current or _translate(self.language, "dashcam.settings.dir_default"))
        row.set_subtitle_lines(1)
        btn = Gtk.Button(label=_translate(self.language, "dashcam.settings.choose_dir"))
        btn.add_css_class("flat")
        btn.set_valign(Gtk.Align.CENTER)
        btn.connect("clicked", lambda _b, r=row, cb=callback: self._pick_folder(r, cb))
        row.add_suffix(btn)
        return row

    def _pick_folder(self, row: Adw.ActionRow, callback: Callable[[str], None]) -> None:
        chooser = Gtk.FileChooserNative(
            title=row.get_title(),
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            accept_label=_translate(self.language, "dashcam.settings.dir_select"),
            cancel_label=_translate(self.language, "dashcam.settings.dir_cancel"),
        )
        chooser.set_transient_for(self.get_root())
        chooser.connect("response", lambda dlg, resp, r=row, cb=callback: self._on_folder_response(dlg, resp, r, cb))
        chooser.show()

    def _on_folder_response(
        self, dlg: Gtk.FileChooserNative, resp: int, row: Adw.ActionRow, callback: Callable[[str], None]
    ) -> None:
        if resp == Gtk.ResponseType.ACCEPT:
            f = dlg.get_file()
            path = f.get_path() if f else None
            if path:
                row.set_subtitle(path)
                callback(path)

    def _on_dc_gps_osd_toggled(self, row: Adw.SwitchRow, _param: Any) -> None:
        if self.on_dashcam_gps_osd_changed:
            self.on_dashcam_gps_osd_changed(row.get_active())

    def _on_dc_speed_osd_toggled(self, row: Adw.SwitchRow, _param: Any) -> None:
        if self.on_dashcam_speed_osd_changed:
            self.on_dashcam_speed_osd_changed(row.get_active())

    def _on_dc_rolling_dir_chosen(self, path: str) -> None:
        if self.on_dashcam_rolling_dir_changed:
            self.on_dashcam_rolling_dir_changed(path)

    def _on_dc_saved_dir_chosen(self, path: str) -> None:
        if self.on_dashcam_saved_dir_changed:
            self.on_dashcam_saved_dir_changed(path)

    def _on_dc_camera_entry_changed(self, entry: Adw.EntryRow) -> None:
        path = entry.get_text().strip()
        if path and self.on_dashcam_camera_changed:
            self.on_dashcam_camera_changed(path)

    def _on_dc_camera_entry_apply(self, entry: Adw.EntryRow) -> None:
        path = entry.get_text().strip()
        if path:
            self._populate_modes(path)

    def _on_dc_cam_popover_show(self, _pop: Gtk.Popover) -> None:
        from .dashcam_recorder import list_cameras
        while (child := self._dc_cam_list_box.get_first_child()) is not None:
            self._dc_cam_list_box.remove(child)
        cameras = list_cameras()
        if not cameras:
            lbl = Gtk.Label(label=_translate(self.language, "dashcam.settings.camera_none"))
            lbl.add_css_class("dim-label")
            lbl.set_margin_top(8)
            lbl.set_margin_bottom(8)
            self._dc_cam_list_box.append(lbl)
            return
        for cam in cameras:
            row = Gtk.ListBoxRow()
            lbl = Gtk.Label(label=cam, xalign=0)
            lbl.set_margin_top(8)
            lbl.set_margin_bottom(8)
            lbl.set_margin_start(12)
            lbl.set_margin_end(12)
            row.set_child(lbl)
            row.cam_path = cam
            self._dc_cam_list_box.append(row)
        self._dc_cam_list_box.connect("row-activated", self._on_dc_cam_row_activated)

    def _on_dc_cam_row_activated(self, _lb: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        self._dc_cam_entry.set_text(row.cam_path)
        self._dc_cam_popover.popdown()
        self._populate_modes(row.cam_path)

    # ── Camera mode helpers ───────────────────────────────────────────────────

    def _populate_modes(self, device: str) -> None:
        """Query camera modes and fill resolution + fps combos."""
        from .dashcam_recorder import FPS_OPTIONS, RESOLUTIONS, query_camera_modes
        modes = query_camera_modes(device)
        self._dc_cam_modes = modes

        resolutions = list(modes.keys()) if modes else RESOLUTIONS
        res_model = Gtk.StringList()
        for r in resolutions:
            res_model.append(r)
        self._dc_res_row.set_model(res_model)
        idx = resolutions.index(self._dc_current_res) if self._dc_current_res in resolutions else 0
        self._dc_res_row.set_selected(idx)

        self._populate_fps_for_res(resolutions[idx] if resolutions else self._dc_current_res)

    def _populate_fps_for_res(self, resolution: str) -> None:
        """Fill the FPS combo for the given resolution."""
        from .dashcam_recorder import FPS_OPTIONS
        fps_list = self._dc_cam_modes.get(resolution, FPS_OPTIONS)
        fps_model = Gtk.StringList()
        for f in fps_list:
            fps_model.append(str(f))
        self._dc_fps_row.set_model(fps_model)
        strs = [str(f) for f in fps_list]
        cur = str(self._dc_current_fps)
        idx = strs.index(cur) if cur in strs else 0
        self._dc_fps_row.set_selected(idx)

    def _on_dc_resolution_changed(self, row: Adw.ComboRow, _pspec: Any) -> None:
        item = row.get_selected_item()
        if not item:
            return
        res = item.get_string()
        self._dc_current_res = res
        self._populate_fps_for_res(res)
        if self.on_dashcam_resolution_changed:
            self.on_dashcam_resolution_changed(res)

    def _on_dc_fps_changed(self, row: Adw.ComboRow, _pspec: Any) -> None:
        item = row.get_selected_item()
        if not item:
            return
        fps = int(item.get_string())
        self._dc_current_fps = fps
        if self.on_dashcam_fps_changed:
            self.on_dashcam_fps_changed(fps)

    def _on_dc_seg_minutes_changed(self, spin: Gtk.SpinButton) -> None:
        if self.on_dashcam_seg_minutes_changed:
            self.on_dashcam_seg_minutes_changed(int(spin.get_value()))

    def _on_dc_max_segments_changed(self, spin: Gtk.SpinButton) -> None:
        if self.on_dashcam_max_segments_changed:
            self.on_dashcam_max_segments_changed(int(spin.get_value()))

    def _on_dc_dim_timeout_changed(self, spin: Gtk.SpinButton) -> None:
        if self.on_dashcam_dim_timeout_changed:
            self.on_dashcam_dim_timeout_changed(int(spin.get_value()))

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

    def _on_force_webkit_map_changed(self, *_args: Any) -> None:
        if self.on_force_webkit_map_changed is not None:
            self.on_force_webkit_map_changed(self.force_webkit_map_switch.get_active())

    def _on_traffic_bundesweit_changed(self, *_args: Any) -> None:
        if self.on_traffic_bundesweit_changed is not None:
            self.on_traffic_bundesweit_changed(self.traffic_bundesweit_switch.get_active())

    def _on_traffic_nrw_changed(self, *_args: Any) -> None:
        if self.on_traffic_nrw_changed is not None:
            self.on_traffic_nrw_changed(self.traffic_nrw_switch.get_active())

    def _on_nav_position_selected(self, *_args: Any) -> None:
        if self.on_nav_position_changed is not None:
            pos = "top" if self.nav_position_row.get_selected() == 1 else "bottom"
            self.on_nav_position_changed(pos)

    def _on_rotation_mode_selected(self, *_args: Any) -> None:
        if self.on_rotation_mode_changed is not None:
            idx = self.rotation_mode_row.get_selected()
            mode = self._ROTATION_MODES[idx] if 0 <= idx < len(self._ROTATION_MODES) else self._ROTATION_MODES[0]
            self.on_rotation_mode_changed(mode)

    def _on_tts_enabled_toggled(self, row: Adw.SwitchRow, _param: Any) -> None:
        if self.on_tts_enabled_changed is not None:
            self.on_tts_enabled_changed(row.get_active())

    def _on_tts_backend_selected(self, *_args: Any) -> None:
        idx = self.tts_backend_row.get_selected()
        backend = self._TTS_BACKENDS[idx] if 0 <= idx < len(self._TTS_BACKENDS) else "espeak"
        piper = backend == "piper"
        self.tts_language_row.set_visible(piper)
        self.tts_voice_row.set_visible(piper)
        self.tts_quality_row.set_visible(piper)
        if self.on_tts_backend_changed is not None:
            self.on_tts_backend_changed(backend)

    def _on_tts_language_selected(self, *_args: Any) -> None:
        if self.on_tts_language_changed is not None:
            idx = self.tts_language_row.get_selected()
            lang = self._TTS_LANGUAGES[idx] if 0 <= idx < len(self._TTS_LANGUAGES) else "auto"
            self.on_tts_language_changed(lang)

    def _on_tts_voice_selected(self, *_args: Any) -> None:
        if self.on_tts_voice_changed is not None:
            idx = self.tts_voice_row.get_selected()
            voice = self._TTS_VOICES[idx] if 0 <= idx < len(self._TTS_VOICES) else "female"
            self.on_tts_voice_changed(voice)

    def _on_tts_quality_selected(self, *_args: Any) -> None:
        if self.on_tts_quality_changed is not None:
            idx = self.tts_quality_row.get_selected()
            quality = self._TTS_QUALITIES[idx] if 0 <= idx < len(self._TTS_QUALITIES) else "high"
            self.on_tts_quality_changed(quality)

    def _on_piper_dl_progress(self, model_name: str, fraction: float) -> None:
        """Callback from tts_service — runs on GLib main loop."""
        if fraction == 2.0:
            self._piper_dl_row.set_visible(False)
            self._piper_dl_bar.set_fraction(0.0)
            self._piper_dl_bar.set_text(None)
        elif fraction == -1.0:
            self._piper_dl_row.set_visible(False)
            self._piper_dl_bar.set_text(None)
        else:
            self._piper_dl_row.set_title(f"Piper: {model_name}")
            self._piper_dl_bar.set_fraction(max(0.0, min(1.0, fraction)))
            pct = int(fraction * 100)
            self._piper_dl_bar.set_text(f"{pct} %")
            self._piper_dl_row.set_visible(True)

    def _on_piper_dl_cancel(self, _btn: Gtk.Button) -> None:
        for model in tts_service.active_downloads():
            tts_service.cancel_download(model)

    def _on_log_app_toggled(self, row: Adw.SwitchRow, _param: Any) -> None:
        if self.on_log_app_enabled_changed is not None:
            self.on_log_app_enabled_changed(row.get_active())

    def _on_log_obd_toggled(self, row: Adw.SwitchRow, _param: Any) -> None:
        if self.on_log_obd_enabled_changed is not None:
            self.on_log_obd_enabled_changed(row.get_active())

    def _on_autodev_key_changed(self, row: Adw.EntryRow) -> None:
        if self.on_autodev_api_key_changed is not None:
            self.on_autodev_api_key_changed(row.get_text().strip())

    def _on_vd_api_key_changed(self, row: Adw.EntryRow) -> None:
        if self.on_vindecoder_api_key_changed is not None:
            self.on_vindecoder_api_key_changed(row.get_text().strip())

    def _on_vd_secret_changed(self, row: Adw.EntryRow) -> None:
        if self.on_vindecoder_secret_key_changed is not None:
            self.on_vindecoder_secret_key_changed(row.get_text().strip())

    # ── Update check ──────────────────────────────────────────────────────────

    def _on_check_update(self, _btn: Gtk.Button) -> None:
        self._update_btn.set_label(_translate(self.language, "settings.app.checking"))
        self._update_btn.set_sensitive(False)
        threading.Thread(target=self._do_check, daemon=True).start()

    def _do_check(self) -> None:
        info = updater.check_for_update()
        now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
        GLib.idle_add(self._on_check_done, info, now_iso)

    def _on_check_done(self, info: updater.UpdateInfo, now_iso: str) -> bool:
        # Persist timestamp
        if self.on_last_check_updated is not None:
            self.on_last_check_updated(now_iso)
        try:
            dt = datetime.fromisoformat(now_iso)
            check_str = _translate(self.language, "settings.app.last_check.prefix") + \
                        dt.strftime("%d.%m.%Y %H:%M")
        except ValueError:
            check_str = now_iso
        self._update_row.set_subtitle(
            f"v{updater.get_current_version()}  ·  {check_str}"
        )
        if info.available:
            ver = info.remote_version or "?"
            label = _translate(self.language, "settings.app.update_btn").format(version=ver)
            self._update_btn.set_label(label)
            self._update_btn.add_css_class("suggested-action")
            self._remote_version = info.remote_version
            self._update_btn.set_sensitive(True)
            self._update_btn.disconnect_by_func(self._on_check_update)
            self._update_btn.connect("clicked", self._on_apply_update)
        else:
            self._update_btn.set_label(_translate(self.language, "settings.app.no_update"))
            self._update_btn.set_sensitive(False)
        return False

    def _on_apply_update(self, _btn: Gtk.Button) -> None:
        self._update_btn.set_label(_translate(self.language, "settings.app.updating"))
        self._update_btn.set_sensitive(False)
        threading.Thread(target=self._do_apply, daemon=True).start()

    def _do_apply(self) -> None:
        ok = updater.apply_update()
        GLib.idle_add(self._on_apply_done, ok)

    def _on_apply_done(self, ok: bool) -> bool:
        if ok:
            self._update_btn.set_label(_translate(self.language, "settings.app.update_done"))
            # Reload version label
            new_ver = updater.get_current_version()
            subtitle = self._update_row.get_subtitle() or ""
            prefix = subtitle.split("·")[1].strip() if "·" in subtitle else ""
            self._update_row.set_subtitle(f"v{new_ver}  ·  {prefix}")
        else:
            self._update_btn.set_label(_translate(self.language, "settings.app.update_error"))
        self._update_btn.set_sensitive(False)
        return False

    # ── Bluetooth OBD ─────────────────────────────────────────────────────────

    def _on_bt_refresh_clicked(self, _btn: Gtk.Button) -> None:
        self._bt_scan_async()

    def _bt_scan_async(self) -> None:
        self._bt_expander.set_subtitle(_translate(self.language, "settings.bt_obd.scanning"))
        threading.Thread(target=self._bt_scan_thread, daemon=True).start()

    def _bt_scan_thread(self) -> None:
        devices = scan_bt_paired_devices()  # [(label, "bt:ADDR"), ...]
        GLib.idle_add(self._bt_scan_done, devices)

    def _bt_scan_done(self, devices: list[tuple[str, str]]) -> bool:
        if self._closing:
            return False
        for row in self._bt_device_rows:
            self._bt_expander.remove(row)
        self._bt_device_rows.clear()
        self._paired_addrs = {bt_port[3:].upper() for _, bt_port in devices}

        if not devices:
            self._bt_expander.set_subtitle(_translate(self.language, "settings.bt_obd.none_found"))
            return False

        count = len(devices)
        self._bt_expander.set_subtitle(
            _translate(self.language, "settings.bt_obd.found").format(n=count)
        )

        for label, bt_port in devices:
            addr = bt_port[3:]  # strip "bt:"
            row = Adw.ActionRow(title=label)
            row.set_activatable(False)

            connect_btn = Gtk.Button(label=_translate(self.language, "settings.bt_obd.connect"))
            connect_btn.set_valign(Gtk.Align.CENTER)
            connect_btn.add_css_class("suggested-action")
            connect_btn.connect("clicked", self._on_bt_connect_clicked, addr, row)
            row.add_suffix(connect_btn)

            self._bt_expander.add_row(row)
            self._bt_device_rows.append(row)

        return False

    def _on_bt_connect_clicked(self, btn: Gtk.Button, addr: str, row: Adw.ActionRow) -> None:
        btn.set_sensitive(False)
        spinner = Gtk.Spinner()
        spinner.start()
        row.add_suffix(spinner)
        row.set_subtitle(_translate(self.language, "settings.bt_obd.connecting"))
        threading.Thread(
            target=self._bt_bind_thread,
            args=(addr, btn, spinner, row),
            daemon=True,
        ).start()

    def _bt_bind_thread(
        self,
        addr: str,
        btn: Gtk.Button,
        spinner: Gtk.Spinner,
        row: Adw.ActionRow,
    ) -> None:
        dev, err = bind_bt_to_rfcomm(addr)
        GLib.idle_add(self._bt_bind_done, dev, err, addr, btn, spinner, row)

    def _bt_bind_done(
        self,
        dev: str | None,
        err: str,
        addr: str,
        btn: Gtk.Button,
        spinner: Gtk.Spinner,
        row: Adw.ActionRow,
    ) -> bool:
        spinner.stop()
        row.remove(spinner)
        if dev:
            row.set_subtitle(f"✓ {dev}")
            btn.set_label(dev)
            btn.remove_css_class("suggested-action")
            btn.add_css_class("success")
            bt_port = f"bt:{addr}"
            if self.on_obd_port_changed is not None:
                self.on_obd_port_changed(bt_port)
            self._refresh_dongle_dropdown(bt_port)
        else:
            # rfcomm bind failed — try direct RFCOMM socket as fallback
            row.set_subtitle(_translate(self.language, "settings.bt_obd.trying_direct"))
            btn.set_label(_translate(self.language, "settings.bt_obd.trying_direct"))
            spinner2 = Gtk.Spinner()
            spinner2.start()
            row.add_suffix(spinner2)
            threading.Thread(
                target=self._bt_direct_fallback_thread,
                args=(addr, btn, spinner2, row),
                daemon=True,
            ).start()
        return False

    def _bt_direct_fallback_thread(
        self,
        addr: str,
        btn: Gtk.Button,
        spinner: Gtk.Spinner,
        row: Adw.ActionRow,
    ) -> None:
        ok, err = probe_bt_rfcomm_socket(addr)
        GLib.idle_add(self._bt_direct_fallback_done, ok, addr, err, btn, spinner, row)

    def _bt_direct_fallback_done(
        self,
        ok: bool,
        addr: str,
        err: str,
        btn: Gtk.Button,
        spinner: Gtk.Spinner,
        row: Adw.ActionRow,
    ) -> bool:
        spinner.stop()
        row.remove(spinner)
        if ok:
            bt_port = f"bt:{addr}"
            row.set_subtitle(f"✓ {bt_port}")
            btn.set_label(bt_port)
            btn.remove_css_class("suggested-action")
            btn.add_css_class("success")
            if self.on_obd_port_changed is not None:
                self.on_obd_port_changed(bt_port)
            self._refresh_dongle_dropdown(bt_port)
        else:
            row.set_subtitle(f"✗ {err}")
            btn.set_label(_translate(self.language, "settings.bt_obd.connect"))
            btn.add_css_class("suggested-action")
            btn.set_sensitive(True)
        return False

    # ── Nearby BT scan ────────────────────────────────────────────────────────

    def _on_bt_nearby_scan_clicked(self, btn: Gtk.Button) -> None:
        btn.set_sensitive(False)
        self._bt_nearby_expander.set_subtitle(_translate(self.language, "settings.bt_obd.nearby.scanning"))
        threading.Thread(target=self._bt_nearby_scan_thread, daemon=True).start()

    def _bt_nearby_scan_thread(self) -> None:
        devices = scan_bt_nearby_devices(scan_seconds=6, known_addrs=self._paired_addrs)
        GLib.idle_add(self._bt_nearby_scan_done, devices)

    def _bt_nearby_scan_done(self, devices: list[tuple[str, str]]) -> bool:
        if self._closing:
            return False
        self._bt_nearby_scan_btn.set_sensitive(True)
        for row in self._bt_nearby_rows:
            self._bt_nearby_expander.remove(row)
        self._bt_nearby_rows.clear()

        if not devices:
            self._bt_nearby_expander.set_subtitle(_translate(self.language, "settings.bt_obd.nearby.none_found"))
            return False

        self._bt_nearby_expander.set_subtitle(
            _translate(self.language, "settings.bt_obd.found").format(n=len(devices))
        )
        for label, bt_port in devices:
            addr = bt_port[3:]  # strip "bt:"
            row = Adw.ActionRow(title=label)
            row.set_activatable(False)
            connect_btn = Gtk.Button(label=_translate(self.language, "settings.bt_obd.connect"))
            connect_btn.set_valign(Gtk.Align.CENTER)
            connect_btn.add_css_class("suggested-action")
            connect_btn.connect("clicked", self._on_bt_connect_clicked, addr, row)
            row.add_suffix(connect_btn)
            self._bt_nearby_expander.add_row(row)
            self._bt_nearby_rows.append(row)
        return False
