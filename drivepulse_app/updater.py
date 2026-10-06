"""Update checker and installer for DrivePulse (git pull or zip download)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import NamedTuple

from drivepulse_app.common import APP_VERSION, LOG_DIR
from drivepulse_app.diagnostics import atomic_write_text, get_logger

log = get_logger(__name__)

_APP_DIR = Path(__file__).parent.parent
_GITHUB_REPO = "misc-de/DrivePulse"
_BRANCH = "main"
_API_COMMIT_URL = f"https://api.github.com/repos/{_GITHUB_REPO}/commits/{_BRANCH}"
_RAW_URL = "https://raw.githubusercontent.com/" + _GITHUB_REPO + "/{ref}/VERSION"
_ZIP_URL = "https://github.com/" + _GITHUB_REPO + "/archive/{ref}.zip"

# Top-level entries of the install dir that belong to the user, never to an
# update: they are carried over into the new tree untouched.
_ZIP_KEEP = {".git", "drivepulse.db"}
# Records which top-level entries the last zip update installed, so the next
# one can drop entries that were removed upstream instead of leaving them.
_MANIFEST_NAME = ".update-manifest.json"
# Files a downloaded tree must contain before it may replace the install.
_REQUIRED_FILES = ("VERSION", "drivepulse_app/__init__.py", "drivepulse_app/app.py")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class UpdateInfo(NamedTuple):
    available: bool
    remote_version: str | None  # None when no update or unknown


def get_current_version() -> str:
    return APP_VERSION


def _parse_version(text: str | None) -> tuple[int, ...] | None:
    """``"0.5.81"`` → ``(0, 5, 81)``; anything else → None."""
    parts = (text or "").strip().split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def _is_newer(remote: str | None, local: str = APP_VERSION) -> bool:
    """Only a strictly higher remote version counts as an update — an older
    or unparsable one (rollback, broken VERSION file) never does."""
    r, cur = _parse_version(remote), _parse_version(local)
    return r is not None and cur is not None and r > cur


def _is_git_repo() -> bool:
    return (_APP_DIR / ".git").exists()


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------

def _git(*args: str, timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=_APP_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = r.stdout.strip() or r.stderr.strip()
        return r.returncode, output
    except Exception as exc:
        log.debug("git %s: %s", args, exc)
        return -1, ""


def _current_branch() -> str:
    _, branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    return branch or _BRANCH


# ---------------------------------------------------------------------------
# zip / HTTP helpers
# ---------------------------------------------------------------------------

def _http_get_text(url: str, timeout: int = 15, headers: dict[str, str] | None = None) -> str | None:
    try:
        import requests as _req
        r = _req.get(url, timeout=timeout, headers=headers)
        r.raise_for_status()
        return r.text.strip()
    except Exception as exc:
        log.debug("HTTP GET %s: %s", url, exc)
        return None


def _http_download(url: str, dest: Path, timeout: int = 120) -> bool:
    try:
        import requests as _req
        with _req.get(url, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
        return True
    except Exception as exc:
        log.error("Download %s failed: %s", url, exc)
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_for_update() -> UpdateInfo:
    """Return whether a newer version is available."""
    if _is_git_repo():
        return _check_git()
    return _check_zip()


def apply_update() -> bool:
    """Download and apply the update."""
    if _is_git_repo():
        return _apply_git()
    return _apply_zip()


# ---------------------------------------------------------------------------
# git strategy
# ---------------------------------------------------------------------------

def _check_git() -> UpdateInfo:
    _git("fetch", "--quiet", timeout=30)
    branch = _current_branch()
    _, count_str = _git("rev-list", f"HEAD..origin/{branch}", "--count")
    try:
        behind = int(count_str) > 0
    except ValueError:
        return UpdateInfo(False, None)
    if not behind:
        return UpdateInfo(False, None)
    _, remote_ver = _git("show", f"origin/{branch}:VERSION")
    remote_ver = remote_ver.strip()
    if not _is_newer(remote_ver):
        log.info("origin/%s is ahead but not newer (%s vs %s) — no update", branch, remote_ver, APP_VERSION)
        return UpdateInfo(False, None)
    return UpdateInfo(True, remote_ver)


def _apply_git() -> bool:
    # --ff-only: never create a merge commit on the device; a diverged
    # checkout must be sorted out by hand instead of half-merged.
    code, out = _git("pull", "--ff-only", "--quiet", timeout=120)
    if code != 0:
        log.error("git pull failed: %s", out)
        return False
    _run_migrations()
    return True


# ---------------------------------------------------------------------------
# zip strategy
# ---------------------------------------------------------------------------

def _resolve_remote_sha() -> str | None:
    """Pin the update to one commit so VERSION check and download always
    refer to the same tree (the branch may move in between)."""
    sha = _http_get_text(_API_COMMIT_URL, headers={"Accept": "application/vnd.github.sha"})
    if sha and _SHA_RE.match(sha):
        return sha
    log.warning("Could not resolve %s to a commit: %r", _BRANCH, sha)
    return None


def _check_zip() -> UpdateInfo:
    sha = _resolve_remote_sha()
    if not sha:
        return UpdateInfo(False, None)
    remote_ver = _http_get_text(_RAW_URL.format(ref=sha))
    if not _is_newer(remote_ver):
        return UpdateInfo(False, None)
    return UpdateInfo(True, remote_ver)


def _apply_zip() -> bool:
    sha = _resolve_remote_sha()
    if not sha:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "drivepulse.zip"
        log.info("Downloading update %s…", sha[:12])
        if not _http_download(_ZIP_URL.format(ref=sha), zip_path):
            return False

        extract_dir = Path(tmp) / "extracted"
        extract_dir.mkdir()
        try:
            with zipfile.ZipFile(zip_path) as zf:
                if zf.testzip() is not None:
                    log.error("ZIP archive is corrupt")
                    return False
                zf.extractall(extract_dir)
        except Exception as exc:
            log.error("ZIP extraction failed: %s", exc)
            return False

        # GitHub extracts to a single subdirectory (e.g. DrivePulse-<sha>)
        subdirs = [d for d in extract_dir.iterdir() if d.is_dir()]
        if len(subdirs) != 1:
            log.error("Unexpected ZIP structure: %s", subdirs)
            return False
        src_root = subdirs[0]
        if not _validate_tree(src_root):
            return False

        try:
            _install_tree(src_root, _APP_DIR)
        except Exception:
            log.exception("Installing the update failed — previous version kept")
            return False
        _run_migrations()
        return True


def _validate_tree(root: Path) -> bool:
    """Refuse a downloaded tree that is incomplete or not actually newer."""
    missing = [f for f in _REQUIRED_FILES if not (root / f).is_file()]
    if missing:
        log.error("Update tree incomplete, missing: %s", missing)
        return False
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not _is_newer(version):
        log.error("Downloaded version %r is not newer than %s", version, APP_VERSION)
        return False
    return True


def _read_manifest(app_dir: Path) -> set[str] | None:
    try:
        return set(json.loads((app_dir / _MANIFEST_NAME).read_text(encoding="utf-8")))
    except Exception:
        return None


def _install_tree(src: Path, app_dir: Path) -> None:
    """Replace *app_dir* with *src* as one directory swap.

    The new tree is assembled next to the install (same filesystem), then
    swapped in with two renames, so an interrupted update leaves either the
    old or the new version — never a mix of both. Upstream-deleted files
    disappear with the old tree. Top-level entries the user owns (database,
    .git, anything an earlier update did not install) are moved across.
    """
    parent = app_dir.parent
    staging = parent / f".{app_dir.name}.update"
    backup = parent / f".{app_dir.name}.previous"
    for leftover in (staging, backup):
        if leftover.exists():
            shutil.rmtree(leftover)

    shutil.copytree(src, staging, symlinks=True)
    installed = sorted(p.name for p in staging.iterdir())
    previous = _read_manifest(app_dir)
    for item in app_dir.iterdir():
        if item.name == _MANIFEST_NAME or (staging / item.name).exists():
            continue
        if item.name in _ZIP_KEEP or previous is None or item.name not in previous:
            # User-owned (or unknown on the very first manifest-less update):
            # keep it. Copy rather than move so a failed swap loses nothing.
            if item.is_dir():
                shutil.copytree(item, staging / item.name, symlinks=True)
            else:
                shutil.copy2(item, staging / item.name)
    atomic_write_text(staging / _MANIFEST_NAME, json.dumps(installed))

    app_dir.rename(backup)
    try:
        staging.rename(app_dir)
    except Exception:
        backup.rename(app_dir)
        raise
    shutil.rmtree(backup, ignore_errors=True)


# ---------------------------------------------------------------------------
# migrations (shared)
# ---------------------------------------------------------------------------

def _run_migrations() -> None:
    mig_dir = _APP_DIR / "migrations"
    if not mig_dir.exists():
        return

    done_file = LOG_DIR / "migrations_done.json"
    try:
        done: set[str] = set(json.loads(done_file.read_text(encoding="utf-8")))
    except Exception:
        done = set()

    for script in sorted(mig_dir.glob("*.py")):
        if script.name in done:
            continue
        try:
            r = subprocess.run(
                [sys.executable, str(script)],
                cwd=_APP_DIR,
                timeout=60,
                capture_output=True,
                check=False,
            )
            if r.returncode == 0:
                done.add(script.name)
                log.info("Migration %s applied", script.name)
            else:
                log.warning("Migration %s failed: %s", script.name, r.stderr.decode())
        except Exception as exc:
            log.warning("Migration %s error: %s", script.name, exc)

    atomic_write_text(done_file, json.dumps(sorted(done)))
