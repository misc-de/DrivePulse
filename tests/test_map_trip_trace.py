from __future__ import annotations


class _Text:
    def __init__(self) -> None:
        self.value = ""

    def set_text(self, value: str) -> None:
        self.value = value


class _Button:
    def __init__(self) -> None:
        self.sensitive = True

    def set_sensitive(self, sensitive: bool) -> None:
        self.sensitive = sensitive


def test_load_trip_as_route_waits_for_calculate_click():
    from drivepulse_app.map.page import MapPage

    page = MapPage.__new__(MapPage)
    page.language = "en"
    page._tour_paused = False
    page._tour_active = False
    page._tour_save_btn = None
    page._steps_panel = None
    page._steps_toggle_btn = None
    page._backend = "webkit"
    page._shumate_map = None
    page._entry_rows = [[object(), _Text()], [object(), _Text()]]
    page._status_lbl = _Text()
    page._js_calls = []
    page._button_modes = []

    page._clear_replay_overlays = lambda: None
    page._compute_route_progress_tables = lambda: None
    page._update_placeholders = lambda: None
    page._populate_trip_route_info = lambda *args: None
    page._set_tour_controls_visible = lambda visible: None
    page._update_left_chrome_visibility = lambda: None
    page._set_follow = lambda follow: None
    page._js = page._js_calls.append
    page._set_tour_button = page._button_modes.append
    page._push_route_to_map = lambda: (_ for _ in ()).throw(
        AssertionError("route must wait for Calculate tour")
    )

    coords = [[7.0, 50.0], [7.1, 50.1]]
    page.load_trip_as_route(coords, distance_km=12.3, duration_s=456.0, label="Trip")

    assert page._pending_trip_trace_args == (coords, "Trip", 12.3, 456.0)
    assert page._button_modes == ["calculate"]
    assert page._js_calls == ["mapClearRoute()"]


def test_failed_trip_trace_keeps_calculate_retry_available():
    from drivepulse_app.map.page import MapPage

    page = MapPage.__new__(MapPage)
    page.language = "en"
    page._tour_start_btn = _Button()
    page._button_modes = []
    page._set_tour_button = page._button_modes.append
    page.get_root = lambda: None

    coords = [[7.0, 50.0], [7.1, 50.1]]
    assert page._trip_trace_result(None, coords, "Trip", 12.3, 456.0) is False

    assert page._pending_trip_trace_args == (coords, "Trip", 12.3, 456.0)
    assert page._button_modes == ["calculate"]
    assert page._tour_start_btn.sensitive is True
