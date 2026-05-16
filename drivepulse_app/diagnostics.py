"""Small logging helpers for DrivePulse runtime diagnostics."""
from __future__ import annotations

import logging
import os
from pathlib import Path


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_LOG_DIR = Path(os.environ.get("OBD_LOG_DIR", Path.home() / ".local" / "state" / "drivepulse"))


def get_logger(name: str) -> logging.Logger:
    """Return a configured package logger without failing app startup."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(_LOG_DIR / "drivepulse.log", encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(handler)
    logger.propagate = False
    return logger
