"""Tests for the dashcam_recorder helpers that parse v4l2-ctl output.

list_cameras and query_camera_modes are thin shells around subprocess
calls. Mocking subprocess.run lets us verify the parsing without
needing an actual capture device hooked up."""
from __future__ import annotations

import subprocess

from drivepulse_app.dashcam.recorder import (
    FPS_OPTIONS,
    RESOLUTIONS,
    query_camera_modes,
)

# ─── public schema lock ──────────────────────────────────────────────────────

def test_resolutions_schema():
    # Lock the resolution dropdown contents — settings UI snaps current
    # resolution to this list, dropping any unknown value.
    assert RESOLUTIONS == ["1920x1080", "1280x720", "854x480", "640x480"]


def test_fps_options_schema():
    assert FPS_OPTIONS == [30, 25, 15]


# ─── query_camera_modes ─────────────────────────────────────────────────────

def _mock_run(stdout: str = "", returncode: int = 0):
    """Build a callable that mimics subprocess.run-with-text-output."""
    class _R:
        def __init__(self, out):
            self.stdout = out
            self.returncode = returncode
    return lambda *_a, **_kw: _R(stdout)


def test_query_camera_modes_empty_when_v4l2_unavailable(monkeypatch):
    def _boom(*_a, **_kw):
        raise FileNotFoundError("v4l2-ctl missing")
    monkeypatch.setattr(subprocess, "run", _boom)
    assert query_camera_modes("/dev/video0") == {}


def test_query_camera_modes_parses_size_and_fps(monkeypatch):
    sample = (
        "ioctl: VIDIOC_ENUM_FMT\n"
        "    Index       : 0\n"
        "    Type        : Video Capture\n"
        "    Pixel Format: 'YUYV'\n"
        "    Name        : YUYV 4:2:2\n"
        "        Size: Discrete 1280x720\n"
        "            Interval: Discrete 0.033s (30.000 fps)\n"
        "            Interval: Discrete 0.040s (25.000 fps)\n"
        "        Size: Discrete 640x480\n"
        "            Interval: Discrete 0.066s (15.000 fps)\n"
        "            Interval: Discrete 0.033s (30.000 fps)\n"
    )
    monkeypatch.setattr(subprocess, "run", _mock_run(stdout=sample))
    modes = query_camera_modes("/dev/video0")
    assert "1280x720" in modes
    assert "640x480" in modes
    assert 30 in modes["1280x720"]
    assert 25 in modes["1280x720"]
    assert 15 in modes["640x480"]


def test_query_camera_modes_fps_sorted_descending(monkeypatch):
    sample = (
        "Size: Discrete 1280x720\n"
        "    Interval: Discrete 0.066s (15.000 fps)\n"
        "    Interval: Discrete 0.033s (30.000 fps)\n"
        "    Interval: Discrete 0.040s (25.000 fps)\n"
    )
    monkeypatch.setattr(subprocess, "run", _mock_run(stdout=sample))
    modes = query_camera_modes("/dev/video0")
    # FPS list per resolution should be sorted highest-first so the UI
    # defaults to the snappiest option.
    assert modes["1280x720"] == [30, 25, 15]


def test_query_camera_modes_resolutions_sorted_by_pixel_count_desc(monkeypatch):
    sample = (
        "Size: Discrete 640x480\n"
        "    Interval: Discrete 0.033s (30.000 fps)\n"
        "Size: Discrete 1920x1080\n"
        "    Interval: Discrete 0.033s (30.000 fps)\n"
        "Size: Discrete 1280x720\n"
        "    Interval: Discrete 0.033s (30.000 fps)\n"
    )
    monkeypatch.setattr(subprocess, "run", _mock_run(stdout=sample))
    modes = query_camera_modes("/dev/video0")
    # Resolutions ordered by pixel count descending.
    assert list(modes.keys()) == ["1920x1080", "1280x720", "640x480"]


def test_query_camera_modes_deduplicates_fps_entries(monkeypatch):
    # Real cameras sometimes list the same fps twice (different intervals
    # rounding to the same integer). We should not produce duplicates.
    sample = (
        "Size: Discrete 1280x720\n"
        "    Interval: Discrete 0.0333s (30.000 fps)\n"
        "    Interval: Discrete 0.0334s (30.000 fps)\n"
    )
    monkeypatch.setattr(subprocess, "run", _mock_run(stdout=sample))
    modes = query_camera_modes("/dev/video0")
    assert modes["1280x720"] == [30]


def test_query_camera_modes_rounds_fractional_fps_to_int(monkeypatch):
    # 29.97 fps → 30. NTSC-style cameras report non-integer fps.
    sample = (
        "Size: Discrete 1920x1080\n"
        "    Interval: Discrete 0.0333s (29.970 fps)\n"
    )
    monkeypatch.setattr(subprocess, "run", _mock_run(stdout=sample))
    modes = query_camera_modes("/dev/video0")
    assert modes["1920x1080"] == [30]


def test_query_camera_modes_empty_stdout_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _mock_run(stdout=""))
    assert query_camera_modes("/dev/video0") == {}


def test_query_camera_modes_no_size_lines_yields_empty(monkeypatch):
    # Garbage output without any "Size:" lines → no modes captured.
    monkeypatch.setattr(subprocess, "run", _mock_run(stdout="not v4l2 output"))
    assert query_camera_modes("/dev/video0") == {}


def test_query_camera_modes_fps_before_size_is_dropped(monkeypatch):
    # Edge case: an Interval line appearing before any Size line has no
    # current_res to attach to and must be ignored without crashing.
    sample = (
        "Interval: Discrete 0.033s (30.000 fps)\n"
        "Size: Discrete 1280x720\n"
        "    Interval: Discrete 0.033s (30.000 fps)\n"
    )
    monkeypatch.setattr(subprocess, "run", _mock_run(stdout=sample))
    modes = query_camera_modes("/dev/video0")
    assert modes == {"1280x720": [30]}
