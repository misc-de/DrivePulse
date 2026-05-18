"""Shared constants and utility helpers for DrivePulse."""
from __future__ import annotations

import os
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango  # noqa: E402

from .translations import SOURCE_LANGUAGE, SUPPORTED_LANGUAGES, TRANSLATIONS, language_name

# ---------------------------------------------------------------------------
# Paths and OBD configuration
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int_or_none(name: str) -> int | None:
    value = os.environ.get(name)
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


APP_ID = "de.cais.DrivePulse"
_VERSION_FILE = Path(__file__).parent.parent / "VERSION"
APP_VERSION: str = _VERSION_FILE.read_text(encoding="utf-8").strip() if _VERSION_FILE.exists() else "?"
LOG_DIR = Path(os.environ.get("OBD_LOG_DIR", Path.home() / ".local" / "state" / "drivepulse"))
LOG_FILE = LOG_DIR / "obd-log.jsonl"
CONNECTION_LOG_FILE = LOG_DIR / "connection-log.jsonl"
PROFILES_DIR = LOG_DIR / "profiles"
THEMES_DIR = LOG_DIR / "themes"
POLL_INTERVAL_SECONDS = _env_float("OBD_POLL_INTERVAL", 0.5)
OBD_PORT = os.environ.get("OBD_PORT")
OBD_BAUDRATE = _env_int_or_none("OBD_BAUDRATE")
OBD_TIMEOUT_SECONDS = _env_float("OBD_TIMEOUT", 2.0)
OBD_FAST = os.environ.get("OBD_FAST", "1").lower() in {"1", "true", "yes", "on"}
# Direct Bluetooth RFCOMM: comma-separated addresses, optional channel suffix
# e.g. "00:1D:A5:68:98:8A" or "00:1D:A5:68:98:8A:1" or "AA:BB:CC:DD:EE:FF:1,11:22:33:44:55:66"
OBD_BT_ADDR = os.environ.get("OBD_BT_ADDR")
# socat/TCP bridge URL passed directly to pyserial, e.g. "socket://localhost:35000"
OBD_SOCKET_URL = os.environ.get("OBD_SOCKET_URL")
SETTINGS_FILE = LOG_DIR / "settings.json"
DB_FILE = LOG_DIR / "drives.sqlite3"
SYNC_DIR = LOG_DIR / "sync"
PAIRED_DEVICES_FILE = LOG_DIR / "paired_devices.json"

# ---------------------------------------------------------------------------
# Language helpers
# ---------------------------------------------------------------------------


def _normalize_language(language: str | None) -> str:
    if not language:
        return SOURCE_LANGUAGE
    normalized = language.split(".", 1)[0].split("_", 1)[0].split("-", 1)[0].lower()
    return normalized if normalized in SUPPORTED_LANGUAGES else SOURCE_LANGUAGE


def _detect_language() -> str:
    return _normalize_language(os.environ.get("DRIVEPULSE_LANG") or os.environ.get("LANG"))


def _translate(language: str, key: str, **values: object) -> str:
    text = TRANSLATIONS.get(_normalize_language(language), {}).get(key)
    if text is None:
        text = TRANSLATIONS[SOURCE_LANGUAGE].get(key, key)
    return text.format(**values) if values else text


# ---------------------------------------------------------------------------
# GTK helpers
# ---------------------------------------------------------------------------


def _make_label_responsive(label: Gtk.Label, max_width_chars: int = 34, xalign: float = 0.0) -> Gtk.Label:
    label.set_wrap(True)
    label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    label.set_max_width_chars(max_width_chars)
    label.set_xalign(xalign)
    label.set_hexpand(True)
    return label
