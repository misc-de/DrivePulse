from __future__ import annotations


def test_dashcam_save_event_creates_protected_dir(tmp_path):
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    rolling = tmp_path / "rolling"
    protected = tmp_path / "saved"
    rolling.mkdir()
    seg = rolling / "dc_1.mp4"
    seg.write_bytes(b"video")

    recorder = DashcamRecorder()
    recorder.protected_dir = protected
    recorder._segments = [seg]

    saved = recorder.save_event()

    assert len(saved) == 1
    assert saved[0].parent == protected
    assert saved[0].read_bytes() == b"video"


def test_dashcam_segment_paths_include_subsecond_precision(tmp_path):
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    recorder = DashcamRecorder()
    recorder.rolling_dir = tmp_path

    first = recorder._next_segment_path()
    second = recorder._next_segment_path()

    assert first != second
    assert first.name.startswith("dc_")
    assert first.suffix == ".mp4"
