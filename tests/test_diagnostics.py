from __future__ import annotations

import logging


def _reload_diagnostics(monkeypatch, tmp_path):
    """Re-import diagnostics with a clean LOG_DIR + reset root state."""
    import importlib
    import sys

    # Clear any previously-attached handlers so this test starts fresh.
    root = logging.getLogger("drivepulse_app")
    for h in list(root.handlers):
        root.removeHandler(h)

    monkeypatch.setenv("OBD_LOG_DIR", str(tmp_path))
    if "drivepulse_app.diagnostics" in sys.modules:
        return importlib.reload(sys.modules["drivepulse_app.diagnostics"])
    from drivepulse_app import diagnostics
    return diagnostics


def test_get_logger_attaches_a_single_handler_for_the_whole_package(monkeypatch, tmp_path):
    """Regression: previously every get_logger() call attached its own
    FileHandler to the named logger. With 76 modules that meant 76 file
    descriptors writing to drivepulse.log. After the refactor, the
    rotating file handler lives on the drivepulse_app parent logger and
    children propagate to it."""
    diag = _reload_diagnostics(monkeypatch, tmp_path)
    diag._root_configured = False  # force re-setup with our tmp dir

    a = diag.get_logger("drivepulse_app.foo")
    b = diag.get_logger("drivepulse_app.bar")
    root = logging.getLogger("drivepulse_app")

    assert len(root.handlers) == 1, "exactly one handler on the parent logger"
    assert a.handlers == [] and b.handlers == [], "no per-module handlers"
    assert a.propagate is True and b.propagate is True
    assert a.getEffectiveLevel() == logging.INFO


def test_file_handler_rotates(monkeypatch, tmp_path):
    """The handler must be a RotatingFileHandler so drivepulse.log cannot
    grow unbounded over months of in-car use."""
    import logging.handlers

    diag = _reload_diagnostics(monkeypatch, tmp_path)
    diag._root_configured = False

    diag.get_logger("drivepulse_app.test_rotation")
    root = logging.getLogger("drivepulse_app")

    assert len(root.handlers) == 1
    handler = root.handlers[0]
    assert isinstance(handler, logging.handlers.RotatingFileHandler), (
        f"expected RotatingFileHandler, got {type(handler).__name__}"
    )
    assert handler.maxBytes > 0
    assert handler.backupCount > 0


def test_set_log_enabled_disables_all_child_loggers(monkeypatch, tmp_path):
    diag = _reload_diagnostics(monkeypatch, tmp_path)
    diag._root_configured = False

    child = diag.get_logger("drivepulse_app.child")
    assert child.getEffectiveLevel() == logging.INFO

    diag.set_log_enabled(False)
    assert child.getEffectiveLevel() == logging.CRITICAL

    diag.set_log_enabled(True)
    assert child.getEffectiveLevel() == logging.INFO


def test_append_jsonl_writes_one_line_per_call(tmp_path):
    """Each payload becomes one JSON line. The file is created on demand."""
    from drivepulse_app.diagnostics import append_jsonl

    target = tmp_path / "stream.jsonl"
    append_jsonl(target, {"a": 1})
    append_jsonl(target, {"b": "two", "umlaut": "ö"})

    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines == ['{"a": 1}', '{"b": "two", "umlaut": "ö"}']


def test_append_jsonl_rotates_when_max_bytes_exceeded(tmp_path):
    """Regression: obd-log.jsonl used to grow without bound at ~2 Hz.
    append_jsonl must rename the live file aside once it crosses the size
    threshold and start a fresh one for the next payload."""
    from drivepulse_app.diagnostics import append_jsonl

    target = tmp_path / "stream.jsonl"
    payload = {"speed": 100, "rpm": 2500}  # serializes to 25 bytes incl. newline
    append_jsonl(target, payload, max_bytes=60, backup_count=2)
    append_jsonl(target, payload, max_bytes=60, backup_count=2)
    # Two writes (~50 bytes) fit under the 60-byte cap; no rotation yet.
    assert target.exists()
    assert not target.with_suffix(".jsonl.1").exists()

    # Third write pushes total over 60 bytes → rotation, then fresh write.
    append_jsonl(target, payload, max_bytes=60, backup_count=2)
    rotated = target.with_suffix(".jsonl.1")
    assert rotated.exists(), "live file should have been moved aside on rotation"
    assert target.exists(), "a fresh live file should exist for the new write"
    # The new live file holds only the most recent payload.
    new_lines = target.read_text(encoding="utf-8").splitlines()
    assert len(new_lines) == 1


def test_atomic_write_text_replaces_existing_file(tmp_path):
    from drivepulse_app.diagnostics import atomic_write_text

    target = tmp_path / "settings.json"
    target.write_text("original", encoding="utf-8")

    atomic_write_text(target, "replaced")

    assert target.read_text(encoding="utf-8") == "replaced"
    # Tempfile must not be left behind on success.
    assert not target.with_name(target.name + ".tmp").exists()


def test_atomic_write_text_leaves_original_intact_on_failure(monkeypatch, tmp_path):
    """Regression: a crash mid-write previously truncated the file. The
    atomic helper writes to a sibling temp and renames, so if the write
    raises the original is untouched and the temp is cleaned up."""
    import os

    from drivepulse_app.diagnostics import atomic_write_text

    target = tmp_path / "paired_devices.json"
    target.write_text("important original content", encoding="utf-8")

    real_replace = os.replace

    def fail_replace(*_args, **_kwargs):
        raise OSError("simulated crash")

    monkeypatch.setattr(os, "replace", fail_replace)

    try:
        atomic_write_text(target, "new content that should never land")
    except OSError:
        pass

    assert target.read_text(encoding="utf-8") == "important original content"
    assert not target.with_name(target.name + ".tmp").exists()

    # Restore and confirm the helper still works normally.
    monkeypatch.setattr(os, "replace", real_replace)
    atomic_write_text(target, "new content")
    assert target.read_text(encoding="utf-8") == "new content"


def test_atomic_write_text_creates_parent_directory(tmp_path):
    from drivepulse_app.diagnostics import atomic_write_text

    target = tmp_path / "nested" / "deeper" / "file.txt"
    atomic_write_text(target, "hi")
    assert target.read_text(encoding="utf-8") == "hi"


def test_atomic_write_text_applies_mode_before_rename(tmp_path):
    """Files holding secrets (API keys, paired-device fingerprints) must
    not be world-readable for any window. mode=0o600 is applied to the
    temp file before os.replace, so the live file is never visible to
    other users on the system."""
    import os
    import stat

    from drivepulse_app.diagnostics import atomic_write_text

    target = tmp_path / "secret.json"
    atomic_write_text(target, '{"api_key": "hunter2"}', mode=0o600)

    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"


def test_atomic_write_text_default_mode_unchanged(tmp_path):
    """No mode= argument should not chmod the file."""
    import os
    import stat

    from drivepulse_app.diagnostics import atomic_write_text

    target = tmp_path / "public.txt"
    atomic_write_text(target, "hello")
    mode = stat.S_IMODE(os.stat(target).st_mode)
    # We don't pin the exact umask-derived value; just verify chmod did NOT
    # restrict to 0o600 (which the secret-write path would).
    assert mode != 0o600


def test_append_jsonl_drops_oldest_backup_at_backup_count(tmp_path):
    """Once foo.jsonl.N exists, the next rotation must overwrite it
    rather than letting backups accumulate forever."""
    from drivepulse_app.diagnostics import append_jsonl

    target = tmp_path / "stream.jsonl"
    payload = {"x": "y" * 30}
    # Force rotations: cap=50 means almost every write rotates.
    for _ in range(6):
        append_jsonl(target, payload, max_bytes=50, backup_count=2)

    assert target.exists()
    assert target.with_suffix(".jsonl.1").exists()
    assert target.with_suffix(".jsonl.2").exists()
    # Backup count is 2 — there must not be a .3.
    assert not target.with_suffix(".jsonl.3").exists()
