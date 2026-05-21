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
    from drivepulse_app.obd_polling import response_to_plain_value

    assert response_to_plain_value(_Response(is_null=True)) is None
    assert response_to_plain_value(_Response(_Quantity())) == {"value": 42.5, "unit": "rpm"}
    assert response_to_plain_value(_Response("plain")) == "plain"


def test_should_query_key_respects_slow_poll_intervals(drivepulse_module):
    from drivepulse_app.obd_polling import should_query_key

    assert should_query_key("speed", 100.0, {"speed": 99.9}) is True
    assert should_query_key("fuel_level", 100.0, {}) is True
    assert should_query_key("fuel_level", 100.0, {"fuel_level": 95.0}) is False
    assert should_query_key("fuel_level", 106.0, {"fuel_level": 95.0}) is True


def test_candidate_ports_prefers_explicit_port(monkeypatch, drivepulse_module):
    from drivepulse_app import obd_reader

    monkeypatch.setattr(obd_reader, "OBD_PORT", "/dev/rfcomm0")

    reader = drivepulse_module.ObdReader(lambda payload: None)

    assert reader._candidate_ports() == ["/dev/rfcomm0"]


def test_candidate_ports_discovers_bluetooth_usb_and_auto(monkeypatch, drivepulse_module):
    from drivepulse_app import obd_reader

    monkeypatch.setattr(obd_reader, "OBD_PORT", None)

    def fake_glob(self: Path, pattern: str):
        return {
            "dev/rfcomm*": [Path("/dev/rfcomm0")],
            "dev/ttyUSB*": [Path("/dev/ttyUSB0")],
            "dev/ttyACM*": [],
            "dev/serial/by-id/*": [Path("/dev/serial/by-id/elm327")],
        }[pattern]

    monkeypatch.setattr(obd_reader.Path, "glob", fake_glob)
    reader = drivepulse_module.ObdReader(lambda payload: None)

    assert reader._candidate_ports() == [
        "/dev/rfcomm0",
        "/dev/ttyUSB0",
        "/dev/serial/by-id/elm327",
        None,
    ]


def test_connect_uses_configured_obd_parameters(monkeypatch, drivepulse_module, tmp_log_paths):
    from drivepulse_app import obd_reader

    connection = _Connection(True)
    monkeypatch.setattr(obd_reader, "obd", _fake_obd_module([connection]))
    monkeypatch.setattr(obd_reader, "OBD_PORT", "/dev/rfcomm0")
    monkeypatch.setattr(obd_reader, "OBD_BAUDRATE", 38400)
    monkeypatch.setattr(obd_reader, "OBD_TIMEOUT_SECONDS", 3.0)
    monkeypatch.setattr(obd_reader, "OBD_FAST", False)

    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader._connect()

    assert reader.mock is False
    assert reader.connected_port == "/dev/rfcomm0"
    assert connection.kwargs == {"fast": False, "timeout": 3.0, "baudrate": 38400}


def test_connect_falls_back_to_mock_when_no_connection(monkeypatch, drivepulse_module):
    from drivepulse_app import obd_reader

    monkeypatch.setattr(obd_reader, "obd", _fake_obd_module([_Connection(False)]))
    monkeypatch.setattr(obd_reader, "OBD_PORT", "/dev/rfcomm0")
    monkeypatch.setattr(drivepulse_module.ObdReader, "_connection_log", lambda *args, **kwargs: None)

    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader._connect()

    assert reader.mock is True
    assert reader.mock_reason == "kein nutzbarer Dongle gefunden"
    assert reader.connected_port is None


def test_read_obd_collects_values_and_error_counts(monkeypatch, drivepulse_module):
    from drivepulse_app import obd_reader

    fake_obd = _fake_obd_module([])
    fake_obd.commands.SPEED = "bad"
    monkeypatch.setattr(obd_reader, "obd", fake_obd)

    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader.connection = _Connection(True)

    payload = reader._read_obd()

    assert payload["rpm"] == {"value": 42.5, "unit": "rpm"}
    assert "speed_error" in payload
    assert payload["_command_count"] == 7
    assert payload["_read_error_count"] == 1


def test_read_obd_reuses_cached_slow_values_between_fast_polls(monkeypatch, drivepulse_module):
    from drivepulse_app import obd_reader

    fake_obd = _fake_obd_module([])
    monkeypatch.setattr(obd_reader, "obd", fake_obd)
    times = iter([100.0, 100.5])
    monkeypatch.setattr(obd_reader.time, "monotonic", lambda: next(times))

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
    from drivepulse_app import obd_reader

    monkeypatch.setattr(obd_reader, "obd", object())
    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader.mock = True
    calls = []
    monkeypatch.setattr(reader, "_connect", lambda: calls.append("connect"))
    monkeypatch.setattr(obd_reader.time, "monotonic", lambda: 100.0)

    reader._maybe_reconnect_from_mock()
    reader._maybe_reconnect_from_mock()

    assert calls == ["connect"]
    assert reader.next_mock_reconnect_attempt == 100.0 + reader._MOCK_RECONNECT_INTERVAL_S


def test_write_log_writes_jsonl(drivepulse_module, tmp_log_paths):
    from drivepulse_app import obd_reader

    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader.mock = False  # obd is mocked as None, forcing mock=True; override for log test

    reader._write_log({"speed": {"value": 12}})

    lines = obd_reader.LOG_FILE.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"speed": {"value": 12}}


def test_gpsd_line_ignores_bad_optional_numbers(drivepulse_module):
    from drivepulse_app.gps_reader import GpsReader

    updates = []
    reader = GpsReader(updates.append)

    reader._handle_gpsd_line(json.dumps({
        "class": "TPV",
        "mode": 3,
        "speed": "10",
        "track": "bad",
        "lat": "48.1",
        "lon": "not-a-number",
        "alt": float("nan"),
    }))

    assert len(updates) == 1
    assert updates[0]["gps_speed"] == {"value": 36.0, "unit": "km/h"}
    assert "gps_heading" not in updates[0]
    assert "gps_lat" not in updates[0]
    assert "gps_lon" not in updates[0]
    assert "gps_altitude" not in updates[0]


def test_gpsd_line_rejects_bad_speed(drivepulse_module):
    from drivepulse_app.gps_reader import GpsReader

    updates = []
    reader = GpsReader(updates.append)

    reader._handle_gpsd_line(json.dumps({"class": "TPV", "mode": 3, "speed": "bad"}))
    reader._handle_gpsd_line(json.dumps({"class": "TPV", "mode": 3, "speed": -1}))

    assert updates == []


def test_obd_scanner_emits_scan_profile_without_profile_file(drivepulse_module):
    from drivepulse_app.obd_scanner import ObdScanner

    class Connection:
        supported_commands = set()

        def query(self, command):
            if command == "VIN":
                return _Response("TESTVIN123")
            return _Response(is_null=True)

        def protocol_name(self):
            return "ISO"

    obd = types.SimpleNamespace(
        commands=types.SimpleNamespace(
            VIN="VIN",
            GET_DTC=None,
            PENDING_DTC=None,
            CALIBRATION_ID=None,
            CVN=None,
            ECU_NAME=None,
        )
    )
    updates = []
    cache = set()
    scanner = ObdScanner(Connection(), "/dev/rfcomm0", updates.append, cache, obd_module=obd)

    scanner.run()

    complete = next(payload for payload in updates if payload.get("scan_status") == "complete")
    identity = next(payload for payload in updates if payload.get("source") == "obd_scan_identity")

    assert complete["scan_profile"]["vin"] == "TESTVIN123"
    assert identity["profile_path"] == "vin_TESTVIN123"
    assert "vin_TESTVIN123" in cache
