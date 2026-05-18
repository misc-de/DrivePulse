"""Persistent user settings for DrivePulse."""
from __future__ import annotations

import json
from typing import Any

from .common import LOG_DIR, SETTINGS_FILE, _detect_language, _normalize_language
from .diagnostics import get_logger


log = get_logger(__name__)


DEFAULT_SETTINGS: dict[str, Any] = {
    "units": "metric",
    "language": None,
    "mock_mode": False,
    "obd_port": None,
    "gauge_theme": "cockpit",
    "engage_threshold": 0.20,
    "theme_mode": "auto",
    "force_webkit_map": False,
}


def load_settings() -> dict[str, Any]:
    """Read settings.json and normalize invalid or missing values."""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except json.JSONDecodeError:
        log.warning("Ignoring invalid settings JSON at %s", SETTINGS_FILE)
        data = {}
    except OSError as exc:
        log.warning("Could not read settings from %s: %s", SETTINGS_FILE, exc)
        data = {}

    units = data.get("units")
    language = data.get("language") or _detect_language()
    raw_thresh = data.get("engage_threshold", DEFAULT_SETTINGS["engage_threshold"])
    try:
        engage_threshold = max(0.05, min(1.50, round(float(raw_thresh), 2)))
    except (TypeError, ValueError):
        engage_threshold = DEFAULT_SETTINGS["engage_threshold"]
    return {
        "units": units if units in {"metric", "imperial"} else "metric",
        "language": _normalize_language(language),
        "mock_mode": bool(data.get("mock_mode", DEFAULT_SETTINGS["mock_mode"])),
        "obd_port": data.get("obd_port") or None,
        "gauge_theme": data.get("gauge_theme", DEFAULT_SETTINGS["gauge_theme"]) or "cockpit",
        "engage_threshold": engage_threshold,
        "theme_mode": data.get("theme_mode", "auto") if data.get("theme_mode") in {"auto", "dark", "light"} else "auto",
        "force_webkit_map": bool(data.get("force_webkit_map", DEFAULT_SETTINGS["force_webkit_map"])),
        "sidebar_side": data.get("sidebar_side", "left") if data.get("sidebar_side") in {"left", "right"} else "left",
        "last_update_check": data.get("last_update_check") or None,
    }


def save_settings(settings: dict[str, Any]) -> None:
    """Persist normalized settings to settings.json."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(
            {
                "units": settings.get("units", "metric"),
                "language": _normalize_language(settings.get("language") or _detect_language()),
                "mock_mode": bool(settings.get("mock_mode", False)),
                "obd_port": settings.get("obd_port") or None,
                "gauge_theme": settings.get("gauge_theme", "cockpit") or "cockpit",
                "engage_threshold": float(settings.get("engage_threshold", DEFAULT_SETTINGS["engage_threshold"])),
                "theme_mode": settings.get("theme_mode", "auto") if settings.get("theme_mode") in {"auto", "dark", "light"} else "auto",
                "force_webkit_map": bool(settings.get("force_webkit_map", False)),
                "sidebar_side": settings.get("sidebar_side", "left") if settings.get("sidebar_side") in {"left", "right"} else "left",
                "last_update_check": settings.get("last_update_check") or None,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
