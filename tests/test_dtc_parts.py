"""Tests for _dtc_parts, the normaliser that the cars detail and scan
detail views rely on to turn whatever shape `dtcs`/`pending_dtcs` came in
as into a clean (code, description) tuple. Real-world inputs include
dict, bytes, tuple, JSON-stringified dict and "CODE: desc" strings —
each of those shapes must render the same way in the UI."""
from __future__ import annotations

from drivepulse_app.cars.scan_widgets import _dtc_parts


def test_dtc_parts_dict_with_code_and_description():
    assert _dtc_parts({"code": "P0420", "description": "Catalyst"}) == ("P0420", "Catalyst")


def test_dtc_parts_dict_missing_description_returns_empty_desc():
    assert _dtc_parts({"code": "U0100"}) == ("U0100", "")


def test_dtc_parts_dict_alternate_case_keys():
    # Some legacy paths used capitalised keys — accept both.
    assert _dtc_parts({"Code": "B1234", "desc": "Body something"}) == ("B1234", "Body something")


def test_dtc_parts_bytes_decoded():
    assert _dtc_parts(b"P0301") == ("P0301", "")


def test_dtc_parts_tuple_pair():
    assert _dtc_parts(("P0420", "Catalyst")) == ("P0420", "Catalyst")


def test_dtc_parts_list_with_single_element():
    assert _dtc_parts(["P0420"]) == ("P0420", "")


def test_dtc_parts_json_string_dict_is_parsed():
    # When a dict slips through as a JSON-encoded string the user used to
    # see literal "{'code': '...', ...}" in the UI — we recover the parts.
    assert _dtc_parts('{"code": "P0171", "description": "System Too Lean"}') == (
        "P0171", "System Too Lean",
    )


def test_dtc_parts_code_colon_description_string():
    assert _dtc_parts("P0420: Catalyst System Efficiency Below Threshold") == (
        "P0420", "Catalyst System Efficiency Below Threshold",
    )


def test_dtc_parts_freeform_string_with_colon_kept_as_code():
    # "Notice: foo" is NOT a real code → leave the whole string in code,
    # description empty, so we don't lie about the structure.
    assert _dtc_parts("Notice: foo") == ("Notice: foo", "")


def test_dtc_parts_plain_code_string():
    assert _dtc_parts("P0301") == ("P0301", "")


def test_dtc_parts_none_returns_question_mark():
    assert _dtc_parts(None) == ("?", "")


def test_dtc_parts_code_colon_with_extra_colons_in_description():
    # Descriptions sometimes contain colons (e.g. "Mode: Reading..."). Only
    # the first colon separates code from description.
    code, desc = _dtc_parts("P0420: Mode: ratio out of range")
    assert code == "P0420"
    assert desc == "Mode: ratio out of range"
