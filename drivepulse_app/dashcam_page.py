"""Dashcam page — loop recording with event save and lock-screen dimmer."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from .common import SOURCE_LANGUAGE, _normalize_language, _translate
from .dashcam_recorder import DashcamRecorder, list_cameras
from .diagnostics import get_logger

log = get_logger(__name__)

_DIM_DEFAULT_S = 30   # seconds until screen dims; 0 = off


class DashcamPage(Gtk.Box):
    __gtype_name__ = "DashcamPage"

    def __init__(self, language: str = SOURCE_LANGUAGE) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.language = _normalize_language(language)
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._recorder = DashcamRecorder(
            on_segment_start=lambda p: GLib.idle_add(self._on_segment_start, p),
            on_segment_done=lambda p:  GLib.idle_add(self._on_segment_done, p),
            on_error=lambda msg:       GLib.idle_add(self._show_error, msg),
        )

        self._tick_source:    int | None = None
        self._dim_source:     int | None = None   # countdown timer id
        self._dim_timeout_s:  int = _DIM_DEFAULT_S
        self._dim_remaining:  int = _DIM_DEFAULT_S
        self._lock_visible:   bool = False
        self._orientation_deg: int = 0

        self._build_ui()
        self._refresh_cameras()
        self._update_status_row()

    # ── Widget construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        self.append(overlay)

        # ── Base layer: normal UI ─────────────────────────────────────────────
        scroll = Gtk.ScrolledWindow()
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        overlay.set_child(scroll)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        root.set_margin_top(16)
        root.set_margin_bottom(16)
        root.set_margin_start(16)
        root.set_margin_end(16)
        scroll.set_child(root)

        root.append(self._build_indicator())
        root.append(self._build_actions())
        root.append(self._build_status_card())
        root.append(self._build_saved_group())

        # Activity detection on the normal UI
        for ctrl in (
            Gtk.EventControllerMotion(),
            Gtk.GestureClick(),
            Gtk.EventControllerKey(),
        ):
            ctrl.connect(
                "motion" if isinstance(ctrl, Gtk.EventControllerMotion)
                else "pressed" if isinstance(ctrl, Gtk.GestureClick)
                else "key-pressed",
                lambda *_: self._reset_dim_timer(),
            )
            scroll.add_controller(ctrl)

        # ── Overlay layer: lock / dim screen ─────────────────────────────────
        self._lock_overlay = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self._lock_overlay.set_hexpand(True)
        self._lock_overlay.set_vexpand(True)
        self._lock_overlay.set_halign(Gtk.Align.FILL)
        self._lock_overlay.set_valign(Gtk.Align.FILL)
        self._lock_overlay.set_visible(False)

        # Pure black background via CSS
        self._lock_overlay.add_css_class("dashcam-lock-bg")
        css = Gtk.CssProvider()
        css.load_from_data(b".dashcam-lock-bg { background-color: #000000; }")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # Big red save button in the centre
        save_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        save_inner.set_halign(Gtk.Align.CENTER)
        save_inner.set_valign(Gtk.Align.CENTER)

        lock_icon = Gtk.Image.new_from_icon_name("starred-symbolic")
        lock_icon.set_pixel_size(72)
        save_inner.append(lock_icon)

        self._lock_save_btn = Gtk.Button()
        self._lock_save_btn.set_child(save_inner)
        self._lock_save_btn.set_size_request(240, 120)
        self._lock_save_btn.add_css_class("destructive-action")
        self._lock_save_btn.add_css_class("pill")
        self._lock_save_btn.set_halign(Gtk.Align.CENTER)
        self._lock_save_btn.set_valign(Gtk.Align.CENTER)
        self._lock_save_btn.connect("clicked", self._on_lock_save)
        self._lock_overlay.append(self._lock_save_btn)
        self._update_lock_btn_label()

        # Tap anywhere else on the overlay → just wake up (no save)
        wake_gesture = Gtk.GestureClick()
        wake_gesture.connect("pressed", self._on_lock_tap)
        self._lock_overlay.add_controller(wake_gesture)

        overlay.add_overlay(self._lock_overlay)

    def _build_indicator(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_halign(Gtk.Align.CENTER)
        box.set_margin_top(8)

        self._rec_dot = Gtk.DrawingArea()
        self._rec_dot.set_size_request(64, 64)
        self._rec_dot.set_draw_func(self._draw_rec_dot, None)
        box.append(self._rec_dot)

        self._rec_label = Gtk.Label(label=_translate(self.language, "dashcam.status.idle"))
        self._rec_label.add_css_class("title-2")
        box.append(self._rec_label)

        self._elapsed_label = Gtk.Label(label="")
        self._elapsed_label.add_css_class("dim-label")
        box.append(self._elapsed_label)

        self._orientation_label = Gtk.Label(label="")
        self._orientation_label.add_css_class("dim-label")
        box.append(self._orientation_label)

        return box

    def _build_actions(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_halign(Gtk.Align.CENTER)

        self._toggle_btn = Gtk.Button()
        self._toggle_btn.set_size_request(160, 48)
        self._toggle_btn.add_css_class("suggested-action")
        self._toggle_btn.add_css_class("pill")
        self._toggle_btn.connect("clicked", self._on_toggle)
        self._update_toggle_btn()
        box.append(self._toggle_btn)

        save_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        save_inner.append(Gtk.Image.new_from_icon_name("starred-symbolic"))
        save_inner.append(Gtk.Label(label=_translate(self.language, "dashcam.btn.save")))
        self._save_btn = Gtk.Button()
        self._save_btn.set_child(save_inner)
        self._save_btn.set_size_request(160, 48)
        self._save_btn.add_css_class("destructive-action")
        self._save_btn.add_css_class("pill")
        self._save_btn.set_sensitive(False)
        self._save_btn.connect("clicked", self._on_save_event)
        box.append(self._save_btn)

        return box

    def _build_status_card(self) -> Gtk.Widget:
        frame = Gtk.Frame()
        frame.add_css_class("card")

        grid = Gtk.Grid()
        grid.set_row_spacing(6)
        grid.set_column_spacing(24)
        grid.set_margin_top(12)
        grid.set_margin_bottom(12)
        grid.set_margin_start(16)
        grid.set_margin_end(16)

        def _row(r: int, key: str, widget: Gtk.Widget) -> None:
            lbl = Gtk.Label(label=_translate(self.language, key))
            lbl.add_css_class("dim-label")
            lbl.set_halign(Gtk.Align.START)
            grid.attach(lbl, 0, r, 1, 1)
            widget.set_halign(Gtk.Align.END)
            widget.set_hexpand(True)
            grid.attach(widget, 1, r, 1, 1)

        self._seg_count_lbl  = Gtk.Label(label="0")
        self._seg_size_lbl   = Gtk.Label(label="0 MB")
        self._seg_total_lbl  = Gtk.Label(label="0")

        _row(0, "dashcam.status.segments",   self._seg_count_lbl)
        _row(1, "dashcam.status.disk",        self._seg_size_lbl)
        _row(2, "dashcam.status.saved_count", self._seg_total_lbl)

        frame.set_child(grid)
        return frame

    # ── Public setters (called by dashboard_settings via settings dialog) ────

    def set_camera(self, camera: str) -> None:
        self._recorder.camera = camera

    def set_resolution(self, resolution: str) -> None:
        self._recorder.resolution = resolution

    def set_segment_minutes(self, minutes: int) -> None:
        self._recorder.segment_minutes = minutes

    def set_max_segments(self, max_seg: int) -> None:
        self._recorder.max_segments = max_seg

    def set_dim_timeout(self, seconds: int) -> None:
        self._dim_timeout_s = seconds
        self._stop_dim_timer()
        if self._recorder.is_recording:
            self._reset_dim_timer()

    def _build_saved_group(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        hdr = Gtk.Label(label=_translate(self.language, "dashcam.saved.title"))
        hdr.add_css_class("title-4")
        hdr.set_halign(Gtk.Align.START)
        box.append(hdr)

        self._saved_list_box = Gtk.ListBox()
        self._saved_list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._saved_list_box.add_css_class("boxed-list")
        box.append(self._saved_list_box)

        self._saved_placeholder = Gtk.Label(label=_translate(self.language, "dashcam.saved.empty"))
        self._saved_placeholder.add_css_class("dim-label")
        self._saved_placeholder.set_margin_top(8)
        box.append(self._saved_placeholder)

        return box

    # ── Camera detection ──────────────────────────────────────────────────────

    def _refresh_cameras(self) -> None:
        cameras = list_cameras() or ["/dev/video0"]
        self._cam_row.set_model(Gtk.StringList.new(cameras))
        self._cam_row.set_selected(0)
        self._cam_row.connect("notify::selected", self._on_cam_changed)
        self._recorder.camera = cameras[0]

    # ── Orientation (called by dashboard_window) ──────────────────────────────

    def update_orientation(self, angle: int, is_landscape: bool) -> None:
        """Receive device orientation from the system sensor."""
        self._orientation_deg = angle
        self._recorder.rotation = angle
        icon = "⟷" if is_landscape else "↕"
        deg_txt = f"{angle}°" if angle else ""
        self._orientation_label.set_text(f"{icon} {deg_txt}".strip())

    # ── Dim / lock screen ─────────────────────────────────────────────────────

    def _reset_dim_timer(self) -> None:
        if self._lock_visible:
            self._dismiss_lock()
            return
        if self._dim_timeout_s <= 0 or not self._recorder.is_recording:
            return
        self._dim_remaining = self._dim_timeout_s
        if self._dim_source is None:
            self._dim_source = GLib.timeout_add(1000, self._dim_tick)

    def _stop_dim_timer(self) -> None:
        if self._dim_source is not None:
            GLib.source_remove(self._dim_source)
            self._dim_source = None

    def _dim_tick(self) -> bool:
        self._dim_remaining -= 1
        if self._dim_remaining <= 0:
            self._show_lock()
            self._dim_source = None
            return False
        return True

    def _show_lock(self) -> None:
        self._lock_visible = True
        self._lock_overlay.set_visible(True)

    def _dismiss_lock(self) -> None:
        self._lock_visible = False
        self._lock_overlay.set_visible(False)
        self._reset_dim_timer()

    def _on_lock_tap(self, _gesture: Any, _n: int, _x: float, _y: float) -> None:
        self._dismiss_lock()

    def _on_lock_save(self, _btn: Gtk.Button) -> None:
        self._do_save_event()
        self._dismiss_lock()

    def _update_lock_btn_label(self) -> None:
        lbl = Gtk.Label(label=_translate(self.language, "dashcam.btn.save_lock"))
        lbl.add_css_class("title-1")
        self._lock_save_btn.set_child(lbl)

    # ── Record toggle ─────────────────────────────────────────────────────────

    def _on_toggle(self, _btn: Gtk.Button) -> None:
        if self._recorder.is_recording:
            self._recorder.stop()
            self._stop_tick()
            self._stop_dim_timer()
            self._save_btn.set_sensitive(False)
        else:
            self._recorder.start()
            self._start_tick()
            self._reset_dim_timer()
            self._save_btn.set_sensitive(True)
        self._update_toggle_btn()
        self._rec_dot.queue_draw()

    def _on_save_event(self, _btn: Gtk.Button) -> None:
        self._do_save_event()

    def _do_save_event(self) -> None:
        saved = self._recorder.save_event()
        self._update_saved_list()
        self._update_status_row()
        if saved:
            toast = Adw.Toast.new(
                _translate(self.language, "dashcam.event.saved").format(n=len(saved))
            )
            root = self.get_root()
            if isinstance(root, Adw.ApplicationWindow):
                root.add_toast(toast)

    # ── Settings callbacks ────────────────────────────────────────────────────

    def _on_cam_changed(self, row: Adw.ComboRow, _pspec: Any) -> None:
        item = row.get_selected_item()
        if item:
            self._recorder.camera = item.get_string()

    def _on_res_changed(self, row: Adw.ComboRow, _pspec: Any) -> None:
        item = row.get_selected_item()
        if item:
            self._recorder.resolution = item.get_string()

    def _on_seg_len_changed(self, spin: Gtk.SpinButton) -> None:
        self._recorder.segment_minutes = int(spin.get_value())

    def _on_max_seg_changed(self, spin: Gtk.SpinButton) -> None:
        self._recorder.max_segments = int(spin.get_value())

    def _on_dim_timeout_changed(self, spin: Gtk.SpinButton) -> None:
        self._dim_timeout_s = int(spin.get_value())
        self._stop_dim_timer()
        if self._recorder.is_recording:
            self._reset_dim_timer()

    # ── Recorder callbacks ────────────────────────────────────────────────────

    def _on_segment_start(self, _path: Path) -> bool:
        self._rec_dot.queue_draw()
        return False

    def _on_segment_done(self, _path: Path) -> bool:
        self._update_status_row()
        return False

    def _show_error(self, msg: str) -> bool:
        self._rec_label.set_text(msg)
        self._update_toggle_btn()
        self._stop_tick()
        self._stop_dim_timer()
        return False

    # ── Tick timer (1 Hz) ────────────────────────────────────────────────────

    def _start_tick(self) -> None:
        if self._tick_source is None:
            self._tick_source = GLib.timeout_add(1000, self._tick)

    def _stop_tick(self) -> None:
        if self._tick_source is not None:
            GLib.source_remove(self._tick_source)
            self._tick_source = None
        self._elapsed_label.set_text("")

    def _tick(self) -> bool:
        if not self._recorder.is_recording:
            self._tick_source = None
            return False
        elapsed = self._recorder.segment_elapsed_seconds
        mm, ss  = divmod(int(elapsed), 60)
        self._elapsed_label.set_text(f"{mm:02d}:{ss:02d} / {self._recorder.segment_minutes:02d}:00")
        self._update_status_row()
        return True

    # ── Status helpers ────────────────────────────────────────────────────────

    def _update_toggle_btn(self) -> None:
        rec = self._recorder.is_recording
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        inner.append(Gtk.Image.new_from_icon_name(
            "media-playback-stop-symbolic" if rec else "media-record-symbolic"
        ))
        inner.append(Gtk.Label(label=_translate(
            self.language, "dashcam.btn.stop" if rec else "dashcam.btn.start"
        )))
        self._toggle_btn.set_child(inner)
        if rec:
            self._toggle_btn.remove_css_class("suggested-action")
            self._toggle_btn.add_css_class("destructive-action")
            self._rec_label.set_text(_translate(self.language, "dashcam.status.recording"))
        else:
            self._toggle_btn.remove_css_class("destructive-action")
            self._toggle_btn.add_css_class("suggested-action")
            self._rec_label.set_text(_translate(self.language, "dashcam.status.idle"))

    def _update_status_row(self) -> None:
        self._seg_count_lbl.set_text(str(len(self._recorder.segments)))
        self._seg_size_lbl.set_text(f"{self._recorder.rolling_size_mb:.1f} MB")
        self._seg_total_lbl.set_text(str(len(self._recorder.protected_clips)))

    def _update_saved_list(self) -> None:
        while (child := self._saved_list_box.get_first_child()) is not None:
            self._saved_list_box.remove(child)
        clips = self._recorder.protected_clips
        self._saved_placeholder.set_visible(not clips)
        for clip in reversed(clips):
            row = Adw.ActionRow()
            row.set_title(clip.name)
            size_mb = clip.stat().st_size / 1_048_576 if clip.exists() else 0
            row.set_subtitle(f"{size_mb:.1f} MB")
            del_btn = Gtk.Button(icon_name="user-trash-symbolic")
            del_btn.add_css_class("flat")
            del_btn.set_valign(Gtk.Align.CENTER)
            del_btn.connect("clicked", lambda _b, p=clip: self._delete_saved(p))
            row.add_suffix(del_btn)
            self._saved_list_box.append(row)

    def _delete_saved(self, path: Path) -> None:
        self._recorder.delete_protected(path)
        self._update_saved_list()
        self._update_status_row()

    # ── Cairo REC dot ────────────────────────────────────────────────────────

    def _draw_rec_dot(self, _da: Any, cr: Any, w: int, h: int, _d: Any) -> None:
        cx, cy = w / 2, h / 2
        r = min(w, h) / 2 - 4
        cr.set_source_rgb(0.85, 0.15, 0.15) if self._recorder.is_recording \
            else cr.set_source_rgb(0.35, 0.35, 0.35)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.3)
        cr.set_line_width(2)
        cr.arc(cx, cy, r + 2, 0, 2 * math.pi)
        cr.stroke()

    # ── Language ─────────────────────────────────────────────────────────────

    def set_language(self, language: str) -> None:
        self.language = _normalize_language(language)
        self._update_toggle_btn()
        self._update_lock_btn_label()
        self._update_status_row()
