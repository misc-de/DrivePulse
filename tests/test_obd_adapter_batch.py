"""Tests for obd_adapter.batch_query_stpx — the STPX path used by
STN/OBDLink adapters to fetch many PIDs in a single CAN exchange. The
parser (_parse_stpx_line) and the Mode-1 decode table sit at the heart
of every live frame on those adapters."""
from __future__ import annotations

import pytest

from drivepulse_app.obd.adapter import _MODE1_DECODE, batch_query_stpx


def _send_fixed(response: str):
    """Return a send_raw callable that always replies with *response*."""
    return lambda _cmd: response


def test_batch_query_decodes_rpm_response():
    # 0x0C (RPM) decodes from two bytes as (d0*256 + d1) / 4.
    # 0x1A 0xF8 → (26*256 + 248)/4 = 6904/4 = 1726.0
    raw = "7E8 04 41 0C 1A F8"
    out = batch_query_stpx(_send_fixed(raw), pid_numbers=[0x0C])
    assert "RPM" in out
    assert out["RPM"]["value"] == pytest.approx(1726.0)
    assert out["RPM"]["unit"] == ""


def test_batch_query_decodes_speed_response():
    # 0x0D (SPEED) is the raw single-byte km/h value.
    raw = "7E8 03 41 0D 64"
    out = batch_query_stpx(_send_fixed(raw), pid_numbers=[0x0D])
    assert out["SPEED"]["value"] == 0x64  # 100 km/h


def test_batch_query_decodes_coolant_temp_response():
    # 0x05 (COOLANT_TEMP) is byte - 40.
    raw = "7E8 03 41 05 78"
    out = batch_query_stpx(_send_fixed(raw), pid_numbers=[0x05])
    assert out["COOLANT_TEMP"]["value"] == 0x78 - 40


def test_batch_query_handles_multi_frame_response():
    # Real STPX output has multiple lines, one per PID.
    raw = "\n".join([
        "7E8 04 41 0C 1A F8",       # RPM = 1731
        "7E8 03 41 0D 50",          # Speed = 80
        "7E8 03 41 11 7F",          # Throttle = 0x7F * 100/255 ≈ 49.8 %
    ])
    out = batch_query_stpx(_send_fixed(raw), pid_numbers=[0x0C, 0x0D, 0x11])
    assert "RPM" in out and "SPEED" in out and "THROTTLE_POS" in out
    assert out["SPEED"]["value"] == 80
    assert out["THROTTLE_POS"]["value"] == pytest.approx(49.8, abs=0.1)


def test_batch_query_returns_empty_for_no_decodable_pids():
    out = batch_query_stpx(_send_fixed(""), pid_numbers=[])
    assert out == {}


def test_batch_query_swallows_send_raw_exceptions():
    def boom(_cmd):
        raise RuntimeError("serial gone")
    # An adapter outage doesn't crash the polling loop; it just returns {}.
    assert batch_query_stpx(boom, [0x0C, 0x0D]) == {}


def test_batch_query_silently_skips_unknown_pid_in_response():
    # 0xFE isn't in the decode table — skipped without erroring.
    raw = "7E8 03 41 FE 12"
    out = batch_query_stpx(_send_fixed(raw), pid_numbers=[0xFE])
    assert out == {}


def test_batch_query_skips_malformed_lines_and_keeps_good_ones():
    raw = "\n".join([
        "garbage tokens here",
        "7E8 04 41 0C 1A F8",       # valid RPM
        "7E8 NA 41 0D 50",          # bad hex in length token
        "7E8 03 41 0D 50",          # valid speed
    ])
    out = batch_query_stpx(_send_fixed(raw), pid_numbers=[0x0C, 0x0D])
    assert "RPM" in out
    assert "SPEED" in out


def test_batch_query_chunks_pid_list_into_groups_of_six():
    # Drive the send_raw fake with a chunk counter to verify chunking.
    chunk_calls: list[str] = []

    def capturing(cmd: str) -> str:
        chunk_calls.append(cmd)
        return ""  # empty responses are fine — we only count calls.

    # 13 PIDs should split into chunks of 6, 6, 1 → three calls.
    batch_query_stpx(capturing, pid_numbers=list(range(0x0C, 0x0C + 13)))
    assert len(chunk_calls) == 3


def test_decode_table_includes_core_telemetry_pids():
    # Lock the PID set that the rest of the app depends on — a regression
    # here means readings vanish silently from the dashboard.
    for pid in (0x04, 0x05, 0x0C, 0x0D, 0x0F, 0x10, 0x11, 0x2F, 0x42):
        assert pid in _MODE1_DECODE


def test_decode_rpm_function_round_trips_engineering_value():
    # The decoder is `(d0*256 + d1)/4` — verify against a known fixture
    # so we'd notice if someone "simplified" the formula.
    _name, decode = _MODE1_DECODE[0x0C]
    assert decode(bytes([0x0E, 0x00])) == pytest.approx(896.0)


def test_decode_engine_load_function_returns_percent():
    # ENGINE_LOAD = d0 * 100/255, rounded to 1 decimal.
    _name, decode = _MODE1_DECODE[0x04]
    assert decode(bytes([255])) == pytest.approx(100.0, abs=0.1)
    assert decode(bytes([0])) == 0.0


def test_decode_voltage_returns_three_decimal_volts():
    # CONTROL_MODULE_VOLTAGE = (d0*256 + d1)/1000.
    _name, decode = _MODE1_DECODE[0x42]
    # 0x3A 0x18 → 14 872 / 1000 = 14.872
    assert decode(bytes([0x3A, 0x18])) == pytest.approx(14.872, abs=0.001)


# ─── raw_send / probe_adapter over a pty (BluetoothPtyBridge) ─────────────────

class _FakePtyPort:
    """A port where in_waiting stays 0 but read() returns relayed bytes — the
    BluetoothPtyBridge case that defeated the old in_waiting-only raw_send."""

    in_waiting = 0

    def __init__(self, response: bytes):
        self._out = response
        self.timeout = None
        self.written = b""

    def reset_input_buffer(self):
        pass

    def write(self, data: bytes):
        self.written += data

    def read(self, n: int) -> bytes:
        chunk, self._out = self._out[:n], self._out[n:]
        return chunk


def test_raw_send_reads_over_pty_when_in_waiting_stays_zero():
    from drivepulse_app.obd.adapter import raw_send

    port = _FakePtyPort(b"STN2120 v5.4.4\r\r>")
    out = raw_send(port, "STI")

    assert "STN2120" in out
    assert ">" not in out
    assert port.written == b"STI\r"


def test_probe_adapter_detects_stn_over_pty():
    from drivepulse_app.obd.adapter import AdapterKind, probe_adapter, raw_send

    port = _FakePtyPort(b"STN2120 v5.4.4\r\r>")
    info = probe_adapter(None, locked_raw=lambda cmd: raw_send(port, cmd))

    assert info.kind == AdapterKind.STN
    assert info.supports_stpx is True


def test_obd_text_decodes_bytearray_vin():
    from drivepulse_app.obd.scanner import _obd_text

    # python-obd returns VIN/Cal-ID as bytearray; we must store plain ASCII,
    # not the "bytearray(b'…')" repr that used to leak into the profile key.
    assert _obd_text(bytearray(b"WAUZZZ4G8EN073189")) == "WAUZZZ4G8EN073189"
    assert _obd_text(b"4G0115  0006BDA") == "4G0115  0006BDA"
    assert _obd_text("already-string ") == "already-string"


class _FakeSerial:
    in_waiting = 0

    def read(self, _n):
        return b""

    def write(self, _d):
        pass


def test_serial_port_finds_python_obd_mangled_port():
    from drivepulse_app.obd.adapter import _serial_port

    # python-obd's ELM327 stores the port as self.__port → _ELM327__port.
    class ELM327:
        def __init__(self):
            self.__port = _FakeSerial()

    class _FakeOBD:
        def __init__(self):
            self.interface = ELM327()

    assert _serial_port(_FakeOBD()) is not None
    assert _serial_port(None) is None


def test_serial_port_finds_native_backend_port():
    from drivepulse_app.obd.adapter import _serial_port

    class _NativeIface:
        def __init__(self):
            self._port = _FakeSerial()

    class _NativeOBD:
        def __init__(self):
            self.interface = _NativeIface()

    assert _serial_port(_NativeOBD()) is not None


# ---------------------------------------------------------------------------
# ObdScanner._run_mode1_batch orchestration: the STPX→fallback hand-off.
# Regression guard for scan 208 (STN2255 over BT pty): STPX came back empty,
# the scan saved 0 live values. The batch step must (1) resync the adapter
# after the raw STPX exchange and (2) when STPX yields nothing, return empty
# so the caller does a *complete* single-query fallback instead of a partial
# scan that drops every STPX-only PID.
# ---------------------------------------------------------------------------

class _Cmd:
    """Minimal stand-in for a python-obd Mode-01 command."""
    def __init__(self, pid: int, name: str):
        self.pid = pid
        self.mode = 1
        self._name = name

    def __str__(self):
        return self._name


class _Resp:
    def __init__(self, null: bool, value=None):
        self._null = null
        self.value = value

    def is_null(self):
        return self._null


def _make_scanner(raw_send, resync, query_locked):
    from types import SimpleNamespace

    from drivepulse_app.obd.scanner import ObdScanner

    return ObdScanner(
        connection=SimpleNamespace(supported_commands=set()),
        port="bt:test",
        on_update=lambda payload: None,
        session_cache=set(),
        query_locked=query_locked,
        raw_send_locked=raw_send,
        resync_locked=resync,
    )


def test_scanner_batch_returns_empty_and_resyncs_when_stpx_silent():
    calls = {"resync": 0, "queries": []}
    scanner = _make_scanner(
        raw_send=lambda _cmd: "",                       # STPX silent over BT pty
        resync=lambda: calls.__setitem__("resync", calls["resync"] + 1),
        query_locked=lambda cmd: calls["queries"].append(str(cmd)) or _Resp(False, "x"),
    )
    cmds = [_Cmd(0x0C, "RPM"), _Cmd(0x05, "COOLANT_TEMP")]  # both decode-table PIDs

    out = scanner._run_mode1_batch(cmds, total_steps=10)

    assert out == {}                 # → run() falls back over the FULL PID set
    assert calls["resync"] == 1      # adapter resynced before returning
    assert calls["queries"] == []    # no partial python-obd query slipped through


def test_scanner_batch_keeps_stpx_values_and_single_queries_nondecode_pids():
    calls = {"resync": 0, "queries": []}
    scanner = _make_scanner(
        raw_send=lambda _cmd: "7E8 04 41 0C 1A F8",     # STPX decodes RPM
        resync=lambda: calls.__setitem__("resync", calls["resync"] + 1),
        query_locked=lambda cmd: calls["queries"].append(str(cmd)) or _Resp(False, "present"),
    )
    # 0x0C is in the decode table (STPX), 0x00 is not (single-query fallback).
    cmds = [_Cmd(0x0C, "RPM"), _Cmd(0x00, "PIDS_A")]

    out = scanner._run_mode1_batch(cmds, total_steps=10)

    assert "RPM" in out                     # STPX value preserved
    assert calls["resync"] == 1             # resynced before the fallback queries
    assert calls["queries"] == ["PIDS_A"]   # only the non-decode PID single-queried
    assert out["PIDS_A"] == "present"       # _to_plain str-fallback for plain value


def test_scanner_resync_is_noop_without_callable():
    # Older callers may not inject resync_locked; the batch must not crash.
    scanner = _make_scanner(
        raw_send=lambda _cmd: "",
        resync=None,
        query_locked=lambda cmd: _Resp(True),
    )
    assert scanner._run_mode1_batch([_Cmd(0x0C, "RPM")], total_steps=10) == {}
