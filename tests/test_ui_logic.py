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
    assert window.view_stack.get_visible_child_name() == window.PAGE_ACCELERATION

    window._on_swipe(None, 300, 0)
    assert window.view_stack.get_visible_child_name() == window.PAGE_DASHBOARD


def test_plain_number_and_speed_conversion(drivepulse_module):
    window = drivepulse_module.DashboardWindow.__new__(drivepulse_module.DashboardWindow)
    window.units = "metric"
    window.language = "en"

    assert window._plain_number({"speed": {"value": "100"}}, "speed") == 100.0
    assert window._plain_number({"speed": "bad"}, "speed") is None
    assert window._display_speed(100) == 100

    window.units = "imperial"
    assert round(window._display_speed(100), 3) == 62.137


def test_update_from_payload_updates_gauges_and_status(drivepulse_module):
    window = drivepulse_module.DashboardWindow.__new__(drivepulse_module.DashboardWindow)
    window.units = "metric"
    window.rpm_gauge = drivepulse_module.Gauge("RPM", "rpm", 0, 7000, (1, 1, 1))
    window.speed_gauge = drivepulse_module.Gauge("Speed", "km/h", 0, 240, (1, 1, 1))
    window.temp_gauge = drivepulse_module.Gauge("Temp", "C", 40, 130, (1, 1, 1))
    window.acceleration_page = type("AccelerationSpy", (), {"update_payload": lambda self, payload, reader: None})()
    window.status_label = drivepulse_module.Gtk.Label(label="")
    window.obd_indicator = window._build_link_indicator("network-wired-symbolic", "OBD")
    window.gps_indicator = window._build_link_indicator("find-location-symbolic", "GPS")

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
    window = drivepulse_module.DashboardWindow.__new__(drivepulse_module.DashboardWindow)
    window.units = "metric"
    window.language = "en"
    window.rpm_gauge = drivepulse_module.Gauge("RPM", "rpm", 0, 7000, (1, 1, 1))
    window.speed_gauge = drivepulse_module.Gauge("Speed", "km/h", 0, 240, (1, 1, 1))
    window.temp_gauge = drivepulse_module.Gauge("Temp", "C", 40, 130, (1, 1, 1))
    window.acceleration_page = type("AccelerationSpy", (), {"update_payload": lambda self, payload, reader: None})()
    window.status_label = drivepulse_module.Gtk.Label(label="")
    window.obd_indicator = window._build_link_indicator("network-wired-symbolic", "OBD")
    window.gps_indicator = window._build_link_indicator("find-location-symbolic", "GPS")

    window._update_from_payload({"gps_speed": {"value": 42}, "source": "gps"})

    assert window.rpm_gauge.active is False
    assert window.temp_gauge.active is False
    assert window.speed_gauge.active is True
    assert window.speed_gauge.state.label == "42"
    assert "dim-label" in window.obd_indicator["box"].props["css_classes"]
    assert "success" in window.gps_indicator["box"].props["css_classes"]


def test_obd_indicator_spins_while_connecting(drivepulse_module):
    window = drivepulse_module.DashboardWindow.__new__(drivepulse_module.DashboardWindow)
    window.units = "metric"
    window.language = "en"
    window.rpm_gauge = drivepulse_module.Gauge("RPM", "rpm", 0, 7000, (1, 1, 1))
    window.speed_gauge = drivepulse_module.Gauge("Speed", "km/h", 0, 240, (1, 1, 1))
    window.temp_gauge = drivepulse_module.Gauge("Temp", "C", 40, 130, (1, 1, 1))
    window.acceleration_page = type("AccelerationSpy", (), {"update_payload": lambda self, payload, reader: None})()
    window.status_label = drivepulse_module.Gtk.Label(label="")
    window.obd_indicator = window._build_link_indicator("network-wired-symbolic", "OBD")
    window.gps_indicator = window._build_link_indicator("find-location-symbolic", "GPS")

    window._update_from_payload({"source": "status", "obd_connecting": True, "connection_status": "Connecting to OBD..."})

    assert window.obd_indicator["spinner"].props["visible"] is True
    assert window.obd_indicator["spinner"].props["spinning"] is True
    assert window.obd_indicator["image"].props["visible"] is False
