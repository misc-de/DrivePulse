"""Update checker and installer for DrivePulse via git pull."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import NamedTuple

from .common import APP_VERSION, LOG_DIR
from .diagnostics import get_logger

log = get_logger(__name__)

_APP_DIR = Path(__file__).parent.parent


class UpdateInfo(NamedTuple):
    available: bool
    remote_version: str | None  # None when no update or unknown


def get_current_version() -> str:
    return APP_VERSION


def _git(*args: str, timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=_APP_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, r.stdout.strip()
    except Exception as exc:
        log.debug("git %s: %s", args, exc)
        return -1, ""


def _current_branch() -> str:
    _, branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    return branch or "main"


def check_for_update() -> UpdateInfo:
    """Fetch from remote and return whether the local branch is behind."""
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


def apply_update() -> bool:
    """Pull latest commits and run any pending migration scripts."""
    code, out = _git("pull", "--quiet", timeout=120)
    if code != 0:
        log.error("git pull failed: %s", out)
        return False
    _run_migrations()
    return True


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
