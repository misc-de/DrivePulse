"""systemd-User-Unit für die Hintergrund-Aufzeichnung an- und abschalten."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)

UNIT_NAME = "drivepulse-recorder.service"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def unit_path() -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config / "systemd" / "user" / UNIT_NAME


def unit_text(python: str | None = None, root: Path | None = None) -> str:
    python = python or sys.executable or "/usr/bin/python3"
    root = root or _project_root()
    return f"""[Unit]
Description=DrivePulse Hintergrund-Aufzeichnung (Fahrten bei laufendem Motor erfassen)
After=dbus.socket

[Service]
Type=simple
WorkingDirectory={root}
Environment=PYTHONPATH={root}
Environment=PYTHONUNBUFFERED=1
ExecStart={python} -m drivepulse_app.service
Restart=on-failure
RestartSec=30
Nice=10

[Install]
WantedBy=default.target
"""


def _systemctl(*args: str) -> tuple[bool, str]:
    try:
        res = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return res.returncode == 0, (res.stdout + res.stderr).strip()


def enable() -> tuple[bool, str]:
    """Unit schreiben (immer neu, falls Pfade sich geändert haben) und starten."""
    path = unit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(unit_text(), encoding="utf-8")
    except OSError as exc:
        log.exception("Could not write %s", path)
        return False, str(exc)
    ok, out = _systemctl("daemon-reload")
    if ok:
        ok, out = _systemctl("enable", "--now", UNIT_NAME)
    if ok:
        # Läuft der Dienst schon mit altem Code, nach Update neu starten
        ok, out = _systemctl("restart", UNIT_NAME)
    log.info("Background recorder enable ok=%s %s", ok, out[-200:])
    return ok, out


def disable() -> tuple[bool, str]:
    if not unit_path().exists():
        return True, ""
    ok, out = _systemctl("disable", "--now", UNIT_NAME)
    log.info("Background recorder disable ok=%s %s", ok, out[-200:])
    return ok, out
