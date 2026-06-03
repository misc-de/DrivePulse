"""Tests for the vehicle-health snapshot the scanner now persists: Mode 06
monitors, Mode 01 readiness, Mode 0A permanent DTCs and Mode 09 PID 08 IUMPR.

The IUMPR / ISO-TP fixtures are the exact bytes captured from the car
(Audi A6, STN2255 over Bluetooth) so a parsing regression is caught."""
from __future__ import annotations

from drivepulse_app.obd.scanner import (
    ObdScanner,
    _reassemble_isotp,
    parse_iumpr,
)

# Real Mode 09 PID 08 reply captured from the engine ECU (7E8), headers on.
IUMPR_RAW = "\r".join([
    "7E8 10 2B 49 08 14 08 B1 21",
    "7E8 21 43 08 06 08 B1 00 00",
    "7E8 22 00 00 04 85 08 B1 00",
    "7E8 23 00 00 00 1A 4F 08 B1",
    "7E8 24 00 00 00 00 00 00 00",
    "7E8 25 00 0B F9 08 B1 00 00",
    "7E8 26 00 00 00 00 00 00 00",
])


# ─── ISO-TP reassembly ──────────────────────────────────────────────────────

def test_reassemble_single_frame():
    out = _reassemble_isotp("7E8 04 41 0C 1A F8")
    assert out["7E8"] == bytes([0x41, 0x0C, 0x1A, 0xF8])


def test_reassemble_multiframe_truncates_to_declared_length():
    out = _reassemble_isotp(IUMPR_RAW)
    payload = out["7E8"]
    # First-frame length nibble says 0x2B = 43 bytes; CAN padding is dropped.
    assert len(payload) == 0x2B
    assert payload[0:3] == bytes([0x49, 0x08, 0x14])  # mode, pid, NODI=20


def test_reassemble_skips_garbage_lines():
    out = _reassemble_isotp("garbage\n7E8 03 41 0D 50\nNA NA NA")
    assert out["7E8"] == bytes([0x41, 0x0D, 0x50])


# ─── IUMPR parse ────────────────────────────────────────────────────────────

def test_parse_iumpr_decodes_captured_counters():
    out = parse_iumpr(IUMPR_RAW)
    assert "7E8" in out
    vals = out["7E8"]["values"]
    assert vals["OBDCOND"] == 0x08B1   # 2225 — OBD monitoring condition counts
    assert vals["IGNCNTR"] == 0x2143   # 8515 — ignition cycles
    assert vals["CATCOMP1"] == 0x0806  # 2054
    assert vals["CATCOND1"] == 0x08B1  # 2225
    assert vals["O2SCOMP1"] == 0x0485  # 1157
    assert vals["EGRCOMP"] == 0x1A4F   # 6735
    assert len(out["7E8"]["raw_words"]) == 20  # NODI items


def test_parse_iumpr_ignores_unrelated_ecu_frames():
    # A frame that isn't a 49 08 reply must not produce a bogus IUMPR entry.
    assert parse_iumpr("7E8 04 41 0C 1A F8") == {}


def test_parse_iumpr_handles_empty():
    assert parse_iumpr("") == {}
    assert parse_iumpr("NO DATA") == {}


# ─── scanner query methods (with fakes, no real adapter) ────────────────────

class _Qty:
    def __init__(self, mag, units):
        self.magnitude = mag
        self.units = units


class _Test:
    def __init__(self, tid, name, value, lo, hi, passed):
        self.tid, self.name, self.value = tid, name, value
        self.min, self.max, self.passed = lo, hi, passed


class _MonitorVal:
    def __init__(self, tests):
        self.tests = tests


class _Resp:
    def __init__(self, value, null=False):
        self.value, self._null = value, null

    def is_null(self):
        return self._null


class _Cmd:
    def __init__(self, name, mode):
        self.name, self.mode = name, mode


class _Conn:
    def __init__(self, cmds):
        self.supported_commands = cmds


def _scanner(conn, query, obd_module=None):
    return ObdScanner(
        conn, "pty", lambda _d: None, set(),
        query_locked=query, obd_module=obd_module,
    )


def test_query_monitors_groups_and_skips_mids():
    conn = _Conn([_Cmd("MONITOR_CATALYST_B1", 6), _Cmd("MIDS_A", 6), _Cmd("RPM", 1)])
    resp = _Resp(_MonitorVal([_Test(0x84, "Unknown", _Qty(146.02, "percent"), 100.0, 655.35, True)]))
    out = _scanner(conn, lambda _c: resp)._query_monitors()
    assert "MIDS_A" not in out          # bitmask skipped
    assert "RPM" not in out             # not a monitor
    cat = out["MONITOR_CATALYST_B1"]
    assert cat[0]["tid"] == 0x84
    assert cat[0]["value"] == 146.02
    assert cat[0]["passed"] is True


def test_query_readiness_extracts_available_monitors():
    class _StatusTest:
        def __init__(self, available, complete):
            self.available, self.complete = available, complete

    class _Status:
        MIL = False
        DTC_count = 0
        ignition_type = "spark"
        MISFIRE_MONITORING = _StatusTest(True, True)
        CATALYST_MONITORING = _StatusTest(True, False)
        SECONDARY_AIR_SYSTEM_MONITORING = _StatusTest(False, False)  # unavailable → skip

    class _Obd:
        class commands:
            STATUS = _Cmd("STATUS", 1)

    out = _scanner(_Conn([]), lambda _c: _Resp(_Status()), obd_module=_Obd)._query_readiness()
    assert out["MIL"] is False
    assert out["ignition_type"] == "spark"
    assert out["monitors"]["MISFIRE_MONITORING"]["complete"] is True
    assert out["monitors"]["CATALYST_MONITORING"]["complete"] is False
    assert "SECONDARY_AIR_SYSTEM_MONITORING" not in out["monitors"]


def test_query_permanent_dtcs_without_obd_module_is_empty():
    assert _scanner(_Conn([]), lambda _c: _Resp(None, null=True))._query_permanent_dtcs() == []


def test_query_iumpr_without_raw_channel_is_empty():
    # No raw_send_locked provided → no IUMPR (e.g. ELM without raw access).
    assert _scanner(_Conn([]), lambda _c: _Resp(None, null=True))._query_iumpr() == {}
