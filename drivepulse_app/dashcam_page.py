"""Dashcam page — fullscreen live preview + loop recording + lock-screen dimmer."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk  # noqa: E402

from .common import SOURCE_LANGUAGE, _normalize_language, _translate
from .dashcam_recorder import DashcamRecorder
from .diagnostics import get_logger
from .rotated_container import RotatedContainer

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
        self._camera       = "/dev/video0"
        self._got_frame    = False
        self._attempts: list[tuple[str, bool]] = []   # (desc, is_paintable)

    def set_camera(self, device: str) -> None:
        was_running = self._pipeline is not None
        if was_running:
            self.stop()
        self._camera = device
        if was_running:
            self.start()

    def start(self) -> None:
        if not _GST_OK or self._pipeline is not None:
            return
        self._got_frame = False
        self._attempts  = self._build_attempts()
        log.debug("Camera preview: %d pipeline(s) to try", len(self._attempts))
        self._try_next()

    def _build_attempts(self) -> "list[tuple[str, bool]]":
        cam = self._camera
        # Sources in priority order: PipeWire (Furios/Halium) → libcamera → V4L2 → auto
        sources = [
            "pipewiresrc",
            "libcamerasrc",
            f"v4l2src device={cam}",
            "autovideosrc",
        ]
        out: list[tuple[str, bool]] = []
        for src in sources:
            # videoflip method=0 forces identity — strips any upstream orientation
            # metadata that pipewiresrc/libcamerasrc may inject on mobile devices.
            # queue leaky=downstream drops stale frames instead of buffering them.
            # gtk4paintablesink: GPU-native GTK4 rendering, no CPU copy
            out.append((
                f"{src} ! videoconvert ! videoflip method=0 "
                f"! queue max-size-buffers=2 leaky=downstream "
                f"! gtk4paintablesink name=sink sync=false",
                True,
            ))
            # appsink: CPU frame-copy fallback
            out.append((
                f"{src} ! videoconvert ! videoflip method=0 ! video/x-raw,format=RGB "
                f"! queue max-size-buffers=1 leaky=downstream "
                f"! appsink name=sink max-buffers=1 drop=true sync=false",
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
            bus = self._pipeline.get_bus()
            bus.remove_signal_watch()
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        self._sink = None
        self._picture.set_paintable(None)

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
                gbytes = GLib.Bytes.new(mi.data)
                buf.unmap(mi)
                del sample, buf, caps, st, mi
                tex = Gdk.MemoryTexture.new(
                    w, h, Gdk.MemoryFormat.R8G8B8, gbytes, w * 3
                )
                del gbytes
                self._picture.set_paintable(tex)
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
    _css_loaded: bool = False

    def __init__(self, language: str = SOURCE_LANGUAGE) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.language = _normalize_language(language)
        self.add_css_class("dp-dashcam-page")
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._recorder = DashcamRecorder(
            on_segment_start=lambda p: GLib.idle_add(self._on_segment_start, p),
            on_segment_done=lambda p:  GLib.idle_add(self._on_segment_done, p),
            on_error=lambda msg:       GLib.idle_add(self._show_error, msg),
        )
        self._recorder.on_preview_ready = self._on_preview_ready

        # Called on the GTK main thread whenever recording starts or stops.
        self.on_recording_changed: "Callable[[bool], None] | None" = None

        self._tick_source:   int | None = None
        self._dim_source:    int | None = None
        self._rec_dot_on:    bool = True
        self._dim_timeout_s: int = _DIM_DEFAULT_S
        self._dim_remaining: int = _DIM_DEFAULT_S
        self._lock_visible:  bool = False
        self._is_landscape:  bool = False

        # widget lists kept in sync across portrait/landscape layouts
        self._toggle_btns: list[Gtk.Button] = []
        self._save_btns:   list[Gtk.Button] = []
        self._clips_btns:  list[Gtk.MenuButton] = []

        self._build_ui()
        self._update_status()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        if not DashcamPage._css_loaded:
            DashcamPage._css_loaded = True
            css = Gtk.CssProvider()
            css.load_from_data(
                # Translucent gray bar — drawn ON TOP of the video at the bottom,
                # never to the side regardless of portrait/landscape orientation.
                b".dp-dashcam-page{background:#000000;color:#ffffff;}"
                b".dc-black-bg{background:#000000;color:#ffffff;}"
                b".dc-bottom { background: rgba(50,50,50,0.78); padding: 10px 14px 14px 14px; border-radius: 14px 14px 0 0; }"
                b".dc-lock-bg { background: #000000; }"
                b".dc-status  { color: rgba(255,255,255,0.85); font-size: 0.85em; }"
            )
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

        # Outer overlay wraps everything so the lock screen can cover both
        # the camera area AND the bottom bar.
        outer = Gtk.Overlay()
        outer.add_css_class("dc-black-bg")
        outer.set_hexpand(True)
        outer.set_vexpand(True)
        self.append(outer)

        # Camera area fills the entire outer overlay; the controls float
        # *over* the bottom edge as a separate overlay (not a side rail).
        cam_overlay = Gtk.Overlay()
        cam_overlay.add_css_class("dc-black-bg")
        cam_overlay.set_hexpand(True)
        cam_overlay.set_vexpand(True)
        outer.set_child(cam_overlay)

        self._preview_pic = Gtk.Picture()
        self._preview_pic.add_css_class("dc-black-bg")
        self._preview_pic.set_hexpand(True)
        self._preview_pic.set_vexpand(True)
        self._preview_pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._preview_pic.set_can_shrink(True)
        cam_overlay.set_child(self._preview_pic)

        # "No camera" icon — hidden on first frame
        self._no_cam_icon = Gtk.Image.new_from_icon_name("camera-video-symbolic")
        self._no_cam_icon.set_pixel_size(64)
        self._no_cam_icon.add_css_class("dim-label")
        self._no_cam_icon.set_halign(Gtk.Align.CENTER)
        self._no_cam_icon.set_valign(Gtk.Align.CENTER)
        cam_overlay.add_overlay(self._no_cam_icon)

        self._rec_dot = Gtk.DrawingArea()
        self._rec_dot.set_size_request(16, 16)
        self._rec_dot.set_draw_func(self._draw_rec_dot, None)

        # Activity detection (resets dim timer) on the camera area
        for ctrl_cls, sig in (
            (Gtk.EventControllerMotion, "motion"),
            (Gtk.GestureClick,          "pressed"),
        ):
            ctrl = ctrl_cls()
            ctrl.connect(sig, lambda *_: self._reset_dim_timer())
            cam_overlay.add_controller(ctrl)

        # ── Controls overlay — always at the user's visual bottom ─────────────
        # The inner `bar_wrap` carries the styled translucent backdrop and the
        # controls. It is wrapped in a RotatedContainer, which moves/rotates the
        # whole thing as the device orientation changes so the buttons stay
        # reachable at the user's visual bottom edge and read upright.
        bar_wrap = Gtk.Box()
        bar_wrap.add_css_class("dc-bottom")
        bar_wrap.set_hexpand(True)
        bar_wrap.append(self._build_controls())

        rotator = RotatedContainer()
        rotator.set_child(bar_wrap)
        rotator.set_valign(Gtk.Align.END)
        rotator.set_halign(Gtk.Align.FILL)
        rotator.set_hexpand(True)
        rotator.set_margin_start(8)
        rotator.set_margin_end(8)
        rotator.set_margin_bottom(8)
        cam_overlay.add_overlay(rotator)
        self._bar_wrap = bar_wrap
        self._bar_rotator = rotator
        self._update_toggle_btn()

        # ── Lock / dim screen — covers entire outer overlay ───────────────────
        self._lock_overlay = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
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
        self._lock_save_btn.set_size_request(200, 80)
        self._lock_save_btn.add_css_class("destructive-action")
        self._lock_save_btn.add_css_class("pill")
        self._lock_save_btn.set_halign(Gtk.Align.CENTER)
        self._lock_save_btn.set_valign(Gtk.Align.CENTER)
        self._lock_save_btn.connect("clicked", self._on_lock_save)

        self._lock_btn_rotator = RotatedContainer()
        self._lock_btn_rotator.set_hexpand(True)
        self._lock_btn_rotator.set_vexpand(True)
        self._lock_btn_rotator.set_child(self._lock_save_btn)
        self._lock_overlay.append(self._lock_btn_rotator)

        wake = Gtk.GestureClick()
        wake.connect("pressed", self._on_lock_tap)
        self._lock_overlay.add_controller(wake)
        outer.add_overlay(self._lock_overlay)

        # ── GStreamer preview ─────────────────────────────────────────────────
        # Lazily started — the camera pipeline only opens once the user
        # actually views the Dashcam tab (via on_shown).  This avoids holding
        # /dev/video0 open while the user is on other tabs.  The recorder
        # process is independent and keeps running across tab switches.
        self._preview = _CameraPreview(
            self._preview_pic,
            on_first_frame=self._on_first_frame,
            on_all_failed=self._on_preview_failed,
        )

    def _build_controls(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_hexpand(True)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.CENTER)
        btn_row.set_hexpand(True)

        toggle_btn = Gtk.Button()
        toggle_btn.set_hexpand(True)
        toggle_btn.add_css_class("pill")
        toggle_btn.connect("clicked", self._on_toggle)
        self._toggle_btns.append(toggle_btn)
        btn_row.append(toggle_btn)

        save_btn = Gtk.Button(label=_translate(self.language, "dashcam.btn.save"))
        save_btn.set_visible(False)
        save_btn.set_hexpand(True)
        save_btn.add_css_class("pill")
        save_btn.connect("clicked", self._on_save_event)
        self._save_btns.append(save_btn)
        btn_row.append(save_btn)

        self._clips_popover = self._build_clips_popover()
        clips_btn = Gtk.MenuButton(icon_name="list-compact-symbolic")
        clips_btn.set_popover(self._clips_popover)
        self._clips_popover.connect("show", lambda _: self._update_saved_list())
        clips_btn.add_css_class("circular")
        clips_btn.add_css_class("osd")
        self._clips_btns.append(clips_btn)
        btn_row.append(clips_btn)

        box.append(btn_row)
        return box

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

    def _on_preview_failed(self, _msg: str) -> None:
        pass

    def _on_preview_ready(self, paintable: Any) -> bool:
        """Called on the main thread when GStreamer in-process recording provides a preview."""
        self._preview_pic.set_paintable(paintable)
        self._no_cam_icon.set_visible(False)
        return False

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

    def set_gps_osd(self, enabled: bool) -> None:
        self._recorder.gps_osd = enabled

    def set_fps(self, fps: int) -> None:
        self._recorder.fps = fps

    def set_speed_osd(self, enabled: bool) -> None:
        self._recorder.speed_osd = enabled

    def set_units(self, units: str) -> None:
        self._recorder.units = units

    def update_gps(self, lat: float | None, lon: float | None, speed_kmh: float | None) -> None:
        self._recorder.update_gps(lat, lon, speed_kmh)

    def update_obd_speed(self, speed_kmh: float | None) -> None:
        self._recorder.update_obd_speed(speed_kmh)

    # ── Tab visibility ────────────────────────────────────────────────────────

    def on_shown(self) -> None:
        """Called when the Dashcam tab becomes visible.

        Starts the preview-only pipeline when not recording.  During recording
        the GStreamer tee pipeline already provides the live preview — starting
        a second pipeline would conflict with the camera device.
        """
        if not self._recorder.is_recording:
            self._preview.start()

    def on_hidden(self) -> None:
        """Called when the user navigates away from the Dashcam tab.

        Tears down the on-screen preview when no recording is active so we
        stop holding /dev/video0.  If a recording is in progress, the
        recorder's own ffmpeg process keeps running independently — the
        preview is still torn down since no one is watching it, but the
        on-disk segmentation continues uninterrupted.
        """
        self._preview.stop()

    # ── Orientation (from dashboard_window orientation sensor) ────────────────

    def update_orientation(self, angle: int, is_landscape: bool) -> None:
        self._recorder.rotation = angle

    def update_ui_rotation(self, angle: int) -> None:
        landscape = angle in (90, 270)
        self._is_landscape = landscape
        self._apply_bar_position(angle, landscape)
        self._lock_btn_rotator.set_rotation(angle)

    def _apply_bar_position(self, angle: int, is_landscape: bool) -> None:
        """Reposition + rotate the control bar to follow physical orientation.

        Screen is portrait-locked. The bar is moved to whichever portrait edge
        is at the user's visual bottom and the whole bar is rotated so labels
        and buttons read upright.

        angle=0   (normal)                       → portrait BOTTOM, no rotation
        angle=90  (left-up  / CW  device tilt)   → portrait LEFT,   rotate 90°
        angle=180 (bottom-up)                    → portrait TOP,    rotate 180°
        angle=270 (right-up / CCW device tilt)   → portrait RIGHT,  rotate 270°
        """
        rot = self._bar_rotator
        if is_landscape and angle == 90:
            rot.set_valign(Gtk.Align.CENTER)
            rot.set_halign(Gtk.Align.START)
            rot.set_hexpand(False)
            rot.set_vexpand(False)
            rot.set_margin_start(8)
            rot.set_margin_end(0)
            rot.set_margin_top(8)
            rot.set_margin_bottom(8)
            rot.set_rotation(90)
        elif is_landscape and angle == 270:
            rot.set_valign(Gtk.Align.CENTER)
            rot.set_halign(Gtk.Align.END)
            rot.set_hexpand(False)
            rot.set_vexpand(False)
            rot.set_margin_start(0)
            rot.set_margin_end(8)
            rot.set_margin_top(8)
            rot.set_margin_bottom(8)
            rot.set_rotation(270)
        elif angle == 180:
            rot.set_valign(Gtk.Align.START)
            rot.set_halign(Gtk.Align.FILL)
            rot.set_hexpand(True)
            rot.set_vexpand(False)
            rot.set_margin_start(8)
            rot.set_margin_end(8)
            rot.set_margin_top(8)
            rot.set_margin_bottom(0)
            rot.set_rotation(180)
        else:
            rot.set_valign(Gtk.Align.END)
            rot.set_halign(Gtk.Align.FILL)
            rot.set_hexpand(True)
            rot.set_vexpand(False)
            rot.set_margin_start(8)
            rot.set_margin_end(8)
            rot.set_margin_bottom(8)
            rot.set_margin_top(0)
            rot.set_rotation(0)

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
            for btn in self._save_btns:
                btn.set_visible(False)
            # V4L2 is now free again — restart the live preview
            self._preview.start()
        else:
            # V4L2 only allows one capturer at a time; release the preview
            # before ffmpeg opens the device.  The kernel may not free the
            # device node immediately after GStreamer sets the pipeline to NULL,
            # so we defer the recorder start by 400 ms.
            self._preview.stop()
            for btn in self._save_btns:
                btn.set_visible(True)
            if self.on_recording_changed is not None:
                self.on_recording_changed(True)
            GLib.timeout_add(400, self._start_recording_deferred)
            return
        self._update_toggle_btn()
        if self.on_recording_changed is not None:
            self.on_recording_changed(self._recorder.is_recording)

    def _start_recording_deferred(self) -> bool:
        self._recorder.start()
        self._update_toggle_btn()
        self._start_tick()
        self._reset_dim_timer()
        return False

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

    def _show_error(self, _msg: str) -> bool:
        self._stop_tick()
        self._stop_dim_timer()
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
        self._rec_dot_on = True
        self._rec_dot.queue_draw()

    def _tick(self) -> bool:
        if not self._recorder.is_recording:
            self._tick_source = None
            return False
        self._rec_dot_on = not self._rec_dot_on
        self._rec_dot.queue_draw()
        self._update_status()
        return True

    # ── Status ────────────────────────────────────────────────────────────────

    def _update_toggle_btn(self) -> None:
        rec = self._recorder.is_recording
        label = _translate(self.language, "dashcam.btn.stop" if rec else "dashcam.btn.start")
        for btn in self._toggle_btns:
            btn.set_label(label)
            if rec:
                btn.remove_css_class("suggested-action")
                btn.add_css_class("destructive-action")
            else:
                btn.remove_css_class("destructive-action")
                btn.add_css_class("suggested-action")
        for btn in self._clips_btns:
            btn.set_visible(not rec)

    def _update_status(self) -> None:
        pass

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
        if self._rec_dot_on:
            cr.set_source_rgb(0.9, 0.1, 0.1)
        else:
            cr.set_source_rgba(0.9, 0.1, 0.1, 0.0)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.fill()

    # ── Language ──────────────────────────────────────────────────────────────

    def set_language(self, language: str) -> None:
        self.language = _normalize_language(language)
        self._update_toggle_btn()
        self._update_lock_btn_label()
        self._update_status()
