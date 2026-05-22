"""Small logging helpers for DrivePulse runtime diagnostics."""
from __future__ import annotations

import logging
import logging.handlers
import os
import threading
from pathlib import Path


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_LOG_DIR = Path(os.environ.get("OBD_LOG_DIR", Path.home() / ".local" / "state" / "drivepulse"))
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUP_COUNT = 3
_ROOT_LOGGER_NAME = "drivepulse_app"

_root_setup_lock = threading.Lock()
_root_configured = False


def _configure_root_logger() -> None:
    """Attach a rotating file handler to the drivepulse_app parent logger once.

    Module loggers (``drivepulse_app.foo``) propagate to this parent, so a
    single file handle owns the rotation state. Without this, every
    ``get_logger`` call would attach its own handler — fine for a plain
    FileHandler but a correctness problem for RotatingFileHandler, because
    each handler would rotate independently and the others would keep
    writing to the rotated-out inode.
    """
    global _root_configured
    with _root_setup_lock:
        if _root_configured:
            return
        root = logging.getLogger(_ROOT_LOGGER_NAME)
        if not root.handlers:
            try:
                _LOG_DIR.mkdir(parents=True, exist_ok=True)
                handler: logging.Handler = logging.handlers.RotatingFileHandler(
                    _LOG_DIR / "drivepulse.log",
                    maxBytes=_LOG_MAX_BYTES,
                    backupCount=_LOG_BACKUP_COUNT,
                    encoding="utf-8",
                )
            except OSError:
                handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(_LOG_FORMAT))
            root.addHandler(handler)
        root.setLevel(logging.INFO)
        root.propagate = False
        _root_configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a child logger that propagates to the shared file handler."""
    _configure_root_logger()
    return logging.getLogger(name)


def set_log_enabled(enabled: bool) -> None:
    """Enable or disable file logging for all drivepulse_app loggers."""
    level = logging.INFO if enabled else logging.CRITICAL
    logging.getLogger(_ROOT_LOGGER_NAME).setLevel(level)
