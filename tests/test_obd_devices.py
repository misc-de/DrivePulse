"""Tests for obd_devices: BT-address parsing, paired-device scan, port-string
parsing. The pure parsers exercise the env-var schema callers depend on
(``OBD_BT_ADDR=AA:BB:CC:DD:EE:FF[:channel],...``)."""
from __future__ import annotations

import subprocess

from drivepulse_app import common
from drivepulse_app.obd import devices as obd_devices
from drivepulse_app.obd.devices import (
    candidate_bt_addresses,
    paired_obd_addresses,
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


# ─── paired_obd_addresses (multi-dongle auto-discovery) ──────────────────────


def _spp_info_for(spp_addrs: set[str], also_audio_addrs: set[str] | None = None):
    """Build a bluetoothctl-info mock.

    *spp_addrs* report Serial Port. *also_audio_addrs* report SPP **and** A2DP
    (the Soundcore-style false-positive case) — these must be rejected.
    """
    also_audio = also_audio_addrs or set()

    def _run(*args, **_kw):
        cmd = args[0] if args else []

        class _R:
            returncode = 0
            stdout = ""

        r = _R()
        if len(cmd) >= 3 and cmd[0] == "bluetoothctl" and cmd[1] == "info":
            addr = cmd[2].upper()
            if addr in also_audio:
                r.stdout = (
                    f"Device {addr}\n"
                    "\tUUID: Serial Port               "
                    "(00001101-0000-1000-8000-00805f9b34fb)\n"
                    "\tUUID: Audio Sink                "
                    "(0000110b-0000-1000-8000-00805f9b34fb)\n"
                )
            elif addr in spp_addrs:
                r.stdout = (
                    f"Device {addr}\n"
                    "\tUUID: Serial Port               "
                    "(00001101-0000-1000-8000-00805f9b34fb)\n"
                )
            else:
                r.stdout = (
                    f"Device {addr}\n"
                    "\tUUID: Audio Sink                "
                    "(0000110b-0000-1000-8000-00805f9b34fb)\n"
                )
        return r
    return _run


def test_paired_obd_addresses_filters_only_spp_advertising(monkeypatch):
    # Detection is brand-agnostic now: any paired device advertising the SPP
    # UUID is considered an OBD candidate, even with an unknown name. Devices
    # without SPP (headphones, HID) are filtered.
    monkeypatch.setattr(obd_devices, "scan_bt_paired_devices", lambda: [
        ("BT: OBDLink MX+ 02393 (00:04:3E:8C:16:AC)", "bt:00:04:3E:8C:16:AC"),
        ("BT: Sony WH-1000XM4 (AA:BB:CC:DD:EE:FF)", "bt:AA:BB:CC:DD:EE:FF"),
        ("BT: No-Name Klon (11:22:33:44:55:66)", "bt:11:22:33:44:55:66"),
    ])
    spp = {"00:04:3E:8C:16:AC", "11:22:33:44:55:66"}
    monkeypatch.setattr(subprocess, "run", _spp_info_for(spp))
    out = paired_obd_addresses()
    addrs = [a for a, _ch, _n in out]
    assert "00:04:3E:8C:16:AC" in addrs
    assert "11:22:33:44:55:66" in addrs  # name not in keyword list — accepted via SPP
    assert "AA:BB:CC:DD:EE:FF" not in addrs  # Sony headset (no SPP) filtered
    assert all(ch == 1 for _a, ch, _n in out)


def test_paired_obd_addresses_empty_when_no_paired_devices(monkeypatch):
    monkeypatch.setattr(obd_devices, "scan_bt_paired_devices", lambda: [])
    assert paired_obd_addresses() == []


def test_paired_obd_addresses_returns_recovered_name(monkeypatch):
    monkeypatch.setattr(obd_devices, "scan_bt_paired_devices", lambda: [
        ("BT: OBDLink MX+ 02393 (00:04:3E:8C:16:AC)", "bt:00:04:3E:8C:16:AC"),
    ])
    monkeypatch.setattr(subprocess, "run", _spp_info_for({"00:04:3E:8C:16:AC"}))
    out = paired_obd_addresses()
    assert out == [("00:04:3E:8C:16:AC", 1, "OBDLink MX+ 02393")]


def test_paired_obd_addresses_rejects_spp_plus_audio_devices(monkeypatch):
    # Anker Soundcore TWS expose SPP for firmware updates *and* A2DP. Without
    # the audio-profile exclusion the connector would burn ~8 s timing out
    # against headphones every connect cycle.
    soundcore = "F4:9D:8A:7C:5C:66"
    monkeypatch.setattr(obd_devices, "scan_bt_paired_devices", lambda: [
        ("BT: soundcore Liberty 4 Pro (F4:9D:8A:7C:5C:66)", "bt:F4:9D:8A:7C:5C:66"),
        ("BT: OBDLink MX+ (00:04:3E:8C:16:AC)", "bt:00:04:3E:8C:16:AC"),
    ])
    monkeypatch.setattr(
        subprocess, "run",
        _spp_info_for({"00:04:3E:8C:16:AC"}, also_audio_addrs={soundcore}),
    )
    out = paired_obd_addresses()
    addrs = [a for a, _ch, _n in out]
    assert soundcore not in addrs
    assert "00:04:3E:8C:16:AC" in addrs


def test_paired_obd_addresses_empty_when_bluetoothctl_missing(monkeypatch):
    # No bluetoothctl on the host (CI/sandbox) → no SPP probe possible →
    # silently empty result, never raises.
    monkeypatch.setattr(obd_devices, "scan_bt_paired_devices", lambda: [
        ("BT: OBDLink MX+ 02393 (00:04:3E:8C:16:AC)", "bt:00:04:3E:8C:16:AC"),
    ])
    def _missing(*_a, **_kw):
        raise FileNotFoundError("bluetoothctl missing")
    monkeypatch.setattr(subprocess, "run", _missing)
    assert paired_obd_addresses() == []


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


# ─── scan_bt_nearby_devices (ranking + in-range fallback) ─────────────────────

import io  # noqa: E402
import time  # noqa: E402


class _FakePopen:
    def __init__(self, out: str):
        self._out = out
        self.stdin = io.StringIO()  # supports write()/flush()

    def communicate(self, timeout=None):  # noqa: ARG002
        return self._out, ""

    def kill(self):
        pass


def _mock_popen(out: str):
    return lambda *_a, **_kw: _FakePopen(out)


def test_scan_bt_nearby_drops_absent_paired_and_named_non_obd(monkeypatch):
    # Live scan: an OBD dongle and a pair of headphones are both physically in
    # range (have RSSI). The headphones have a real, non-OBD name → noise, skip.
    # The paired OBDLink MX+ is only in the device cache (no RSSI = not here now).
    scan_out = (
        "[NEW] Device 11:22:33:44:55:66 OBDII\n"
        "[CHG] Device 11:22:33:44:55:66 RSSI: -55\n"
        "[NEW] Device 77:88:99:AA:BB:CC soundcore\n"
        "[CHG] Device 77:88:99:AA:BB:CC RSSI: -40\n"
    )
    devices_out = (
        "Device 11:22:33:44:55:66 OBDII\n"
        "Device 77:88:99:AA:BB:CC soundcore\n"
        "Device AA:BB:CC:DD:EE:FF OBDLink MX+ 02393\n"
    )
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(subprocess, "Popen", _mock_popen(scan_out))
    monkeypatch.setattr(subprocess, "run", _mock_run(devices_out))

    ports = [p for _, p in obd_devices.scan_bt_nearby_devices(scan_seconds=0)]
    # Only the in-range OBD dongle. Named-non-OBD headphones (noise) and the
    # paired-but-absent MX+ (cache only, no RSSI) are both excluded.
    assert ports == ["bt:11:22:33:44:55:66"]


def test_scan_bt_nearby_surfaces_unnamed_in_range_device(monkeypatch):
    # A just-plugged ELM clone often advertises only its MAC (no OBD token) until
    # paired — it must still appear, labelled as a generic BT device.
    addr = "DE:AD:BE:EF:00:11"
    scan_out = f"[CHG] Device {addr} RSSI: -60\n"
    devices_out = f"Device {addr}\n"  # name == MAC, no friendly name yet
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(subprocess, "Popen", _mock_popen(scan_out))
    monkeypatch.setattr(subprocess, "run", _mock_run(devices_out))

    result = obd_devices.scan_bt_nearby_devices(scan_seconds=0)
    assert result == [(f"BT {addr}  ({addr})", f"bt:{addr}")]


def test_scan_sets_dual_mode_transport_before_scan_on(monkeypatch):
    # The scan must set a BR/EDR+LE discovery filter (transport auto) before
    # `scan on`, so Bluetooth-Classic OBD dongles surface on LE-defaulting stacks.
    captured: dict[str, str] = {}

    class _Cap:
        def __init__(self) -> None:
            self.stdin = io.StringIO()

        def communicate(self, timeout=None):  # noqa: ARG002
            captured["stdin"] = self.stdin.getvalue()
            return "", ""

        def kill(self) -> None:
            pass

    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: _Cap())
    monkeypatch.setattr(subprocess, "run", _mock_run(""))

    obd_devices.scan_bt_nearby_devices(scan_seconds=0)
    cmds = captured["stdin"]
    assert "transport auto" in cmds
    assert cmds.index("transport auto") < cmds.index("scan on")
