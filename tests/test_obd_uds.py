"""Tests for the read-only UDS client (drivepulse_app.obd.uds).

These exercise the pure parsing/decoding logic and the client orchestration
against a fake send_raw, so the whole read path is verified without hardware —
the real adapter only arrives later. The ISO-TP reassembly and negative-response
decoding sit at the heart of every module read."""
from __future__ import annotations

import pytest

from drivepulse_app.obd.uds import (
    IDENTIFICATION_DIDS,
    VAG_MODULES,
    UdsClient,
    UdsError,
    as_ascii,
    candidate_modules,
    did_payload,
    interpret,
    parse_isotp_response,
    standard_ecu_modules,
)


def _replies(mapping):
    """send_raw fake: returns mapping[cmd], defaulting to 'NO DATA'."""
    return lambda cmd: mapping.get(cmd, "NO DATA")


# --- parse_isotp_response ---------------------------------------------------

def test_parse_single_frame_returns_message_bytes():
    assert parse_isotp_response("62 F1 86 03\n>") == bytes([0x62, 0xF1, 0x86, 0x03])


def test_parse_multiframe_joins_numbered_rows_and_ignores_length_line():
    raw = "\n".join([
        "0014",                      # lone ISO-TP length line — must be ignored
        "0: 62 F1 90 57 30 4C 31",
        "1: 32 33 34 35 36 37 38",
        "2: 39",
        ">",
    ])
    out = parse_isotp_response(raw)
    assert out == bytes([0x62, 0xF1, 0x90]) + b"W0L123456789"


def test_parse_multiframe_orders_rows_by_sequence():
    raw = "1: 34 35 36\n0: 62 F1 90 31 32 33\n>"
    assert parse_isotp_response(raw) == bytes([0x62, 0xF1, 0x90]) + b"123456"


@pytest.mark.parametrize("reply", ["NO DATA", "CAN ERROR", "BUFFER FULL", "?", "STOPPED"])
def test_parse_raises_on_adapter_error_tokens(reply):
    with pytest.raises(UdsError):
        parse_isotp_response(reply)


def test_parse_raises_on_empty():
    with pytest.raises(UdsError):
        parse_isotp_response(">\n")


# --- interpret / negative responses ----------------------------------------

def test_interpret_positive_response():
    resp = interpret(bytes([0x62, 0xF1, 0x90, 0x01]))
    assert resp.positive
    assert resp.service == 0x62
    assert resp.negative is None


def test_interpret_negative_response_decodes_nrc_name():
    resp = interpret(bytes([0x7F, 0x22, 0x31]))
    assert not resp.positive
    assert resp.negative is not None
    assert resp.negative.service == 0x22
    assert resp.negative.nrc == 0x31
    assert resp.negative.name == "requestOutOfRange"


def test_interpret_unknown_nrc_is_labelled():
    resp = interpret(bytes([0x7F, 0x22, 0xAB]))
    assert resp.negative is not None
    assert "unknown" in resp.negative.name


# --- did_payload ------------------------------------------------------------

def test_did_payload_extracts_value_and_verifies_echoed_did():
    resp = interpret(bytes([0x62, 0xF1, 0x90]) + b"WVWZZZ")
    assert did_payload(resp, 0xF190) == b"WVWZZZ"


def test_did_payload_rejects_mismatched_did():
    resp = interpret(bytes([0x62, 0xF1, 0x90, 0x01]))
    assert did_payload(resp, 0xF191) is None


def test_did_payload_rejects_negative_response():
    resp = interpret(bytes([0x7F, 0x22, 0x31]))
    assert did_payload(resp, 0xF190) is None


# --- as_ascii ---------------------------------------------------------------

def test_as_ascii_returns_text_when_printable():
    assert as_ascii(b"W0L000043MB905291") == "W0L000043MB905291"


def test_as_ascii_returns_none_for_binary():
    assert as_ascii(bytes([0x01, 0x00, 0xFF])) is None


# --- UdsClient --------------------------------------------------------------

def test_read_data_by_identifier_builds_request_and_parses():
    client = UdsClient(_replies({"22F190": "62 F1 90 31 32 33"}))
    resp = client.read_data_by_identifier(0xF190)
    assert resp.positive
    assert did_payload(resp, 0xF190) == b"123"


def test_request_waits_out_response_pending(monkeypatch):
    import drivepulse_app.obd.uds as uds

    monkeypatch.setattr(uds.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def flaky(cmd: str) -> str:
        calls["n"] += 1
        return "7F 22 78" if calls["n"] == 1 else "62 F1 86 03"

    resp = UdsClient(flaky).read_data_by_identifier(0xF186)
    assert resp.positive
    assert calls["n"] == 2  # one pending reply, then the real answer


def test_scan_dids_yields_results_and_swallows_per_did_errors():
    client = UdsClient(_replies({
        "22F190": "62 F1 90 31 32 33",
        # 0xF191 absent → fake returns 'NO DATA' → UdsError → negative response
    }))
    results = dict(client.scan_dids([0xF190, 0xF191]))
    assert did_payload(results[0xF190], 0xF190) == b"123"
    assert not results[0xF191].positive


def test_scan_dids_calls_on_result_callback():
    seen: list[int] = []
    client = UdsClient(_replies({"22F190": "62 F1 90 01"}))
    list(client.scan_dids([0xF190], on_result=lambda did, _r: seen.append(did)))
    assert seen == [0xF190]


def test_open_close_configures_and_restores_adapter():
    sent: list[str] = []

    def record(cmd: str) -> str:
        sent.append(cmd)
        return "OK"

    client = UdsClient(record)
    client.open("7E0", "7E8", protocol="6")
    assert "ATSP6" in sent
    assert "ATSH7E0" in sent
    assert "ATCRA7E8" in sent
    sent.clear()
    client.close()
    assert "ATSH7DF" in sent  # restored to functional broadcast


def test_identification_did_table_has_vin():
    assert IDENTIFICATION_DIDS[0xF190] == "VIN"


# --- VAG module presets -----------------------------------------------------

def test_vag_instruments_preset_is_cluster_pair():
    assert VAG_MODULES["instruments"] == ("714", "77E")


def test_standard_ecus_cover_legislated_range_with_plus_8_responses():
    mods = standard_ecu_modules()
    assert len(mods) == 8  # 0x7E0..0x7E7
    by_tx = {m.tx: m for m in mods}
    assert by_tx["7E0"].name == "engine" and by_tx["7E0"].rx == "7E8"
    assert by_tx["7E1"].name == "transmission" and by_tx["7E1"].rx == "7E9"
    assert by_tx["7E7"].rx == "7EF"


def test_candidate_modules_combine_standard_and_vag_without_duplicates():
    mods = candidate_modules()
    pairs = [(m.tx, m.rx) for m in mods]
    assert len(pairs) == len(set(pairs))  # no duplicates
    # Universal legislated ECUs present...
    assert ("7E0", "7E8") in pairs
    # ...plus a VAG-only body module.
    assert ("714", "77E") in pairs


def test_is_present_true_on_any_uds_reply():
    # TesterPresent gets a positive reply → module is present.
    client = UdsClient(_replies({"3E00": "7E 00"}))
    client.set_target("7E0", "7E8")
    assert client.is_present() is True


def test_is_present_true_even_on_negative_reply():
    # A negative response still proves a module is answering.
    client = UdsClient(_replies({"3E00": "7F 3E 11"}))
    assert client.is_present() is True


def test_is_present_false_when_no_answer():
    client = UdsClient(lambda _cmd: "NO DATA")
    assert client.is_present() is False


def test_set_target_sends_header_and_filter():
    sent: list[str] = []
    UdsClient(lambda c: sent.append(c) or "OK").set_target("714", "77E")
    assert "ATSH714" in sent
    assert "ATCRA77E" in sent


def test_vag_body_module_response_ids_follow_plus_0x6a_rule():
    """All 0x7xx-range VAG modules answer on request + 0x6A; the legislated
    powertrain ECUs (0x7Ex) use the standard +8 instead."""
    for name, (tx, rx) in VAG_MODULES.items():
        tx_id, rx_id = int(tx, 16), int(rx, 16)
        expected = tx_id + (8 if 0x7E0 <= tx_id <= 0x7E7 else 0x6A)
        assert rx_id == expected, f"{name}: rx {rx} unexpected for tx {tx}"
