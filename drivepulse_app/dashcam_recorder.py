"""Dashcam loop recorder — continuous segmented recording with event save."""
from __future__ import annotations

import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .diagnostics import get_logger

log = get_logger(__name__)

_VIDEOS_DIR = Path.home() / "Videos" / "DrivePulse" / "Dashcam"

RESOLUTIONS = ["1920x1080", "1280x720", "854x480", "640x480"]
FPS_OPTIONS  = [30, 25, 15]


def list_cameras() -> list[str]:
    """Return available V4L2 video device paths."""
    return sorted(str(d) for d in Path("/dev").glob("video*") if d.is_char_device())


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
        self.rolling_dir:     Path = _VIDEOS_DIR / "rolling"
        self.protected_dir:   Path = _VIDEOS_DIR / "saved"
        # Clockwise rotation in degrees written as MP4 display-matrix metadata.
        # Mirrors what phone cameras embed so players show the video upright.
        self.rotation:        int  = 0

        self._proc:        subprocess.Popen | None = None
        self._thread:      threading.Thread | None = None
        self._stop_event   = threading.Event()
        self._lock         = threading.Lock()
        self._segments:    list[Path] = []
        self._seg_started: datetime | None = None

        self.is_recording: bool = False

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self.is_recording:
            return
        self.rolling_dir.mkdir(parents=True, exist_ok=True)
        self.protected_dir.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self.is_recording = True
        self._thread = threading.Thread(target=self._record_loop, daemon=True, name="dashcam")
        self._thread.start()

    def stop(self) -> None:
        if not self.is_recording:
            return
        self.is_recording = False
        self._stop_event.set()
        self._kill_ffmpeg()
        if self._thread:
            self._thread.join(timeout=8)

    def save_event(self) -> list[Path]:
        """Copy previous + current segment to protected_dir. Returns saved paths."""
        with self._lock:
            candidates = list(self._segments[-2:])
        if not candidates:
            return []
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
        while not self._stop_event.is_set():
            seg = self._next_segment_path()
            with self._lock:
                self._segments.append(seg)
            self._seg_started = datetime.now(timezone.utc)
            if self.on_segment_start:
                self.on_segment_start(seg)
            ok = self._run_ffmpeg(seg)
            self._seg_started = None
            if self.on_segment_done:
                self.on_segment_done(seg)
            if not ok and not self._stop_event.is_set():
                break
            self._prune()

    def _next_segment_path(self) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return self.rolling_dir / f"dc_{ts}.mp4"

    def _run_ffmpeg(self, out: Path) -> bool:
        cmd = [
            "ffmpeg", "-y",
            "-f", "v4l2",
            "-video_size", self.resolution,
            "-framerate", str(self.fps),
            "-i", self.camera,
            "-t", str(self.segment_minutes * 60),
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-movflags", "+faststart",
            "-an",
            # Embed display-matrix rotation so players show the video upright
            # regardless of whether the camera was held in portrait or landscape.
            "-metadata:s:v:0", f"rotate={self.rotation}",
            str(out),
        ]
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            rc = self._proc.wait()
            return rc == 0
        except FileNotFoundError:
            msg = "ffmpeg nicht gefunden"
            log.error(msg)
            if self.on_error:
                self.on_error(msg)
            self._stop_event.set()
            return False
        except Exception as exc:
            log.warning("ffmpeg error: %s", exc)
            return False
        finally:
            self._proc = None

    def _kill_ffmpeg(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _prune(self) -> None:
        with self._lock:
            while len(self._segments) > self.max_segments:
                oldest = self._segments.pop(0)
                try:
                    oldest.unlink(missing_ok=True)
                    log.debug("Rolled: %s", oldest)
                except Exception:
                    pass
