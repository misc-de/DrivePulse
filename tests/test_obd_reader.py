from __future__ import annotations

import json
import types
from pathlib import Path
from typing import ClassVar


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


class _Cmd:
    """python-obd-like command exposing a Mode-01 PID for STPX batching."""

    def __init__(self, name: str, pid: int) -> None:
        self.name = name
        self.pid = pid

    def __str__(self) -> str:
        return self.name


def _fake_obd_module_with_pids():
    commands = types.SimpleNamespace(
        RPM=_Cmd("RPM", 0x0C),
        SPEED=_Cmd("SPEED", 0x0D),
        COOLANT_TEMP=_Cmd("COOLANT_TEMP", 0x05),
        THROTTLE_POS=_Cmd("THROTTLE_POS", 0x11),
        ENGINE_LOAD=_Cmd("ENGINE_LOAD", 0x04),
        INTAKE_TEMP=_Cmd("INTAKE_TEMP", 0x0F),
        MAF=_Cmd("MAF", 0x10),
        FUEL_LEVEL=_Cmd("FUEL_LEVEL", 0x2F),
        RUN_TIME=_Cmd("RUN_TIME", 0x1F),
        CONTROL_MODULE_VOLTAGE=_Cmd("CONTROL_MODULE_VOLTAGE", 0x42),
    )
    return types.SimpleNamespace(OBD=lambda *a, **k: None, commands=commands)


def test_read_obd_batch_decodes_all_pids_in_one_round_trip(monkeypatch, drivepulse_module):
    """STN/OBDLink: a supports_stpx adapter must route the live read through a
    single STPX batch instead of one query per PID."""
    from drivepulse_app.obd import reader as obd_reader
    from drivepulse_app.obd.adapter import AdapterInfo, AdapterKind

    monkeypatch.setattr(obd_reader, "obd", _fake_obd_module_with_pids())
    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader.connection = _Connection(True)
    reader._adapter_info = AdapterInfo(kind=AdapterKind.STN, supports_stpx=True)

    frames = "\n".join([
        "7E8 04 41 0C 1A F8",   # RPM   = 1726.0
        "7E8 03 41 0D 64",      # SPEED = 100
        "7E8 03 41 05 78",      # COOLANT = 80
        "7E8 03 41 11 7F",      # THROTTLE ≈ 49.8
        "7E8 03 41 04 FF",      # ENGINE_LOAD = 100
        "7E8 03 41 0F 64",      # INTAKE = 60
        "7E8 04 41 10 01 00",   # MAF = 2.56
        "7E8 03 41 2F 80",      # FUEL ≈ 50.2
        "7E8 04 41 1F 00 64",   # RUN_TIME = 100
        "7E8 04 41 42 3A 18",   # VOLTAGE = 14.872
    ])
    monkeypatch.setattr(reader, "_send_raw_locked", lambda cmd: frames)

    payload = reader._read_obd()

    assert payload["rpm"]["value"] == 1726.0
    assert payload["speed"]["value"] == 100
    assert payload["control_module_voltage"]["value"] == 14.872
    # One batch round-trip, no per-PID queries, no errors.
    assert payload["_command_count"] == 1
    assert payload["_read_error_count"] == 0
    assert reader.connection.queries == []


def test_read_obd_batch_demotes_unanswered_pid_to_single_query(monkeypatch, drivepulse_module):
    """If the batch drops a PID the adapter didn't answer, it must fall back to
    an individual query so the gauge never goes blank."""
    from drivepulse_app.obd import reader as obd_reader
    from drivepulse_app.obd.adapter import AdapterInfo, AdapterKind

    monkeypatch.setattr(obd_reader, "obd", _fake_obd_module_with_pids())
    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader.connection = _Connection(True)
    reader._adapter_info = AdapterInfo(kind=AdapterKind.STN, supports_stpx=True)

    # Batch answers only RPM; the other nine PIDs must be queried individually.
    monkeypatch.setattr(reader, "_send_raw_locked", lambda cmd: "7E8 04 41 0C 1A F8")

    payload = reader._read_obd()

    assert payload["rpm"]["value"] == 1726.0          # from the batch
    assert payload["speed"]["value"] == 42.5          # from the single-query fallback
    assert payload["_command_count"] == 10            # 1 batch + 9 demoted singles
    assert payload["_read_error_count"] == 0
    assert len(reader.connection.queries) == 9


def test_response_to_plain_value_handles_null_quantity_and_fallback(drivepulse_module):
    from drivepulse_app.obd.polling import response_to_plain_value

    assert response_to_plain_value(_Response(is_null=True)) is None
    assert response_to_plain_value(_Response(_Quantity())) == {"value": 42.5, "unit": "rpm"}
    assert response_to_plain_value(_Response("plain")) == "plain"


def test_should_query_key_respects_slow_poll_intervals(drivepulse_module):
    from drivepulse_app.obd.polling import should_query_key

    assert should_query_key("speed", 100.0, {"speed": 99.9}) is True
    assert should_query_key("fuel_level", 100.0, {}) is True
    assert should_query_key("fuel_level", 100.0, {"fuel_level": 95.0}) is False
    assert should_query_key("fuel_level", 106.0, {"fuel_level": 95.0}) is True


def test_should_query_key_min_interval_floors_fast_pids(drivepulse_module):
    """Idle backoff: passing ``min_interval`` must raise the floor even for
    PIDs whose table value is 0 (rpm/speed/coolant). Slow PIDs must stay at
    their own (higher) interval when min_interval is smaller."""
    from drivepulse_app.obd.polling import should_query_key

    # speed is normally interval=0 → always True. With min_interval=2.0 it
    # behaves like a 2 s-interval PID.
    assert should_query_key("speed", 100.0, {"speed": 99.0}, min_interval=2.0) is False
    assert should_query_key("speed", 102.0, {"speed": 99.0}, min_interval=2.0) is True
    # fuel_level (interval=10) must not be lowered by a smaller min_interval.
    assert should_query_key(
        "fuel_level", 105.0, {"fuel_level": 100.0}, min_interval=2.0
    ) is False


def test_candidate_ports_prefers_explicit_port(monkeypatch, drivepulse_module):
    from drivepulse_app.obd import reader as obd_reader

    monkeypatch.setattr(obd_reader, "OBD_PORT", "/dev/rfcomm0")

    reader = drivepulse_module.ObdReader(lambda payload: None)

    assert reader._candidate_ports() == ["/dev/rfcomm0"]


def test_candidate_ports_discovers_bluetooth_usb_and_auto(monkeypatch, drivepulse_module):
    from drivepulse_app.obd import reader as obd_reader

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
    from drivepulse_app.obd import reader as obd_reader

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
    from drivepulse_app.obd import reader as obd_reader

    monkeypatch.setattr(obd_reader, "obd", _fake_obd_module([_Connection(False)]))
    monkeypatch.setattr(obd_reader, "OBD_PORT", "/dev/rfcomm0")
    monkeypatch.setattr(drivepulse_module.ObdReader, "_connection_log", lambda *args, **kwargs: None)

    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader._connect()

    assert reader.mock is True
    assert reader.mock_reason == "kein nutzbarer Dongle gefunden"
    assert reader.connected_port is None


def test_read_obd_collects_values_and_error_counts(monkeypatch, drivepulse_module):
    from drivepulse_app.obd import reader as obd_reader

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
    from drivepulse_app.obd import reader as obd_reader

    fake_obd = _fake_obd_module([])
    monkeypatch.setattr(obd_reader, "obd", fake_obd)
    # Values consumed by: ObdReader.__init__ (_last_motion_monotonic),
    # then one per _read_obd call.
    times = iter([100.0, 100.5, 101.0])
    monkeypatch.setattr(obd_reader.time, "monotonic", lambda: next(times))

    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader.connection = _Connection(True)

    first = reader._read_obd()
    second = reader._read_obd()

    assert first["_command_count"] == 7
    assert second["_command_count"] == 3
    assert second["throttle_pos"] == first["throttle_pos"]
    assert second["engine_load"] == first["engine_load"]


def test_idle_min_interval_zero_while_recently_moving(drivepulse_module):
    """Within the IDLE_HOLD window after the last motion sample, the floor
    must stay at 0 so fast PIDs continue to poll at full rate."""
    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader._last_motion_monotonic = 100.0
    # 5 s after motion: hold window (10 s) not yet expired.
    assert reader._idle_min_interval(105.0) == 0.0


def test_idle_min_interval_raises_floor_after_hold(drivepulse_module):
    """Once the vehicle has been parked past IDLE_HOLD_S, the floor must
    rise to IDLE_MIN_INTERVAL_S so fast PIDs back off."""
    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader._last_motion_monotonic = 100.0
    assert reader._idle_min_interval(120.0) == reader._IDLE_MIN_INTERVAL_S


def test_note_speed_for_idle_only_advances_on_real_motion(drivepulse_module):
    """A speed sample above the motion threshold must refresh the timestamp;
    a slow/stationary reading must NOT — otherwise the backoff would never
    engage. A bad/None reading is treated conservatively as motion."""
    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader._last_motion_monotonic = 50.0

    reader._note_speed_for_idle({"value": 0.0, "unit": "kph"}, 200.0)
    assert reader._last_motion_monotonic == 50.0  # parked → no advance

    reader._note_speed_for_idle({"value": 1.5, "unit": "kph"}, 201.0)
    assert reader._last_motion_monotonic == 50.0  # below threshold (3 km/h)

    reader._note_speed_for_idle({"value": 12.0, "unit": "kph"}, 202.0)
    assert reader._last_motion_monotonic == 202.0  # moving → advance

    reader._note_speed_for_idle(None, 250.0)
    assert reader._last_motion_monotonic == 250.0  # unknown → treat as motion


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
    from drivepulse_app.obd import reader as obd_reader

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
    from drivepulse_app.obd import reader as obd_reader

    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader.mock = False  # obd is mocked as None, forcing mock=True; override for log test

    reader._write_log({"speed": {"value": 12}})

    lines = obd_reader.LOG_FILE.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"speed": {"value": 12}}


def test_gpsd_line_ignores_bad_optional_numbers(drivepulse_module):
    from drivepulse_app.sensors.gps import GpsReader

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
    from drivepulse_app.sensors.gps import GpsReader

    updates = []
    reader = GpsReader(updates.append)

    reader._handle_gpsd_line(json.dumps({"class": "TPV", "mode": 3, "speed": "bad"}))
    reader._handle_gpsd_line(json.dumps({"class": "TPV", "mode": 3, "speed": -1}))

    assert updates == []


def test_obd_scanner_emits_scan_profile_without_profile_file(drivepulse_module):
    from drivepulse_app.obd.scanner import ObdScanner

    class Connection:
        supported_commands: ClassVar[set] = set()

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
