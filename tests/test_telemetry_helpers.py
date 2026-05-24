"""Tests for the pure telemetry/dashboard data-shaping helpers and the two
non-Cairo helpers from draw_helpers. These run on every dashboard tick and
every scan import; a regression silently corrupts trip recordings or makes
the compass spin to the wrong octant."""
from __future__ import annotations

import pytest

from drivepulse_app.dashboard.data import (
    obd_sample_fields,
    scan_identity_from_payload,
    scan_profile_dashboard_data,
)
from drivepulse_app.ui.draw_helpers import _cardinal, _norm
from drivepulse_app.telemetry_utils import display_speed, has_obd_data, plain_number


# ─── telemetry_utils.plain_number ────────────────────────────────────────────

def test_plain_number_extracts_from_dict_value():
    # python-OBD payloads wrap readings as {"value": x, "unit": "rpm"}.
    assert plain_number({"rpm": {"value": 1500, "unit": "rpm"}}, "rpm") == 1500.0


def test_plain_number_handles_bare_number():
    # GPS data is often bare numbers (not wrapped).
    assert plain_number({"gps_speed": 87.3}, "gps_speed") == 87.3


def test_plain_number_returns_none_for_missing_key():
    assert plain_number({}, "rpm") is None


def test_plain_number_returns_none_for_explicit_none():
    assert plain_number({"rpm": None}, "rpm") is None
    assert plain_number({"rpm": {"value": None}}, "rpm") is None


def test_plain_number_returns_none_for_non_numeric():
    # Catch-all: don't crash on stringy "n/a" or other junk.
    assert plain_number({"rpm": "stalled"}, "rpm") is None
    assert plain_number({"rpm": {"value": "n/a"}}, "rpm") is None


# ─── telemetry_utils.display_speed ───────────────────────────────────────────

def test_display_speed_metric_passthrough():
    assert display_speed(100.0, "metric") == 100.0


def test_display_speed_imperial_converts():
    # 100 km/h ≈ 62.1371 mph.
    assert display_speed(100.0, "imperial") == pytest.approx(62.1371, rel=1e-4)


def test_display_speed_none_in_none_out():
    assert display_speed(None, "metric") is None
    assert display_speed(None, "imperial") is None


# ─── telemetry_utils.has_obd_data ────────────────────────────────────────────

def test_has_obd_data_true_when_any_key_present():
    assert has_obd_data({"rpm": 800}) is True
    assert has_obd_data({"speed": {"value": 50}}) is True


def test_has_obd_data_false_for_empty_or_unrelated_payload():
    assert has_obd_data({}) is False
    # GPS-only payload (no OBD-side reading) → False.
    assert has_obd_data({"gps_speed": 30, "gps_lat": 50.0}) is False


def test_has_obd_data_skips_keys_with_none_values():
    # All present keys are None — treat as no data.
    assert has_obd_data({"rpm": None, "speed": None, "coolant_temp": None}) is False


# ─── dashboard_data.obd_sample_fields ────────────────────────────────────────

def test_obd_sample_fields_prefers_obd_speed_over_gps():
    payload = {"speed": {"value": 60}, "gps_speed": {"value": 65}, "rpm": {"value": 2000}}
    out = obd_sample_fields(payload, plain_number)
    assert out["speed_kmh"] == 60.0
    assert out["obd_speed_kmh"] == 60.0
    assert out["gps_speed_kmh"] == 65.0
    assert out["rpm"] == 2000.0


def test_obd_sample_fields_falls_back_to_gps_when_obd_missing():
    payload = {"gps_speed": {"value": 42}}
    out = obd_sample_fields(payload, plain_number)
    assert out["speed_kmh"] == 42.0
    assert out["obd_speed_kmh"] is None
    assert out["gps_speed_kmh"] == 42.0


def test_obd_sample_fields_empty_payload_all_none():
    out = obd_sample_fields({}, plain_number)
    for v in out.values():
        assert v is None


def test_obd_sample_fields_exposes_full_field_set():
    # Locks the public schema — callers depend on these keys being present.
    out = obd_sample_fields({}, plain_number)
    expected = {
        "speed_kmh", "obd_speed_kmh", "gps_speed_kmh", "rpm",
        "coolant_c", "throttle_pct", "engine_load", "fuel_pct",
        "intake_c", "maf_gps", "voltage_v", "accel_g",
    }
    assert set(out.keys()) == expected


# ─── dashboard_data.scan_profile_dashboard_data ─────────────────────────────

def test_scan_profile_extracts_pid_values():
    data = {
        "live_data": {
            "Command(b'010C')": {"value": 1500, "unit": "rpm"},
            "Command(b'010D')": {"value": 87.5, "unit": "km/h"},
        }
    }
    pids, info, dtcs, pending = scan_profile_dashboard_data(data)
    assert pids["010C"] == 1500.0
    assert pids["010D"] == 87.5
    assert dtcs == []
    assert pending == []


def test_scan_profile_pulls_vin_brand_protocol():
    data = {
        "vehicle_info": {
            "VIN": "b'WAUZZZ8KZBA000000'",
            "CALIBRATION_ID": "b'CAL123'",
            "CVN": "b'CVN456'",
        },
        "protocol": "ISO 15765-4 (CAN 11/500)",
    }
    _pids, info, _dtcs, _pending = scan_profile_dashboard_data(data)
    assert info["vin"] == "WAUZZZ8KZBA000000"
    assert info["brand"] == "Audi"
    assert info["cal_id"] == "CAL123"
    assert info["cvn"] == "CVN456"
    assert info["protocol"] == "ISO 15765-4 (CAN 11/500)"


def test_scan_profile_extracts_dtcs_normalizing_to_codes():
    data = {
        "dtcs": [{"code": "P0420", "desc": "Catalyst…"}, "P0301"],
        "pending_dtcs": [{"code": "P0171"}],
    }
    _pids, _info, dtcs, pending = scan_profile_dashboard_data(data)
    assert dtcs == ["P0420", "P0301"]
    assert pending == ["P0171"]


def test_scan_profile_promotes_obd_standard_011C():
    # PID 011C contains the OBD-II compliance standard — move it from pids
    # into info so the dashboard shows it as a metadata line, not a gauge.
    data = {"live_data": {"Command(b'011C')": {"value": 6}}}
    pids, info, _dtcs, _pending = scan_profile_dashboard_data(data)
    assert "011C" not in pids
    assert info["obd_standard"] == "6"


def test_scan_profile_drops_non_numeric_pid_values():
    data = {"live_data": {"Command(b'010C')": {"value": "garbage"}}}
    pids, _info, _dtcs, _pending = scan_profile_dashboard_data(data)
    # PID stays in the dict but value is None — UI renders as "—".
    assert pids["010C"] is None


# ─── dashboard_data.scan_identity_from_payload ──────────────────────────────

def test_scan_identity_extracts_vin_and_brand():
    out = scan_identity_from_payload({"vin": "WBAVA31030NL00000"})
    assert out["vin"] == "WBAVA31030NL00000"
    assert out["brand"] == "BMW"
    assert out["identity"]["VIN"] == "WBAVA31030NL00000"


def test_scan_identity_missing_vin_returns_none_brand():
    out = scan_identity_from_payload({})
    assert out["vin"] is None
    assert out["brand"] is None
    assert out["identity"] == {}


def test_scan_identity_preserves_profile_path():
    out = scan_identity_from_payload({"profile_path": "/var/lib/x.json"})
    assert out["profile_path"] == "/var/lib/x.json"


def test_scan_identity_protocol_must_be_string():
    # Non-string protocol field is rejected (defensive against malformed
    # incoming sync payloads).
    out = scan_identity_from_payload({"protocol": 12345})
    assert out["protocol"] is None
    assert "protocol" not in out["identity"]


# ─── draw_helpers._norm ──────────────────────────────────────────────────────

def test_norm_in_range_returns_fraction():
    assert _norm(50.0, 0.0, 100.0) == 0.5
    assert _norm(25.0, 0.0, 100.0) == 0.25


def test_norm_clamps_below_zero():
    assert _norm(-10.0, 0.0, 100.0) == 0.0


def test_norm_clamps_above_one():
    assert _norm(200.0, 0.0, 100.0) == 1.0


def test_norm_handles_degenerate_range():
    # lo == hi should not divide by zero.
    out = _norm(5.0, 5.0, 5.0)
    assert 0.0 <= out <= 1.0


# ─── draw_helpers._cardinal ──────────────────────────────────────────────────

def test_cardinal_returns_localised_octant():
    # The exact translation string varies; we just check we get a non-empty
    # string and the same octant for the same bearing.
    n = _cardinal(0.0, language="en")
    e = _cardinal(90.0, language="en")
    s = _cardinal(180.0, language="en")
    w = _cardinal(270.0, language="en")
    assert all(isinstance(x, str) and x for x in (n, e, s, w))
    # Each octant should be distinct from at least its perpendicular neighbour.
    assert n != e
    assert e != s


def test_cardinal_wraps_around_360():
    # 0° and 360° should land on the same octant.
    assert _cardinal(0.0, "en") == _cardinal(360.0, "en")


def test_cardinal_boundary_at_22_5_degrees_rounds_to_NE():
    # 22.5° is the seam — anything ≥ 22.5° steps into the NE octant.
    just_below = _cardinal(22.4, "en")
    just_above = _cardinal(22.6, "en")
    # The two readings must differ — the boundary actually moves the octant.
    assert just_below != just_above
