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
    assert page.status_label.get_text() == "Armed. Timing starts when acceleration is detected."

    page.update_payload(_payload(speed=0), lambda payload, key: drivepulse_module.DashboardWindow._plain_number(page, payload, key))
    page.update_payload(_payload(speed=8, g=0.1), lambda payload, key: drivepulse_module.DashboardWindow._plain_number(page, payload, key))
    page.update_payload(_payload(speed=50, gps_speed=50, g=0.1), lambda payload, key: drivepulse_module.DashboardWindow._plain_number(page, payload, key))

    assert page.running is True
    assert page.results[30]["obd"] == 1.25
    assert page.results[50]["gps"] == 1.25
    assert page.result_labels[(30, "obd")].get_text() == "1.25 s"
    assert page.result_labels[(30, "best")].get_text() == "1.25 s"
    assert page.abort_button.get_visible() is True
    assert page.start_button.get_visible() is False

    page.reset_measurement()
    assert page.armed is False
    assert page.running is False
    assert page.g_label.get_text() == "G: --"
    assert page.result_labels[(30, "obd")].get_text() == "--"
    assert page.start_button.get_visible() is True
    assert page.abort_button.get_visible() is False


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
    assert page.status_label.get_text() == "Measurement complete."


def test_settings_dialog_calls_callbacks(drivepulse_module):
    unit_calls = []
    language_calls = []
    mock_calls = []
    dialog = drivepulse_module.SettingsDialog(
        None, "metric", "en", unit_calls.append, language_calls.append,
        current_mock_mode=False, on_mock_mode_changed=mock_calls.append,
    )

    dialog.unit_row.set_selected(1)
    dialog._on_unit_selected()
    dialog.language_row.set_selected(1)
    dialog._on_language_selected()
    dialog.mock_switch.set_active(True)
    dialog._on_mock_changed()

    assert unit_calls == ["imperial"]
    assert language_calls == ["de"]
    assert mock_calls == [True]


def test_load_and_save_units(drivepulse_module, tmp_log_paths):
    window = drivepulse_module.DashboardWindow.__new__(drivepulse_module.DashboardWindow)
    window.units = "imperial"
    window.language = "de"

    window._save_units()
    saved = drivepulse_module.SETTINGS_FILE.read_text(encoding="utf-8").strip()
    assert '"units": "imperial"' in saved
    assert '"language": "de"' in saved
    assert window._load_units() == "imperial"
    loaded = window._load_settings()
    assert loaded["units"] == "imperial"
    assert loaded["language"] == "de"
    assert "mock_mode" in loaded

    drivepulse_module.SETTINGS_FILE.write_text('{"units": "invalid"}', encoding="utf-8")
    assert window._load_units() == "metric"
