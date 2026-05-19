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
from .obd_devices import scan_obd_devices
from . import tts_service, updater


class DeviceItem(GObject.Object):
    __gtype_name__ = "DrivePulseDeviceItem"

    def __init__(self, label: str, port: str | None, is_present: bool = False, is_connected: bool = False) -> None:
        super().__init__()
        self._label = label
        self._port = port
        self._is_present = is_present
        self._is_connected = is_connected


class SettingsDialog(Adw.Dialog):
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
        current_last_check: str | None = None,
        on_last_check_updated: Callable[[str], None] | None = None,
        current_dashcam_camera: str = "/dev/video0",
        on_dashcam_camera_changed: Callable[[str], None] | None = None,
        current_dashcam_resolution: str = "1280x720",
        on_dashcam_resolution_changed: Callable[[str], None] | None = None,
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
        current_log_app_enabled: bool = True,
        on_log_app_enabled_changed: Callable[[bool], None] | None = None,
        current_log_obd_enabled: bool = True,
        on_log_obd_enabled_changed: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__()
        self.language = _normalize_language(current_language)
        self.on_units_changed = on_units_changed
        self.on_language_changed = on_language_changed
        self.on_mock_mode_changed = on_mock_mode_changed
        self.on_obd_port_changed = on_obd_port_changed
        self.on_gauge_theme_changed = on_gauge_theme_changed
        self.on_sidebar_side_changed = on_sidebar_side_changed
        self.on_theme_mode_changed = on_theme_mode_changed
        self.on_force_webkit_map_changed = on_force_webkit_map_changed
        self.on_last_check_updated = on_last_check_updated
        self.on_dashcam_camera_changed = on_dashcam_camera_changed
        self.on_dashcam_resolution_changed = on_dashcam_resolution_changed
        self.on_dashcam_seg_minutes_changed = on_dashcam_seg_minutes_changed
        self.on_dashcam_max_segments_changed = on_dashcam_max_segments_changed
        self.on_dashcam_dim_timeout_changed = on_dashcam_dim_timeout_changed
        self.on_dashcam_rolling_dir_changed = on_dashcam_rolling_dir_changed
        self.on_dashcam_saved_dir_changed = on_dashcam_saved_dir_changed
        self._current_dashcam_rolling_dir = current_dashcam_rolling_dir
        self._current_dashcam_saved_dir = current_dashcam_saved_dir
        self.on_dashcam_gps_osd_changed = on_dashcam_gps_osd_changed
        self._current_dashcam_gps_osd = current_dashcam_gps_osd
        self.on_nav_position_changed = on_nav_position_changed
        self.on_rotation_mode_changed = on_rotation_mode_changed
        self.on_tts_enabled_changed = on_tts_enabled_changed
        self.on_tts_backend_changed = on_tts_backend_changed
        self.on_tts_language_changed = on_tts_language_changed
        self.on_tts_voice_changed = on_tts_voice_changed
        self.on_log_app_enabled_changed = on_log_app_enabled_changed
        self.on_log_obd_enabled_changed = on_log_obd_enabled_changed
        self._remote_version: str | None = None
        self.set_title(_translate(self.language, "settings.title"))
        self.set_content_width(380)

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

        dongle_store = Gio.ListStore(item_type=DeviceItem)
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

        # OBD group
        app_page.add(obd_group)

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

        tts_group = Adw.PreferencesGroup(title=_translate(self.language, "settings.tts"))
        tts_group.add(self.tts_enabled_row)
        tts_group.add(self.tts_backend_row)
        tts_group.add(self.tts_language_row)
        tts_group.add(self.tts_voice_row)
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

        # Camera selector
        cameras = list_cameras() or [current_dashcam_camera]
        cam_model = Gtk.StringList()
        for camera in cameras:
            cam_model.append(camera)
        self._dc_cam_row = Adw.ComboRow(title=_translate(self.language, "dashcam.settings.camera"))
        self._dc_cam_row.set_model(cam_model)
        sel_cam = cameras.index(current_dashcam_camera) if current_dashcam_camera in cameras else 0
        self._dc_cam_row.set_selected(sel_cam)
        self._dc_cam_row.connect("notify::selected", self._on_dc_camera_changed)
        dc_group.add(self._dc_cam_row)

        # Resolution
        res_model = Gtk.StringList()
        for resolution in RESOLUTIONS:
            res_model.append(resolution)
        self._dc_res_row = Adw.ComboRow(title=_translate(self.language, "dashcam.settings.resolution"))
        self._dc_res_row.set_model(res_model)
        sel_res = RESOLUTIONS.index(current_dashcam_resolution) if current_dashcam_resolution in RESOLUTIONS else 1
        self._dc_res_row.set_selected(sel_res)
        self._dc_res_row.connect("notify::selected", self._on_dc_resolution_changed)
        dc_group.add(self._dc_res_row)

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
        dc_page.add(gps_group)

        # ── Build ViewStack + ViewSwitcher header (icon-only, mobile-friendly) ─
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

        switcher = Adw.ViewSwitcher()
        switcher.set_stack(view_stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.NARROW)

        dlg_header = Adw.HeaderBar()
        dlg_header.set_title_widget(switcher)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(dlg_header)
        toolbar_view.set_content(view_stack)

        self.set_child(toolbar_view)

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

    def _on_dc_rolling_dir_chosen(self, path: str) -> None:
        if self.on_dashcam_rolling_dir_changed:
            self.on_dashcam_rolling_dir_changed(path)

    def _on_dc_saved_dir_chosen(self, path: str) -> None:
        if self.on_dashcam_saved_dir_changed:
            self.on_dashcam_saved_dir_changed(path)

    def _on_dc_camera_changed(self, row: Adw.ComboRow, _pspec: Any) -> None:
        item = row.get_selected_item()
        if item and self.on_dashcam_camera_changed:
            self.on_dashcam_camera_changed(item.get_string())

    def _on_dc_resolution_changed(self, row: Adw.ComboRow, _pspec: Any) -> None:
        item = row.get_selected_item()
        if item and self.on_dashcam_resolution_changed:
            self.on_dashcam_resolution_changed(item.get_string())

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

    def _on_dongle_selected(self, *_args: Any) -> None:
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
        if self.on_tts_backend_changed is not None:
            idx = self.tts_backend_row.get_selected()
            backend = self._TTS_BACKENDS[idx] if 0 <= idx < len(self._TTS_BACKENDS) else "espeak"
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

    def _on_log_app_toggled(self, row: Adw.SwitchRow, _param: Any) -> None:
        if self.on_log_app_enabled_changed is not None:
            self.on_log_app_enabled_changed(row.get_active())

    def _on_log_obd_toggled(self, row: Adw.SwitchRow, _param: Any) -> None:
        if self.on_log_obd_enabled_changed is not None:
            self.on_log_obd_enabled_changed(row.get_active())

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
