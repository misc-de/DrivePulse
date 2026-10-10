from __future__ import annotations

import types


def test_reverse_trace_mirrors_points_and_keeps_time_gaps():
    from drivepulse_app.map.tour_reverse import reverse_trace

    coords = [[7.0, 50.0], [7.1, 50.1], [7.3, 50.2]]
    rev, ts = reverse_trace(coords, [100.0, 110.0, 160.0])

    assert rev == [[7.3, 50.2], [7.1, 50.1], [7.0, 50.0]]
    assert ts == [100.0, 150.0, 160.0]  # gaps 50 s, 10 s — mirrored, increasing
    assert reverse_trace(coords, None)[1] is None
    assert reverse_trace(coords, [1.0])[1] is None  # length mismatch → drop


class _Btn:
    def __init__(self) -> None:
        self.visible = False
        self.sensitive = True

    def set_visible(self, v: bool) -> None:
        self.visible = v

    def set_sensitive(self, v: bool) -> None:
        self.sensitive = v


class _Entry:
    def __init__(self, text: str) -> None:
        self.text = text

    def get_text(self) -> str:
        return self.text

    def set_text(self, text: str) -> None:
        self.text = text


def _page():
    from drivepulse_app.map.page import MapPage

    page = MapPage.__new__(MapPage)
    page.language = "de"
    page._tour_active = False
    page._tour_paused = False
    page._tour_start_btn = _Btn()
    page._tour_reverse_btn = _Btn()
    page._tour_abort_btn = None
    page._tour_start_lbl = None
    page._tour_btn_icon = None
    page._entry_rows = [[object(), _Entry("Köln")], [object(), _Entry("Bonn")]]
    page._update_placeholders = lambda: None
    page._loading = []
    page._set_route_loading = page._loading.append
    return page


def test_reverse_button_only_visible_for_a_calculated_unstarted_tour():
    page = _page()
    page._reverse_source = None
    page._set_tour_button("start")
    assert page._tour_reverse_btn.visible is False

    page._reverse_source = ("plan", [(50.9, 6.9), (50.7, 7.1)])
    page._set_tour_button("start")
    assert page._tour_reverse_btn.visible is True
    for mode in ("calculate", "stop", "resume"):
        page._set_tour_button(mode)
        assert page._tour_reverse_btn.visible is False


def test_reverse_plan_routes_waypoints_backwards(monkeypatch):
    from drivepulse_app.map import tour_reverse

    page = _page()
    page._reverse_source = ("plan", [(50.9, 6.9), (50.8, 7.0), (50.7, 7.1)])
    routed = []
    results = []
    monkeypatch.setattr(tour_reverse, "compute_route", lambda pts: routed.append(pts) or ("route",))
    monkeypatch.setattr(tour_reverse.threading, "Thread", lambda target, args, daemon: types.SimpleNamespace(start=lambda: target(*args)))
    monkeypatch.setattr(tour_reverse.GLib, "idle_add", lambda fn, *a: fn(*a), raising=False)
    page._route_result = lambda pts, res: results.append((pts, res)) or False

    page._on_tour_reverse_clicked(None)

    assert routed == [[(50.7, 7.1), (50.8, 7.0), (50.9, 6.9)]]
    assert results == [([(50.7, 7.1), (50.8, 7.0), (50.9, 6.9)], ("route",))]
    assert [r[1].text for r in page._entry_rows] == ["Bonn", "Köln"]
    assert page._loading == [True, False]
    assert page._tour_start_btn.sensitive is True


def test_reverse_plan_failure_restores_form(monkeypatch):
    from drivepulse_app.map import tour_reverse

    page = _page()
    page._reverse_source = ("plan", [(50.9, 6.9), (50.7, 7.1)])
    monkeypatch.setattr(tour_reverse, "compute_route", lambda pts: None)
    monkeypatch.setattr(tour_reverse.threading, "Thread", lambda target, args, daemon: types.SimpleNamespace(start=lambda: target(*args)))
    monkeypatch.setattr(tour_reverse.GLib, "idle_add", lambda fn, *a: fn(*a), raising=False)
    page._route_result = lambda pts, res: False

    page._on_tour_reverse_clicked(None)

    assert [r[1].text for r in page._entry_rows] == ["Köln", "Bonn"]


def test_reverse_trace_refetches_mirrored_trace(monkeypatch):
    from drivepulse_app.map import tour_reverse

    page = _page()
    coords = [[7.0, 50.0], [7.1, 50.1]]
    page._reverse_source = ("trace", (coords, "Fahrt", 3.2, 400.0, [10.0, 20.0]))
    fetched = []
    monkeypatch.setattr(tour_reverse.threading, "Thread", lambda target, args, daemon: types.SimpleNamespace(start=lambda: target(*args)))
    page._fetch_trip_trace = lambda *a: fetched.append(a)

    page._on_tour_reverse_clicked(None)

    assert fetched == [([[7.1, 50.1], [7.0, 50.0]], "Fahrt", 3.2, 400.0, [10.0, 20.0])]
    assert page._tour_start_btn.sensitive is False  # until the trace result arrives
