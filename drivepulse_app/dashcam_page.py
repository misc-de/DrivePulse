"""Dashcam page — fullscreen live preview + loop recording + lock-screen dimmer."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from .common import SOURCE_LANGUAGE, _normalize_language, _translate
from .dashcam_recorder import DashcamRecorder
from .diagnostics import get_logger

log = get_logger(__name__)

_DIM_DEFAULT_S = 30

# ── GStreamer availability ─────────────────────────────────────────────────────

_GST_OK = False
try:
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # type: ignore[attr-defined]
    Gst.init(None)
    _GST_OK = True
except Exception:
    Gst = None  # type: ignore[assignment]


class _CameraPreview:
    """
    GStreamer camera preview wired into a Gtk.Picture.

    Tries pipelines in order — gtk4paintablesink (GPU, no CPU copy) first,
    appsink (CPU frame-poll) as fallback — across multiple source elements
    so it works on V4L2, PipeWire (Furios/Halium) and libcamera devices.
    """

    _FPS_MS = 67   # appsink poll interval ≈15 fps

    # device rotation angle → videoflip method index
    _FLIP_MAP = {0: 0, 90: 1, 180: 2, 270: 3}

    def __init__(
        self,
        picture: Gtk.Picture,
        on_first_frame: "Callable[[], None] | None" = None,
        on_all_failed:  "Callable[[str], None] | None" = None,
    ) -> None:
        self._picture        = picture
        self._on_first_frame = on_first_frame
        self._on_all_failed  = on_all_failed
        self._pipeline       = None
        self._sink           = None        # only set for appsink mode
        self._timer: int | None = None
        self._camera    = "/dev/video0"
        self._flip      = 0
        self._got_frame = False
        self._attempts: list[tuple[str, bool]] = []   # (desc, is_paintable)

    def set_camera(self, device: str) -> None:
        was_running = self._pipeline is not None
        if was_running:
            self.stop()
        self._camera = device
        if was_running:
            self.start()

    def set_rotation(self, angle: int) -> None:
        self._flip = self._FLIP_MAP.get(angle % 360, 0)
        if self._pipeline is not None:
            self.stop()
            self.start()

    def start(self) -> None:
        if not _GST_OK or self._pipeline is not None:
            return
        self._got_frame = False
        self._attempts  = self._build_attempts()
        log.debug("Camera preview: %d pipeline(s) to try", len(self._attempts))
        self._try_next()

    def _build_attempts(self) -> "list[tuple[str, bool]]":
        cam  = self._camera
        flip = f"videoflip method={self._flip} ! " if self._flip else ""
        # Sources in priority order: PipeWire (Furios/Halium) → libcamera → V4L2 → auto
        sources = [
            "pipewiresrc",
            "libcamerasrc",
            f"v4l2src device={cam}",
            "autovideosrc",
        ]
        out: list[tuple[str, bool]] = []
        for src in sources:
            # gtk4paintablesink: GPU-native GTK4 rendering, no CPU copy
            out.append((
                f"{src} ! videoconvert ! {flip}"
                f"gtk4paintablesink name=sink sync=false",
                True,
            ))
            # appsink: CPU frame-copy fallback
            out.append((
                f"{src} ! videoconvert ! video/x-raw,format=RGB ! {flip}"
                f"appsink name=sink max-buffers=1 drop=true sync=false",
                False,
            ))
        return out

    def _try_next(self) -> None:
        if not self._attempts:
            msg = "Keine Kameraquelle gefunden"
            log.warning("Camera preview: all pipelines exhausted")
            if self._on_all_failed:
                self._on_all_failed(msg)
            return

        desc, is_paintable = self._attempts.pop(0)
        log.debug("Trying pipeline: %s", desc)
        try:
            pipeline = Gst.parse_launch(desc)
        except Exception as exc:
            log.debug("parse_launch failed: %s", exc)
            GLib.idle_add(self._try_next)
            return

        sink_el = pipeline.get_by_name("sink")

        if is_paintable:
            try:
                paintable = sink_el.get_property("paintable")
                self._picture.set_paintable(paintable)
                paintable.connect("invalidate-contents", self._on_paintable_updated)
            except Exception as exc:
                log.debug("gtk4paintablesink property error: %s", exc)
                pipeline.set_state(Gst.State.NULL)
                GLib.idle_add(self._try_next)
                return
        else:
            self._sink  = sink_el
            self._timer = GLib.timeout_add(self._FPS_MS, self._pull)

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)
        pipeline.set_state(Gst.State.PLAYING)
        self._pipeline = pipeline
        log.info("Camera preview running: %s", desc.split("!")[0].strip())

    def _on_paintable_updated(self, _paintable: Any) -> None:
        if not self._got_frame:
            self._got_frame = True
            if self._on_first_frame:
                GLib.idle_add(self._on_first_frame)

    def _on_bus_error(self, _bus: Any, msg: Any) -> None:
        err, _debug = msg.parse_error()
        log.debug("Pipeline error: %s — trying next", err)
        self._teardown_pipeline()
        GLib.idle_add(self._try_next)

    def _teardown_pipeline(self) -> None:
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        self._sink = None

    def stop(self) -> None:
        self._teardown_pipeline()

    def _pull(self) -> bool:
        if self._sink is None:
            return False
        try:
            sample = self._sink.emit("try-pull-sample", 0)
            if sample is None:
                return True
            buf  = sample.get_buffer()
            caps = sample.get_caps()
            st   = caps.get_structure(0)
            w    = st.get_value("width")
            h    = st.get_value("height")
            ok, mi = buf.map(Gst.MapFlags.READ)
            if ok:
                raw    = bytes(mi.data)
                gbytes = GLib.Bytes.new(raw)
                tex    = Gdk.MemoryTexture.new(
                    w, h, Gdk.MemoryFormat.R8G8B8, gbytes, w * 3
                )
                self._picture.set_paintable(tex)
                buf.unmap(mi)
                if not self._got_frame:
                    self._got_frame = True
                    if self._on_first_frame:
                        GLib.idle_add(self._on_first_frame)
        except Exception as exc:
            log.debug("Frame pull error: %s", exc)
        return True


# ── Page widget ───────────────────────────────────────────────────────────────

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

        self._tick_source:   int | None = None
        self._dim_source:    int | None = None
        self._dim_timeout_s: int = _DIM_DEFAULT_S
        self._dim_remaining: int = _DIM_DEFAULT_S
        self._lock_visible:  bool = False

        self._build_ui()
        self._update_status()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Inject CSS once
        css = Gtk.CssProvider()
        css.load_from_data(
            b".dc-bottom { background: rgba(0,0,0,0.70); padding: 8px 12px 16px 12px; }"
            b".dc-lock-bg { background: #000000; }"
            b".dc-status  { color: rgba(255,255,255,0.80); font-size: 0.85em; }"
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # Outer overlay wraps everything so the lock screen can cover both
        # the camera area AND the bottom bar.
        outer = Gtk.Overlay()
        outer.set_hexpand(True)
        outer.set_vexpand(True)
        self.append(outer)

        # Inner ToolbarView: camera = content, controls = bottom_bar.
        # Adw.ToolbarView always places the bottom_bar at the physical bottom
        # regardless of portrait/landscape window shape.
        inner_tv = Adw.ToolbarView()
        inner_tv.set_hexpand(True)
        inner_tv.set_vexpand(True)
        outer.set_child(inner_tv)

        # ── Camera area (ToolbarView content) ─────────────────────────────────
        cam_overlay = Gtk.Overlay()
        cam_overlay.set_hexpand(True)
        cam_overlay.set_vexpand(True)
        inner_tv.set_content(cam_overlay)

        self._preview_pic = Gtk.Picture()
        self._preview_pic.set_hexpand(True)
        self._preview_pic.set_vexpand(True)
        self._preview_pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        cam_overlay.set_child(self._preview_pic)

        # "No camera" icon — hidden on first frame
        self._no_cam_icon = Gtk.Image.new_from_icon_name("camera-video-symbolic")
        self._no_cam_icon.set_pixel_size(64)
        self._no_cam_icon.add_css_class("dim-label")
        self._no_cam_icon.set_halign(Gtk.Align.CENTER)
        self._no_cam_icon.set_valign(Gtk.Align.CENTER)
        cam_overlay.add_overlay(self._no_cam_icon)

        # REC indicator — top-left of camera area
        self._rec_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._rec_bar.set_halign(Gtk.Align.START)
        self._rec_bar.set_valign(Gtk.Align.START)
        self._rec_bar.set_margin_top(12)
        self._rec_bar.set_margin_start(12)
        self._rec_bar.set_visible(False)
        self._rec_dot = Gtk.DrawingArea()
        self._rec_dot.set_size_request(16, 16)
        self._rec_dot.set_draw_func(self._draw_rec_dot, None)
        self._rec_bar.append(self._rec_dot)
        self._rec_lbl = Gtk.Label(label="REC")
        self._rec_lbl.add_css_class("dc-status")
        self._rec_bar.append(self._rec_lbl)
        cam_overlay.add_overlay(self._rec_bar)

        # Activity detection (resets dim timer) on the camera area
        for ctrl_cls, sig in (
            (Gtk.EventControllerMotion, "motion"),
            (Gtk.GestureClick,          "pressed"),
        ):
            ctrl = ctrl_cls()
            ctrl.connect(sig, lambda *_: self._reset_dim_timer())
            cam_overlay.add_controller(ctrl)

        # ── Controls (ToolbarView bottom_bar) ─────────────────────────────────
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        bottom.add_css_class("dc-bottom")
        bottom.set_hexpand(True)
        inner_tv.add_bottom_bar(bottom)

        self._status_lbl = Gtk.Label(label="")
        self._status_lbl.add_css_class("dc-status")
        self._status_lbl.set_halign(Gtk.Align.CENTER)
        bottom.append(self._status_lbl)

        self._elapsed_lbl = Gtk.Label(label="")
        self._elapsed_lbl.add_css_class("dc-status")
        self._elapsed_lbl.set_halign(Gtk.Align.CENTER)
        self._elapsed_lbl.set_visible(False)
        bottom.append(self._elapsed_lbl)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        btn_row.set_halign(Gtk.Align.CENTER)

        self._toggle_btn = Gtk.Button()
        self._toggle_btn.set_size_request(160, 48)
        self._toggle_btn.add_css_class("suggested-action")
        self._toggle_btn.add_css_class("pill")
        self._toggle_btn.connect("clicked", self._on_toggle)
        self._update_toggle_btn()
        btn_row.append(self._toggle_btn)

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
        btn_row.append(self._save_btn)

        clips_btn = Gtk.MenuButton()
        clips_btn.set_icon_name("view-list-symbolic")
        clips_btn.add_css_class("flat")
        clips_btn.set_valign(Gtk.Align.CENTER)
        clips_btn.set_tooltip_text(_translate(self.language, "dashcam.saved.title"))
        self._clips_popover = self._build_clips_popover()
        clips_btn.set_popover(self._clips_popover)
        self._clips_popover.connect("show", lambda _: self._update_saved_list())
        btn_row.append(clips_btn)

        bottom.append(btn_row)

        # ── Lock / dim screen — covers entire outer overlay ───────────────────
        self._lock_overlay = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._lock_overlay.add_css_class("dc-lock-bg")
        self._lock_overlay.set_hexpand(True)
        self._lock_overlay.set_vexpand(True)
        self._lock_overlay.set_halign(Gtk.Align.FILL)
        self._lock_overlay.set_valign(Gtk.Align.FILL)
        self._lock_overlay.set_visible(False)

        lock_btn_lbl = Gtk.Label()
        lock_btn_lbl.add_css_class("title-1")
        self._lock_btn_lbl = lock_btn_lbl
        self._update_lock_btn_label()

        self._lock_save_btn = Gtk.Button()
        self._lock_save_btn.set_child(lock_btn_lbl)
        self._lock_save_btn.set_size_request(240, 120)
        self._lock_save_btn.add_css_class("destructive-action")
        self._lock_save_btn.add_css_class("pill")
        self._lock_save_btn.set_halign(Gtk.Align.CENTER)
        self._lock_save_btn.set_valign(Gtk.Align.CENTER)
        self._lock_save_btn.set_vexpand(True)
        self._lock_save_btn.connect("clicked", self._on_lock_save)
        self._lock_overlay.append(self._lock_save_btn)

        wake = Gtk.GestureClick()
        wake.connect("pressed", self._on_lock_tap)
        self._lock_overlay.add_controller(wake)
        outer.add_overlay(self._lock_overlay)

        # ── GStreamer preview ─────────────────────────────────────────────────
        self._preview = _CameraPreview(
            self._preview_pic,
            on_first_frame=self._on_first_frame,
            on_all_failed=self._on_preview_failed,
        )
        self._preview.start()

    def _build_clips_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        pop.set_size_request(300, 400)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_size_request(280, 360)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        self._saved_list_box = Gtk.ListBox()
        self._saved_list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._saved_list_box.add_css_class("boxed-list")
        self._saved_placeholder = Gtk.Label(
            label=_translate(self.language, "dashcam.saved.empty")
        )
        self._saved_placeholder.add_css_class("dim-label")
        box.append(self._saved_list_box)
        box.append(self._saved_placeholder)
        scroll.set_child(box)
        pop.set_child(scroll)
        return pop

    def _on_first_frame(self) -> None:
        self._no_cam_icon.set_visible(False)

    def _on_preview_failed(self, msg: str) -> None:
        self._status_lbl.set_text(msg)

    # ── Public setters (called from dashboard_settings) ───────────────────────

    def set_camera(self, camera: str) -> None:
        self._recorder.camera = camera
        self._preview.set_camera(camera)

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

    def set_rolling_dir(self, path: str) -> None:
        if path:
            self._recorder.rolling_dir = Path(path)

    def set_saved_dir(self, path: str) -> None:
        if path:
            self._recorder.protected_dir = Path(path)

    # ── Orientation (from dashboard_window orientation sensor) ────────────────

    def update_orientation(self, angle: int, is_landscape: bool) -> None:
        self._recorder.rotation = angle
        self._preview.set_rotation(angle)

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

    def _on_lock_tap(self, _g: Any, _n: int, _x: float, _y: float) -> None:
        self._dismiss_lock()

    def _on_lock_save(self, _btn: Gtk.Button) -> None:
        self._do_save_event()
        self._dismiss_lock()

    def _update_lock_btn_label(self) -> None:
        self._lock_btn_lbl.set_text(_translate(self.language, "dashcam.btn.save_lock"))

    # ── Record toggle ─────────────────────────────────────────────────────────

    def _on_toggle(self, _btn: Gtk.Button) -> None:
        if self._recorder.is_recording:
            self._recorder.stop()
            self._stop_tick()
            self._stop_dim_timer()
            self._save_btn.set_sensitive(False)
            self._rec_bar.set_visible(False)
            self._elapsed_lbl.set_visible(False)
        else:
            self._recorder.start()
            self._start_tick()
            self._reset_dim_timer()
            self._save_btn.set_sensitive(True)
            self._rec_bar.set_visible(True)
        self._update_toggle_btn()
        self._update_status()

    def _on_save_event(self, _btn: Gtk.Button) -> None:
        self._do_save_event()

    def _do_save_event(self) -> None:
        saved = self._recorder.save_event()
        self._update_status()
        if saved:
            toast = Adw.Toast.new(
                _translate(self.language, "dashcam.event.saved").format(n=len(saved))
            )
            root = self.get_root()
            if isinstance(root, Adw.ApplicationWindow):
                root.add_toast(toast)

    # ── Recorder callbacks ────────────────────────────────────────────────────

    def _on_segment_start(self, _path: Path) -> bool:
        self._rec_dot.queue_draw()
        return False

    def _on_segment_done(self, _path: Path) -> bool:
        self._update_status()
        return False

    def _show_error(self, msg: str) -> bool:
        self._status_lbl.set_text(msg)
        self._stop_tick()
        self._stop_dim_timer()
        self._rec_bar.set_visible(False)
        self._update_toggle_btn()
        return False

    # ── Tick timer ────────────────────────────────────────────────────────────

    def _start_tick(self) -> None:
        if self._tick_source is None:
            self._tick_source = GLib.timeout_add(1000, self._tick)

    def _stop_tick(self) -> None:
        if self._tick_source is not None:
            GLib.source_remove(self._tick_source)
            self._tick_source = None
        self._elapsed_lbl.set_text("")
        self._elapsed_lbl.set_visible(False)

    def _tick(self) -> bool:
        if not self._recorder.is_recording:
            self._tick_source = None
            return False
        elapsed = self._recorder.segment_elapsed_seconds
        mm, ss  = divmod(int(elapsed), 60)
        self._elapsed_lbl.set_text(
            f"{mm:02d}:{ss:02d} / {self._recorder.segment_minutes:02d}:00"
        )
        self._elapsed_lbl.set_visible(True)
        self._update_status()
        return True

    # ── Status ────────────────────────────────────────────────────────────────

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
        else:
            self._toggle_btn.remove_css_class("destructive-action")
            self._toggle_btn.add_css_class("suggested-action")

    def _update_status(self) -> None:
        segs  = len(self._recorder.segments)
        mb    = self._recorder.rolling_size_mb
        saved = len(self._recorder.protected_clips)
        t = _translate
        parts = [
            f"{segs} {t(self.language, 'dashcam.status.segments')}",
            f"{mb:.1f} MB",
            f"{saved} {t(self.language, 'dashcam.status.saved_count')}",
        ]
        self._status_lbl.set_text("  ·  ".join(parts))

    def _update_saved_list(self) -> None:
        while (c := self._saved_list_box.get_first_child()) is not None:
            self._saved_list_box.remove(c)
        clips = self._recorder.protected_clips
        self._saved_placeholder.set_visible(not clips)
        for clip in reversed(clips):
            row = Adw.ActionRow()
            row.set_title(clip.name)
            mb = clip.stat().st_size / 1_048_576 if clip.exists() else 0
            row.set_subtitle(f"{mb:.1f} MB")
            del_btn = Gtk.Button(icon_name="user-trash-symbolic")
            del_btn.add_css_class("flat")
            del_btn.set_valign(Gtk.Align.CENTER)
            del_btn.connect("clicked", lambda _b, p=clip: self._delete_saved(p))
            row.add_suffix(del_btn)
            self._saved_list_box.append(row)

    def _delete_saved(self, path: Path) -> None:
        self._recorder.delete_protected(path)
        self._update_saved_list()
        self._update_status()

    # ── Cairo REC dot ─────────────────────────────────────────────────────────

    def _draw_rec_dot(self, _da: Any, cr: Any, w: int, h: int, _d: Any) -> None:
        cx, cy, r = w / 2, h / 2, min(w, h) / 2 - 1
        cr.set_source_rgb(0.9, 0.1, 0.1)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.fill()

    # ── Language ──────────────────────────────────────────────────────────────

    def set_language(self, language: str) -> None:
        self.language = _normalize_language(language)
        self._update_toggle_btn()
        self._update_lock_btn_label()
        self._update_status()
