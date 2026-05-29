"""Tests for the GPL-free native ELM327 backend (drivepulse_app.obd.native).

The native driver duck-types the slice of python-OBD that DrivePulse uses, so
the reader/scanner can run without bundling python-OBD. These tests drive it
against a fake pyserial port that speaks the ELM327 request/response protocol,
plus the shared decode table, so a regression in the connect handshake or the
Mode-01 parsing surfaces here rather than only against real hardware.
"""
from __future__ import annotations

import pytest

from drivepulse_app.obd import native
from drivepulse_app.obd.adapter import _serial_port
from drivepulse_app.obd.polling import command_map, response_to_plain_value

# Canned ELM327 replies. Init commands just need to not error; the 0100
# supported-PID probe must contain "4100" for the link to be considered live.
_DEFAULT_RESPONSES: dict[str, str] = {
    "ATZ": "ELM327 v1.5",
    "ATE0": "OK",
    "ATL0": "OK",
    "ATS0": "OK",
    "ATH0": "OK",
    "ATSP0": "OK",
    "0100": "41 00 BE 3E B8 11",
    "ATDP": "ISO 15765-4 (CAN 11/500)",
    "010C": "41 0C 1A F8",   # RPM = (0x1A*256 + 0xF8)/4 = 1726.0
    "010D": "41 0D 64",      # SPEED = 0x64 = 100 km/h
    "0105": "41 05 78",      # COOLANT_TEMP = 0x78 - 40 = 80
    "04": "44",              # CLEAR_DTC positive response
}


class FakeSerial:
    """Minimal pyserial stand-in driven by a command -> response map."""

    def __init__(self, responses: dict[str, str]):
        self.responses = responses
        self._buf = b""
        self.written: list[str] = []
        self.closed = False

    def reset_input_buffer(self) -> None:
        self._buf = b""

    def write(self, data: bytes) -> int:
        cmd = data.decode("ascii").strip()
        self.written.append(cmd)
        resp = self.responses.get(cmd, "NO DATA")
        # Every ELM reply ends with the '>' prompt that raw_send waits for.
        self._buf += f"{resp}\r>".encode("ascii")
        return len(data)

    @property
    def in_waiting(self) -> int:
        return len(self._buf)

    def read(self, n: int) -> bytes:
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_port(monkeypatch):
    """Patch serial.serial_for_url so native.OBD opens our FakeSerial."""
    holder: dict[str, FakeSerial] = {}
    responses = dict(_DEFAULT_RESPONSES)

    def fake_serial_for_url(_url, **_kwargs):
        port = FakeSerial(responses)
        holder["port"] = port
        return port

    import serial
    monkeypatch.setattr(serial, "serial_for_url", fake_serial_for_url)
    return holder, responses


def test_connects_when_supported_pid_probe_succeeds(fake_port):
    conn = native.OBD("/dev/ttyFAKE")
    assert conn.is_connected()
    assert conn.status() == "Car Connected"
    assert "CAN" in conn.protocol_name()
    # The shared _serial_port helper must reach the underlying port.
    assert _serial_port(conn) is fake_port[0]["port"]


def test_stays_disconnected_when_probe_returns_no_data(fake_port):
    _holder, responses = fake_port
    responses["0100"] = "SEARCHING..."  # never resolves to a live ECU link
    conn = native.OBD("/dev/ttyFAKE")
    assert not conn.is_connected()
    assert conn.interface is None  # port closed and released on failed init


def test_no_port_string_stays_disconnected(fake_port):
    conn = native.OBD(None)
    assert not conn.is_connected()
    assert conn.interface is None


def test_query_decodes_rpm(fake_port):
    conn = native.OBD("/dev/ttyFAKE")
    resp = conn.query(native.commands.RPM)
    assert not resp.is_null()
    assert resp.value.magnitude == pytest.approx(1726.0)
    assert resp.value.units == ""


def test_query_decodes_speed(fake_port):
    conn = native.OBD("/dev/ttyFAKE")
    resp = conn.query(native.commands.SPEED)
    assert resp.value.magnitude == 100


def test_query_returns_null_on_nodata(fake_port):
    _holder, responses = fake_port
    responses["010C"] = "NO DATA"
    conn = native.OBD("/dev/ttyFAKE")
    assert conn.query(native.commands.RPM).is_null()


def test_query_none_command_returns_null(fake_port):
    conn = native.OBD("/dev/ttyFAKE")
    assert conn.query(None).is_null()


def test_clear_dtc_positive_response_is_not_null(fake_port):
    conn = native.OBD("/dev/ttyFAKE")
    resp = conn.query(native.commands.CLEAR_DTC)
    assert not resp.is_null()


def test_clear_dtc_null_when_unacknowledged(fake_port):
    _holder, responses = fake_port
    responses["04"] = "NO DATA"
    conn = native.OBD("/dev/ttyFAKE")
    assert conn.query(native.commands.CLEAR_DTC).is_null()


def test_close_releases_port(fake_port):
    conn = native.OBD("/dev/ttyFAKE")
    port = fake_port[0]["port"]
    conn.close()
    assert port.closed
    assert not conn.is_connected()


def test_commands_namespace_exposes_core_telemetry(fake_port):
    for attr in ("RPM", "SPEED", "COOLANT_TEMP", "THROTTLE_POS", "MAF", "CLEAR_DTC"):
        assert getattr(native.commands, attr, None) is not None


def test_command_map_resolves_against_native_module(fake_port):
    # The reader builds its poll set with command_map(obd_backend); the native
    # module must satisfy the same key -> command lookup as python-OBD.
    cmds = command_map(native)
    assert cmds["rpm"] is native.commands.RPM
    assert cmds["speed"] is native.commands.SPEED


def test_response_normalizes_through_polling_helper(fake_port):
    # response_to_plain_value is shared by the python-OBD and native paths;
    # the native Response/quantity shim must flow through it unchanged.
    conn = native.OBD("/dev/ttyFAKE")
    value = response_to_plain_value(conn.query(native.commands.RPM))
    assert value == {"value": pytest.approx(1726.0), "unit": ""}


def test_parse_mode1_extracts_payload_after_marker():
    # "41 0C" marker followed by two data bytes.
    assert native._parse_mode1("41 0C 1A F8", 0x0C) == bytes([0x1A, 0xF8])


def test_parse_mode1_returns_none_without_marker():
    assert native._parse_mode1("7F 01 12", 0x0C) is None


def test_clean_hex_drops_error_tokens():
    assert native._clean_hex("NO DATA") == ""
    assert native._clean_hex("SEARCHING...") == ""
    assert native._clean_hex("41 0C 1A F8") == "410C1AF8"
