from __future__ import annotations

import json
import types
from pathlib import Path


class _Quantity:
    magnitude = 42.5
    units = "rpm"


class _Response:
    def __init__(self, value=None, is_null: bool = False) -> None:
        self.value = value
        self._is_null = is_null

    def is_null(self) -> bool:
        return self._is_null


class _Connection:
    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.closed = False
        self.queries = []

    def is_connected(self) -> bool:
        return self.connected

    def close(self) -> None:
        self.closed = True

    def status(self) -> str:
        return "connected" if self.connected else "not-connected"

    def query(self, command):
        self.queries.append(command)
        if command == "bad":
            raise RuntimeError("query failed")
        return _Response(_Quantity())


def _fake_obd_module(connections):
    commands = types.SimpleNamespace(
        RPM="rpm",
        SPEED="speed",
        COOLANT_TEMP="coolant",
        THROTTLE_POS="throttle",
        ENGINE_LOAD="load",
        INTAKE_TEMP="intake",
        MAF="maf",
        FUEL_LEVEL=None,
        RUN_TIME=None,
        CONTROL_MODULE_VOLTAGE=None,
    )

    def obd_factory(port, **kwargs):
        connection = connections.pop(0)
        connection.port = port
        connection.kwargs = kwargs
        return connection

    return types.SimpleNamespace(OBD=obd_factory, commands=commands)


def test_response_to_plain_value_handles_null_quantity_and_fallback(drivepulse_module):
    reader = drivepulse_module.ObdReader(lambda payload: None)

    assert reader._response_to_plain_value(_Response(is_null=True)) is None
    assert reader._response_to_plain_value(_Response(_Quantity())) == {"value": 42.5, "unit": "rpm"}
    assert reader._response_to_plain_value(_Response("plain")) == "plain"


def test_candidate_ports_prefers_explicit_port(monkeypatch, drivepulse_module):
    monkeypatch.setattr(drivepulse_module, "OBD_PORT", "/dev/rfcomm0")

    reader = drivepulse_module.ObdReader(lambda payload: None)

    assert reader._candidate_ports() == ["/dev/rfcomm0"]


def test_candidate_ports_discovers_bluetooth_usb_and_auto(monkeypatch, drivepulse_module):
    monkeypatch.setattr(drivepulse_module, "OBD_PORT", None)

    def fake_glob(self: Path, pattern: str):
        return {
            "dev/rfcomm*": [Path("/dev/rfcomm0")],
            "dev/ttyUSB*": [Path("/dev/ttyUSB0")],
            "dev/ttyACM*": [],
            "dev/serial/by-id/*": [Path("/dev/serial/by-id/elm327")],
        }[pattern]

    monkeypatch.setattr(drivepulse_module.Path, "glob", fake_glob)
    reader = drivepulse_module.ObdReader(lambda payload: None)

    assert reader._candidate_ports() == [
        "/dev/rfcomm0",
        "/dev/ttyUSB0",
        "/dev/serial/by-id/elm327",
        None,
    ]


def test_connect_uses_configured_obd_parameters(monkeypatch, drivepulse_module, tmp_log_paths):
    connection = _Connection(True)
    monkeypatch.setattr(drivepulse_module, "obd", _fake_obd_module([connection]))
    monkeypatch.setattr(drivepulse_module, "OBD_PORT", "/dev/rfcomm0")
    monkeypatch.setattr(drivepulse_module, "OBD_BAUDRATE", 38400)
    monkeypatch.setattr(drivepulse_module, "OBD_TIMEOUT_SECONDS", 3.0)
    monkeypatch.setattr(drivepulse_module, "OBD_FAST", False)

    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader._connect()

    assert reader.mock is False
    assert reader.connected_port == "/dev/rfcomm0"
    assert connection.kwargs == {"fast": False, "timeout": 3.0, "baudrate": 38400}


def test_connect_falls_back_to_mock_when_no_connection(monkeypatch, drivepulse_module):
    monkeypatch.setattr(drivepulse_module, "obd", _fake_obd_module([_Connection(False)]))
    monkeypatch.setattr(drivepulse_module, "OBD_PORT", "/dev/rfcomm0")
    monkeypatch.setattr(drivepulse_module.ObdReader, "_connection_log", lambda *args, **kwargs: None)

    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader._connect()

    assert reader.mock is True
    assert reader.mock_reason == "kein nutzbarer Dongle gefunden"
    assert reader.connected_port is None


def test_read_obd_collects_values_and_error_counts(monkeypatch, drivepulse_module):
    fake_obd = _fake_obd_module([])
    fake_obd.commands.SPEED = "bad"
    monkeypatch.setattr(drivepulse_module, "obd", fake_obd)

    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader.connection = _Connection(True)

    payload = reader._read_obd()

    assert payload["rpm"] == {"value": 42.5, "unit": "rpm"}
    assert "speed_error" in payload
    assert payload["_command_count"] == 7
    assert payload["_read_error_count"] == 1


def test_read_obd_reuses_cached_slow_values_between_fast_polls(monkeypatch, drivepulse_module):
    fake_obd = _fake_obd_module([])
    monkeypatch.setattr(drivepulse_module, "obd", fake_obd)
    times = iter([100.0, 100.5])
    monkeypatch.setattr(drivepulse_module.time, "monotonic", lambda: next(times))

    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader.connection = _Connection(True)

    first = reader._read_obd()
    second = reader._read_obd()

    assert first["_command_count"] == 7
    assert second["_command_count"] == 3
    assert second["throttle_pos"] == first["throttle_pos"]
    assert second["engine_load"] == first["engine_load"]


def test_close_connection_clears_obd_value_cache(drivepulse_module):
    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader.connection = _Connection(True)
    reader.connected_port = "/dev/rfcomm0"
    reader._obd_value_cache["fuel_level"] = {"value": 50}
    reader._obd_last_query["fuel_level"] = 123.0

    reader._close_connection()

    assert reader.connection is None
    assert reader.connected_port is None
    assert reader._obd_value_cache == {}
    assert reader._obd_last_query == {}


def test_reconnect_after_three_failed_reads(monkeypatch, drivepulse_module):
    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader.mock = False
    reader.connection = _Connection(True)
    calls = []
    monkeypatch.setattr(reader, "_connect", lambda: calls.append("connect"))

    payload = {"_command_count": 2, "_read_error_count": 2}
    reader._maybe_reconnect_after_read(payload)
    reader._maybe_reconnect_after_read(payload)
    reader._maybe_reconnect_after_read(payload)

    assert calls == ["connect"]


def test_mock_reconnect_probe_is_throttled(monkeypatch, drivepulse_module):
    monkeypatch.setattr(drivepulse_module, "obd", object())
    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader.mock = True
    calls = []
    monkeypatch.setattr(reader, "_connect", lambda: calls.append("connect"))
    monkeypatch.setattr(drivepulse_module.time, "monotonic", lambda: 100.0)

    reader._maybe_reconnect_from_mock()
    reader._maybe_reconnect_from_mock()

    assert calls == ["connect"]
    assert reader.next_mock_reconnect_attempt == 100.0 + reader._MOCK_RECONNECT_INTERVAL_S


def test_write_log_writes_jsonl(drivepulse_module, tmp_log_paths):
    reader = drivepulse_module.ObdReader(lambda payload: None)

    reader._write_log({"speed": {"value": 12}})

    lines = drivepulse_module.LOG_FILE.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"speed": {"value": 12}}
