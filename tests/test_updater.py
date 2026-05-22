from __future__ import annotations


def test_git_returns_stderr_when_stdout_is_empty(monkeypatch, drivepulse_module):
    import subprocess

    from drivepulse_app import updater

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, stdout="", stderr="fatal: no remote")

    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    assert updater._git("fetch") == (1, "fatal: no remote")


def test_check_for_update_treats_bad_rev_list_as_unknown(monkeypatch, drivepulse_module):
    from drivepulse_app import updater

    calls = []

    def fake_git(*args, timeout=30):
        calls.append(args)
        if args[:2] == ("rev-parse", "--abbrev-ref"):
            return 0, "main"
        if args[:1] == ("rev-list",):
            return 0, "not-a-number"
        return 0, ""

    monkeypatch.setattr(updater, "_git", fake_git)

    assert updater.check_for_update() == updater.UpdateInfo(False, None)


def test_run_migrations_uses_current_interpreter(monkeypatch, tmp_path, drivepulse_module):
    """Migrations must run under the same Python interpreter as the app,
    not the unrelated `python3` on $PATH. Regression: a Debian box where
    `python3` is 3.11 but the app runs under a 3.12 venv was running
    migrations with the wrong interpreter."""
    import subprocess
    import sys

    from drivepulse_app import updater

    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_test.py").write_text("import sys; sys.exit(0)\n", encoding="utf-8")
    log_dir = tmp_path / "state"
    log_dir.mkdir()

    monkeypatch.setattr(updater, "_APP_DIR", tmp_path)
    monkeypatch.setattr(updater, "LOG_DIR", log_dir)

    captured: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        captured.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    updater._run_migrations()

    assert len(captured) == 1
    assert captured[0][0] == sys.executable
    assert captured[0][0] != "python3"
