"""Regression tests for the OBD nearby-scan UI callbacks.

These guard the bug where ``_bt_nearby_scan_done`` (and the watchdog) bailed on
``self._closing``. ``_closing`` is set by the *main* settings page in its
"hiding" signal — which fires the moment the OBD-dongle subpage is pushed on top
of it. So on the very page that hosts the scan, ``_closing`` was always True and
every scan stayed stuck on "Scanning…" with a dead button, even though the
worker thread had finished. The callbacks must update the UI regardless of the
main page's hidden state.

The suite stubs GTK (see conftest), so we drive the mixin methods directly with
lightweight fakes for the two widgets they touch.
"""
from __future__ import annotations

from drivepulse_app.settings.bluetooth import SettingsBluetoothMixin


class _FakeExpander:
    def __init__(self) -> None:
        self.subtitle = "scanning"
        self.expanded = False
        self.rows: list[object] = []

    def set_subtitle(self, subtitle: str) -> None:
        self.subtitle = subtitle

    def set_expanded(self, value: bool) -> None:
        self.expanded = value

    def add_row(self, row: object) -> None:
        self.rows.append(row)

    def remove(self, row: object) -> None:
        self.rows.remove(row)


class _FakeBtn:
    def __init__(self) -> None:
        self.icon: str | None = None
        self.sensitive = False

    def set_icon_name(self, name: str) -> None:
        self.icon = name

    def set_sensitive(self, value: bool) -> None:
        self.sensitive = value


def _make_mixin(closing: bool) -> SettingsBluetoothMixin:
    obj = SettingsBluetoothMixin.__new__(SettingsBluetoothMixin)
    obj._closing = closing
    obj._bt_nearby_scan_active = True
    obj._bt_nearby_scan_token = 1
    obj.language = "en"
    obj._bt_nearby_rows = []
    obj._bt_nearby_expander = _FakeExpander()  # type: ignore[assignment]
    obj._bt_nearby_scan_btn = _FakeBtn()  # type: ignore[assignment]
    return obj


def test_scan_done_renders_results_even_when_main_page_marked_closing():
    # closing=True mimics the main settings page having emitted "hiding" because
    # the OBD subpage was pushed over it. The scan result must still render.
    obj = _make_mixin(closing=True)
    obj._bt_nearby_scan_done([("OBDII  (AA:BB:CC:DD:EE:FF)", "bt:AA:BB:CC:DD:EE:FF")])

    assert obj._bt_nearby_expander.subtitle == "1 device(s) found"  # not "scanning"
    assert obj._bt_nearby_expander.expanded is True                 # auto-expanded
    assert len(obj._bt_nearby_expander.rows) == 1                   # device row added
    assert obj._bt_nearby_scan_btn.icon == "view-refresh-symbolic"  # spinner cleared
    assert obj._bt_nearby_scan_btn.sensitive is True
    assert obj._bt_nearby_scan_active is False


def test_scan_done_empty_leaves_scanning_state_and_reenables_button():
    obj = _make_mixin(closing=True)
    obj._bt_nearby_scan_done([])

    assert obj._bt_nearby_expander.subtitle != "scanning"  # reset to "none found"
    assert obj._bt_nearby_expander.rows == []
    assert obj._bt_nearby_scan_btn.sensitive is True
    assert obj._bt_nearby_scan_active is False


def test_scan_watchdog_unsticks_ui_even_when_main_page_marked_closing():
    obj = _make_mixin(closing=True)
    obj._bt_nearby_scan_token = 7
    keep_going = obj._bt_nearby_scan_watchdog(7)

    assert keep_going is False
    assert obj._bt_nearby_scan_active is False
    assert obj._bt_nearby_scan_btn.icon == "view-refresh-symbolic"
    assert obj._bt_nearby_scan_btn.sensitive is True


def test_scan_watchdog_noops_when_a_newer_scan_superseded_it():
    obj = _make_mixin(closing=False)
    obj._bt_nearby_scan_token = 9  # current scan is newer than the stale watchdog
    obj._bt_nearby_scan_btn.sensitive = False
    keep_going = obj._bt_nearby_scan_watchdog(8)  # stale token

    assert keep_going is False
    assert obj._bt_nearby_scan_active is True          # untouched
    assert obj._bt_nearby_scan_btn.sensitive is False  # untouched
