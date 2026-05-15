"""Persistent user settings for DrivePulse."""
from __future__ import annotations

import json
from typing import Any

from common import LOG_DIR, SETTINGS_FILE, _detect_language, _normalize_language


DEFAULT_SETTINGS: dict[str, Any] = {
    "units": "metric",
    "language": None,
    "mock_mode": False,
    "obd_port": None,
    "gauge_theme": "cockpit",
    "auto_rotate": True,
}


def load_settings() -> dict[str, Any]:
    """Read settings.json and normalize invalid or missing values."""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}

    units = data.get("units")
    language = data.get("language") or _detect_language()
    return {
        "units": units if units in {"metric", "imperial"} else "metric",
        "language": _normalize_language(language),
        "mock_mode": bool(data.get("mock_mode", DEFAULT_SETTINGS["mock_mode"])),
        "obd_port": data.get("obd_port") or None,
        "gauge_theme": data.get("gauge_theme", DEFAULT_SETTINGS["gauge_theme"]) or "cockpit",
        "auto_rotate": bool(data.get("auto_rotate", DEFAULT_SETTINGS["auto_rotate"])),
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
                "auto_rotate": bool(settings.get("auto_rotate", True)),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
