"""Settings dialog for DrivePulse."""
from __future__ import annotations

from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GObject, Gtk  # noqa: E402

from .common import SUPPORTED_LANGUAGES, _normalize_language, _translate
from .gauge import all_theme_options
from .obd_devices import scan_obd_devices


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
    ) -> None:
        super().__init__()
        self.language = _normalize_language(current_language)
        self.on_units_changed = on_units_changed
        self.on_language_changed = on_language_changed
        self.on_mock_mode_changed = on_mock_mode_changed
        self.on_obd_port_changed = on_obd_port_changed
        self.on_gauge_theme_changed = on_gauge_theme_changed
        self.on_sidebar_side_changed = on_sidebar_side_changed
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

        group.add(self.unit_row)
        group.add(self.language_row)
        group.add(self.gauge_theme_row)
        group.add(self.sidebar_side_row)
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

        page.add(obd_group)

        self.add(page)

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
