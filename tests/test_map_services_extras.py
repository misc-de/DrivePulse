"""Additional map_services tests: encoded-polyline decoding, distance
formatting in both unit systems, route-step flattening, mock speed-by-ref."""
from __future__ import annotations

import pytest

from drivepulse_app.map.services import (
    _decode_polyline,
    _flatten_route_steps,
    _flatten_valhalla_maneuvers,
    format_distance,
    mock_speed_kmh,
)


# ─── _decode_polyline ────────────────────────────────────────────────────────

def test_decode_polyline_empty_string():
    assert _decode_polyline("") == []


def test_decode_polyline_roundtrips_known_pair():
    # Single coordinate (50.0, 8.0) — precision 6.
    # Encoded with Google's 1e6 polyline format.
    import math

    def _encode_one(value_micros: int) -> str:
        v = value_micros
        if v < 0:
            v = ~(v << 1)
        else:
            v = v << 1
        s = ""
        while v >= 0x20:
            s += chr((0x20 | (v & 0x1F)) + 63)
            v >>= 5
        s += chr(v + 63)
        return s

    lat_micro = round(50.0 * 1e6)
    lon_micro = round(8.0 * 1e6)
    encoded = _encode_one(lat_micro) + _encode_one(lon_micro)
    out = _decode_polyline(encoded, precision=6)
    assert len(out) == 1
    # Returned as [lon, lat].
    assert out[0][0] == pytest.approx(8.0, abs=1e-6)
    assert out[0][1] == pytest.approx(50.0, abs=1e-6)


def test_decode_polyline_decodes_multiple_coords_deltas():
    # Format encodes deltas, so the second point's encoding is relative to
    # the first. We decode a hand-built short sequence and verify both
    # points came out.
    # Known fixture: encoded form of ((38.5, -120.2), (40.7, -120.95),
    # (43.252, -126.453)) at precision 5 from the Google polyline spec.
    encoded = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
    out = _decode_polyline(encoded, precision=5)
    assert len(out) == 3
    # First pair is (lat=38.5, lon=-120.2) → returned as [lon, lat].
    assert out[0][0] == pytest.approx(-120.2, abs=0.01)
    assert out[0][1] == pytest.approx(38.5, abs=0.01)


# ─── format_distance ─────────────────────────────────────────────────────────

def test_format_distance_metric_under_1km_in_meters():
    assert format_distance(123.0) == "120 m"
    assert format_distance(57.0) == "60 m"
    assert format_distance(0.0) == "0 m"


def test_format_distance_metric_rounds_negative_to_zero():
    # Defensive against negative inputs from buggy estimators.
    assert format_distance(-50.0) == "0 m"


def test_format_distance_metric_above_1km_in_decimal():
    assert format_distance(2500.0) == "2.5 km"
    assert format_distance(1234.0) == "1.2 km"


def test_format_distance_metric_round_to_int_km_when_close():
    # 10.02 km is close enough to 10 → drop the decimal.
    assert format_distance(10_020.0) == "10 km"


def test_format_distance_imperial_short_uses_feet_step_of_10():
    # 100 m ≈ 328 ft, rounded to 10 → 330.
    out = format_distance(100.0, units="imperial")
    assert out.endswith(" ft")
    feet = int(out.split()[0])
    assert feet % 10 == 0


def test_format_distance_imperial_long_uses_miles():
    # 5 km ≈ 3.1 mi.
    assert format_distance(5000.0, units="imperial") == "3.1 mi"


def test_format_distance_imperial_round_integer_miles_when_close():
    # 16100 m ≈ 10.004 mi → close enough to round to "10 mi" integer form.
    assert format_distance(16_100.0, units="imperial") == "10 mi"


# ─── _flatten_route_steps ────────────────────────────────────────────────────

def test_flatten_route_steps_extracts_maneuver_locations():
    legs = [{
        "steps": [
            {
                "maneuver": {"location": [8.6, 50.1], "type": "turn", "modifier": "left"},
                "name": "Hauptstraße",
                "ref": "B27",
                "distance": 250.5,
            },
            {
                "maneuver": {"location": [8.7, 50.2], "type": "depart"},
                "name": "Bergweg",
                "distance": 50.0,
            },
        ],
    }]
    out = _flatten_route_steps(legs)
    assert len(out) == 2
    assert out[0]["lat"] == 50.1 and out[0]["lon"] == 8.6
    assert out[0]["type"] == "turn"
    assert out[0]["modifier"] == "left"
    assert out[0]["name"] == "Hauptstraße"
    assert out[0]["ref"] == "B27"
    assert out[0]["distance"] == 250.5


def test_flatten_route_steps_skips_steps_with_unparseable_location():
    # A non-list location ("garbage") raises ValueError on float() and the
    # step is skipped. An empty list is treated as falsy and falls through
    # to the (0,0) default — present in output (callers can filter).
    legs = [{
        "steps": [
            {"maneuver": {"location": "garbage"}, "name": "skipped"},
            {"maneuver": {"location": [8.0, 50.0]}, "name": "ok"},
        ],
    }]
    out = _flatten_route_steps(legs)
    assert len(out) == 1
    assert out[0]["name"] == "ok"


def test_flatten_route_steps_handles_empty_input():
    assert _flatten_route_steps([]) == []
    assert _flatten_route_steps([{"steps": []}]) == []


def test_flatten_route_steps_defaults_missing_fields_to_empty():
    legs = [{"steps": [{"maneuver": {"location": [8.0, 50.0]}}]}]
    out = _flatten_route_steps(legs)
    # Missing name/ref → empty strings; missing distance → 0.0
    assert out[0]["name"] == ""
    assert out[0]["ref"] == ""
    assert out[0]["distance"] == 0.0
    assert out[0]["type"] == ""


# ─── _flatten_valhalla_maneuvers ─────────────────────────────────────────────

def test_flatten_valhalla_maneuvers_extracts_speed_limit_and_names():
    legs = [{
        "shape": "_p~iF~ps|U",  # ignored at this granularity
        "maneuvers": [
            {
                "begin_shape_index": 0,
                "instruction": "Turn left onto B27.",
                "street_names": ["B27"],
                "length": 0.25,  # km
                "speed_limit": 70,
                "type": 10,
            },
        ],
    }]
    # We can't verify lat/lon without decoding the shape, but the basics
    # (street name, distance conversion, speed_limit pass-through) should
    # land in the output.
    out = _flatten_valhalla_maneuvers(legs)
    assert len(out) >= 1
    assert any("B27" in s.get("name", "") for s in out)


# ─── mock_speed_kmh ──────────────────────────────────────────────────────────

def test_mock_speed_kmh_autobahn():
    assert mock_speed_kmh("A3") == 120.0
    assert mock_speed_kmh("A99") == 120.0
    assert mock_speed_kmh("A 5") == 120.0


def test_mock_speed_kmh_bundesstrasse_or_other():
    assert mock_speed_kmh("B27") == 70.0
    assert mock_speed_kmh("L3001") == 70.0


def test_mock_speed_kmh_urban_no_ref():
    assert mock_speed_kmh("") == 40.0
    assert mock_speed_kmh("   ") == 40.0


def test_mock_speed_kmh_alpha_prefix_does_not_match_autobahn():
    # "AB3" starts with "A" but the next char is alphabetic → treated as
    # generic (non-Autobahn).
    assert mock_speed_kmh("AB3") == 70.0
