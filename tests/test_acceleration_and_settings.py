from __future__ import annotations


def _payload(speed: float | None = None, gps_speed: float | None = None, g: float | None = None):
    data = {}
    if speed is not None:
        data["speed"] = {"value": speed}
    if gps_speed is not None:
        data["gps_speed"] = {"value": gps_speed}
    if g is not None:
        data["acceleration_g"] = {"value": g}
    return data


def test_acceleration_start_reset_and_target_capture(monkeypatch, drivepulse_module):
    times = iter([0.0, 0.5, 1.75])
    monkeypatch.setattr(drivepulse_module.time, "monotonic", lambda: next(times))
    page = drivepulse_module.AccelerationPage()

    page.start_measurement()
    assert page.armed is True
    assert page.running is False
    assert page.status_label.get_text() == "Scharf. Zeit startet bei erkannter Beschleunigung."

    page.update_payload(_payload(speed=0), lambda payload, key: drivepulse_module.DashboardWindow._plain_number(page, payload, key))
    page.update_payload(_payload(speed=8, g=0.1), lambda payload, key: drivepulse_module.DashboardWindow._plain_number(page, payload, key))
    page.update_payload(_payload(speed=50, gps_speed=50, g=0.1), lambda payload, key: drivepulse_module.DashboardWindow._plain_number(page, payload, key))

    assert page.running is True
    assert page.results[30]["obd"] == 1.25
    assert page.results[50]["gps"] == 1.25
    assert page.result_labels[(30, "obd")].get_text() == "1.25 s"

    page.reset_measurement()
    assert page.armed is False
    assert page.running is False
    assert page.g_label.get_text() == "G: --"
    assert page.result_labels[(30, "obd")].get_text() == "--"


def test_acceleration_finishes_when_all_targets_have_a_source(monkeypatch, drivepulse_module):
    times = iter([0.0, 0.5, 2.0])
    monkeypatch.setattr(drivepulse_module.time, "monotonic", lambda: next(times))
    page = drivepulse_module.AccelerationPage()
    page.start_measurement()

    page.update_payload(_payload(speed=0), lambda payload, key: drivepulse_module.DashboardWindow._plain_number(page, payload, key))
    page.update_payload(_payload(speed=5, g=0.1), lambda payload, key: drivepulse_module.DashboardWindow._plain_number(page, payload, key))
    page.update_payload(_payload(speed=220, gps_speed=220), lambda payload, key: drivepulse_module.DashboardWindow._plain_number(page, payload, key))

    assert page.running is False
    assert page.armed is False
    assert page.status_label.get_text() == "Messung abgeschlossen."


def test_settings_dialog_calls_units_callback(drivepulse_module):
    calls = []
    dialog = drivepulse_module.SettingsDialog(None, "metric", calls.append)

    dialog.unit_row.set_selected(1)
    dialog._on_unit_selected()

    assert calls == ["imperial"]


def test_load_and_save_units(drivepulse_module, tmp_log_paths):
    window = drivepulse_module.DashboardWindow.__new__(drivepulse_module.DashboardWindow)
    window.units = "imperial"

    window._save_units()
    assert drivepulse_module.SETTINGS_FILE.read_text(encoding="utf-8").strip() == '{\n  "units": "imperial"\n}'
    assert window._load_units() == "imperial"

    drivepulse_module.SETTINGS_FILE.write_text('{"units": "invalid"}', encoding="utf-8")
    assert window._load_units() == "metric"
