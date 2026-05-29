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


# ── save_event / delete_protected edge cases ──────────────────────────────────


def test_dashcam_save_event_returns_empty_when_no_segments(tmp_path):
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    recorder = DashcamRecorder()
    recorder.protected_dir = tmp_path / "saved"

    saved = recorder.save_event()

    assert saved == []
    # An empty save must not even create the protected dir — leaving the
    # filesystem untouched on no-op preserves a clean state for later runs.
    assert not (tmp_path / "saved").exists()


def test_dashcam_save_event_copies_last_two_segments(tmp_path):
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    rolling = tmp_path / "rolling"
    rolling.mkdir()
    segs = [rolling / f"dc_{i}.webm" for i in range(4)]
    for i, s in enumerate(segs):
        s.write_bytes(f"seg{i}".encode())

    recorder = DashcamRecorder()
    recorder.protected_dir = tmp_path / "saved"
    recorder._segments = list(segs)

    saved = recorder.save_event()

    # Only the last two are protected — the rolling buffer's most recent
    # context around the trigger moment is what makes the saved clip useful.
    assert len(saved) == 2
    assert sorted(s.read_bytes() for s in saved) == [b"seg2", b"seg3"]


def test_dashcam_save_event_while_recording_defers_current_segment(tmp_path):
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    rolling = tmp_path / "rolling"
    rolling.mkdir()
    prev = rolling / "dc_0.webm"
    current = rolling / "dc_1.webm"
    prev.write_bytes(b"finished")
    current.write_bytes(b"partial")  # still being recorded

    recorder = DashcamRecorder()
    recorder.protected_dir = tmp_path / "saved"
    recorder._segments = [prev, current]
    recorder.is_recording = True

    planned = recorder.save_event()

    # Both clips are promised, but only the finalised previous segment exists yet.
    assert len(planned) == 2
    existing = sorted(p.name for p in (tmp_path / "saved").iterdir())
    assert len(existing) == 1
    assert (tmp_path / "saved" / existing[0]).read_bytes() == b"finished"
    assert len(recorder._pending_saves) == 1


def test_dashcam_deferred_save_captures_complete_segment(tmp_path):
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    rolling = tmp_path / "rolling"
    rolling.mkdir()
    current = rolling / "dc_0.webm"
    current.write_bytes(b"partial")  # only part written at save time

    recorder = DashcamRecorder()
    recorder.protected_dir = tmp_path / "saved"
    recorder._segments = [current]
    recorder.is_recording = True

    planned = recorder.save_event()
    assert len(planned) == 1
    assert not planned[0].exists()  # nothing copied yet — segment still recording

    # The rest of the segment gets written, then it finalises.
    current.write_bytes(b"partial-plus-the-complete-rest")
    recorder._finalize_segment(current)

    assert not recorder._pending_saves
    # The saved clip holds the COMPLETE segment, not the truncated save-time state.
    assert planned[0].read_bytes() == b"partial-plus-the-complete-rest"


def test_dashcam_finalize_segment_ignores_unrelated_segments(tmp_path):
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    rolling = tmp_path / "rolling"
    rolling.mkdir()
    current = rolling / "dc_0.webm"
    current.write_bytes(b"data")

    recorder = DashcamRecorder()
    recorder.protected_dir = tmp_path / "saved"
    recorder._segments = [current]
    recorder.is_recording = True
    recorder.save_event()

    # Finalising a different segment must not flush the pending save.
    recorder._finalize_segment(rolling / "dc_other.webm")
    assert len(recorder._pending_saves) == 1
    assert not (tmp_path / "saved").exists() or not list((tmp_path / "saved").iterdir())


def test_dashcam_delete_protected_tolerates_missing(tmp_path):
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    recorder = DashcamRecorder()

    # Should not raise even if the file was never there.
    recorder.delete_protected(tmp_path / "does-not-exist.webm")


# ── _prune (rolling buffer cap) ───────────────────────────────────────────────


def test_dashcam_prune_keeps_only_max_segments(tmp_path):
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    rolling = tmp_path
    files = [rolling / f"s{i}.webm" for i in range(5)]
    for f in files:
        f.write_bytes(b"x")

    recorder = DashcamRecorder()
    recorder.rolling_dir = rolling
    recorder.max_segments = 3
    recorder._segments = list(files)

    recorder._prune()

    # The two oldest entries (front of the list) must be deleted from disk
    # and from the in-memory segments list — what remains is the tail.
    assert recorder._segments == files[2:]
    assert not files[0].exists()
    assert not files[1].exists()
    assert files[2].exists()


def test_dashcam_prune_is_noop_when_under_cap(tmp_path):
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    f = tmp_path / "only.webm"
    f.write_bytes(b"x")

    recorder = DashcamRecorder()
    recorder.max_segments = 10
    recorder._segments = [f]

    recorder._prune()

    assert recorder._segments == [f]
    assert f.exists()


# ── Encoder selection by codec ────────────────────────────────────────────────


def test_dashcam_gst_encoder_tail_uses_vp8_by_default():
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    recorder = DashcamRecorder()
    enc, muxer, _ = recorder._gst_encoder_tail()

    assert "vp8enc" in enc
    assert muxer == "webmmux"


def test_dashcam_gst_encoder_tail_switches_for_vp9_and_av1():
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    recorder = DashcamRecorder()

    recorder.codec = "vp9"
    enc, _, _ = recorder._gst_encoder_tail()
    assert "vp9enc" in enc

    recorder.codec = "av1"
    enc, _, _ = recorder._gst_encoder_tail()
    assert "svtav1enc" in enc


def test_dashcam_ffmpeg_encoder_args_match_codec():
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    recorder = DashcamRecorder()
    assert "libvpx" in recorder._ffmpeg_encoder_args()

    recorder.codec = "vp9"
    assert "libvpx-vp9" in recorder._ffmpeg_encoder_args()

    recorder.codec = "av1"
    assert "libsvtav1" in recorder._ffmpeg_encoder_args()


def test_dashcam_container_ext_always_webm():
    # All three supported codecs (vp8/vp9/av1) ship in WebM — verify the
    # extension stays constant so saved files keep one extension everywhere.
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    recorder = DashcamRecorder()
    for codec in ("vp8", "vp9", "av1"):
        recorder.codec = codec
        assert recorder._container_ext() == "webm"


# ── OSD text formatting ───────────────────────────────────────────────────────


def test_dashcam_refresh_osd_is_noop_when_both_overlays_disabled(tmp_path):
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    osd = tmp_path / "osd.txt"
    osd.write_text("stale")

    recorder = DashcamRecorder()
    recorder._osd_txt = osd
    recorder.gps_osd = False
    recorder.speed_osd = False

    recorder._refresh_osd_file()

    # Stale content must not be overwritten when overlays are off.
    assert osd.read_text() == "stale"


def test_dashcam_refresh_osd_writes_gps_with_hemisphere_letters(tmp_path):
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    osd = tmp_path / "osd.txt"
    recorder = DashcamRecorder()
    recorder._osd_txt = osd
    recorder.gps_osd = True
    recorder.lat = -22.9068    # southern + western hemisphere
    recorder.lon = -43.1729

    recorder._refresh_osd_file()

    text = osd.read_text()
    assert "22.9068S" in text
    assert "43.1729W" in text


def test_dashcam_refresh_osd_omits_gps_when_position_unknown(tmp_path):
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    osd = tmp_path / "osd.txt"
    recorder = DashcamRecorder()
    recorder._osd_txt = osd
    recorder.gps_osd = True
    recorder.lat = None
    recorder.lon = None

    recorder._refresh_osd_file()

    # GPS overlay enabled but no fix -> file must end up empty (or
    # contain only the speed part if that were enabled). Either way, no
    # "None" string should leak into the burnt-in video text.
    assert "None" not in osd.read_text()


def test_dashcam_refresh_osd_uses_obd_speed_when_gps_missing(tmp_path):
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    osd = tmp_path / "osd.txt"
    recorder = DashcamRecorder()
    recorder._osd_txt = osd
    recorder.speed_osd = True
    recorder.speed_kmh = None       # GPS missing
    recorder.obd_speed_kmh = 73.2   # OBD present

    recorder._refresh_osd_file()

    assert "73 km/h" in osd.read_text()


def test_dashcam_refresh_osd_converts_to_mph_in_imperial(tmp_path):
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    osd = tmp_path / "osd.txt"
    recorder = DashcamRecorder()
    recorder._osd_txt = osd
    recorder.speed_osd = True
    recorder.speed_kmh = 100.0
    recorder.units = "imperial"

    recorder._refresh_osd_file()

    # 100 km/h ≈ 62.1 mph → rounds to 62.
    assert "62 mph" in osd.read_text()


# ── Properties ────────────────────────────────────────────────────────────────


def test_dashcam_rolling_size_mb_sums_files(tmp_path):
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    (tmp_path / "a.webm").write_bytes(b"\x00" * 524_288)   # 0.5 MB
    (tmp_path / "b.webm").write_bytes(b"\x00" * 524_288)   # 0.5 MB
    (tmp_path / "subdir").mkdir()
    # Subdirectory entry must be skipped — it's not a file.
    (tmp_path / "subdir" / "ignored").write_bytes(b"\x00" * 1_048_576)

    recorder = DashcamRecorder()
    recorder.rolling_dir = tmp_path

    assert recorder.rolling_size_mb == 1.0


def test_dashcam_rolling_size_mb_returns_zero_when_dir_missing(tmp_path):
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    recorder = DashcamRecorder()
    recorder.rolling_dir = tmp_path / "nope"

    assert recorder.rolling_size_mb == 0.0


def test_dashcam_protected_clips_returns_sorted_video_files(tmp_path):
    from drivepulse_app.dashcam.recorder import DashcamRecorder

    (tmp_path / "z.webm").write_bytes(b"x")
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "ignore.txt").write_bytes(b"x")
    (tmp_path / "ignore.log").write_bytes(b"x")

    recorder = DashcamRecorder()
    recorder.protected_dir = tmp_path

    clips = recorder.protected_clips
    names = [c.name for c in clips]
    assert names == sorted(names)
    assert "ignore.txt" not in names
    assert "ignore.log" not in names
    assert "z.webm" in names
    assert "a.mp4" in names
