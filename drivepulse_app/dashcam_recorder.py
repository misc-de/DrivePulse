"""Dashcam loop recorder — continuous segmented recording with event save."""
from __future__ import annotations

import shutil
import signal
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .diagnostics import get_logger

log = get_logger(__name__)

# ── GStreamer (optional) ────────────────────────────────────────────────────────
# Used for in-process recording so preview and capture share one pipeline,
# eliminating the V4L2 device-busy race and providing live preview while recording.
_GST_OK = False
try:
    import gi as _gi
    _gi.require_version("Gst", "1.0")
    from gi.repository import Gst as _Gst   # type: ignore[attr-defined]
    from gi.repository import GLib as _GLib  # type: ignore[attr-defined]
    _Gst.init(None)
    _GST_OK = True
except Exception:
    _Gst = None   # type: ignore[assignment]
    _GLib = None  # type: ignore[assignment]

_VIDEOS_DIR = Path.home() / "Videos" / "Dashcam"

RESOLUTIONS = ["1920x1080", "1280x720", "854x480", "640x480"]
FPS_OPTIONS  = [30, 25, 15]


def list_cameras() -> list[str]:
    """Return V4L2 video capture device paths (excludes codec/metadata devices)."""
    cameras: list[str] = []
    for dev in sorted(Path("/dev").glob("video*")):
        if not dev.is_char_device():
            continue
        try:
            out = subprocess.run(
                ["v4l2-ctl", "--device", str(dev), "--info"],
                capture_output=True, text=True, timeout=2,
            )
            if "Video Capture" in out.stdout:
                cameras.append(str(dev))
        except Exception:
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
            capture_output=True, text=True, timeout=4,
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
    except Exception:
        pass
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
            self._segments.extend(sorted(self.rolling_dir.glob("dc_*.mp4")))
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
        tag = datetime.now(timezone.utc).strftime("event_%Y%m%d_%H%M%S")
        saved: list[Path] = []
        for i, src in enumerate(candidates):
            if src.exists():
                dst = self.protected_dir / f"{tag}_{i:02d}{src.suffix}"
                try:
                    shutil.copy2(src, dst)
                    saved.append(dst)
                    log.info("Event saved: %s", dst)
                except Exception as exc:
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
        except Exception as exc:
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
        return (datetime.now(timezone.utc) - self._seg_started).total_seconds()

    @property
    def rolling_size_mb(self) -> float:
        try:
            return sum(f.stat().st_size for f in self.rolling_dir.iterdir() if f.is_file()) / 1_048_576
        except Exception:
            return 0.0

    @property
    def protected_clips(self) -> list[Path]:
        try:
            return sorted(self.protected_dir.glob("*.mp4"))
        except Exception:
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
            self._seg_started = datetime.now(timezone.utc)
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
          1. tee → gtk4paintablesink (preview) + x264enc → splitmuxsink (recording)
          2. x264enc → splitmuxsink only (recording without preview)

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
        rec_tail = (
            f"! x264enc tune=zerolatency speed-preset=ultrafast bitrate=2000 "
            f"! h264parse ! splitmuxsink name=mux async-finalize=true "
            f"max-size-time={seg_ns} muxer-factory=mp4mux "
            f"location={self.rolling_dir}/dc_%05d.mp4"
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
                        # Fragmented MP4: write a self-contained moof/mdat every 2 s.
                        # Each fragment is independently decodable, so a crash or
                        # power cut leaves at most ~2 s of the current segment unplayable.
                        frag_props = _Gst.Structure.new_from_string(
                            "props,fragment-duration=(uint)2000"
                        )
                        mux.set_property("muxer-properties", frag_props)
                        if wp:
                            try:
                                paintable = (
                                    p.get_by_name("preview")
                                    .get_property("paintable")
                                )
                                r["paintable"] = paintable
                                if self.on_preview_ready and paintable:
                                    self.on_preview_ready(paintable)
                            except Exception:
                                pass
                        p.set_state(_Gst.State.PLAYING)
                        r["pipeline"] = p
                    except Exception as exc:
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

                last = self._gst_last_seg
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
        self._seg_started = datetime.now(timezone.utc)
        with self._lock:
            self._segments.append(seg)
        if self.on_segment_start:
            self.on_segment_start(seg)
        self._prune()
        return str(seg)

    def _next_segment_path(self) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        return self.rolling_dir / f"dc_{ts}.mp4"

    def _run_segment(self, out: Path) -> bool:
        """Record one segment. Tries GStreamer sources first, then FFmpeg V4L2."""
        duration_s = self.segment_minutes * 60

        # ── GStreamer strategies (droidcamsrc for Halium/FuriPhone, then PipeWire,
        #    libcamera, V4L2).  gst-launch-1.0 -e finalises the MP4 on SIGINT.
        w, h = self.resolution.split("x") if "x" in self.resolution else ("1280", "720")
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
        for src in gst_sources:
            if self._stop_event.is_set():
                return False
            pipeline = (
                f"{src} ! videoconvert ! videoflip method=0"
                f"{osd_elements}"
                f" ! x264enc tune=zerolatency speed-preset=ultrafast bitrate=2000"
                f" ! h264parse ! mp4mux fragment-duration=2000"
                f" ! filesink location={out}"
            )
            cmd = ["gst-launch-1.0", "-e"] + pipeline.split()
            log.debug("gst attempt src=%s", src)
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

        base_out = [
            "-t", str(duration_s),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-maxrate", "2000k", "-bufsize", "4000k",
            "-movflags", "+empty_moov+default_base_moof", "-frag_duration", "2000000", "-an",
            "-metadata:s:v:0", f"rotate={self.rotation}",
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
            cmd = ["ffmpeg", "-y"] + input_flags + ["-i", self.camera] + base_out + [str(out)]
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
        except Exception as exc:
            log.debug("proc error: %s", exc)
            return False
        finally:
            self._proc = None

    def _kill_proc(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
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
        except Exception:
            pass

    def _prune(self) -> None:
        with self._lock:
            while len(self._segments) > self.max_segments:
                oldest = self._segments.pop(0)
                try:
                    oldest.unlink(missing_ok=True)
                    log.debug("Rolled: %s", oldest)
                except Exception:
                    pass
