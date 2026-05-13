from __future__ import annotations


def test_python_package_status_reports_missing(monkeypatch, drivepulse_module):
    monkeypatch.setattr(drivepulse_module.util, "find_spec", lambda module_name: None)

    assert drivepulse_module._python_package_status("missing", "missing") == "fehlt"


def test_python_package_status_reports_installed_without_metadata(monkeypatch, drivepulse_module):
    monkeypatch.setattr(drivepulse_module.util, "find_spec", lambda module_name: object())

    def raise_not_found(package_name: str):
        raise drivepulse_module.metadata.PackageNotFoundError(package_name)

    monkeypatch.setattr(drivepulse_module.metadata, "version", raise_not_found)

    assert drivepulse_module._python_package_status("PyGObject", "gi") == "installiert"


def test_python_package_status_reports_version(monkeypatch, drivepulse_module):
    monkeypatch.setattr(drivepulse_module.util, "find_spec", lambda module_name: object())
    monkeypatch.setattr(drivepulse_module.metadata, "version", lambda package_name: "1.2.3")

    assert drivepulse_module._python_package_status("pyserial", "serial") == "installiert (1.2.3)"


def test_print_required_python_packages_includes_obd_config(monkeypatch, capsys, drivepulse_module):
    monkeypatch.setattr(drivepulse_module, "_python_package_status", lambda package, module: "installiert")
    monkeypatch.setattr(drivepulse_module, "OBD_PORT", "/dev/rfcomm0")
    monkeypatch.setattr(drivepulse_module, "OBD_BAUDRATE", 38400)
    monkeypatch.setattr(drivepulse_module, "OBD_TIMEOUT_SECONDS", 2.5)
    monkeypatch.setattr(drivepulse_module, "OBD_FAST", True)

    drivepulse_module._print_required_python_packages()

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
