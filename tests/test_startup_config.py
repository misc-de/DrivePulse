from __future__ import annotations


def test_python_package_status_reports_missing(monkeypatch, drivepulse_module):
    from drivepulse_app import startup_info

    monkeypatch.setattr(startup_info.util, "find_spec", lambda module_name: None)

    assert startup_info.python_package_status("missing", "missing") == "fehlt"


def test_python_package_status_reports_installed_without_metadata(monkeypatch, drivepulse_module):
    from drivepulse_app import startup_info

    monkeypatch.setattr(startup_info.util, "find_spec", lambda module_name: object())

    def raise_not_found(package_name: str):
        raise startup_info.metadata.PackageNotFoundError(package_name)

    monkeypatch.setattr(startup_info.metadata, "version", raise_not_found)

    assert startup_info.python_package_status("PyGObject", "gi") == "installiert"


def test_python_package_status_reports_version(monkeypatch, drivepulse_module):
    from drivepulse_app import startup_info

    monkeypatch.setattr(startup_info.util, "find_spec", lambda module_name: object())
    monkeypatch.setattr(startup_info.metadata, "version", lambda package_name: "1.2.3")

    assert startup_info.python_package_status("pyserial", "serial") == "installiert (1.2.3)"


def test_print_required_python_packages_includes_obd_config(monkeypatch, capsys, drivepulse_module):
    from drivepulse_app import startup_info

    monkeypatch.setattr(startup_info, "python_package_status", lambda package, module: "installiert")
    monkeypatch.setattr(startup_info, "OBD_PORT", "/dev/rfcomm0")
    monkeypatch.setattr(startup_info, "OBD_BAUDRATE", 38400)
    monkeypatch.setattr(startup_info, "OBD_TIMEOUT_SECONDS", 2.5)
    monkeypatch.setattr(startup_info, "OBD_FAST", True)

    startup_info.print_required_python_packages()

    output = capsys.readouterr().out
    assert "PyGObject: installiert" in output
    assert "pyserial: installiert" in output
    assert "obd: installiert" in output
    assert "OBD_PORT: /dev/rfcomm0" in output
    assert "OBD_BAUDRATE: 38400" in output
    assert "OBD_TIMEOUT: 2.5s" in output
    assert "OBD_FAST: an" in output


def test_translate_uses_english_fallback_and_german_translation(drivepulse_module):
    assert drivepulse_module._translate("de", "settings.title") == "Einstellungen"
    assert drivepulse_module._translate("fr", "settings.title") == "Settings"
    assert drivepulse_module._translate("de", "missing.key") == "missing.key"
    assert drivepulse_module._translate("en", "status.updated", status="OBD", time="12:00") == "OBD | last update: 12:00"


def test_detect_language_uses_supported_language_or_source_fallback(monkeypatch, drivepulse_module):
    monkeypatch.setenv("DRIVEPULSE_LANG", "de_DE.UTF-8")
    assert drivepulse_module._detect_language() == "de"

    monkeypatch.setenv("DRIVEPULSE_LANG", "fr_FR.UTF-8")
    assert drivepulse_module._detect_language() == "en"


def test_common_ignores_invalid_obd_environment(monkeypatch, drivepulse_module):
    import importlib

    from drivepulse_app import common

    monkeypatch.setenv("OBD_POLL_INTERVAL", "bad")
    monkeypatch.setenv("OBD_BAUDRATE", "also-bad")
    monkeypatch.setenv("OBD_TIMEOUT", "nope")

    reloaded = importlib.reload(common)

    assert reloaded.POLL_INTERVAL_SECONDS == 0.5
    assert reloaded.OBD_BAUDRATE is None
    assert reloaded.OBD_TIMEOUT_SECONDS == 2.0


def test_load_settings_ignores_invalid_json(monkeypatch, tmp_path, drivepulse_module):
    from drivepulse_app import app_settings

    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(app_settings, "SETTINGS_FILE", settings_file)

    settings = app_settings.load_settings()

    assert settings["units"] == "metric"
    assert settings["language"] in {"en", "de"}
    assert settings["force_webkit_map"] is False


def test_load_settings_falls_back_for_invalid_numeric_values(monkeypatch, tmp_path, drivepulse_module):
    import json

    from drivepulse_app import app_settings

    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({
            "dashcam_seg_minutes": "abc",
            "dashcam_max_segments": None,
            "dashcam_dim_timeout": {},
            "engage_threshold": [],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_settings, "SETTINGS_FILE", settings_file)

    settings = app_settings.load_settings()

    assert settings["dashcam_seg_minutes"] == 3
    assert settings["dashcam_max_segments"] == 10
    assert settings["dashcam_dim_timeout"] == 30
    assert settings["engage_threshold"] == 0.20


def test_save_settings_falls_back_for_invalid_numeric_values(monkeypatch, tmp_path, drivepulse_module):
    import json

    from drivepulse_app import app_settings

    settings_file = tmp_path / "nested" / "settings.json"
    monkeypatch.setattr(app_settings, "SETTINGS_FILE", settings_file)

    app_settings.save_settings({
        "dashcam_seg_minutes": "abc",
        "dashcam_max_segments": None,
        "dashcam_dim_timeout": {},
        "engage_threshold": [],
    })

    saved = json.loads(settings_file.read_text(encoding="utf-8"))

    assert saved["dashcam_seg_minutes"] == 3
    assert saved["dashcam_max_segments"] == 10
    assert saved["dashcam_dim_timeout"] == 30
    assert saved["engage_threshold"] == 0.20
