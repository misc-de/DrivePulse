"""Shared constants, translations and utility helpers for DrivePulse."""
from __future__ import annotations

import os
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango  # noqa: E402

# ---------------------------------------------------------------------------
# Paths and OBD configuration
# ---------------------------------------------------------------------------

APP_ID = "de.cais.DrivePulse"
LOG_DIR = Path(os.environ.get("OBD_LOG_DIR", Path.home() / ".local" / "state" / "drivepulse"))
LOG_FILE = LOG_DIR / "obd-log.jsonl"
CONNECTION_LOG_FILE = LOG_DIR / "connection-log.jsonl"
PROFILES_DIR = LOG_DIR / "profiles"
THEMES_DIR = LOG_DIR / "themes"
POLL_INTERVAL_SECONDS = float(os.environ.get("OBD_POLL_INTERVAL", "0.5"))
OBD_PORT = os.environ.get("OBD_PORT")
OBD_BAUDRATE = int(os.environ["OBD_BAUDRATE"]) if os.environ.get("OBD_BAUDRATE") else None
OBD_TIMEOUT_SECONDS = float(os.environ.get("OBD_TIMEOUT", "2.0"))
OBD_FAST = os.environ.get("OBD_FAST", "0").lower() in {"1", "true", "yes", "on"}
# Direct Bluetooth RFCOMM: comma-separated addresses, optional channel suffix
# e.g. "00:1D:A5:68:98:8A" or "00:1D:A5:68:98:8A:1" or "AA:BB:CC:DD:EE:FF:1,11:22:33:44:55:66"
OBD_BT_ADDR = os.environ.get("OBD_BT_ADDR")
# socat/TCP bridge URL passed directly to pyserial, e.g. "socket://localhost:35000"
OBD_SOCKET_URL = os.environ.get("OBD_SOCKET_URL")
SETTINGS_FILE = LOG_DIR / "settings.json"

# ---------------------------------------------------------------------------
# Translations
# ---------------------------------------------------------------------------

SOURCE_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ("en", "de")
TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "acceleration.title": "Acceleration",
        "acceleration.ready": "Ready. Press Start and accelerate.",
        "acceleration.armed": "Armed. Timing starts when acceleration is detected.",
        "acceleration.running": "Measurement running...",
        "acceleration.done": "Measurement complete.",
        "acceleration.g": "G: {value}",
        "acceleration.g.empty": "G: --",
        "acceleration.start": "Start",
        "acceleration.abort": "Abort",
        "acceleration.reset": "Reset",
        "acceleration.best": "Best",
        "acceleration.obd": "OBD",
        "acceleration.gps": "GPS",
        "settings.title": "Settings",
        "settings.display": "Display",
        "settings.units": "Units",
        "settings.speed": "Speed",
        "settings.metric": "Metric (km/h)",
        "settings.imperial": "Imperial (mph)",
        "settings.language": "Language",
        "settings.language.en": "English",
        "settings.language.de": "Deutsch",
        "settings.mock_mode": "Mock Mode",
        "settings.mock_mode.subtitle": "Simulate OBD and GPS data without hardware",
        "settings.obd": "OBD",
        "settings.obd_dongle": "Dongle",
        "settings.obd_dongle.auto": "Auto-detect",
        "settings.obd_dongle.none_found": "No dongle found",
        "settings.gauge_theme": "Gauge Style",
        "settings.gauge_theme.cockpit": "Cockpit",
        "settings.gauge_theme.neon": "Neon",
        "settings.gauge_theme.minimal": "Minimal",
        "settings.gauge_theme.digital": "Digital",
        "settings.gauge_theme.sport": "Sport",
        "settings.gauge_theme.racing": "Racing",
        "settings.gauge_theme.analog": "Analog",
        "gauge.rpm": "RPM",
        "gauge.speed": "Speed",
        "gauge.coolant": "Coolant",
        "status.connecting": "Connecting...",
        "status.obd": "OBD",
        "status.gps": "GPS",

        "status.updated": "{status} | last update: {time}",
        "nav.gauges": "Gauges",
        "nav.acceleration": "Acceleration",
        "window.title": "DrivePulse",
        "settings.tooltip": "Settings",
    },
    "de": {
        "acceleration.title": "Beschleunigung",
        "acceleration.ready": "Bereit. Start drücken und losfahren.",
        "acceleration.armed": "Scharf. Zeit startet bei erkannter Beschleunigung.",
        "acceleration.running": "Messung läuft...",
        "acceleration.done": "Messung abgeschlossen.",
        "acceleration.g": "G: {value}",
        "acceleration.g.empty": "G: --",
        "acceleration.start": "Start",
        "acceleration.abort": "Abbruch",
        "acceleration.reset": "Reset",
        "acceleration.best": "Bestzeit",
        "acceleration.obd": "OBD",
        "acceleration.gps": "GPS",
        "settings.title": "Einstellungen",
        "settings.display": "Anzeige",
        "settings.units": "Einheiten",
        "settings.speed": "Geschwindigkeit",
        "settings.metric": "Metrisch (km/h)",
        "settings.imperial": "Imperial (mph)",
        "settings.language": "Sprache",
        "settings.language.en": "English",
        "settings.language.de": "Deutsch",
        "settings.mock_mode": "Mock-Modus",
        "settings.mock_mode.subtitle": "OBD- und GPS-Daten ohne Hardware simulieren",
        "settings.obd": "OBD",
        "settings.obd_dongle": "Dongle",
        "settings.obd_dongle.auto": "Automatisch erkennen",
        "settings.obd_dongle.none_found": "Kein Dongle erkannt",
        "settings.gauge_theme": "Tacho-Design",
        "settings.gauge_theme.cockpit": "Cockpit",
        "settings.gauge_theme.neon": "Neon",
        "settings.gauge_theme.minimal": "Minimal",
        "settings.gauge_theme.digital": "Digital",
        "settings.gauge_theme.sport": "Sport",
        "settings.gauge_theme.racing": "Racing",
        "settings.gauge_theme.analog": "Analog",
        "gauge.rpm": "Drehzahl",
        "gauge.speed": "Geschwindigkeit",
        "gauge.coolant": "Kühlmittel",
        "status.connecting": "Verbinde...",
        "status.obd": "OBD",
        "status.gps": "GPS",

        "status.updated": "{status} | letzte Aktualisierung: {time}",
        "nav.gauges": "Tachos",
        "nav.acceleration": "Beschleunigung",
        "window.title": "DrivePulse",
        "settings.tooltip": "Einstellungen",
    },
}

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
