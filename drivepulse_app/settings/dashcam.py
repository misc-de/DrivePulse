"""Dashcam settings callbacks for the settings dialog.

Extracted from ``settings_dialog.py``: folder pickers, camera entry,
camera-mode probing, OSD toggles, and the spinbutton handlers for segment
length / max segments / dim timeout. Composed onto ``SettingsDialog`` as a
mixin; relies on the ``_dc_*`` widget attributes that ``__init__`` wires up.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from gi.repository import Adw, GLib, Gtk

from drivepulse_app.common import _translate


class SettingsDashcamMixin:
    """Folder-picker rows, camera popover, resolution/fps combos, OSD toggles."""

    # Concrete SettingsDialog state surfaced to this mixin. See
    # project_mixin_typing.md.
    language: str
    _closing: bool
    _dc_cam_entry: Gtk.Entry
    _dc_cam_popover: Gtk.Popover
    _dc_cam_list_box: Gtk.ListBox
    _dc_res_row: Adw.ComboRow
    _dc_fps_row: Adw.ComboRow
    _dc_codec_ids: list[str]
    _dc_cam_modes: dict[str, list[int]]

    on_dashcam_camera_changed: Callable[[str], None] | None
    on_dashcam_resolution_changed: Callable[[str], None] | None
    on_dashcam_codec_changed: Callable[[str], None] | None
    on_dashcam_fps_changed: Callable[[int], None] | None
    on_dashcam_seg_minutes_changed: Callable[[int], None] | None
    on_dashcam_max_segments_changed: Callable[[int], None] | None
    on_dashcam_dim_timeout_changed: Callable[[int], None] | None
    on_dashcam_rolling_dir_changed: Callable[[str], None] | None
    on_dashcam_saved_dir_changed: Callable[[str], None] | None
    on_dashcam_gps_osd_changed: Callable[[bool], None] | None
    on_dashcam_speed_osd_changed: Callable[[bool], None] | None

    get_root: Callable[[], Any]

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
        from drivepulse_app.dashcam.recorder import list_cameras
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
        """Fill resolution + fps combos.

        The real v4l2-ctl probe (`query_camera_modes`) can take up to four
        seconds — running it inline blocks the settings dialog from rendering.
        So we paint the fallback list first and refresh once the subprocess
        returns from a background thread.
        """
        from drivepulse_app.dashcam.recorder import RESOLUTIONS
        # Synchronous fallback so the dialog opens immediately.
        self._dc_cam_modes = {}
        self._apply_resolution_list(RESOLUTIONS)
        # Real query off the main loop.
        def _worker() -> None:
            from drivepulse_app.dashcam.recorder import query_camera_modes
            modes = query_camera_modes(device)
            GLib.idle_add(self._on_camera_modes_ready, modes)
        threading.Thread(target=_worker, daemon=True).start()

    def _apply_resolution_list(self, resolutions: list[str]) -> None:
        res_model = Gtk.StringList()
        for r in resolutions:
            res_model.append(r)
        self._dc_res_row.set_model(res_model)
        idx = resolutions.index(self._dc_current_res) if self._dc_current_res in resolutions else 0
        self._dc_res_row.set_selected(idx)
        self._populate_fps_for_res(resolutions[idx] if resolutions else self._dc_current_res)

    def _on_camera_modes_ready(self, modes: dict[str, list[int]]) -> bool:
        if self._closing:
            return False
        self._dc_cam_modes = modes
        if modes:
            self._apply_resolution_list(list(modes.keys()))
        return False  # one-shot

    def _populate_fps_for_res(self, resolution: str) -> None:
        """Fill the FPS combo for the given resolution."""
        from drivepulse_app.dashcam.recorder import FPS_OPTIONS
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

    def _on_dc_codec_changed(self, row: Adw.ComboRow, _pspec: Any) -> None:
        idx = row.get_selected()
        if idx < 0 or idx >= len(self._dc_codec_ids):
            return
        codec = self._dc_codec_ids[idx]
        self._current_dashcam_codec = codec
        if self.on_dashcam_codec_changed:
            self.on_dashcam_codec_changed(codec)

    def _on_dc_seg_minutes_changed(self, spin: Gtk.SpinButton) -> None:
        if self.on_dashcam_seg_minutes_changed:
            self.on_dashcam_seg_minutes_changed(int(spin.get_value()))

    def _on_dc_max_segments_changed(self, spin: Gtk.SpinButton) -> None:
        if self.on_dashcam_max_segments_changed:
            self.on_dashcam_max_segments_changed(int(spin.get_value()))

    def _on_dc_dim_timeout_changed(self, spin: Gtk.SpinButton) -> None:
        if self.on_dashcam_dim_timeout_changed:
            self.on_dashcam_dim_timeout_changed(int(spin.get_value()))
