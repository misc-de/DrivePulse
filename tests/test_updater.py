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


def test_is_newer_only_accepts_strictly_higher_versions(drivepulse_module):
    from drivepulse_app import updater

    assert updater._is_newer("0.5.10", "0.5.9")
    assert updater._is_newer("1.0.0", "0.9.99")
    assert not updater._is_newer("0.5.9", "0.5.10")
    assert not updater._is_newer("0.5.9", "0.5.9")
    assert not updater._is_newer("", "0.5.9")
    assert not updater._is_newer("404: Not Found", "0.5.9")


def test_check_git_ignores_ahead_branch_with_older_version(monkeypatch, drivepulse_module):
    from drivepulse_app import updater

    def fake_git(*args, timeout=30):
        if args[:2] == ("rev-parse", "--abbrev-ref"):
            return 0, "main"
        if args[:1] == ("rev-list",):
            return 0, "3"
        if args[:1] == ("show",):
            return 0, "0.0.1"
        return 0, ""

    monkeypatch.setattr(updater, "_git", fake_git)
    monkeypatch.setattr(updater, "_is_git_repo", lambda: True)

    assert updater.check_for_update() == updater.UpdateInfo(False, None)


def test_apply_git_never_merges(monkeypatch, drivepulse_module):
    from drivepulse_app import updater

    calls = []
    monkeypatch.setattr(updater, "_git", lambda *a, timeout=30: (calls.append(a), (1, "diverged"))[1])
    monkeypatch.setattr(updater, "_is_git_repo", lambda: True)

    assert updater.apply_update() is False
    assert "--ff-only" in calls[0]


def test_check_zip_reads_version_of_pinned_commit(monkeypatch, drivepulse_module):
    from drivepulse_app import updater

    sha = "a" * 40
    urls = []

    def fake_get(url, timeout=15, headers=None):
        urls.append(url)
        if url == updater._API_COMMIT_URL:
            return sha
        return "999.0.0"

    monkeypatch.setattr(updater, "_http_get_text", fake_get)
    monkeypatch.setattr(updater, "_is_git_repo", lambda: False)

    assert updater.check_for_update() == updater.UpdateInfo(True, "999.0.0")
    assert sha in urls[1]


def test_check_zip_rejects_garbage_sha(monkeypatch, drivepulse_module):
    from drivepulse_app import updater

    monkeypatch.setattr(updater, "_http_get_text", lambda *a, **k: "<html>rate limited</html>")
    monkeypatch.setattr(updater, "_is_git_repo", lambda: False)

    assert updater.check_for_update() == updater.UpdateInfo(False, None)


def _make_tree(root, version):
    (root / "drivepulse_app").mkdir(parents=True)
    (root / "drivepulse_app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "drivepulse_app" / "app.py").write_text("", encoding="utf-8")
    (root / "VERSION").write_text(version, encoding="utf-8")


def test_validate_tree_rejects_incomplete_or_older(tmp_path, drivepulse_module):
    from drivepulse_app import updater

    old = tmp_path / "old"
    _make_tree(old, "0.0.1")
    assert not updater._validate_tree(old)

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "VERSION").write_text("999.0.0", encoding="utf-8")
    assert not updater._validate_tree(broken)

    good = tmp_path / "good"
    _make_tree(good, "999.0.0")
    assert updater._validate_tree(good)


def test_install_tree_swaps_whole_tree_and_drops_stale_files(tmp_path, drivepulse_module):
    from drivepulse_app import updater

    app = tmp_path / "DrivePulse"
    _make_tree(app, "0.0.1")
    (app / "drivepulse_app" / "removed_upstream.py").write_text("", encoding="utf-8")
    (app / "drivepulse.db").write_text("user data", encoding="utf-8")
    (app / "my_notes.txt").write_text("mine", encoding="utf-8")

    new = tmp_path / "new"
    _make_tree(new, "999.0.0")
    (new / "old_toplevel.txt").write_text("", encoding="utf-8")

    updater._install_tree(new, app)

    assert (app / "VERSION").read_text(encoding="utf-8") == "999.0.0"
    assert not (app / "drivepulse_app" / "removed_upstream.py").exists()
    assert (app / "drivepulse.db").read_text(encoding="utf-8") == "user data"
    # No manifest yet → unknown top-level entries are treated as the user's.
    assert (app / "my_notes.txt").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["DrivePulse", "new"]

    # Second update: a top-level entry the previous update installed and
    # upstream has since deleted must go; user files still stay.
    newer = tmp_path / "newer"
    _make_tree(newer, "999.0.1")
    updater._install_tree(newer, app)

    assert not (app / "old_toplevel.txt").exists()
    assert (app / "my_notes.txt").exists()
    assert (app / "drivepulse.db").exists()


def test_install_tree_restores_old_version_when_swap_fails(monkeypatch, tmp_path, drivepulse_module):
    from pathlib import Path

    import pytest

    from drivepulse_app import updater

    app = tmp_path / "DrivePulse"
    _make_tree(app, "0.0.1")
    new = tmp_path / "new"
    _make_tree(new, "999.0.0")

    real_rename = Path.rename

    def flaky_rename(self, target):
        if self.name.endswith(".update"):
            raise OSError("disk full")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)

    with pytest.raises(OSError):
        updater._install_tree(new, app)
    assert (app / "VERSION").read_text(encoding="utf-8") == "0.0.1"
