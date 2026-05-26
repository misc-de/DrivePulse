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
    assert first.suffix == ".webm"


def test_dashcam_run_proc_returns_false_when_executable_missing(monkeypatch):
    import subprocess

    from drivepulse_app.dashcam.recorder import DashcamRecorder

    def fail_popen(*_args, **_kwargs):
        raise FileNotFoundError("missing encoder")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)

    recorder = DashcamRecorder()

    assert recorder._run_proc(["missing-encoder"], 1, use_sigint=False) is False


def test_dashcam_kill_proc_tolerates_process_disappearing():
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    class GoneProcess:
        def poll(self):
            return None

        def send_signal(self, _signal):
            raise ProcessLookupError("gone")

    recorder = DashcamRecorder()
    recorder._proc = GoneProcess()

    recorder._kill_proc()
