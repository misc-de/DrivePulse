"""Regression tests for CarsPage list-select button visibility (Fahrten/Scans/Stopwatch).

History of breakage that these tests guard against:
- Trash button invisible after entering select mode (pending GLib idle reset).
- Trash button stuck visible + non-functional after _reset_detail_state cleared
  select flags without calling _update_list_select_buttons.
"""
from __future__ import annotations

import pytest


def _make_page(drivepulse_module):
    """Minimal CarsPage stub with list-select and related button attributes."""
    from drivepulse_app.cars.page import CarsPage

    page = CarsPage.__new__(CarsPage)
    Btn = drivepulse_module.Gtk.Button

    page._list_select_trash_btn = Btn()
    page._list_select_share_btn = Btn()
    page._photo_select_trash_btn = Btn()
    page._photo_select_share_btn = Btn()
    page._detail_trash_btn = Btn()
    page._detail_share_btn = Btn()
    page._detail_trash_handler = None
    page._rename_btn = Btn()
    page._vin_refresh_btn = Btn()
    page._detail_merge_btn = Btn()
    page._photo_upload_btn = Btn()

    page._trip_select_mode = False
    page._trip_selected_ids = set()
    page._scan_select_mode = False
    page._scan_selected_ids = set()
    page._run_select_mode = False
    page._run_selected_ids = set()
    page._photo_select_mode = False
    page._photo_selected_ids = set()
    page._photo_detail_page = None
    page._detail_pushed = False
    page._selected_scan_id = None
    page._scan_pid_stats = {}
    page._has_vin = False
    page._is_real_car = False

    page.mock_mode = False
    page._is_sync_active = lambda: False
    page._update_photo_upload_btn_visibility = lambda: None
    page._update_photo_select_buttons = lambda: None
    page._restoring_state = False
    page.on_state_changed = None

    return page


# ------------------------------------------------------------------ unit: _update_list_select_buttons


def test_buttons_hidden_when_no_mode_active(drivepulse_module):
    from drivepulse_app.cars.page import CarsPage

    page = _make_page(drivepulse_module)
    page._list_select_trash_btn.set_visible(True)
    page._list_select_share_btn.set_visible(True)

    CarsPage._update_list_select_buttons(page)

    assert page._list_select_trash_btn.get_visible() is False
    assert page._list_select_share_btn.get_visible() is False


def test_trash_visible_when_trip_mode_active(drivepulse_module):
    from drivepulse_app.cars.page import CarsPage

    page = _make_page(drivepulse_module)
    page._trip_select_mode = True

    CarsPage._update_list_select_buttons(page)

    assert page._list_select_trash_btn.get_visible() is True


def test_trash_visible_when_scan_mode_active(drivepulse_module):
    from drivepulse_app.cars.page import CarsPage

    page = _make_page(drivepulse_module)
    page._scan_select_mode = True

    CarsPage._update_list_select_buttons(page)

    assert page._list_select_trash_btn.get_visible() is True


def test_trash_visible_when_run_mode_active(drivepulse_module):
    from drivepulse_app.cars.page import CarsPage

    page = _make_page(drivepulse_module)
    page._run_select_mode = True

    CarsPage._update_list_select_buttons(page)

    assert page._list_select_trash_btn.get_visible() is True


def test_trash_hidden_in_mock_mode_even_when_trip_mode_active(drivepulse_module):
    from drivepulse_app.cars.page import CarsPage

    page = _make_page(drivepulse_module)
    page._trip_select_mode = True
    page.mock_mode = True

    CarsPage._update_list_select_buttons(page)

    assert page._list_select_trash_btn.get_visible() is False


def test_share_visible_when_mode_active_and_sync_active(drivepulse_module):
    from drivepulse_app.cars.page import CarsPage

    page = _make_page(drivepulse_module)
    page._trip_select_mode = True
    page._is_sync_active = lambda: True

    CarsPage._update_list_select_buttons(page)

    assert page._list_select_share_btn.get_visible() is True


def test_share_hidden_when_sync_inactive(drivepulse_module):
    from drivepulse_app.cars.page import CarsPage

    page = _make_page(drivepulse_module)
    page._trip_select_mode = True
    page._is_sync_active = lambda: False

    CarsPage._update_list_select_buttons(page)

    assert page._list_select_share_btn.get_visible() is False


# ------------------------------------------------------------------ unit: _on_list_select_trash_clicked


def test_trash_click_dispatches_to_trips(drivepulse_module):
    from drivepulse_app.cars.page import CarsPage

    page = _make_page(drivepulse_module)
    page._trip_select_mode = True
    called = []
    page._confirm_delete_selected_trips = lambda: called.append("trips")
    page._confirm_delete_selected_scans = lambda: called.append("scans")
    page._confirm_delete_selected_runs = lambda: called.append("runs")

    CarsPage._on_list_select_trash_clicked(page)

    assert called == ["trips"]


def test_trash_click_dispatches_to_scans(drivepulse_module):
    from drivepulse_app.cars.page import CarsPage

    page = _make_page(drivepulse_module)
    page._scan_select_mode = True
    called = []
    page._confirm_delete_selected_trips = lambda: called.append("trips")
    page._confirm_delete_selected_scans = lambda: called.append("scans")
    page._confirm_delete_selected_runs = lambda: called.append("runs")

    CarsPage._on_list_select_trash_clicked(page)

    assert called == ["scans"]


def test_trash_click_dispatches_to_runs(drivepulse_module):
    from drivepulse_app.cars.page import CarsPage

    page = _make_page(drivepulse_module)
    page._run_select_mode = True
    called = []
    page._confirm_delete_selected_trips = lambda: called.append("trips")
    page._confirm_delete_selected_scans = lambda: called.append("scans")
    page._confirm_delete_selected_runs = lambda: called.append("runs")

    CarsPage._on_list_select_trash_clicked(page)

    assert called == ["runs"]


def test_trash_click_does_nothing_when_no_mode_active(drivepulse_module):
    """Regression: stuck-visible trash must not fire when all modes are False."""
    from drivepulse_app.cars.page import CarsPage

    page = _make_page(drivepulse_module)
    called = []
    page._confirm_delete_selected_trips = lambda: called.append("trips")
    page._confirm_delete_selected_scans = lambda: called.append("scans")
    page._confirm_delete_selected_runs = lambda: called.append("runs")

    CarsPage._on_list_select_trash_clicked(page)

    assert called == []


# ------------------------------------------------------------------ regression: _reset_detail_state


def test_reset_detail_state_hides_list_select_buttons(drivepulse_module):
    """Regression: _reset_detail_state previously left buttons stuck visible."""
    from drivepulse_app.cars.page import CarsPage

    page = _make_page(drivepulse_module)
    page._trip_select_mode = True
    page._list_select_trash_btn.set_visible(True)

    CarsPage._reset_detail_state(page)

    assert page._trip_select_mode is False
    assert page._list_select_trash_btn.get_visible() is False


def test_reset_detail_state_clears_all_select_modes(drivepulse_module):
    from drivepulse_app.cars.page import CarsPage

    page = _make_page(drivepulse_module)
    page._trip_select_mode = True
    page._scan_select_mode = True
    page._run_select_mode = True

    CarsPage._reset_detail_state(page)

    assert page._trip_select_mode is False
    assert page._scan_select_mode is False
    assert page._run_select_mode is False


# ------------------------------------------------------------------ regression: _reapply_list_select_ui


def test_reapply_shows_buttons_when_mode_still_active(drivepulse_module):
    """Regression: idle reapply must restore buttons if they were reset mid-flight."""
    from drivepulse_app.cars.page import CarsPage

    page = _make_page(drivepulse_module)
    page._trip_select_mode = True
    page._list_select_trash_btn.set_visible(False)  # Simulates interim reset

    CarsPage._reapply_list_select_ui(page)

    assert page._list_select_trash_btn.get_visible() is True


def test_reapply_hides_buttons_when_mode_was_cleared(drivepulse_module):
    """Regression: idle reapply must clean up if mode was cleared before it fired."""
    from drivepulse_app.cars.page import CarsPage

    page = _make_page(drivepulse_module)
    page._list_select_trash_btn.set_visible(True)  # Stuck visible

    CarsPage._reapply_list_select_ui(page)

    assert page._list_select_trash_btn.get_visible() is False


# ------------------------------------------------------------------ integration: enter/exit select mode


def test_enter_trip_select_mode_shows_trash(drivepulse_module):
    from drivepulse_app.cars.trips import CarsTripsMixin

    page = _make_page(drivepulse_module)
    page._render_detail = lambda: None
    page._update_merge_btn_visibility = lambda: None

    CarsTripsMixin._enter_trip_select_mode(page, trip_id=42)

    assert page._trip_select_mode is True
    assert 42 in page._trip_selected_ids
    assert page._list_select_trash_btn.get_visible() is True


def test_exit_trip_select_mode_hides_trash(drivepulse_module):
    from drivepulse_app.cars.trips import CarsTripsMixin

    page = _make_page(drivepulse_module)
    page._trip_select_mode = True
    page._trip_selected_ids = {42}
    page._list_select_trash_btn.set_visible(True)
    page._render_detail = lambda: None
    page._update_merge_btn_visibility = lambda: None
    page._update_trash_default = lambda: None

    CarsTripsMixin._exit_trip_select_mode(page)

    assert page._trip_select_mode is False
    assert page._list_select_trash_btn.get_visible() is False


def test_enter_scan_select_mode_shows_trash(drivepulse_module):
    from drivepulse_app.cars.scans import CarsScansMixin

    page = _make_page(drivepulse_module)
    page._render_detail = lambda: None
    page._update_merge_btn_visibility = lambda: None

    CarsScansMixin._enter_scan_select_mode(page, scan_id=7)

    assert page._scan_select_mode is True
    assert page._list_select_trash_btn.get_visible() is True


def test_exit_scan_select_mode_hides_trash(drivepulse_module):
    from drivepulse_app.cars.scans import CarsScansMixin

    page = _make_page(drivepulse_module)
    page._scan_select_mode = True
    page._scan_selected_ids = {7}
    page._list_select_trash_btn.set_visible(True)
    page._render_detail = lambda: None
    page._update_merge_btn_visibility = lambda: None
    page._update_trash_default = lambda: None

    CarsScansMixin._exit_scan_select_mode(page)

    assert page._scan_select_mode is False
    assert page._list_select_trash_btn.get_visible() is False


def test_enter_run_select_mode_shows_trash(drivepulse_module):
    from drivepulse_app.cars.stopwatch_runs import CarsStopWatchRunsMixin

    page = _make_page(drivepulse_module)
    page._render_detail = lambda: None

    CarsStopWatchRunsMixin._enter_run_select_mode(page, run_id=3)

    assert page._run_select_mode is True
    assert page._list_select_trash_btn.get_visible() is True


def test_exit_run_select_mode_hides_trash(drivepulse_module):
    from drivepulse_app.cars.stopwatch_runs import CarsStopWatchRunsMixin

    page = _make_page(drivepulse_module)
    page._run_select_mode = True
    page._run_selected_ids = {3}
    page._list_select_trash_btn.set_visible(True)
    page._render_detail = lambda: None
    page._update_trash_default = lambda: None

    CarsStopWatchRunsMixin._exit_run_select_mode(page)

    assert page._run_select_mode is False
    assert page._list_select_trash_btn.get_visible() is False
