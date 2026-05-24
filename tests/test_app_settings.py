"""Tests for app_settings: bounded value parsing, JSON load/save round-trip,
and rejection of out-of-spec values. Settings persist the user's entire
configuration; a regression here can wipe state or apply unsafe values."""
from __future__ import annotations

import json

import pytest

from drivepulse_app import app_settings
from drivepulse_app.app_settings import (
    DEFAULT_SETTINGS,
    _bounded_float,
    _bounded_int,
    load_settings,
    save_settings,
)

# ─── _bounded_int ─────────────────────────────────────────────────────────────

def test_bounded_int_passes_in_range():
    assert _bounded_int(5, default=3, lower=1, upper=10) == 5


def test_bounded_int_clamps_below_lower():
    assert _bounded_int(0, default=3, lower=1, upper=10) == 1


def test_bounded_int_clamps_above_upper():
    assert _bounded_int(99, default=3, lower=1, upper=10) == 10


def test_bounded_int_uses_default_on_non_numeric():
    assert _bounded_int("nope", default=3, lower=1, upper=10) == 3
    assert _bounded_int(None, default=3, lower=1, upper=10) == 3


def test_bounded_int_uses_default_when_default_in_range():
    # Default itself can be out of range — bounds still win.
    assert _bounded_int("nope", default=99, lower=1, upper=10) == 10


def test_bounded_int_accepts_numeric_string():
    assert _bounded_int("7", default=3, lower=1, upper=10) == 7


# ─── _bounded_float ──────────────────────────────────────────────────────────

def test_bounded_float_rounds_to_two_decimals():
    # The OBD engage threshold persists with 2-decimal precision so the
    # JSON round-trip is bit-stable.
    assert _bounded_float(0.123456, default=0.20, lower=0.05, upper=1.50) == 0.12


def test_bounded_float_clamps_to_bounds():
    assert _bounded_float(2.0, default=0.20, lower=0.05, upper=1.50) == 1.50
    assert _bounded_float(-0.5, default=0.20, lower=0.05, upper=1.50) == 0.05


def test_bounded_float_uses_default_on_non_numeric():
    assert _bounded_float("abc", default=0.20, lower=0.05, upper=1.50) == 0.20


# ─── load_settings / save_settings ───────────────────────────────────────────

@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """Redirect SETTINGS_FILE to a tmp path so we never touch the real one."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(app_settings, "SETTINGS_FILE", path)
    return path


def test_load_settings_returns_defaults_when_file_missing(settings_file):
    # Empty / missing file = brand-new install → app should still come up.
    s = load_settings()
    assert s["units"] == "metric"
    assert s["gauge_theme"] == DEFAULT_SETTINGS["gauge_theme"]
    assert s["theme_mode"] == "auto"


def test_load_settings_ignores_invalid_json(settings_file):
    settings_file.write_text("{ not valid json", encoding="utf-8")
    s = load_settings()
    # Falls back to defaults rather than crashing.
    assert s["units"] == "metric"


def test_load_settings_rejects_unknown_units(settings_file):
    settings_file.write_text(json.dumps({"units": "weird"}), encoding="utf-8")
    s = load_settings()
    assert s["units"] == "metric"  # unknown → fall back to metric


def test_load_settings_accepts_imperial_units(settings_file):
    settings_file.write_text(json.dumps({"units": "imperial"}), encoding="utf-8")
    s = load_settings()
    assert s["units"] == "imperial"


def test_load_settings_rejects_unknown_theme_mode(settings_file):
    settings_file.write_text(json.dumps({"theme_mode": "neon"}), encoding="utf-8")
    s = load_settings()
    assert s["theme_mode"] == "auto"


def test_load_settings_clamps_engage_threshold(settings_file):
    settings_file.write_text(json.dumps({"engage_threshold": 99.0}), encoding="utf-8")
    s = load_settings()
    assert s["engage_threshold"] == 1.50  # clamped to upper bound


def test_load_settings_rejects_unknown_tts_backend(settings_file):
    settings_file.write_text(json.dumps({"tts_backend": "speakerphone"}), encoding="utf-8")
    s = load_settings()
    assert s["tts_backend"] == DEFAULT_SETTINGS["tts_backend"]


def test_load_settings_rejects_unknown_rotation_mode(settings_file):
    settings_file.write_text(json.dumps({"rotation_mode": "spin_around"}), encoding="utf-8")
    s = load_settings()
    assert s["rotation_mode"] == DEFAULT_SETTINGS["rotation_mode"]


def test_load_settings_clamps_dashcam_seg_minutes(settings_file):
    settings_file.write_text(json.dumps({"dashcam_seg_minutes": 999}), encoding="utf-8")
    s = load_settings()
    assert s["dashcam_seg_minutes"] == 30  # upper bound


def test_save_and_load_roundtrip(settings_file):
    save_settings({
        "units": "imperial",
        "language": "de",
        "mock_mode": True,
        "obd_port": "/dev/rfcomm0",
        "gauge_theme": "neon",
        "engage_threshold": 0.42,
        "theme_mode": "dark",
        "force_webkit_map": True,
        "dashcam_camera": "/dev/video2",
        "dashcam_seg_minutes": 5,
    })
    s = load_settings()
    assert s["units"] == "imperial"
    assert s["language"] == "de"
    assert s["mock_mode"] is True
    assert s["obd_port"] == "/dev/rfcomm0"
    assert s["gauge_theme"] == "neon"
    assert s["engage_threshold"] == 0.42
    assert s["theme_mode"] == "dark"
    assert s["force_webkit_map"] is True
    assert s["dashcam_camera"] == "/dev/video2"
    assert s["dashcam_seg_minutes"] == 5


def test_save_overwrites_atomically(settings_file):
    save_settings({"units": "metric"})
    save_settings({"units": "imperial"})
    s = load_settings()
    assert s["units"] == "imperial"
    # File contains exactly one JSON object — atomic_write_text didn't append.
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data["units"] == "imperial"


def test_load_settings_blanks_invalid_vin_keys(settings_file):
    # VIN-decoder keys are persisted as strings, but None or non-strings
    # should normalise to "".
    settings_file.write_text(json.dumps({
        "vindecoder_api_key": None,
        "vindecoder_secret_key": None,
        "autodev_api_key": None,
    }), encoding="utf-8")
    s = load_settings()
    assert s["vindecoder_api_key"] == ""
    assert s["vindecoder_secret_key"] == ""
    assert s["autodev_api_key"] == ""
