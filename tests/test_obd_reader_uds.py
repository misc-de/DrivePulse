"""Tests for the reader's read-only UDS diagnostic session (Car Lab backend).

Verify without hardware: the session pauses live polling, reads DIDs through the
UdsClient, and always restores the live loop afterwards (flag cleared even on
error)."""
from __future__ import annotations

import types


class _Iface:
    def __init__(self) -> None:
        self._port = object()  # opaque; raw_send is monkeypatched, never touched


class _Conn:
    interface = _Iface()

    def is_connected(self) -> bool:
        return True


def _wire_reader(monkeypatch, drivepulse_module, raw_send_fake):
    from drivepulse_app.obd import reader as obd_reader

    monkeypatch.setattr(obd_reader, "obd", types.SimpleNamespace())  # truthy
    monkeypatch.setattr(obd_reader, "raw_send", raw_send_fake)
    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader.mock = False
    reader.connection = _Conn()
    return reader


def test_uds_snapshot_reads_dids_through_session(monkeypatch, drivepulse_module):
    def fake_raw(_port, cmd: str) -> str:
        return "62 F1 90 31 32 33" if cmd == "22F190" else "OK"

    reader = _wire_reader(monkeypatch, drivepulse_module, fake_raw)
    snap = reader.uds_snapshot("714", "77E", [0xF190])

    assert snap == {0xF190: "313233"}
    # Live polling must be re-enabled after the session.
    assert reader._diagnostic_active is False


def test_read_obd_serves_cache_while_diagnostic_active(monkeypatch, drivepulse_module):
    from drivepulse_app.obd import reader as obd_reader

    monkeypatch.setattr(obd_reader, "obd", types.SimpleNamespace())
    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader.connection = _Conn()
    reader._obd_value_cache = {"rpm": {"value": 800.0, "unit": "rpm"}}
    reader._diagnostic_active = True

    payload = reader._read_obd()
    assert payload["rpm"] == {"value": 800.0, "unit": "rpm"}
    assert payload["_command_count"] == 0


def test_run_uds_session_clears_flag_on_exception(monkeypatch, drivepulse_module):
    reader = _wire_reader(monkeypatch, drivepulse_module, lambda _p, _c: "OK")

    def boom(_client):
        raise RuntimeError("session blew up")

    result = reader.run_uds_session("714", "77E", boom)
    assert result is None  # swallowed
    assert reader._diagnostic_active is False  # restored despite the error


def test_run_uds_session_noop_in_mock_mode(monkeypatch, drivepulse_module):
    reader = _wire_reader(monkeypatch, drivepulse_module, lambda _p, _c: "OK")
    reader.mock = True
    called = []
    assert reader.run_uds_session("714", "77E", called.append) is None
    assert called == []


def test_discover_module_collects_identification(monkeypatch, drivepulse_module):
    # VIN DID answers with an ASCII string; everything else returns NO DATA.
    def fake_raw(_port, cmd: str) -> str:
        if cmd == "22F190":
            return "62 F1 90 " + " ".join(f"{b:02X}" for b in b"WAUZZZ")
        return "NO DATA"

    reader = _wire_reader(monkeypatch, drivepulse_module, fake_raw)
    inv = reader.discover_module("714", "77E")

    assert inv["identification"]["VIN"]["ascii"] == "WAUZZZ"
    assert inv["tx"] == "714" and inv["rx"] == "77E"
