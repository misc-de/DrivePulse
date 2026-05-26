"""App-update flow in the settings dialog: check, download, restart prompt."""
from __future__ import annotations

import os
import sys
import threading
from datetime import datetime

from gi.repository import Adw, GLib, Gtk

from drivepulse_app import updater
from drivepulse_app.common import _translate


class SettingsUpdatesMixin:
    def _on_check_update(self, _btn: Gtk.Button) -> None:
        self._cancel_no_update_reset()
        self._update_btn.set_label(_translate(self.language, "settings.app.checking"))
        self._update_btn.set_sensitive(False)
        threading.Thread(target=self._do_check, daemon=True).start()

    def _cancel_no_update_reset(self) -> None:
        src = getattr(self, "_no_update_reset_src", 0)
        if src:
            GLib.source_remove(src)
            self._no_update_reset_src = 0

    def _reset_check_btn_to_idle(self) -> bool:
        self._no_update_reset_src = 0
        self._update_btn.set_label(_translate(self.language, "settings.app.check_btn"))
        self._update_btn.set_sensitive(True)
        return False

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
            self._cancel_no_update_reset()
            self._no_update_reset_src = GLib.timeout_add_seconds(
                10, self._reset_check_btn_to_idle
            )
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
            new_ver = updater.get_current_version()
            subtitle = self._update_row.get_subtitle() or ""
            prefix = subtitle.split("·")[1].strip() if "·" in subtitle else ""
            self._update_row.set_subtitle(f"v{new_ver}  ·  {prefix}")
            self._update_btn.set_label(_translate(self.language, "settings.app.restart_required"))
            self._update_btn.set_sensitive(True)
            try:
                self._update_btn.disconnect_by_func(self._on_apply_update)
            except TypeError:
                pass
            self._update_btn.connect("clicked", self._show_restart_dialog)
            self._show_restart_dialog(None)
        else:
            self._update_btn.set_label(_translate(self.language, "settings.app.update_error"))
            self._update_btn.set_sensitive(False)
        return False

    def _show_restart_dialog(self, _btn) -> None:
        dialog = Adw.AlertDialog(
            heading=_translate(self.language, "settings.app.restart_dialog.title"),
            body=_translate(self.language, "settings.app.restart_dialog.body"),
        )
        dialog.add_response("no", _translate(self.language, "settings.app.restart_dialog.no"))
        dialog.add_response("yes", _translate(self.language, "settings.app.restart_dialog.yes"))
        dialog.set_response_appearance("yes", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("yes")
        dialog.set_close_response("no")
        dialog.connect("response", self._on_restart_response)
        dialog.present(self.get_root())

    def _on_restart_response(self, _dialog, response: str) -> None:
        if response == "yes":
            os.execv(sys.executable, [sys.executable, *sys.argv])
