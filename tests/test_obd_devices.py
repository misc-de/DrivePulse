"""Tests for obd_devices: BT-address parsing, paired-device scan, port-string
parsing. The pure parsers exercise the env-var schema callers depend on
(``OBD_BT_ADDR=AA:BB:CC:DD:EE:FF[:channel],...``)."""
from __future__ import annotations

import subprocess

from drivepulse_app import common
from drivepulse_app.obd import devices as obd_devices
from drivepulse_app.obd.devices import (
    candidate_bt_addresses,
    parse_bt_port,
    scan_bt_paired_devices,
)

# ─── parse_bt_port ───────────────────────────────────────────────────────────

def test_parse_bt_port_six_part_mac_uses_default_channel_one():
    addr, channel = parse_bt_port("bt:AA:BB:CC:DD:EE:FF")
    assert addr == "AA:BB:CC:DD:EE:FF"
    assert channel == 1


def test_parse_bt_port_seven_part_includes_channel():
    addr, channel = parse_bt_port("bt:AA:BB:CC:DD:EE:FF:7")
    assert addr == "AA:BB:CC:DD:EE:FF"
    assert channel == 7


def test_parse_bt_port_uppercases_mac():
    addr, _ = parse_bt_port("bt:aa:bb:cc:dd:ee:ff")
    assert addr == "AA:BB:CC:DD:EE:FF"


def test_parse_bt_port_trailing_non_numeric_treated_as_mac_part():
    # If the 7th token isn't digits, it's not a channel — keep the whole
    # tail as the address (preserves weird user input rather than dropping).
    addr, channel = parse_bt_port("bt:AA:BB:CC:DD:EE:FF:GG")
    assert "GG" in addr
    assert channel == 1


# ─── candidate_bt_addresses (env-driven) ─────────────────────────────────────

def test_candidate_bt_addresses_empty_when_no_env(monkeypatch):
    monkeypatch.setattr(common, "OBD_BT_ADDR", None)
    monkeypatch.setattr(common, "OBD_PORT", None)
    monkeypatch.setattr(obd_devices, "OBD_BT_ADDR", None)
    monkeypatch.setattr(obd_devices, "OBD_PORT", None)
    assert candidate_bt_addresses() == []


def test_candidate_bt_addresses_yields_when_port_unset_and_addr_present(monkeypatch):
    monkeypatch.setattr(obd_devices, "OBD_BT_ADDR", "AA:BB:CC:DD:EE:FF")
    monkeypatch.setattr(obd_devices, "OBD_PORT", None)
    assert candidate_bt_addresses() == [("AA:BB:CC:DD:EE:FF", 1)]


def test_candidate_bt_addresses_supports_channel_suffix(monkeypatch):
    monkeypatch.setattr(obd_devices, "OBD_BT_ADDR", "00:11:22:33:44:55:5")
    monkeypatch.setattr(obd_devices, "OBD_PORT", None)
    assert candidate_bt_addresses() == [("00:11:22:33:44:55", 5)]


def test_candidate_bt_addresses_splits_comma_list(monkeypatch):
    monkeypatch.setattr(
        obd_devices, "OBD_BT_ADDR",
        "AA:BB:CC:DD:EE:FF, 11:22:33:44:55:66:3 , 99:88:77:66:55:44",
    )
    monkeypatch.setattr(obd_devices, "OBD_PORT", None)
    out = candidate_bt_addresses()
    assert out == [
        ("AA:BB:CC:DD:EE:FF", 1),
        ("11:22:33:44:55:66", 3),
        ("99:88:77:66:55:44", 1),
    ]


def test_candidate_bt_addresses_yields_nothing_when_explicit_obd_port_set(monkeypatch):
    # Explicit OBD_PORT overrides BT discovery — callers shouldn't probe BT
    # if a serial port is pinned.
    monkeypatch.setattr(obd_devices, "OBD_BT_ADDR", "AA:BB:CC:DD:EE:FF")
    monkeypatch.setattr(obd_devices, "OBD_PORT", "/dev/ttyUSB0")
    assert candidate_bt_addresses() == []


def test_candidate_bt_addresses_skips_empty_csv_segments(monkeypatch):
    monkeypatch.setattr(obd_devices, "OBD_BT_ADDR", "AA:BB:CC:DD:EE:FF,,11:22:33:44:55:66")
    monkeypatch.setattr(obd_devices, "OBD_PORT", None)
    out = candidate_bt_addresses()
    assert len(out) == 2


# ─── scan_bt_paired_devices (subprocess mock) ────────────────────────────────

def _mock_run(stdout: str, returncode: int = 0):
    class _R:
        def __init__(self):
            self.stdout = stdout
            self.returncode = returncode
    return lambda *_a, **_kw: _R()


def test_scan_bt_paired_devices_parses_bluetoothctl_output(monkeypatch):
    sample = (
        "Device AA:BB:CC:DD:EE:FF OBDII-Adapter\n"
        "Device 11:22:33:44:55:66 ELM327\n"
    )
    monkeypatch.setattr(subprocess, "run", _mock_run(sample))
    devices = scan_bt_paired_devices()
    assert len(devices) == 2
    label_a, port_a = devices[0]
    assert "OBDII-Adapter" in label_a
    assert "AA:BB:CC:DD:EE:FF" in label_a
    assert port_a == "bt:AA:BB:CC:DD:EE:FF"


def test_scan_bt_paired_devices_handles_address_without_name(monkeypatch):
    # bluetoothctl can return just "Device AA:BB:..." with no friendly name.
    sample = "Device AA:BB:CC:DD:EE:FF\n"
    monkeypatch.setattr(subprocess, "run", _mock_run(sample))
    devices = scan_bt_paired_devices()
    assert len(devices) == 1
    label, port = devices[0]
    # When no name is present, the address itself is used as the label.
    assert "AA:BB:CC:DD:EE:FF" in label
    assert port == "bt:AA:BB:CC:DD:EE:FF"


def test_scan_bt_paired_devices_ignores_non_device_lines(monkeypatch):
    # bluetoothctl preamble lines / errors that don't start with "Device" must
    # be filtered out.
    sample = (
        "[bluetoothctl] starting…\n"
        "Device AA:BB:CC:DD:EE:FF Real-Adapter\n"
        "Junk line that isn't a device\n"
    )
    monkeypatch.setattr(subprocess, "run", _mock_run(sample))
    devices = scan_bt_paired_devices()
    assert len(devices) == 1


def test_scan_bt_paired_devices_returns_empty_on_subprocess_failure(monkeypatch):
    def _boom(*_a, **_kw):
        raise FileNotFoundError("bluetoothctl missing")
    monkeypatch.setattr(subprocess, "run", _boom)
    assert scan_bt_paired_devices() == []


def test_scan_bt_paired_devices_returns_empty_on_blank_stdout(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _mock_run(""))
    assert scan_bt_paired_devices() == []


def test_scan_bt_paired_devices_uppercases_mac_address(monkeypatch):
    # bluetoothctl could return lowercase in some setups — normalise to upper.
    sample = "Device aa:bb:cc:dd:ee:ff My-OBD\n"
    monkeypatch.setattr(subprocess, "run", _mock_run(sample))
    devices = scan_bt_paired_devices()
    assert devices[0][1] == "bt:AA:BB:CC:DD:EE:FF"


# ─── _looks_like_obd (nearby-scan filter) ────────────────────────────────────

def test_looks_like_obd_accepts_known_adapter_names():
    addr = "00:04:3E:8C:16:AC"
    assert obd_devices._looks_like_obd("OBDLink MX+ 02393", addr)
    assert obd_devices._looks_like_obd("OBDII", addr)
    assert obd_devices._looks_like_obd("Vgate iCar Pro", addr)
    assert obd_devices._looks_like_obd("ELM327 v1.5", addr)


def test_looks_like_obd_rejects_noise_and_unnamed():
    addr = "F4:9D:8A:7C:5C:66"
    assert not obd_devices._looks_like_obd("soundcore Liberty 4 Pro", addr)
    assert not obd_devices._looks_like_obd("Bluetooth 3.0 Keyboard", addr)
    # bluetoothctl echoes the MAC (dashed) as the "name" when none is resolved
    assert not obd_devices._looks_like_obd("F4-9D-8A-7C-5C-66", addr)
    assert not obd_devices._looks_like_obd("F4:9D:8A:7C:5C:66", addr)
    assert not obd_devices._looks_like_obd("", addr)
