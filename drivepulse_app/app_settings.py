"""Persistent user settings for DrivePulse."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from drivepulse_app.common import LOG_DIR, SETTINGS_FILE, _detect_language, _normalize_language  # noqa: F401
from drivepulse_app.diagnostics import atomic_write_text, get_logger

log = get_logger(__name__)

_DASHCAM_BASE = Path.home() / "Videos" / "DrivePulse" / "Dashcam"

DEFAULT_SETTINGS: dict[str, Any] = {
    "units": "metric",
    "language": None,
    "mock_mode": False,
    "obd_port": None,
    "gauge_theme": "cockpit",
    "engage_threshold": 0.20,
    "theme_mode": "auto",
    "force_webkit_map": False,
    "dashcam_camera": "/dev/video0",
    "dashcam_resolution": "1280x720",
    "dashcam_seg_minutes": 3,
    "dashcam_max_segments": 10,
    "dashcam_dim_timeout": 30,
    "dashcam_rolling_dir": str(_DASHCAM_BASE / "rolling"),
    "dashcam_saved_dir": str(_DASHCAM_BASE / "saved"),
    "nav_position": "auto",
    "dashcam_gps_osd": False,
    "map_traffic_visible": False,
    "map_traffic_bundesweit": True,
    "map_traffic_nrw": False,
    "map_3d_view": True,
    "map_layer": "map",
    "map_heading_up": True,
    "rotation_mode": "follow_sensor",
    "tts_enabled": True,
    "speed_limit_warn": True,
    "tts_backend": "espeak",
    "tts_language": "auto",
    "tts_voice": "female",
    "tts_quality": "high",
    "log_app_enabled": False,
    "log_obd_enabled": False,
    "obd_auto_record": True,
    "nhtsa_enabled": True,
    "vindecoder_api_key": "",
    "vindecoder_secret_key": "",
    "autodev_api_key": "",
    # Last viewed position inside the Cars tab: source path ("__live__" or
    # "car:N") and the category key ("vehicle", "trips", ...). Restored on
    # startup so the user lands where they left off.
    "last_cars_source": None,
    "last_cars_category": None,
    "last_cars_scan_id": None,
}

_VALID_ROTATION_MODES = {"follow_sensor", "follow_system"}
_VALID_TTS_BACKENDS = {"espeak", "piper"}
_VALID_TTS_LANGUAGES = {"auto", "en", "de"}
_VALID_TTS_VOICES = {"male", "female"}
_VALID_TTS_QUALITIES = {"low", "medium", "high"}
_VALID_MAP_LAYERS = {"map", "satellite", "dark", "grayscale"}


def _normalize_map_layer(value: Any) -> str:
    if isinstance(value, str) and value in _VALID_MAP_LAYERS:
        return value
    return DEFAULT_SETTINGS["map_layer"]


def _bounded_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lower, min(upper, parsed))


def _bounded_float(value: Any, default: float, lower: float, upper: float) -> float:
    try:
        parsed = round(float(value), 2)
    except (TypeError, ValueError):
        parsed = default
    return max(lower, min(upper, parsed))


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
    engage_threshold = _bounded_float(
        data.get("engage_threshold"),
        DEFAULT_SETTINGS["engage_threshold"],
        0.05,
        1.50,
    )
    return {
        "units": units if units in {"metric", "imperial"} else "metric",
        "language": _normalize_language(language),
        "mock_mode": bool(data.get("mock_mode", DEFAULT_SETTINGS["mock_mode"])),
        "obd_port": data.get("obd_port") or None,
        "gauge_theme": data.get("gauge_theme", DEFAULT_SETTINGS["gauge_theme"]) or "cockpit",
        "engage_threshold": engage_threshold,
        "theme_mode": data.get("theme_mode", "auto") if data.get("theme_mode") in {"auto", "dark", "light"} else "auto",
        "force_webkit_map": bool(data.get("force_webkit_map", DEFAULT_SETTINGS["force_webkit_map"])),
        "map_traffic_visible": bool(data.get("map_traffic_visible", DEFAULT_SETTINGS["map_traffic_visible"])),
        "map_traffic_bundesweit": bool(data.get("map_traffic_bundesweit", DEFAULT_SETTINGS["map_traffic_bundesweit"])),
        "map_traffic_nrw": bool(data.get("map_traffic_nrw", DEFAULT_SETTINGS["map_traffic_nrw"])),
        "map_3d_view": bool(data.get("map_3d_view", DEFAULT_SETTINGS["map_3d_view"])),
        "map_layer": _normalize_map_layer(data.get("map_layer")),
        "map_heading_up": bool(data.get("map_heading_up", DEFAULT_SETTINGS["map_heading_up"])),
        "sidebar_side": data.get("sidebar_side", "left") if data.get("sidebar_side") in {"left", "right"} else "left",
        "last_update_check": data.get("last_update_check") or None,
        "dashcam_camera": data.get("dashcam_camera") or DEFAULT_SETTINGS["dashcam_camera"],
        "dashcam_resolution": data.get("dashcam_resolution") or DEFAULT_SETTINGS["dashcam_resolution"],
        "dashcam_seg_minutes": _bounded_int(data.get("dashcam_seg_minutes"), 3, 1, 30),
        "dashcam_max_segments": _bounded_int(data.get("dashcam_max_segments"), 10, 2, 60),
        "dashcam_dim_timeout": _bounded_int(data.get("dashcam_dim_timeout"), 30, 0, 300),
        "dashcam_rolling_dir": data.get("dashcam_rolling_dir") or DEFAULT_SETTINGS["dashcam_rolling_dir"],
        "dashcam_saved_dir": data.get("dashcam_saved_dir") or DEFAULT_SETTINGS["dashcam_saved_dir"],
        "nav_position": data.get("nav_position", "auto") if data.get("nav_position") in {"auto", "top", "bottom", "left"} else "auto",
        "dashcam_gps_osd": bool(data.get("dashcam_gps_osd", False)),
        "rotation_mode": data.get("rotation_mode") if data.get("rotation_mode") in _VALID_ROTATION_MODES else DEFAULT_SETTINGS["rotation_mode"],
        "tts_enabled": bool(data.get("tts_enabled", DEFAULT_SETTINGS["tts_enabled"])),
        "speed_limit_warn": bool(data.get("speed_limit_warn", DEFAULT_SETTINGS["speed_limit_warn"])),
        "tts_backend": data.get("tts_backend") if data.get("tts_backend") in _VALID_TTS_BACKENDS else DEFAULT_SETTINGS["tts_backend"],
        "tts_language": data.get("tts_language") if data.get("tts_language") in _VALID_TTS_LANGUAGES else DEFAULT_SETTINGS["tts_language"],
        "tts_voice": data.get("tts_voice") if data.get("tts_voice") in _VALID_TTS_VOICES else DEFAULT_SETTINGS["tts_voice"],
        "tts_quality": data.get("tts_quality") if data.get("tts_quality") in _VALID_TTS_QUALITIES else DEFAULT_SETTINGS["tts_quality"],
        "log_app_enabled": bool(data.get("log_app_enabled", DEFAULT_SETTINGS["log_app_enabled"])),
        "log_obd_enabled": bool(data.get("log_obd_enabled", DEFAULT_SETTINGS["log_obd_enabled"])),
        "obd_auto_record": bool(data.get("obd_auto_record", DEFAULT_SETTINGS["obd_auto_record"])),
        "nhtsa_enabled": bool(data.get("nhtsa_enabled", DEFAULT_SETTINGS["nhtsa_enabled"])),
        "vindecoder_api_key": str(data.get("vindecoder_api_key") or "").strip(),
        "vindecoder_secret_key": str(data.get("vindecoder_secret_key") or "").strip(),
        "autodev_api_key": str(data.get("autodev_api_key") or "").strip(),
        "last_cars_source": (str(data["last_cars_source"]) if data.get("last_cars_source") else None),
        "last_cars_category": (str(data["last_cars_category"]) if data.get("last_cars_category") else None),
    }


def save_settings(settings: dict[str, Any]) -> None:
    """Persist normalized settings to settings.json."""
    atomic_write_text(
        SETTINGS_FILE,
        json.dumps(
            {
                "units": settings.get("units", "metric"),
                "language": _normalize_language(settings.get("language") or _detect_language()),
                "mock_mode": bool(settings.get("mock_mode", False)),
                "obd_port": settings.get("obd_port") or None,
                "gauge_theme": settings.get("gauge_theme", "cockpit") or "cockpit",
                "engage_threshold": _bounded_float(
                    settings.get("engage_threshold"),
                    DEFAULT_SETTINGS["engage_threshold"],
                    0.05,
                    1.50,
                ),
                "theme_mode": settings.get("theme_mode", "auto") if settings.get("theme_mode") in {"auto", "dark", "light"} else "auto",
                "force_webkit_map": bool(settings.get("force_webkit_map", False)),
                "map_traffic_visible": bool(settings.get("map_traffic_visible", False)),
                "map_traffic_bundesweit": bool(settings.get("map_traffic_bundesweit", True)),
                "map_traffic_nrw": bool(settings.get("map_traffic_nrw", False)),
                "map_3d_view": bool(settings.get("map_3d_view", True)),
                "map_layer": _normalize_map_layer(settings.get("map_layer")),
                "map_heading_up": bool(settings.get("map_heading_up", True)),
                "sidebar_side": settings.get("sidebar_side", "left") if settings.get("sidebar_side") in {"left", "right"} else "left",
                "last_update_check": settings.get("last_update_check") or None,
                "dashcam_camera": settings.get("dashcam_camera") or DEFAULT_SETTINGS["dashcam_camera"],
                "dashcam_resolution": settings.get("dashcam_resolution") or DEFAULT_SETTINGS["dashcam_resolution"],
                "dashcam_seg_minutes": _bounded_int(settings.get("dashcam_seg_minutes"), 3, 1, 30),
                "dashcam_max_segments": _bounded_int(settings.get("dashcam_max_segments"), 10, 2, 60),
                "dashcam_dim_timeout": _bounded_int(settings.get("dashcam_dim_timeout"), 30, 0, 300),
                "dashcam_rolling_dir": settings.get("dashcam_rolling_dir") or DEFAULT_SETTINGS["dashcam_rolling_dir"],
                "dashcam_saved_dir": settings.get("dashcam_saved_dir") or DEFAULT_SETTINGS["dashcam_saved_dir"],
                "nav_position": settings.get("nav_position", "auto") if settings.get("nav_position") in {"auto", "top", "bottom", "left"} else "auto",
                "dashcam_gps_osd": bool(settings.get("dashcam_gps_osd", False)),
                "rotation_mode": settings.get("rotation_mode") if settings.get("rotation_mode") in _VALID_ROTATION_MODES else DEFAULT_SETTINGS["rotation_mode"],
                "tts_enabled": bool(settings.get("tts_enabled", False)),
                "speed_limit_warn": bool(settings.get("speed_limit_warn", True)),
                "tts_backend": settings.get("tts_backend") if settings.get("tts_backend") in _VALID_TTS_BACKENDS else DEFAULT_SETTINGS["tts_backend"],
                "tts_language": settings.get("tts_language") if settings.get("tts_language") in _VALID_TTS_LANGUAGES else DEFAULT_SETTINGS["tts_language"],
                "tts_voice": settings.get("tts_voice") if settings.get("tts_voice") in _VALID_TTS_VOICES else DEFAULT_SETTINGS["tts_voice"],
                "tts_quality": settings.get("tts_quality") if settings.get("tts_quality") in _VALID_TTS_QUALITIES else DEFAULT_SETTINGS["tts_quality"],
                "log_app_enabled": bool(settings.get("log_app_enabled", False)),
                "log_obd_enabled": bool(settings.get("log_obd_enabled", False)),
                "obd_auto_record": bool(settings.get("obd_auto_record", True)),
                "nhtsa_enabled": bool(settings.get("nhtsa_enabled", DEFAULT_SETTINGS["nhtsa_enabled"])),
                "vindecoder_api_key": str(settings.get("vindecoder_api_key") or "").strip(),
                "vindecoder_secret_key": str(settings.get("vindecoder_secret_key") or "").strip(),
                "autodev_api_key": str(settings.get("autodev_api_key") or "").strip(),
                "last_cars_source": (str(settings["last_cars_source"]) if settings.get("last_cars_source") else None),
                "last_cars_category": (str(settings["last_cars_category"]) if settings.get("last_cars_category") else None),
            },
            indent=2,
        ),
        mode=0o600,
    )
