"""Hintergrund-Aufzeichnung: Motor-an-Erkennung, Payload-Verarbeitung, Unit."""
from __future__ import annotations

from pathlib import Path

from drivepulse_app.db import DriveDB
from drivepulse_app.service import daemon, unit
from drivepulse_app.service.probe import ProbeResult, engine_running, parse_rpm, parse_voltage
from drivepulse_app.trip_recorder import TripRecorder


def test_parse_voltage_variants():
    assert parse_voltage("12.4V\r\r>") == 12.4
    assert parse_voltage("ATRV\r14,1V\r>") == 14.1
    assert parse_voltage("?\r>") is None
    assert parse_voltage("0.0V") is None  # unplausibel


def test_parse_rpm():
    assert parse_rpm("SEARCHING...\r41 0C 0B B8 \r\r>") == 750.0
    assert parse_rpm("410C1F40") == 2000.0
    assert parse_rpm("NO DATA\r>") is None


def test_engine_running_rules():
    assert not engine_running(ProbeResult(False), None)
    assert engine_running(ProbeResult(True, voltage=14.0), None)
    assert not engine_running(ProbeResult(True, voltage=12.5), None)
    # Smarte Lichtmaschine: 12,9 V reicht, wenn die Ruhespannung 12,4 V war
    assert engine_running(ProbeResult(True, voltage=12.9), 12.4)
    assert not engine_running(ProbeResult(True, voltage=12.6), 12.4)
    # Drehzahl schlägt Spannung
    assert engine_running(ProbeResult(True, voltage=12.3, rpm=800.0), None)
    assert not engine_running(ProbeResult(True, voltage=None, rpm=0.0), None)


def _obd(rpm: float, speed: float) -> dict:
    return {"source": "obd", "rpm": {"value": rpm}, "speed": {"value": speed}}


def test_handle_payload_records_trip_after_vin(tmp_path: Path):
    db = DriveDB(tmp_path / "d.sqlite3")
    trip = TripRecorder(db)
    state: dict = {"samples": 0, "last_sample": None}

    # Vor der VIN wird verworfen (wie in der App)
    daemon.handle_payload(db, trip, _obd(900, 0), state)
    assert trip.trip_id is None and state["samples"] == 0

    daemon.handle_payload(db, trip, {"source": "obd_scan_identity", "vin": "WVWZZZ1KZAW000001"}, state)
    assert trip.car_id is not None
    daemon.handle_payload(db, trip, {"source": "gps", "gps_lat": 51.0, "gps_lon": 7.0, "gps_speed": 30}, state)
    daemon.handle_payload(db, trip, _obd(1500, 30), state)
    daemon.handle_payload(db, trip, _obd(1600, 35), state)
    assert trip.trip_id is not None
    assert state["samples"] == 2 and state["last_sample"] is not None

    # Unbekannte VIN → Live-Auto, das die App beim nächsten Start promotet
    row = next(r for r in db.list_cars(include_live=True) if r["id"] == trip.car_id)
    assert int(row["is_live"] or 0) == 1
    trip.end_trip()
    db.close()


def test_handle_payload_reuses_known_car(tmp_path: Path):
    db = DriveDB(tmp_path / "d.sqlite3")
    car_id = db.upsert_car(vin="WVWZZZ1KZAW000001", is_live=False)
    trip = TripRecorder(db)
    daemon.handle_payload(db, trip, {"source": "obd_scan_identity", "vin": "WVWZZZ1KZAW000001"}, {})
    assert trip.car_id == car_id
    row = next(r for r in db.list_cars(include_live=True) if r["id"] == car_id)
    assert int(row["is_live"] or 0) == 0
    db.close()


def test_unit_text_runs_module_from_checkout(tmp_path: Path):
    text = unit.unit_text(python="/usr/bin/python3", root=tmp_path)
    assert "ExecStart=/usr/bin/python3 -m drivepulse_app.service" in text
    assert f"Environment=PYTHONPATH={tmp_path}" in text
    assert "WantedBy=default.target" in text


def test_app_running_ignores_own_process():
    # Der Testprozess selbst ist nicht die App
    assert daemon.app_running() is False


def test_is_app_cmdline():
    assert daemon.is_app_cmdline(b"python3\0-m\0drivepulse_app.app\0")
    assert daemon.is_app_cmdline(b"/usr/bin/python3\0/home/x/DrivePulse/drivepulse_app/app.py\0")
    assert not daemon.is_app_cmdline(b"bash\0-c\0pkill -f drivepulse_app.app\0")
    assert not daemon.is_app_cmdline(b"python3\0-m\0drivepulse_app.service\0")
