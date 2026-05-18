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
