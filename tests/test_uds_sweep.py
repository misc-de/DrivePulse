"""Tests for the Car Lab deep DID sweep: range expansion, the enriched mock
simulator, and that sweep output renders through the existing discovery view
helper. All offline — the sweep mechanism is validated against the mock so it's
ready to run against a real module later."""
from __future__ import annotations

from drivepulse_app.cars.car_lab import partition_discovery_dids
from drivepulse_app.obd.mock import MockUdsSimulator
from drivepulse_app.obd.uds import DISCOVERY_SWEEP_RANGES, expand_ranges


def test_expand_ranges_dedupes_sorts_and_handles_reversed():
    assert expand_ranges([(0x10, 0x12), (0x11, 0x13)]) == [0x10, 0x11, 0x12, 0x13]
    assert expand_ranges([(0x05, 0x03)]) == [0x03, 0x04, 0x05]  # reversed input
    assert expand_ranges([]) == []


def test_sweep_ranges_cover_identification_and_coding():
    dids = set(expand_ranges(DISCOVERY_SWEEP_RANGES))
    assert 0xF190 in dids   # VIN (identification block)
    assert 0xF1FF in dids   # end of identification block
    assert 0x0600 in dids   # VAG long coding
    assert 0x0603 in dids   # the gated demo DID


def test_mock_sweep_collects_positives_gated_and_skips_absent():
    out = MockUdsSimulator().sweep("714", "77E", expand_ranges(DISCOVERY_SWEEP_RANGES))
    resp = out["did_responses"]
    # ASCII identification extras now answered by the enriched mock
    assert "ascii" in resp["F18C"]                 # serial number
    assert "F19D" in resp                          # installation date
    # adaptation-style block: present, non-ASCII
    assert resp["0601"]["hex"] == "012C"
    assert "ascii" not in resp["0601"]
    # long coding mapped into its own bucket
    assert "0600" in out["coding"]
    # gated DID: present but locked (non-0x31 NRC), not silently dropped
    assert resp["0603"]["gated"] is True
    assert resp["0603"]["nrc"] == "33"
    # an absent DID inside the swept range is NOT stored (0x31 = pure noise)
    assert "F1FE" not in resp
    # identification names populated
    assert out["identification"]["VIN"]["ascii"] == "WAUZZZ4GXDN000001"
    assert out["sweep"] is True


def test_mock_discover_includes_new_ident_dids():
    out = MockUdsSimulator().discover("714", "77E")
    assert "ECUSerialNumber" in out["identification"]
    assert "ECUInstallationDate" in out["identification"]


def test_sweep_output_renders_through_discovery_partition():
    out = MockUdsSimulator().sweep("714", "77E", expand_ranges(DISCOVERY_SWEEP_RANGES))
    known, failed = partition_discovery_dids(out, include_failures=True)
    known_keys = {k for k, _i, _v in known}
    failed_keys = {k for k, _i, _v in failed}
    assert "F190" in known_keys      # VIN reads as a known row
    assert "0601" in known_keys      # adaptation value reads too
    assert "0603" in failed_keys     # gated DID shows as a failure row
