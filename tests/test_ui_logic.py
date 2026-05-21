from __future__ import annotations


def test_gauge_set_value_clamps_and_handles_missing(drivepulse_module):
    gauge = drivepulse_module.Gauge("Speed", "km/h", 0, 100, (1, 1, 1))

    gauge.set_value(150)
    assert gauge.state.value == 100
    assert gauge.state.label == "150"

    gauge.set_value(None)
    assert gauge.state.value == 0
    assert gauge.state.label == "--"


def test_make_label_responsive_sets_wrap_properties(drivepulse_module):
    label = drivepulse_module.Gtk.Label(label="long text")

    result = drivepulse_module._make_label_responsive(label, 12, 0.5)

    assert result is label
    assert label.props["wrap"] is True
    assert label.props["wrap_mode"].name == "word-char"
    assert label.props["max_width_chars"] == 12
    assert label.props["xalign"] == 0.5
    assert label.props["hexpand"] is True


def test_dashboard_canvas_batches_multiple_redraws(drivepulse_module):
    canvas = drivepulse_module.DashboardCanvas("racing", "metric", "en")
    calls = []
    canvas.queue_draw = lambda: calls.append("draw")

    with canvas.batch_update():
        canvas.update_rpm(1200)
        canvas.update_speed(80)
        canvas.update_coolant(90)

    assert calls == ["draw"]


def test_builtin_themes_still_load_after_package_move(drivepulse_module):
    assert "cockpit" in drivepulse_module.GAUGE_THEMES
    assert "modern" in drivepulse_module.DASHBOARD_THEMES


def _layout_window(drivepulse_module):
    window = drivepulse_module.DashboardWindow.__new__(drivepulse_module.DashboardWindow)
    window.gauge_box = drivepulse_module.Gtk.Box()
    window.footer = drivepulse_module.Gtk.Box()
    window.footer.props["height"] = 40
    window.dashboard_page = drivepulse_module.Gtk.Box()
    window.dashboard_page.props["width"] = 600
    window.dashboard_page.props["height"] = 400
    window.view_stack = drivepulse_module.Adw.ViewStack()
    window.rpm_gauge = drivepulse_module.Gauge("RPM", "rpm", 0, 7000, (1, 1, 1))
    window.speed_gauge = drivepulse_module.Gauge("Speed", "km/h", 0, 240, (1, 1, 1))
    window.temp_gauge = drivepulse_module.Gauge("Temp", "C", 40, 130, (1, 1, 1))
    return window


def test_layout_uses_landscape_when_width_is_greater(drivepulse_module):
    window = _layout_window(drivepulse_module)

    window._on_size_changed()

    assert window.gauge_box.props["orientation"] is drivepulse_module.Gtk.Orientation.HORIZONTAL
    assert window.rpm_gauge.props["size_request"][0] <= 181


def test_layout_uses_portrait_when_height_is_greater(drivepulse_module):
    window = _layout_window(drivepulse_module)
    window.dashboard_page.props["width"] = 360
    window.dashboard_page.props["height"] = 780

    window._on_size_changed()

    assert window.gauge_box.props["orientation"] is drivepulse_module.Gtk.Orientation.VERTICAL
    assert window.rpm_gauge.props["size_request"][0] <= 230


def test_swipe_changes_pages(drivepulse_module):
    window = drivepulse_module.DashboardWindow.__new__(drivepulse_module.DashboardWindow)
    window.view_stack = drivepulse_module.Adw.ViewStack()
    window.view_stack.visible_child_name = window.PAGE_DASHBOARD

    window._on_swipe(None, -300, 0)
    assert window.view_stack.get_visible_child_name() == window.PAGE_STOPWATCH

    window._on_swipe(None, 300, 0)
    assert window.view_stack.get_visible_child_name() == window.PAGE_DASHBOARD


def test_plain_number_and_speed_conversion(drivepulse_module):
    from drivepulse_app.telemetry_utils import display_speed, plain_number

    window = drivepulse_module.DashboardWindow.__new__(drivepulse_module.DashboardWindow)
    window.units = "metric"
    window.language = "en"

    assert plain_number({"speed": {"value": "100"}}, "speed") == 100.0
    assert plain_number({"speed": "bad"}, "speed") is None
    assert display_speed(100, "metric") == 100
    assert window._plain_number({"speed": {"value": "100"}}, "speed") == 100.0
    assert window._display_speed(100) == 100

    window.units = "imperial"
    assert round(display_speed(100, "imperial"), 3) == 62.137
    assert round(window._display_speed(100), 3) == 62.137


def _payload_window(drivepulse_module):
    window = drivepulse_module.DashboardWindow.__new__(drivepulse_module.DashboardWindow)
    window.units = "metric"
    window.language = "en"
    window.rpm_gauge = drivepulse_module.Gauge("RPM", "rpm", 0, 7000, (1, 1, 1))
    window.speed_gauge = drivepulse_module.Gauge("Speed", "km/h", 0, 240, (1, 1, 1))
    window.temp_gauge = drivepulse_module.Gauge("Temp", "C", 40, 130, (1, 1, 1))
    window.stopwatch_page = type("StopWatchSpy", (), {"update_payload": lambda self, payload, reader: None})()
    window.cars_page = type("CarsSpy", (), {"update_live": lambda self, payload: None})()
    window.dashboard_canvas = drivepulse_module.DashboardCanvas("racing", "metric", "en")
    window.status_label = drivepulse_module.Gtk.Label(label="")
    window.obd_indicator = window._build_link_indicator("network-wired-symbolic", "OBD")
    window.gps_indicator = window._build_link_indicator("find-location-symbolic", "GPS")
    return window


def test_update_from_payload_updates_gauges_and_status(drivepulse_module):
    window = _payload_window(drivepulse_module)

    window._update_from_payload(
        {
            "rpm": {"value": 1234},
            "speed": {"value": 88},
            "coolant_temp": {"value": 91},
            "source": "obd",
            "connection_status": "OBD verbunden: /dev/rfcomm0",
        }
    )

    assert window.rpm_gauge.state.label == "1234"
    assert window.speed_gauge.state.label == "88"
    assert window.temp_gauge.state.label == "91"
    assert "OBD verbunden: /dev/rfcomm0" in window.status_label.get_text()
    assert window.rpm_gauge.active is True
    assert "success" in window.obd_indicator["box"].props["css_classes"]


def test_update_from_payload_uses_gps_speed_when_obd_is_missing(drivepulse_module):
    window = _payload_window(drivepulse_module)

    window._update_from_payload({"gps_speed": {"value": 42}, "source": "gps"})

    assert window.rpm_gauge.active is False
    assert window.temp_gauge.active is False
    assert window.speed_gauge.active is True
    assert window.speed_gauge.state.label == "42"
    assert "dim-label" in window.obd_indicator["box"].props["css_classes"]
    assert "success" in window.gps_indicator["box"].props["css_classes"]


def test_obd_indicator_spins_while_connecting(drivepulse_module):
    window = _payload_window(drivepulse_module)

    window._update_from_payload({"source": "status", "obd_connecting": True, "connection_status": "Connecting to OBD..."})

    assert window.obd_indicator["spinner"].props["visible"] is True
    assert window.obd_indicator["spinner"].props["spinning"] is True
    assert window.obd_indicator["image"].props["visible"] is False


def test_scan_identity_auto_registers_unknown_vehicle(tmp_path, drivepulse_module):
    # Unknown vehicles are immediately registered via _add_live_vehicle_from_identity.
    from drivepulse_app.db import DriveDB

    window = drivepulse_module.DashboardWindow.__new__(drivepulse_module.DashboardWindow)
    window.db = DriveDB(tmp_path / "drivepulse.sqlite3")
    calls = []
    window.trip_recorder = type("TripSpy", (), {
        "car_id": None,
        "trip_id": None,
        "set_car": lambda self, **kw: calls.append(kw) or 1,
    })()
    identities = []
    window.cars_page = type("CarsSpy", (), {
        "set_live_identity": lambda self, identity: identities.append(identity),
        "refresh_profiles": lambda self: None,
    })()
    window.dashboard_canvas = type("CanvasSpy", (), {
        "update_last_trip_stats": lambda self, stats: None,
    })()

    try:
        window._handle_scan_identity({
            "source": "obd_scan_identity",
            "vin": "WVWZZZ1JZXW000001",
            "cal_id": "CAL",
            "cvn": "CVN",
            "protocol": "6",
            "profile_path": "/tmp/profile.json",
        })

        assert len(calls) == 1
        assert calls[0]["vin"] == "WVWZZZ1JZXW000001"
        assert calls[0]["cal_id"] == "CAL"
        assert calls[0]["cvn"] == "CVN"
        assert calls[0]["profile_path"] == "/tmp/profile.json"
        assert identities[-1]["VIN"] == "WVWZZZ1JZXW000001"
        assert identities[-1]["profile_path"] == "/tmp/profile.json"
    finally:
        window.db.close()


def test_scan_identity_selects_known_vehicle(tmp_path, drivepulse_module):
    from drivepulse_app.db import DriveDB

    window = drivepulse_module.DashboardWindow.__new__(drivepulse_module.DashboardWindow)
    window.db = DriveDB(tmp_path / "drivepulse.sqlite3")
    vin = "WVWZZZ1JZXW000001"
    window.db.upsert_car(vin=vin)
    calls = []
    window.trip_recorder = type("TripSpy", (), {
        "car_id": None,
        "trip_id": None,
        "set_car": lambda self, **kw: calls.append(kw) or 1,
    })()
    window.cars_page = type("CarsSpy", (), {
        "set_live_identity": lambda self, identity: None,
    })()
    window.dashboard_canvas = type("CanvasSpy", (), {
        "update_last_trip_stats": lambda self, stats: None,
    })()

    try:
        window._handle_scan_identity({"source": "obd_scan_identity", "vin": vin})

        assert calls and calls[0]["vin"] == vin
    finally:
        window.db.close()


def test_live_vehicle_add_button_hidden_for_known_vehicle(drivepulse_module):
    from drivepulse_app.cars import CarsPage

    page = CarsPage.__new__(CarsPage)
    page.LIVE_ID = CarsPage.LIVE_ID
    page._selected_source = CarsPage.LIVE_ID
    page._live_identity = {"VIN": "WVWZZZ1JZXW000001"}
    page._profiles = []
    page._add_live_vehicle_btn = drivepulse_module.Gtk.Button()

    CarsPage._update_live_add_button(page)
    assert page._add_live_vehicle_btn.get_visible() is True

    page._profiles = [{"vin": "WVWZZZ1JZXW000001", "car_id": 1}]
    CarsPage._update_live_add_button(page)
    assert page._add_live_vehicle_btn.get_visible() is False


def test_live_vehicle_add_uses_callback_and_refreshes(drivepulse_module):
    from drivepulse_app.cars import CarsPage

    page = CarsPage.__new__(CarsPage)
    page._live_identity = {"VIN": "WVWZZZ1JZXW000001", "CALIBRATION_ID": "CAL"}
    page._selected_car_id = None
    page._add_live_vehicle_btn = drivepulse_module.Gtk.Button()
    page._profiles = []
    page._selected_source = CarsPage.LIVE_ID
    page.LIVE_ID = CarsPage.LIVE_ID
    page.db = None
    refreshed = []
    calls = []
    page.refresh_profiles = lambda: refreshed.append(True)
    page.on_live_vehicle_add = lambda identity: calls.append(identity) or 42

    CarsPage._add_live_vehicle(page)

    assert calls == [{"VIN": "WVWZZZ1JZXW000001", "CALIBRATION_ID": "CAL"}]
    assert refreshed == [True]
    assert page._selected_car_id == 42


def test_scan_widgets_tolerate_bad_numeric_counts(drivepulse_module):
    from drivepulse_app.cars_scan_widgets import _build_scan_detail_widget

    widget = _build_scan_detail_widget(
        "en",
        {
            "scanned_at": "2026-01-01T00:00:00+00:00",
            "protocol": "ISO",
            "dtc_count": "bad",
            "pending_dtc_count": None,
            "pids_count": {},
        },
        {"dtc_count": "also-bad"},
        {"dtcs": [], "pending_dtcs": [], "live_data": {}},
    )

    assert widget is not None
