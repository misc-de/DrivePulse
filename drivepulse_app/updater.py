"""Update checker and installer for DrivePulse (git pull or zip download)."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import NamedTuple

from .common import APP_VERSION, LOG_DIR
from .diagnostics import get_logger

log = get_logger(__name__)

_APP_DIR = Path(__file__).parent.parent
_GITHUB_REPO = "misc-de/DrivePulse"
_RAW_BASE = f"https://raw.githubusercontent.com/{_GITHUB_REPO}/main"
_ZIP_URL = f"https://github.com/{_GITHUB_REPO}/archive/refs/heads/main.zip"

# Files/dirs that must never be overwritten during a zip update
_ZIP_SKIP = {".git", "drivepulse.db"}


class UpdateInfo(NamedTuple):
    available: bool
    remote_version: str | None  # None when no update or unknown


def get_current_version() -> str:
    return APP_VERSION


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
        )
        output = r.stdout.strip() or r.stderr.strip()
        return r.returncode, output
    except Exception as exc:
        log.debug("git %s: %s", args, exc)
        return -1, ""


def _current_branch() -> str:
    _, branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    return branch or "main"


# ---------------------------------------------------------------------------
# zip / HTTP helpers
# ---------------------------------------------------------------------------

def _http_get_text(url: str, timeout: int = 15) -> str | None:
    try:
        import requests as _req
        r = _req.get(url, timeout=timeout)
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
    return UpdateInfo(True, remote_ver.strip() or None)


def _apply_git() -> bool:
    code, out = _git("pull", "--quiet", timeout=120)
    if code != 0:
        log.error("git pull failed: %s", out)
        return False
    _run_migrations()
    return True


# ---------------------------------------------------------------------------
# zip strategy
# ---------------------------------------------------------------------------

def _check_zip() -> UpdateInfo:
    remote_ver = _http_get_text(f"{_RAW_BASE}/VERSION")
    if not remote_ver:
        return UpdateInfo(False, None)
    if remote_ver == APP_VERSION:
        return UpdateInfo(False, None)
    return UpdateInfo(True, remote_ver)


def _apply_zip() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "drivepulse.zip"
        log.info("Downloading update zip…")
        if not _http_download(_ZIP_URL, zip_path):
            return False

        extract_dir = Path(tmp) / "extracted"
        extract_dir.mkdir()
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)
        except Exception as exc:
            log.error("ZIP extraction failed: %s", exc)
            return False

        # GitHub extracts to a single subdirectory (e.g. DrivePulse-main)
        subdirs = [d for d in extract_dir.iterdir() if d.is_dir()]
        if len(subdirs) != 1:
            log.error("Unexpected ZIP structure: %s", subdirs)
            return False
        src_root = subdirs[0]

        _copy_update(src_root, _APP_DIR)
        _run_migrations()
        return True


def _copy_update(src: Path, dst: Path) -> None:
    """Recursively copy src → dst, skipping entries in _ZIP_SKIP."""
    for item in src.iterdir():
        if item.name in _ZIP_SKIP:
            continue
        target = dst / item.name
        if item.is_dir():
            target.mkdir(exist_ok=True)
            _copy_update(item, target)
        else:
            shutil.copy2(item, target)


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
                ["python3", str(script)],
                cwd=_APP_DIR,
                timeout=60,
                capture_output=True,
            )
            if r.returncode == 0:
                done.add(script.name)
                log.info("Migration %s applied", script.name)
            else:
                log.warning("Migration %s failed: %s", script.name, r.stderr.decode())
        except Exception as exc:
            log.warning("Migration %s error: %s", script.name, exc)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    done_file.write_text(json.dumps(sorted(done)), encoding="utf-8")
