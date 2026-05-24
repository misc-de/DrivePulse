"""Tests for obd_polling (poll-interval gating + response normalisation)
and obd_adapter._parse_stpx_line (CAN-response token parsing).

These functions sit in the hot path of the live OBD loop. A regression
in should_query_key bursts the connection; a regression in the STPX
parser corrupts every reading on STN/OBDLink adapters."""
from __future__ import annotations

from drivepulse_app.obd_adapter import _parse_stpx_line
from drivepulse_app.obd_polling import (
    OBD_POLL_INTERVALS,
    response_to_plain_value,
    should_query_key,
)


# ─── should_query_key ─────────────────────────────────────────────────────────

def test_should_query_key_fast_pids_always_true():
    # rpm/speed/coolant have interval 0 — every tick should query them.
    last: dict[str, float] = {"rpm": 9.99}  # just polled
    assert should_query_key("rpm", now=10.0, last_query=last) is True


def test_should_query_key_returns_true_when_never_polled():
    # Any PID with no prior timestamp gets polled immediately.
    assert should_query_key("throttle_pos", now=100.0, last_query={}) is True


def test_should_query_key_waits_for_interval():
    # throttle_pos has a 2s interval — at 1s after last poll, still wait.
    last = {"throttle_pos": 100.0}
    assert should_query_key("throttle_pos", now=101.0, last_query=last) is False


def test_should_query_key_polls_after_interval_elapsed():
    last = {"throttle_pos": 100.0}
    # At exactly the interval boundary, we ARE due.
    assert should_query_key("throttle_pos", now=102.0, last_query=last) is True


def test_should_query_key_unknown_pid_uses_default_2s_interval():
    # Unknown PIDs default to 2s spacing rather than every tick.
    last = {"weird": 0.0}
    assert should_query_key("weird", now=1.0, last_query=last) is False
    assert should_query_key("weird", now=2.0, last_query=last) is True


def test_obd_poll_intervals_schema_lock():
    # Lock the public set of polled keys + their cadences. Adding a key
    # downstream silently affects polling pressure — make it explicit.
    assert OBD_POLL_INTERVALS["rpm"] == 0.0
    assert OBD_POLL_INTERVALS["speed"] == 0.0
    assert OBD_POLL_INTERVALS["fuel_level"] >= 5.0  # cheap reading, slow rate


# ─── response_to_plain_value ─────────────────────────────────────────────────

class _PintLike:
    """Stand-in for a pint Quantity that python-OBD returns."""
    def __init__(self, magnitude, units):
        self.magnitude = magnitude
        self.units = units


class _Response:
    def __init__(self, value, null=False):
        self.value = value
        self._null = null
    def is_null(self):
        return self._null


def test_response_to_plain_value_none_returns_none():
    assert response_to_plain_value(None) is None


def test_response_to_plain_value_null_response_returns_none():
    assert response_to_plain_value(_Response(value=None, null=True)) is None


def test_response_to_plain_value_unwraps_pint_quantity():
    # python-OBD hands us pint Quantity objects — flatten to {value, unit}.
    pint = _PintLike(magnitude=1500.0, units="rpm")
    out = response_to_plain_value(_Response(value=pint))
    assert out == {"value": 1500.0, "unit": "rpm"}


def test_response_to_plain_value_handles_string_value():
    # Some readings (VIN, calibration ID) are strings — return them as-is.
    out = response_to_plain_value(_Response(value="ELM327"))
    assert out == "ELM327"


# ─── _parse_stpx_line ─────────────────────────────────────────────────────────

def test_parse_stpx_line_extracts_pid_and_data():
    # 7E8 04 41 0C 1A F8 → header, length=04, mode=41, PID=0C, data=[0x1A, 0xF8]
    pid, data = _parse_stpx_line("7E8 04 41 0C 1A F8")
    assert pid == 0x0C
    assert data == bytes([0x1A, 0xF8])


def test_parse_stpx_line_handles_no_data_bytes():
    # Some responses have only a length+marker+pid (no payload).
    pid, data = _parse_stpx_line("7E8 02 41 04")
    assert pid == 0x04
    assert data == b""


def test_parse_stpx_line_rejects_too_few_tokens():
    # Need at least 4 tokens (header + length + 41 + pid).
    assert _parse_stpx_line("7E8 02") is None
    assert _parse_stpx_line("") is None


def test_parse_stpx_line_rejects_non_response_marker():
    # Mode-01 *response* marker is 0x41. Anything else (request echo,
    # error frame, multi-frame continuation) is rejected.
    assert _parse_stpx_line("7E8 04 7F 0C 1A F8") is None  # 7F = neg response


def test_parse_stpx_line_rejects_non_hex_tokens():
    assert _parse_stpx_line("7E8 NA 41 0C 1A F8") is None


def test_parse_stpx_line_tolerates_extra_whitespace():
    # Real ELM/STN output may have padded spaces — strip and split should
    # handle either.
    pid, data = _parse_stpx_line("  7E8 04 41 0C 1A F8  ")
    assert pid == 0x0C
    assert data == bytes([0x1A, 0xF8])
