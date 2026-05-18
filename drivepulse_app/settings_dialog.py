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

from .common import SUPPORTED_LANGUAGES, _normalize_language, _translate
from .gauge import all_theme_options
from .obd_devices import scan_obd_devices
from . import updater


class DeviceItem(GObject.Object):
    __gtype_name__ = "DrivePulseDeviceItem"

    def __init__(self, label: str, port: str | None, is_present: bool = False, is_connected: bool = False) -> None:
        super().__init__()
        self._label = label
        self._port = port
        self._is_present = is_present
        self._is_connected = is_connected


class SettingsDialog(Adw.PreferencesDialog):
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
        self._remote_version: str | None = None
        self.set_title(_translate(self.language, "settings.title"))

        page = Adw.PreferencesPage(title=_translate(self.language, "settings.display"))
        group = Adw.PreferencesGroup(title=_translate(self.language, "settings.units"))

        self.unit_row = Adw.ComboRow(title=_translate(self.language, "settings.speed"))
        model = Gtk.StringList()
        model.append(_translate(self.language, "settings.metric"))
        model.append(_translate(self.language, "settings.imperial"))
        self.unit_row.set_model(model)
        self.unit_row.set_selected(0 if current_units == "metric" else 1)
        self.unit_row.connect("notify::selected", self._on_unit_selected)

        self.language_row = Adw.ComboRow(title=_translate(self.language, "settings.language"))
        language_model = Gtk.StringList()
        language_model.append(_translate(self.language, "settings.language.en"))
        language_model.append(_translate(self.language, "settings.language.de"))
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

        group.add(self.unit_row)
        group.add(self.language_row)
        group.add(self.theme_mode_row)
        group.add(self.gauge_theme_row)
        group.add(self.sidebar_side_row)
        group.add(self.force_webkit_map_row)
        group.add(self.mock_row)
        page.add(group)

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

        self.add(page)

        # ── App page ──────────────────────────────────────────────────────────
        app_page = Adw.PreferencesPage(
            title=_translate(self.language, "settings.page.app"),
            icon_name="software-update-available-symbolic",
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
        app_group.add(self._update_row)
        app_page.add(app_group)

        # OBD group (moved from display page)
        app_page.add(obd_group)

        self.add(app_page)

        # ── Dashcam page ──────────────────────────────────────────────────────
        from .dashcam_recorder import RESOLUTIONS, list_cameras  # lazy import

        dc_page = Adw.PreferencesPage(
            title=_translate(self.language, "settings.page.dashcam"),
            icon_name="camera-video-symbolic",
        )
        dc_group = Adw.PreferencesGroup(title=_translate(self.language, "dashcam.settings.title"))

        # Camera selector
        cameras = list_cameras() or [current_dashcam_camera]
        cam_model = Gtk.StringList.new(cameras)
        self._dc_cam_row = Adw.ComboRow(title=_translate(self.language, "dashcam.settings.camera"))
        self._dc_cam_row.set_model(cam_model)
        sel_cam = cameras.index(current_dashcam_camera) if current_dashcam_camera in cameras else 0
        self._dc_cam_row.set_selected(sel_cam)
        self._dc_cam_row.connect("notify::selected", self._on_dc_camera_changed)
        dc_group.add(self._dc_cam_row)

        # Resolution
        res_model = Gtk.StringList.new(RESOLUTIONS)
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
        self.add(dc_page)

    # ── Dashcam callbacks ─────────────────────────────────────────────────────

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
        self.on_language_changed(SUPPORTED_LANGUAGES[self.language_row.get_selected()])

    def _on_mock_changed(self, *_args: Any) -> None:
        if self.on_mock_mode_changed is not None:
            self.on_mock_mode_changed(self.mock_switch.get_active())

    def _on_dongle_selected(self, *_args: Any) -> None:
        if self.on_obd_port_changed is not None:
            idx = self.dongle_row.get_selected()
            port = self._obd_port_values[idx] if idx < len(self._obd_port_values) else None
            self.on_obd_port_changed(port)

    def _on_gauge_theme_selected(self, *_args: Any) -> None:
        if self.on_gauge_theme_changed is not None:
            idx = self.gauge_theme_row.get_selected()
            theme = self._theme_options[idx][0] if idx < len(self._theme_options) else "cockpit"
            self.on_gauge_theme_changed(theme)

    def _on_sidebar_side_selected(self, *_args: Any) -> None:
        if self.on_sidebar_side_changed is not None:
            side = "left" if self.sidebar_side_row.get_selected() == 0 else "right"
            self.on_sidebar_side_changed(side)

    def _on_theme_mode_selected(self, *_args: Any) -> None:
        if self.on_theme_mode_changed is not None:
            modes = ["auto", "dark", "light"]
            self.on_theme_mode_changed(modes[self.theme_mode_row.get_selected()])

    def _on_force_webkit_map_changed(self, *_args: Any) -> None:
        if self.on_force_webkit_map_changed is not None:
            self.on_force_webkit_map_changed(self.force_webkit_map_switch.get_active())

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
