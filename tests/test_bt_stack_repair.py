"""Tests for the Bluetooth inquiry-scan self-repair.

The regression these guard against: the startup check used to read a sleeping
adapter (``hciconfig hci0`` prints just ``DOWN``) as "inquiry scan disabled"
and popped a pkexec password prompt on *every* launch, even though the
persistence unit was installed and running fine. A down adapter says nothing
about the configuration — and root cannot fix it either.
"""
from __future__ import annotations

import shutil
import subprocess

from drivepulse_app.obd import bt_stack_repair as repair

_UP_WITH_ISCAN = (
    "hci0:\tType: Primary  Bus: Virtual\n"
    "\tBD Address: FE:1B:46:AA:9C:A7  ACL MTU: 1021:8  SCO MTU: 184:1\n"
    "\tUP RUNNING PSCAN ISCAN \n"
)
_UP_NO_ISCAN = (
    "hci0:\tType: Primary  Bus: Virtual\n"
    "\tBD Address: FE:1B:46:AA:9C:A7  ACL MTU: 1021:8  SCO MTU: 184:1\n"
    "\tUP RUNNING PSCAN \n"
)
_DOWN = (
    "hci0:\tType: Primary  Bus: Virtual\n"
    "\tBD Address: FE:1B:46:AA:9C:A7  ACL MTU: 1021:8  SCO MTU: 184:1\n"
    "\tDOWN \n"
)


def _mock_run(stdout: str, returncode: int = 0):
    class _R:
        def __init__(self):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode
    return lambda *_a, **_kw: _R()


def _with_hciconfig(monkeypatch, output: str) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "run", _mock_run(output))


# ─── _adapter_state ──────────────────────────────────────────────────────────

def test_adapter_state_reports_iscan_when_flag_present(monkeypatch):
    _with_hciconfig(monkeypatch, _UP_WITH_ISCAN)
    assert repair._adapter_state() == repair.ADAPTER_ISCAN


def test_adapter_state_reports_no_iscan_when_up_without_flag(monkeypatch):
    _with_hciconfig(monkeypatch, _UP_NO_ISCAN)
    assert repair._adapter_state() == repair.ADAPTER_NO_ISCAN


def test_adapter_state_reports_down_for_sleeping_adapter(monkeypatch):
    # The controller drops to Powered: no when idle; hciconfig then prints no
    # scan flags at all. That must not be mistaken for a disabled inquiry scan.
    _with_hciconfig(monkeypatch, _DOWN)
    assert repair._adapter_state() == repair.ADAPTER_DOWN


def test_adapter_state_unknown_without_hciconfig(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert repair._adapter_state() == repair.ADAPTER_UNKNOWN


def test_adapter_state_unknown_on_empty_output(monkeypatch):
    _with_hciconfig(monkeypatch, "")
    assert repair._adapter_state() == repair.ADAPTER_UNKNOWN


def test_adapter_state_unknown_when_hciconfig_times_out(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    def _boom(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="hciconfig", timeout=3)

    monkeypatch.setattr(subprocess, "run", _boom)
    assert repair._adapter_state() == repair.ADAPTER_UNKNOWN


# ─── bt_inquiry_ready ────────────────────────────────────────────────────────

def test_ready_when_persisted_and_adapter_asleep(monkeypatch):
    # The regression: persistence installed + adapter down must count as ready,
    # otherwise every launch asks for the password again.
    monkeypatch.setattr(repair, "_persisted", lambda: True)
    monkeypatch.setattr(repair, "_adapter_state", lambda: repair.ADAPTER_DOWN)
    assert repair.bt_inquiry_ready() is True


def test_ready_when_persisted_and_state_unknown(monkeypatch):
    monkeypatch.setattr(repair, "_persisted", lambda: True)
    monkeypatch.setattr(repair, "_adapter_state", lambda: repair.ADAPTER_UNKNOWN)
    assert repair.bt_inquiry_ready() is True


def test_not_ready_when_running_adapter_lacks_iscan(monkeypatch):
    monkeypatch.setattr(repair, "_persisted", lambda: True)
    monkeypatch.setattr(repair, "_adapter_state", lambda: repair.ADAPTER_NO_ISCAN)
    assert repair.bt_inquiry_ready() is False


def test_not_ready_without_persistence_even_when_iscan_is_on(monkeypatch):
    # ISCAN set by hand survives neither a reboot nor a dist-upgrade.
    monkeypatch.setattr(repair, "_persisted", lambda: False)
    monkeypatch.setattr(repair, "_adapter_state", lambda: repair.ADAPTER_ISCAN)
    assert repair.bt_inquiry_ready() is False


# ─── ensure_bt_inquiry_enabled ───────────────────────────────────────────────

def _spy_repair(monkeypatch) -> dict[str, int]:
    calls = {"pkexec": 0, "banner": 0}
    monkeypatch.setattr(repair, "_run_pkexec_repair", lambda: calls.__setitem__("pkexec", calls["pkexec"] + 1))

    def _banner(_parent, on_confirm):
        calls["banner"] += 1
        on_confirm()

    monkeypatch.setattr(repair, "_show_explanation_banner", _banner)
    return calls


def test_sleeping_adapter_never_prompts_for_password(monkeypatch):
    monkeypatch.setattr(repair, "_persisted", lambda: True)
    monkeypatch.setattr(repair, "_adapter_state", lambda: repair.ADAPTER_DOWN)
    calls = _spy_repair(monkeypatch)

    repair.ensure_bt_inquiry_enabled(parent=None, async_=False)

    assert calls == {"pkexec": 0, "banner": 0}


def test_unprivileged_repair_is_tried_before_pkexec(monkeypatch):
    monkeypatch.setattr(repair, "_persisted", lambda: True)
    monkeypatch.setattr(repair, "_adapter_state", lambda: repair.ADAPTER_NO_ISCAN)
    monkeypatch.setattr(repair, "_enable_iscan_unprivileged", lambda: True)
    calls = _spy_repair(monkeypatch)

    repair.ensure_bt_inquiry_enabled(parent=None, async_=False)

    assert calls == {"pkexec": 0, "banner": 0}


def test_pkexec_offered_when_unprivileged_repair_fails(monkeypatch):
    monkeypatch.setattr(repair, "_persisted", lambda: True)
    monkeypatch.setattr(repair, "_adapter_state", lambda: repair.ADAPTER_NO_ISCAN)
    monkeypatch.setattr(repair, "_enable_iscan_unprivileged", lambda: False)
    calls = _spy_repair(monkeypatch)

    repair.ensure_bt_inquiry_enabled(parent=None, async_=False)

    assert calls == {"pkexec": 1, "banner": 1}


def test_missing_persistence_still_offers_the_one_time_setup(monkeypatch):
    monkeypatch.setattr(repair, "_persisted", lambda: False)
    monkeypatch.setattr(repair, "_adapter_state", lambda: repair.ADAPTER_ISCAN)
    # Must not be short-circuited by the unprivileged path: the unit is what's
    # missing here, and only root can install it.
    monkeypatch.setattr(repair, "_enable_iscan_unprivileged", lambda: True)
    calls = _spy_repair(monkeypatch)

    repair.ensure_bt_inquiry_enabled(parent=None, async_=False)

    assert calls == {"pkexec": 1, "banner": 1}


def test_unprivileged_repair_clears_the_discoverable_timeout_first(monkeypatch):
    # BlueZ defaults DiscoverableTimeout to 180 s and drops ISCAN when it
    # expires — the repair would quietly come undone three minutes later.
    cmds: list[list[str]] = []
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    def _record(cmd, **_kw):
        cmds.append(cmd)
        return _mock_run("")()

    monkeypatch.setattr(subprocess, "run", _record)
    monkeypatch.setattr(repair, "_adapter_state", lambda: repair.ADAPTER_ISCAN)

    assert repair._enable_iscan_unprivileged() is True
    flat = [" ".join(c[1:]) for c in cmds]
    assert flat == ["discoverable-timeout 0", "discoverable on"]


def test_unprivileged_repair_verifies_against_the_adapter(monkeypatch):
    # bluetoothctl reports success for a queued command too, so the helper
    # must re-read the adapter instead of trusting the exit code.
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "run", _mock_run("Changing discoverable on succeeded"))
    monkeypatch.setattr(repair, "_adapter_state", lambda: repair.ADAPTER_NO_ISCAN)
    assert repair._enable_iscan_unprivileged() is False

    monkeypatch.setattr(repair, "_adapter_state", lambda: repair.ADAPTER_ISCAN)
    assert repair._enable_iscan_unprivileged() is True
