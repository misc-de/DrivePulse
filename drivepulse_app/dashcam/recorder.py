"""Dashcam loop recorder — continuous segmented recording with event save."""
from __future__ import annotations

import shutil
import signal
import subprocess
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)
_GST_ERRORS = (AttributeError, TypeError, RuntimeError)

# ── GStreamer (optional) ────────────────────────────────────────────────────────
# Used for in-process recording so preview and capture share one pipeline,
# eliminating the V4L2 device-busy race and providing live preview while recording.
_GST_OK = False
try:
    import gi as _gi
    _gi.require_version("Gst", "1.0")
    from gi.repository import GLib as _GLib
    from gi.repository import Gst as _Gst
    _Gst.init(None)
    _GST_OK = True
except (ImportError, ValueError, RuntimeError):
    _Gst = None
    _GLib = None

_VIDEOS_DIR = Path.home() / "Videos" / "Dashcam"

RESOLUTIONS = ["1920x1080", "1280x720", "854x480", "640x480"]
FPS_OPTIONS  = [30, 25, 15]
# Royalty-free codecs only — Flathub forbids x264/x265 in the runtime stack.
# VP8 = best realtime/compatibility tradeoff, VP9 = better compression at higher
# CPU cost, AV1 = future-proof but encode cost is high (desktop / fast SoCs only).
CODECS = ["vp8", "vp9", "av1"]
_SEGMENT_GLOBS = ("dc_*.webm", "dc_*.mp4")
_PROTECTED_GLOBS = ("*.webm", "*.mp4")


def list_cameras() -> list[str]:
    """Return V4L2 video capture device paths (excludes codec/metadata devices)."""
    cameras: list[str] = []
    for dev in sorted(Path("/dev").glob("video*")):
        if not dev.is_char_device():
            continue
        try:
            out = subprocess.run(
                ["v4l2-ctl", "--device", str(dev), "--info"],
                capture_output=True, text=True, timeout=2, check=False,
            )
            if "Video Capture" in out.stdout:
                cameras.append(str(dev))
        except (OSError, subprocess.SubprocessError):
            # v4l2-ctl missing or hung — still surface the device so the user
            # can try it; can't tell capture-capable from non-capture without it.
            log.debug("v4l2-ctl probe failed for %s, including anyway", dev, exc_info=True)
            cameras.append(str(dev))
    return cameras


def query_camera_modes(device: str) -> dict[str, list[int]]:
    """Return {resolution: [fps, ...]} supported by *device* via v4l2-ctl.

    Parses the output of ``v4l2-ctl --list-formats-ext``.  Returns an empty
    dict if v4l2-ctl is unavailable or the device is not a V4L2 device (e.g.
    droidcamsrc / PipeWire cameras on FuriPhone).
    """
    import re
    modes: dict[str, list[int]] = {}
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--device", device, "--list-formats-ext"],
            capture_output=True, text=True, timeout=4, check=False,
        )
        current_res: str | None = None
        for line in result.stdout.splitlines():
            m = re.search(r"Size:\s+\S+\s+(\d+x\d+)", line)
            if m:
                current_res = m.group(1)
                if current_res not in modes:
                    modes[current_res] = []
                continue
            m = re.search(r"\((\d+(?:\.\d+)?)\s+fps\)", line)
            if m and current_res is not None:
                fps = round(float(m.group(1)))
                if fps not in modes[current_res]:
                    modes[current_res].append(fps)
    except (OSError, subprocess.SubprocessError):
        log.debug("v4l2-ctl --list-formats-ext failed for %s", device, exc_info=True)
    # Sort: resolutions by pixel count desc, fps per resolution desc
    return {
        res: sorted(fps_list, reverse=True)
        for res, fps_list in sorted(
            modes.items(),
            key=lambda kv: (int(kv[0].split("x")[0]) * int(kv[0].split("x")[1])),
            reverse=True,
        )
    }


class DashcamRecorder:
    """
    Continuously records in fixed-length segments.
    Oldest segments are deleted once the rolling buffer is full.
    save_event() copies the surrounding segments to a protected folder.
    """

    def __init__(
        self,
        on_segment_start: Callable[[Path], None] | None = None,
        on_segment_done:  Callable[[Path], None] | None = None,
        on_error:         Callable[[str],  None] | None = None,
    ) -> None:
        self.on_segment_start = on_segment_start
        self.on_segment_done  = on_segment_done
        self.on_error         = on_error

        self.camera:          str  = "/dev/video0"
        self.resolution:      str  = "1280x720"
        self.fps:             int  = 25
        self.codec:           str  = "vp8"
        self.segment_minutes: int  = 3
        self.max_segments:    int  = 10
        self.rolling_dir:     Path = _VIDEOS_DIR / "tmp"
        self.protected_dir:   Path = _VIDEOS_DIR
        # Clockwise rotation in degrees written as MP4 display-matrix metadata.
        # Mirrors what phone cameras embed so players show the video upright.
        self.rotation:        int  = 0
        # GPS state updated from the main thread; read at segment-start in the record thread.
        # Python's GIL makes float/None stores effectively atomic here.
        self.lat:           float | None = None
        self.lon:           float | None = None
        self.speed_kmh:     float | None = None   # GPS speed (primary)
        self.obd_speed_kmh: float | None = None   # OBD speed (fallback)
        self.gps_osd:       bool = False
        self.speed_osd:     bool = False           # show speed in video (GPS → OBD fallback)
        self.units:         str  = "metric"

        # Called on the GTK main thread when in-process GStreamer provides a preview.
        self.on_preview_ready: Callable[[Any], None] | None = None

        self._proc:          subprocess.Popen | None = None
        self._thread:        threading.Thread | None = None
        self._stop_event     = threading.Event()
        self._lock           = threading.Lock()
        self._segments:      list[Path] = []
        self._seg_started:   datetime | None = None
        self._osd_txt:       Path | None = None
        self._gst_pipeline:  Any = None
        self._gst_last_seg:  Path | None = None

        self.is_recording: bool = False

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self.is_recording:
            return
        self.rolling_dir.mkdir(parents=True, exist_ok=True)
        self.protected_dir.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        with self._lock:
            self._segments.clear()
            # Adopt files left by previous sessions so _prune() can enforce
            # max_segments across restarts and clean up stale files immediately.
            # Both extensions are listed so older .mp4 segments are pruned even
            # after the user switches to a WebM codec.
            adopted: list[Path] = []
            for pattern in _SEGMENT_GLOBS:
                adopted.extend(self.rolling_dir.glob(pattern))
            self._segments.extend(sorted(adopted))
        self._prune()
        self.is_recording = True
        self._thread = threading.Thread(target=self._record_loop, daemon=True, name="dashcam")
        self._thread.start()

    def stop(self) -> None:
        if not self.is_recording:
            return
        self.is_recording = False
        self._stop_event.set()
        self._kill_proc()
        if self._thread:
            # GStreamer EOS finalisation can take a few seconds; give it time.
            self._thread.join(timeout=10)

    def save_event(self) -> list[Path]:
        """Copy previous + current segment to protected_dir. Returns saved paths."""
        with self._lock:
            candidates = list(self._segments[-2:])
        if not candidates:
            return []
        self.protected_dir.mkdir(parents=True, exist_ok=True)
        tag = datetime.now(UTC).strftime("event_%Y%m%d_%H%M%S")
        saved: list[Path] = []
        for i, src in enumerate(candidates):
            if src.exists():
                dst = self.protected_dir / f"{tag}_{i:02d}{src.suffix}"
                try:
                    shutil.copy2(src, dst)
                    saved.append(dst)
                    log.info("Event saved: %s", dst)
                except OSError as exc:
                    log.warning("Could not save event clip %s: %s", src, exc)
        return saved

    def update_gps(self, lat: float | None, lon: float | None, speed_kmh: float | None) -> None:
        self.lat       = lat
        self.lon       = lon
        self.speed_kmh = speed_kmh
        self._refresh_osd_file()

    def update_obd_speed(self, speed_kmh: float | None) -> None:
        self.obd_speed_kmh = speed_kmh
        self._refresh_osd_file()

    def delete_protected(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Could not delete %s: %s", path, exc)

    # ── Status helpers ────────────────────────────────────────────────────────

    @property
    def segments(self) -> list[Path]:
        with self._lock:
            return list(self._segments)

    @property
    def segment_elapsed_seconds(self) -> float:
        if self._seg_started is None:
            return 0.0
        return (datetime.now(UTC) - self._seg_started).total_seconds()

    @property
    def rolling_size_mb(self) -> float:
        try:
            return sum(f.stat().st_size for f in self.rolling_dir.iterdir() if f.is_file()) / 1_048_576
        except OSError:
            log.debug("Could not stat rolling dir %s", self.rolling_dir, exc_info=True)
            return 0.0

    @property
    def protected_clips(self) -> list[Path]:
        try:
            clips: list[Path] = []
            for pattern in _PROTECTED_GLOBS:
                clips.extend(self.protected_dir.glob(pattern))
            return sorted(clips)
        except OSError:
            log.debug("Could not list protected dir %s", self.protected_dir, exc_info=True)
            return []

    # ── Internal ──────────────────────────────────────────────────────────────

    def _record_loop(self) -> None:
        # Prefer in-process GStreamer: single pipeline with tee for preview + recording.
        # Falls back to external gst-launch-1.0 / ffmpeg when GStreamer is unavailable
        # or when required plugins (x264enc, gtk4paintablesink) are missing.
        if _GST_OK and self._run_gst_recording():
            return
        while not self._stop_event.is_set():
            seg = self._next_segment_path()
            with self._lock:
                self._segments.append(seg)
            self._seg_started = datetime.now(UTC)
            if self.on_segment_start:
                self.on_segment_start(seg)
            ok = self._run_segment(seg)
            self._seg_started = None
            if self.on_segment_done:
                self.on_segment_done(seg)
            if not ok and not self._stop_event.is_set():
                break
            self._prune()

    def _run_gst_recording(self) -> bool:
        """In-process GStreamer recording with optional live preview via tee.

        Tries two pipeline variants per camera source:
          1. tee → gtk4paintablesink (preview) + <vp8/vp9/av1>enc → splitmuxsink
          2. <vp8/vp9/av1>enc → splitmuxsink only (recording without preview)

        Pipeline creation and gtk4paintablesink setup run on the GTK main thread
        (via GLib.idle_add) so the GL rendering context is correct.  The recording
        thread blocks on threading.Event while the main thread initialises, then
        takes over for the long-running poll loop.

        Returns True if a pipeline started and handled recording (even on later error).
        Returns False if no pipeline started at all — caller falls through to external
        gst-launch-1.0 / ffmpeg.
        """
        import threading as _threading

        sources = [
            "droidcamsrc",
            "pipewiresrc",
            "libcamerasrc",
            f"v4l2src device={self.camera}",
            "autovideosrc",
        ]
        seg_ns = self.segment_minutes * 60 * 1_000_000_000  # nanoseconds
        enc_chain, muxer_factory, _muxer_props = self._gst_encoder_tail()
        ext = self._container_ext()
        rec_tail = (
            f"{enc_chain} ! splitmuxsink name=mux async-finalize=true "
            f"max-size-time={seg_ns} muxer-factory={muxer_factory} "
            f"location={self.rolling_dir}/dc_%05d.{ext}"
        )

        for src in sources:
            if self._stop_event.is_set():
                return True
            self._gst_last_seg = None

            for with_preview in (True, False):
                if with_preview:
                    pl = (
                        f"{src} ! videoconvert ! videoflip method=0 ! tee name=t "
                        f"t. ! queue max-size-buffers=2 leaky=downstream "
                        f"! gtk4paintablesink name=preview sync=false "
                        f"t. ! queue {rec_tail}"
                    )
                else:
                    pl = f"{src} ! videoconvert ! videoflip method=0 ! queue {rec_tail}"

                # ── Initialise pipeline on the GTK main thread ─────────────────
                # gtk4paintablesink must connect to the GTK rendering context,
                # which is only valid on the main thread.
                _result: dict[str, Any] = {}
                _ready = _threading.Event()

                def _init(pl=pl, wp=with_preview, r=_result, ev=_ready) -> bool:
                    try:
                        p = _Gst.parse_launch(pl)
                        mux = p.get_by_name("mux")
                        mux.connect("format-location", self._on_gst_format_location)
                        # WebM: streamable=true makes webmmux flush clusters as
                        # they're produced and skip seek-back to the head, so a
                        # crash or power cut leaves the file playable up to the
                        # last cluster (~1 s) instead of zero bytes.
                        _, _, props_str = self._gst_encoder_tail()
                        if props_str:
                            mux_props = _Gst.Structure.new_from_string(props_str)
                            mux.set_property("muxer-properties", mux_props)
                        if wp:
                            try:
                                paintable = (
                                    p.get_by_name("preview")
                                    .get_property("paintable")
                                )
                                r["paintable"] = paintable
                                if self.on_preview_ready and paintable:
                                    self.on_preview_ready(paintable)
                            except _GST_ERRORS:
                                log.debug("Could not wire dashcam preview paintable", exc_info=True)
                        p.set_state(_Gst.State.PLAYING)
                        r["pipeline"] = p
                    except _GST_ERRORS as exc:
                        r["exc"] = exc
                        log.debug("gst init failed: %s", exc)
                    finally:
                        ev.set()
                    return False  # remove idle source

                log.debug("gst in-proc attempt src=%s preview=%s", src, with_preview)
                _GLib.idle_add(_init)
                if not _ready.wait(timeout=5):
                    log.debug("gst main-thread init timed out")
                    continue
                if "exc" in _result or "pipeline" not in _result:
                    continue

                pipeline = _result["pipeline"]
                bus = pipeline.get_bus()

                # Wait for PLAYING on recording thread (blocks here, not on main thread)
                ret = pipeline.get_state(3_000_000_000)
                if ret[0] == _Gst.StateChangeReturn.FAILURE:
                    pipeline.set_state(_Gst.State.NULL)
                    log.debug("gst PLAYING failed: src=%s preview=%s", src, with_preview)
                    continue

                if self._stop_event.is_set():
                    pipeline.set_state(_Gst.State.NULL)
                    return True

                self._gst_pipeline = pipeline
                log.info("gst in-proc recording active: src=%s preview=%s", src, with_preview)

                # Drain ALL bus messages so the queue never grows unbounded.
                # Filtering only ERROR|EOS would leave QoS, StateChanged, etc.
                # piling up in memory for the full duration of the recording.
                while not self._stop_event.is_set():
                    msg = bus.timed_pop_filtered(100_000_000, _Gst.MessageType.ANY)
                    if msg is None:
                        continue
                    if msg.type == _Gst.MessageType.ERROR:
                        err, _ = msg.parse_error()
                        log.warning("gst recording error: %s", err)
                        if self.on_error:
                            self.on_error(str(err))
                        self._stop_event.set()
                        break
                    if msg.type == _Gst.MessageType.EOS:
                        break
                    # All other types consumed and discarded

                # Finalise: send EOS and wait for it to reach all sinks so that
                # splitmuxsink / mp4mux can write the moov atom before we stop.
                pipeline.send_event(_Gst.Event.new_eos())
                bus.timed_pop_filtered(
                    5_000_000_000,  # 5 s
                    _Gst.MessageType.EOS | _Gst.MessageType.ERROR,
                )
                pipeline.set_state(_Gst.State.NULL)
                self._gst_pipeline = None

                last: Path | None = self._gst_last_seg
                self._gst_last_seg = None
                if last is not None and self.on_segment_done:
                    self.on_segment_done(last)

                return True

        return False  # no source started — fall through to external-process path

    def _on_gst_format_location(self, _splitmux: Any, _fragment_id: int) -> str:
        """splitmuxsink format-location signal — fired on the GStreamer streaming thread."""
        # Close out previous segment
        prev = self._gst_last_seg
        if prev is not None and self.on_segment_done:
            self.on_segment_done(prev)
        # Open next segment
        seg = self._next_segment_path()
        self._gst_last_seg = seg
        self._seg_started = datetime.now(UTC)
        with self._lock:
            self._segments.append(seg)
        if self.on_segment_start:
            self.on_segment_start(seg)
        self._prune()
        return str(seg)

    def _next_segment_path(self) -> Path:
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        return self.rolling_dir / f"dc_{ts}.{self._container_ext()}"

    def _container_ext(self) -> str:
        # All supported codecs (VP8/VP9/AV1) ship in WebM.
        return "webm"

    def _gst_encoder_tail(self) -> tuple[str, str, str]:
        """Return (encoder_chain, muxer_factory, muxer_props_struct) for current codec.

        encoder_chain starts with '!' and ends just before the muxer/splitmuxsink.
        muxer_props_struct is a Gst.Structure-parseable string applied to
        splitmuxsink's child mux via the muxer-properties property.
        """
        if self.codec == "vp9":
            enc = "! vp9enc deadline=1 cpu-used=8 target-bitrate=2000000"
        elif self.codec == "av1":
            # svtav1enc target-bitrate is in kbit/s; preset 10 ≈ realtime on x86.
            enc = "! svtav1enc preset=10 target-bitrate=2000 ! av1parse"
        else:
            enc = "! vp8enc deadline=1 cpu-used=4 target-bitrate=2000000"
        # streamable=true makes webmmux flush clusters as they're written so the
        # file stays playable after a crash without needing seek-back to the head.
        return enc, "webmmux", "props,streamable=(boolean)true"

    def _ffmpeg_encoder_args(self) -> list[str]:
        if self.codec == "vp9":
            return ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8",
                    "-b:v", "2M", "-an"]
        if self.codec == "av1":
            return ["-c:v", "libsvtav1", "-preset", "10", "-b:v", "2M", "-an"]
        return ["-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "4",
                "-b:v", "2M", "-an"]

    def _run_segment(self, out: Path) -> bool:
        """Record one segment. Tries GStreamer sources first, then FFmpeg V4L2."""
        duration_s = self.segment_minutes * 60

        # ── GStreamer strategies (droidcamsrc for Halium/FuriPhone, then PipeWire,
        #    libcamera, V4L2).  gst-launch-1.0 -e finalises the MP4 on SIGINT.
        _w, _h = self.resolution.split("x") if "x" in self.resolution else ("1280", "720")
        osd_elements = ""
        if self.gps_osd or self.speed_osd:
            osd_txt = _VIDEOS_DIR / "tmp" / "osd.txt"
            osd_txt.parent.mkdir(parents=True, exist_ok=True)
            self._osd_txt = osd_txt
            self._refresh_osd_file()
            osd_elements = (
                f" ! textoverlay text=\"\" textfile={osd_txt}"
                " valignment=bottom halignment=left"
                " font-desc=\"Sans 18\" shaded-background=true"
            )

        gst_sources = [
            "droidcamsrc",
            "pipewiresrc",
            "libcamerasrc",
            f"v4l2src device={self.camera}",
            "autovideosrc",
        ]
        enc_chain, muxer_factory, _ = self._gst_encoder_tail()
        for src in gst_sources:
            if self._stop_event.is_set():
                return False
            pipeline = (
                f"{src} ! videoconvert ! videoflip method=0"
                f"{osd_elements}"
                f" {enc_chain}"
                f" ! {muxer_factory} streamable=true"
                f" ! filesink location={out}"
            )
            cmd = ["gst-launch-1.0", "-e", *pipeline.split()]
            log.debug("gst attempt src=%s codec=%s", src, self.codec)
            ok = self._run_proc(cmd, duration_s, use_sigint=True)
            if ok:
                return True
            if self._stop_event.is_set():
                return False

        # ── FFmpeg V4L2 fallback (desktop / standard webcams)
        if self.gps_osd or self.speed_osd:
            osd_txt = _VIDEOS_DIR / "tmp" / "osd.txt"
            osd_txt.parent.mkdir(parents=True, exist_ok=True)
            self._osd_txt = osd_txt
            self._refresh_osd_file()

        # WebM/Matroska doesn't carry the MP4 rotate display-matrix tag, but the
        # GStreamer path already applies rotation via videoflip — for the FFmpeg
        # fallback we drop the metadata since players would ignore it.
        base_out = [
            "-t", str(duration_s),
            *self._ffmpeg_encoder_args(),
            "-f", "webm",
        ]
        lat, lon = self.lat, self.lon
        if lat is not None and lon is not None:
            loc = f"{lat:+.4f}{lon:+.4f}/"
            base_out += ["-metadata", f"location={loc}", "-metadata", f"location-eng={loc}"]
        if self._osd_txt:
            base_out += ["-vf",
                f"drawtext=textfile={self._osd_txt}:reload=1"
                ":x=10:y=H-th-10:fontcolor=white:fontsize=22"
                ":box=1:boxcolor=black@0.6:boxborderw=6"]

        for input_flags in [
            ["-f", "v4l2", "-input_format", "mjpeg"],
            ["-f", "v4l2", "-video_size", self.resolution, "-framerate", str(self.fps)],
            ["-f", "v4l2"],
        ]:
            if self._stop_event.is_set():
                return False
            cmd = ["ffmpeg", "-y", *input_flags, "-i", self.camera, *base_out, str(out)]
            log.debug("ffmpeg attempt: %s", input_flags)
            if self._run_proc(cmd, duration_s, use_sigint=False):
                return True
            if self._stop_event.is_set():
                return False

        log.warning("All recording strategies failed for camera %s", self.camera)
        if self.on_error:
            self.on_error(f"Kamera {self.camera} konnte nicht geöffnet werden")
        self._stop_event.set()
        return False

    def _run_proc(self, cmd: list[str], duration_s: int, *, use_sigint: bool) -> bool:
        """Run a subprocess for up to duration_s seconds, then stop it cleanly."""
        try:
            # stderr=DEVNULL: prevents the OS pipe buffer (64 KB) from filling up
            # and stalling the encoder when the child writes progress output.
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._proc = proc
            try:
                proc.wait(timeout=duration_s)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                # Segment duration elapsed — stop cleanly
                if use_sigint:
                    proc.send_signal(signal.SIGINT)   # triggers EOS in gst-launch
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                rc = proc.returncode
            if rc not in (0, -signal.SIGINT):
                log.debug("%s failed rc=%d", cmd[0], rc)
                return False
            return True
        except FileNotFoundError:
            return False
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("proc error: %s", exc)
            return False
        finally:
            self._proc = None

    def _kill_proc(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=5)
            except OSError:
                log.debug("Could not signal dashcam process", exc_info=True)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _refresh_osd_file(self) -> None:
        osd_txt = self._osd_txt
        if osd_txt is None or not (self.gps_osd or self.speed_osd):
            return
        parts: list[str] = []
        if self.gps_osd and self.lat is not None and self.lon is not None:
            ns = "N" if self.lat >= 0 else "S"
            ew = "E" if self.lon >= 0 else "W"
            parts.append(f"GPS {abs(self.lat):.4f}{ns} {abs(self.lon):.4f}{ew}")
        if self.speed_osd:
            speed = self.speed_kmh if self.speed_kmh is not None else self.obd_speed_kmh
            if speed is not None:
                if self.units == "imperial":
                    parts.append(f"{speed * 0.621371:.0f} mph")
                else:
                    parts.append(f"{speed:.0f} km/h")
        text = "  ".join(parts)
        try:
            osd_txt.write_text(text, encoding="utf-8")
        except OSError:
            log.debug("Could not write dashcam OSD text to %s", osd_txt, exc_info=True)

    def _prune(self) -> None:
        with self._lock:
            while len(self._segments) > self.max_segments:
                oldest = self._segments.pop(0)
                try:
                    oldest.unlink(missing_ok=True)
                    log.debug("Rolled: %s", oldest)
                except OSError:
                    log.debug("Could not unlink rolled segment %s", oldest, exc_info=True)
