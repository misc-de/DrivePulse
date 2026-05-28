"""Tests for the Car Lab UDS mock (MockUdsSimulator) and the reader's mock path.

These guarantee the no-hardware workflow actually produces a clean, describable
diff: stable coding bytes, one self-changing "noise" byte, and a toggle that
flips exactly one real bit."""
from __future__ import annotations

import types

from drivepulse_app.obd.coding_diff import diff_snapshots, volatile_bytes
from drivepulse_app.obd.mock import MockUdsSimulator
from drivepulse_app.obd.uds import VAG_CODING_DID, candidate_modules


def _snap_bytes(sim: MockUdsSimulator, dids: list[int]) -> dict[int, bytes]:
    return {d: bytes.fromhex(h) for d, h in sim.snapshot(dids).items()}


def test_discover_reports_identification_and_coding():
    inv = MockUdsSimulator().discover("714", "77E")
    assert inv["mock"] is True
    assert inv["identification"]["VIN"]["ascii"] == "WAUZZZ4GXDN000001"
    assert f"{VAG_CODING_DID:04X}" in inv["coding"]


def test_coding_counter_byte_is_volatile_but_rest_is_stable():
    sim = MockUdsSimulator()
    samples = [_snap_bytes(sim, [VAG_CODING_DID]) for _ in range(5)]
    volatile = volatile_bytes(samples)
    # The trailing counter byte (index 5) drifts; the real coding bytes don't.
    assert (VAG_CODING_DID, 5) in volatile
    assert (VAG_CODING_DID, 3) not in volatile


def test_toggle_then_capture_yields_single_describable_change():
    sim = MockUdsSimulator()
    samples = [_snap_bytes(sim, [VAG_CODING_DID]) for _ in range(5)]
    volatile = volatile_bytes(samples)
    baseline = samples[-1]

    sim.toggle_function()  # user flips the simulated function
    after = _snap_bytes(sim, [VAG_CODING_DID])

    changes = diff_snapshots(baseline, after, volatile)
    assert len(changes) == 1
    c = changes[0]
    assert c.did == VAG_CODING_DID
    assert c.byte_index == 3
    assert c.bits == [0x08]


def test_mock_scan_modules_returns_present_subset():
    found = MockUdsSimulator().scan_modules(candidate_modules())
    txs = {m["tx"] for m in found}
    assert "7E0" in txs        # engine (universal)
    assert "714" in txs        # instruments (VAG)
    assert "7E5" not in txs    # an absent ECU is not reported


def test_reader_scan_modules_in_mock_mode(monkeypatch, drivepulse_module):
    import types as _types

    from drivepulse_app.obd import reader as obd_reader
    monkeypatch.setattr(obd_reader, "obd", _types.SimpleNamespace())
    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader.mock = True
    found = reader.scan_modules()
    assert any(m["tx"] == "7E0" for m in found)
    assert all({"name", "tx", "rx"} <= set(m) for m in found)


def test_reader_uses_mock_uds_when_in_mock_mode(monkeypatch, drivepulse_module):
    from drivepulse_app.obd import reader as obd_reader

    monkeypatch.setattr(obd_reader, "obd", types.SimpleNamespace())
    reader = drivepulse_module.ObdReader(lambda payload: None)
    reader.mock = True  # no real adapter

    snap = reader.uds_snapshot("714", "77E", [VAG_CODING_DID])
    assert VAG_CODING_DID in snap

    inv = reader.discover_module("714", "77E")
    assert inv["identification"]["VIN"]["ascii"] == "WAUZZZ4GXDN000001"

    # Toggle flips coding byte 3 bit 3 (independent of the noisy counter byte).
    before = bytes.fromhex(reader.uds_snapshot("714", "77E", [VAG_CODING_DID])[VAG_CODING_DID])
    reader.mock_uds_toggle()
    after = bytes.fromhex(reader.uds_snapshot("714", "77E", [VAG_CODING_DID])[VAG_CODING_DID])
    assert before[3] ^ after[3] == 0x08
