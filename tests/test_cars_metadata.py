"""Tests for cars_metadata pure helpers: PID parsing, unit display, VIN→brand,
value formatting. These functions ship in nearly every UI render path, so
regressions here surface as visible bugs across the cars detail view, the
scan chart, and the per-PID stat lines."""
from __future__ import annotations

from drivepulse_app.cars.metadata import (
    _extract_inner_string,
    _format_status_string,
    _format_value_unit,
    _parse_profile_pid_key,
    _unit_display,
    _wmi_to_brand,
)

# ─── _unit_display ────────────────────────────────────────────────────────────

def test_unit_display_translates_known_unit_en():
    assert _unit_display("revolutions_per_minute") == "rpm"
    assert _unit_display("kilometer_per_hour") == "km/h"
    assert _unit_display("degree_Celsius") == "°C"


def test_unit_display_uses_german_override_when_available():
    # German prefers "U/min" for RPM and falls back to the EN map otherwise.
    assert _unit_display("revolutions_per_minute", language="de") == "U/min"
    assert _unit_display("kilometer_per_hour", language="de") == "km/h"


def test_unit_display_returns_unknown_unit_unchanged():
    # Unknown unit strings pass through verbatim so the UI never silently
    # hides an unrecognised reading.
    assert _unit_display("furlong_per_fortnight") == "furlong_per_fortnight"


def test_unit_display_empty_strings_for_unitless():
    # "ratio" and "count" are intentionally blanked so the cell shows just
    # the number — no clutter.
    assert _unit_display("ratio") == ""
    assert _unit_display("count") == ""


# ─── _extract_inner_string ───────────────────────────────────────────────────

def test_extract_inner_string_strips_bytes_prefix():
    # python-OBD often returns b'XYZ'-stringified — strip the wrapper.
    assert _extract_inner_string("b'ELM327 v2.1'") == "ELM327 v2.1"
    assert _extract_inner_string('b"OBD2"') == "OBD2"


def test_extract_inner_string_passes_plain_string():
    assert _extract_inner_string("OBD2") == "OBD2"
    assert _extract_inner_string("  trim me  ") == "trim me"


def test_extract_inner_string_handles_none():
    assert _extract_inner_string(None) == ""


# ─── _wmi_to_brand ───────────────────────────────────────────────────────────

def test_wmi_to_brand_known_prefixes():
    assert _wmi_to_brand("WAUZZZ8KZBA000000") == "Audi"
    assert _wmi_to_brand("WBAVA31030NL00000") == "BMW"
    assert _wmi_to_brand("WVWZZZ1JZ3W000000") == "VW"


def test_wmi_to_brand_unknown_returns_empty():
    # Unknown WMIs fall back to "" so the caller can decide what to render.
    assert _wmi_to_brand("ZZZ1234567890ABCD") == ""


def test_wmi_to_brand_short_vin_returns_empty():
    # A 2-char string slices to "WA" — not in the map.
    assert _wmi_to_brand("WA") == ""


# ─── _parse_profile_pid_key ──────────────────────────────────────────────────

def test_parse_profile_pid_key_extracts_hex_uppercased():
    # Whatever python-OBD's repr looks like (bytes-quoted), we want the bare
    # PID in upper hex.
    assert _parse_profile_pid_key("Command(b'010c')") == "010C"
    assert _parse_profile_pid_key('Command(b"010C")') == "010C"


def test_parse_profile_pid_key_returns_empty_for_unmatched():
    assert _parse_profile_pid_key("no hex here") == ""
    assert _parse_profile_pid_key("") == ""


# ─── _format_status_string ───────────────────────────────────────────────────

def test_format_status_string_extracts_tuple_first_element():
    # "(MIL, 5 codes)" style → just the first quoted bit.
    assert _format_status_string("('MIL off',)") == "MIL off"


def test_format_status_string_passes_plain_text():
    assert _format_status_string("plain status") == "plain status"


# ─── _format_value_unit ──────────────────────────────────────────────────────

def test_format_value_unit_none_returns_em_dash():
    assert _format_value_unit(None) == "—"


def test_format_value_unit_dict_formats_value_band():
    # >=100 → no decimals, >=10 → one decimal, else two.
    assert _format_value_unit({"value": 2500, "unit": "rpm"}) == "2500 rpm"
    assert _format_value_unit({"value": 87.5, "unit": "km/h"}) == "87.5 km/h"
    assert _format_value_unit({"value": 0.34, "unit": "percent"}) == "0.34 %"


def test_format_value_unit_dict_translates_unit_via_language():
    assert _format_value_unit({"value": 800, "unit": "revolutions_per_minute"}, language="de") == "800 U/min"


def test_format_value_unit_dict_with_none_value_returns_em_dash():
    assert _format_value_unit({"value": None, "unit": "km/h"}) == "—"


def test_format_value_unit_dict_with_non_numeric_value_keeps_unit():
    # Don't crash on a stringy value — pass it through alongside the unit.
    out = _format_value_unit({"value": "n/a", "unit": "km/h"})
    assert "n/a" in out and "km/h" in out


def test_format_value_unit_string_payload_uses_status_formatter():
    assert _format_value_unit("('MIL off',)") == "MIL off"


def test_format_value_unit_unitless_payload_has_no_trailing_space():
    # The "ratio" unit maps to "" — make sure we don't end up with a dangling
    # space at the end of the formatted string.
    out = _format_value_unit({"value": 0.5, "unit": "ratio"})
    assert out == "0.50"
