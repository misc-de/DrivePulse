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


def test_stopwatch_start_reset_and_target_capture(monkeypatch, drivepulse_module):
    # t=0.0: speed=0, no g  → armed, no trigger (no data)
    # t=0.5: speed=2, g=0.25 → _engage_since=0.5, _prestart_since=0.5; sustained=0 s → no trigger
    # t=0.8: speed=8, g=0.25 → sustained=0.3 s ≥ 0.15, speed=8 ≥ 1 → TRIGGER; start_monotonic=0.5
    # t=1.75: speed=50 → elapsed=1.25 s → records 30/50 km/h targets
    times = iter([0.0, 0.5, 0.8, 1.75])
    monkeypatch.setattr(drivepulse_module.time, "monotonic", lambda: next(times))
    page = drivepulse_module.StopWatchPage()

    page.start_measurement()
    assert page.armed is True
    assert page.running is False
    assert page.status_label.get_text() == "Armed. Timing starts when acceleration is detected."

    def rn(payload, key):
        return drivepulse_module.DashboardWindow._plain_number(page, payload, key)
    page.update_payload(_payload(speed=0), rn)
    page.update_payload(_payload(speed=2, g=0.25), rn)
    assert page.running is False  # sustained only 0 s so far
    page.update_payload(_payload(speed=8, g=0.25), rn)
    assert page.running is True   # 0.3 s sustained ≥ 0.15 s, speed gate passed
    page.update_payload(_payload(speed=50, gps_speed=50, g=0.25), rn)

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


def test_stopwatch_finishes_when_all_targets_have_a_source(monkeypatch, drivepulse_module):
    # t=0.0: speed=0           → armed, no g data
    # t=0.5: speed=5, g=0.25  → engage starts, not sustained yet
    # t=0.8: speed=10, g=0.25 → sustained 0.3 s, speed gate → TRIGGER; start_monotonic=0.5
    # t=2.0: speed=220, gps=220 → all targets hit → done
    times = iter([0.0, 0.5, 0.8, 2.0])
    monkeypatch.setattr(drivepulse_module.time, "monotonic", lambda: next(times))
    page = drivepulse_module.StopWatchPage()
    page.start_measurement()

    def rn(payload, key):
        return drivepulse_module.DashboardWindow._plain_number(page, payload, key)
    page.update_payload(_payload(speed=0), rn)
    page.update_payload(_payload(speed=5, g=0.25), rn)
    page.update_payload(_payload(speed=10, g=0.25), rn)
    page.update_payload(_payload(speed=220, gps_speed=220), rn)

    assert page.running is False
    assert page.armed is False
    assert page.status_label.get_text() == "Measurement complete."


def test_stopwatch_gforce_trigger_starts_at_sensor_rate(monkeypatch, drivepulse_module):
    # With the G-force trigger enabled, the live accelerometer (update_gforce_raw)
    # drives the start at its own sample rate — no waiting for an OBD/GPS poll and
    # no speed gate. A sustained deviation ≥ engage threshold for the confirm
    # window (0.15 s) flips the stopwatch to running.
    # t=0.0: dev=0.3 ≥ 0.20 → engage/prestart start; sustained 0 s
    # t=0.10: sustained 0.10 s < 0.15 → still not running
    # t=0.30: sustained 0.30 s ≥ 0.15 → TRIGGER; start_monotonic=0.0 (prestart)
    times = iter([0.0, 0.10, 0.30])
    monkeypatch.setattr(drivepulse_module.time, "monotonic", lambda: next(times))
    page = drivepulse_module.StopWatchPage()
    page._gforce_trigger = True
    page.start_measurement()

    page.update_gforce_raw(0.0, 0.0, 1.3)   # |(0,0,1.3)| - 1 = 0.30
    assert page.running is False
    page.update_gforce_raw(0.0, 0.0, 1.3)
    assert page.running is False
    page.update_gforce_raw(0.0, 0.0, 1.3)
    assert page.running is True
    assert page.start_monotonic == 0.0      # retroactive to the gentle push


def test_stopwatch_gforce_trigger_ignores_brief_spike(monkeypatch, drivepulse_module):
    # A short bump that drops back below the engage threshold must not start the
    # run: the confirm window resets as soon as the deviation falls off.
    times = iter([0.0, 0.10, 0.30])
    monkeypatch.setattr(drivepulse_module.time, "monotonic", lambda: next(times))
    page = drivepulse_module.StopWatchPage()
    page._gforce_trigger = True
    page.start_measurement()

    page.update_gforce_raw(0.0, 0.0, 1.3)   # dev=0.30 → engage starts
    page.update_gforce_raw(0.0, 0.0, 1.0)   # dev=0.00 → engage window resets
    page.update_gforce_raw(0.0, 0.0, 1.3)   # dev=0.30 → restart, only 0 s sustained
    assert page.running is False


def test_stopwatch_rotation_uses_dashboard_layout_decision(drivepulse_module):
    page = drivepulse_module.StopWatchPage()

    page.set_device_rotation(90)

    assert page._layout_target_for_size(360, 780) == "portrait"
    assert page._layout_target_for_size(780, 360) == "landscape"


def test_settings_dialog_calls_callbacks(drivepulse_module):
    from drivepulse_app.settings.dialog import SettingsDialog

    unit_calls = []
    language_calls = []
    mock_calls = []
    webkit_calls = []
    dialog = SettingsDialog(
        None, "metric", "en", unit_calls.append, language_calls.append,
        current_mock_mode=False, on_mock_mode_changed=mock_calls.append,
        current_force_webkit_map=False, on_force_webkit_map_changed=webkit_calls.append,
    )

    dialog.unit_row.set_selected(1)
    dialog._on_unit_selected()
    dialog.language_row.set_selected(1)
    dialog._on_language_selected()
    dialog.mock_switch.set_active(True)
    dialog._on_mock_changed()
    dialog.force_webkit_map_switch.set_active(True)
    dialog._on_force_webkit_map_changed()

    assert unit_calls == ["imperial"]
    assert language_calls == ["de"]
    assert mock_calls == [True]
    assert webkit_calls == [True]


def test_settings_dialog_callbacks_fallback_for_out_of_range_indices(drivepulse_module):
    from drivepulse_app.settings.dialog import SettingsDialog

    language_calls = []
    obd_calls = []
    gauge_calls = []
    theme_mode_calls = []
    rotation_calls = []
    dialog = SettingsDialog(
        None, "metric", "en", lambda _units: None, language_calls.append,
        on_obd_port_changed=obd_calls.append,
        on_gauge_theme_changed=gauge_calls.append,
        current_theme_mode="auto", on_theme_mode_changed=theme_mode_calls.append,
        current_rotation_mode="follow_sensor", on_rotation_mode_changed=rotation_calls.append,
    )

    for invalid_index in (999, -1):
        dialog.language_row.set_selected(invalid_index)
        dialog._on_language_selected()
        dialog.dongle_row.set_selected(invalid_index)
        dialog._on_dongle_selected()
        dialog.gauge_theme_row.set_selected(invalid_index)
        dialog._on_gauge_theme_selected()
        dialog.theme_mode_row.set_selected(invalid_index)
        dialog._on_theme_mode_selected()
        dialog.rotation_mode_row.set_selected(invalid_index)
        dialog._on_rotation_mode_selected()

    assert language_calls == ["en", "en"]
    assert obd_calls == [None, None]
    assert gauge_calls == ["cockpit", "cockpit"]
    assert theme_mode_calls == ["auto", "auto"]
    assert rotation_calls == ["follow_sensor", "follow_sensor"]


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
    assert loaded["force_webkit_map"] is False

    drivepulse_module.SETTINGS_FILE.write_text('{"units": "invalid"}', encoding="utf-8")
    assert window._load_units() == "metric"
