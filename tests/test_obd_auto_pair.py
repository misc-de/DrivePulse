"""Tests for the brand-agnostic auto-pair fallback (_auto_pair_nearby_obd).

The fallback must onboard an OBD dongle in range WITHOUT relying on the device
name: named OBD adapters are tried directly, unnamed/unknown ones are *probed*
(pair → SPP-verify → unpair again if they aren't a serial adapter), bounded by a
probe budget so a busy car park doesn't get mass-bonded.
"""
from __future__ import annotations

import threading

from drivepulse_app.obd import reader as reader_mod
from drivepulse_app.obd.reader import ObdReader


def _make_reader() -> ObdReader:
    r = ObdReader.__new__(ObdReader)
    r.stop_event = threading.Event()
    r._connection_log = lambda *_a, **_k: None  # type: ignore[method-assign]
    return r


def test_auto_pair_probes_unnamed_and_discards_non_obd(monkeypatch):
    # nearby: a named OBD dongle, an unnamed SPP dongle (the ELM!), and unnamed
    # earbuds that pair but expose no SPP.
    nearby = [
        ("OBDII  (AA:AA:AA:AA:AA:AA)", "bt:AA:AA:AA:AA:AA:AA"),
        ("BT BB:BB:BB:BB:BB:BB  (BB:BB:BB:BB:BB:BB)", "bt:BB:BB:BB:BB:BB:BB"),
        ("BT CC:CC:CC:CC:CC:CC  (CC:CC:CC:CC:CC:CC)", "bt:CC:CC:CC:CC:CC:CC"),
    ]
    spp = {
        "AA:AA:AA:AA:AA:AA": True,   # named OBD → SPP
        "BB:BB:BB:BB:BB:BB": True,   # unnamed but SPP → real ELM
        "CC:CC:CC:CC:CC:CC": False,  # unnamed, no SPP → earbuds
    }
    unpaired: list[str] = []
    monkeypatch.setattr(reader_mod, "scan_bt_paired_devices", lambda: [])
    monkeypatch.setattr(reader_mod, "scan_bt_nearby_devices", lambda **_kw: nearby)
    monkeypatch.setattr(reader_mod, "pair_bt_device", lambda addr: (True, "paired"))
    monkeypatch.setattr(reader_mod, "_has_spp_uuid", lambda addr: spp[addr.upper()])
    monkeypatch.setattr(reader_mod, "unpair_bt_device", lambda addr: unpaired.append(addr.upper()))

    paired_n = _make_reader()._auto_pair_nearby_obd()

    assert paired_n == 2                       # named OBD + unnamed SPP ELM kept
    assert unpaired == ["CC:CC:CC:CC:CC:CC"]   # non-SPP probe discarded, OBD kept


def test_auto_pair_probe_budget_caps_unnamed_attempts(monkeypatch):
    nearby = [
        (f"BT 0{i}:00:00:00:00:00  (0{i}:00:00:00:00:00)", f"bt:0{i}:00:00:00:00:00")
        for i in range(7)
    ]
    probed: list[str] = []
    monkeypatch.setattr(reader_mod, "scan_bt_paired_devices", lambda: [])
    monkeypatch.setattr(reader_mod, "scan_bt_nearby_devices", lambda **_kw: nearby)
    monkeypatch.setattr(reader_mod, "_has_spp_uuid", lambda addr: False)
    monkeypatch.setattr(reader_mod, "unpair_bt_device", lambda addr: None)

    def _pair(addr):
        probed.append(addr.upper())
        return (True, "paired")
    monkeypatch.setattr(reader_mod, "pair_bt_device", _pair)

    paired_n = _make_reader()._auto_pair_nearby_obd()

    assert paired_n == 0       # none expose SPP
    assert len(probed) == 5    # probe budget (_MAX_PROBES) enforced
