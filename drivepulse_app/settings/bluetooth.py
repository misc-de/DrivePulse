"""Bluetooth-OBD scan/pair handlers for the settings dialog.

Extracted from ``settings_dialog.py`` to give the BT plumbing its own
self-contained module. All methods here run as a mixin on ``SettingsDialog``
and rely on ``self._bt_expander``/``self._bt_nearby_expander`` plus the
widget attributes wired up in ``SettingsDialog.__init__``.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from gi.repository import Adw, GLib, Gtk

from drivepulse_app.common import _translate
from drivepulse_app.obd.devices import (
    bind_bt_to_rfcomm,
    pair_bt_device,
    probe_bt_rfcomm_socket,
    scan_bt_nearby_devices,
    scan_bt_paired_devices,
)

if TYPE_CHECKING:
    from drivepulse_app.settings.dialog import _BtExpander


class SettingsBluetoothMixin:
    """Paired-device list + nearby BT scan + connect/bind flows."""

    # Concrete SettingsDialog state surfaced to this mixin. See
    # project_mixin_typing.md.
    language: str
    _closing: bool
    _bt_expander: _BtExpander
    _bt_nearby_expander: _BtExpander
    _bt_device_rows: list[Adw.ActionRow]
    _bt_nearby_rows: list[Adw.ActionRow]
    _bt_nearby_scan_btn: Gtk.Button
    on_obd_port_changed: Callable[[str | None], None] | None
    _refresh_dongle_dropdown: Callable[[str | None], None]

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

    def _on_bt_connect_clicked(
        self,
        btn: Gtk.Button,
        addr: str,
        row: Adw.ActionRow,
        pair_first: bool = False,
    ) -> None:
        btn.set_sensitive(False)
        spinner = Gtk.Spinner()
        spinner.start()
        row.add_suffix(spinner)
        row.set_subtitle(_translate(self.language, "settings.bt_obd.connecting"))
        threading.Thread(
            target=self._bt_bind_thread,
            args=(addr, btn, spinner, row, pair_first),
            daemon=True,
        ).start()

    def _bt_bind_thread(
        self,
        addr: str,
        btn: Gtk.Button,
        spinner: Gtk.Spinner,
        row: Adw.ActionRow,
        pair_first: bool = False,
    ) -> None:
        # Devices picked from the "nearby" scan are not bonded yet. Pair + trust
        # them via BlueZ before rfcomm bind, so the user never has to leave the
        # app for the OS Bluetooth panel.
        if pair_first:
            GLib.idle_add(row.set_subtitle, _translate(self.language, "settings.bt_obd.pairing"))
            ok, err = pair_bt_device(addr)
            if not ok:
                GLib.idle_add(self._bt_pair_failed, err, btn, spinner, row)
                return
        dev, err = bind_bt_to_rfcomm(addr)
        GLib.idle_add(self._bt_bind_done, dev, err, addr, btn, spinner, row)

    def _bt_pair_failed(
        self,
        err: str,
        btn: Gtk.Button,
        spinner: Gtk.Spinner,
        row: Adw.ActionRow,
    ) -> bool:
        spinner.stop()
        row.remove(spinner)
        row.set_subtitle(f"✗ {_translate(self.language, 'settings.bt_obd.pair_failed')}: {err}")
        btn.set_label(_translate(self.language, "settings.bt_obd.connect"))
        btn.add_css_class("suggested-action")
        btn.set_sensitive(True)
        return False

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
            # The actual OBD link is addressed as bt:ADDR (the reader picks the
            # working transport — rfcomm bind or the direct socket bridge). Show
            # that, not the raw /dev/rfcommN node, which is just the bind result
            # and often isn't the transport the reader ends up using.
            bt_port = f"bt:{addr}"
            row.set_subtitle(f"✓ {bt_port}")
            btn.set_label(bt_port)
            btn.remove_css_class("suggested-action")
            btn.add_css_class("success")
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
            connect_btn.connect("clicked", self._on_bt_connect_clicked, addr, row, True)
            row.add_suffix(connect_btn)
            self._bt_nearby_expander.add_row(row)
            self._bt_nearby_rows.append(row)
        return False
