"""OBD adapter type detection and STN/OBDLink batch-query support.

Supported adapter families:
  - ELM327 clone  – cheap BT dongles, slowest, single-query only
  - ELM327 genuine – faster processing, single-query only
  - STN / OBDLink  – STN1110/STN2120 chip (OBDLink MX+, EX, LX, …)
                     supports STPX multi-PID query → scan in seconds not minutes
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)


class AdapterKind(Enum):
    UNKNOWN = "unknown"
    ELM327_CLONE = "elm327_clone"
    ELM327_GENUINE = "elm327"
    STN = "stn"  # OBDLink MX+, EX, LX – STN1110 / STN2120 chip


@dataclass
class AdapterInfo:
    kind: AdapterKind = AdapterKind.UNKNOWN
    version: str = ""
    supports_stpx: bool = False   # multi-PID batch query via STPX command
    optimal_yield_s: float = 0.04  # recommended inter-query pause


# ---------------------------------------------------------------------------
# Low-level serial helpers
# ---------------------------------------------------------------------------

def _serial_port(connection: Any) -> Any | None:
    """Return the underlying pyserial port from an obd.OBD connection."""
    iface = getattr(connection, "interface", None)
    return getattr(iface, "_port", None) if iface is not None else None


def raw_send(port: Any, cmd: str, timeout: float = 1.5) -> str:
    """Write *cmd* to *port* and read back until '>' prompt or timeout.

    Strips the prompt character and surrounding whitespace from the result.
    Returns an empty string on any error.
    """
    try:
        port.reset_input_buffer()
        port.write(f"{cmd}\r".encode("ascii"))
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            n = getattr(port, "in_waiting", 0)
            if n:
                buf += port.read(n)
                if b">" in buf:
                    break
            else:
                time.sleep(0.01)
        return buf.decode("ascii", errors="ignore").replace(">", "").strip()
    except Exception as exc:
        log.debug("raw_send(%r) failed: %s", cmd, exc)
        return ""


# ---------------------------------------------------------------------------
# Adapter probing
# ---------------------------------------------------------------------------

def probe_adapter(
    connection: Any,
    locked_raw: Callable[[str], str] | None = None,
) -> AdapterInfo:
    """Detect the adapter type by sending AT/ST commands.

    *locked_raw* is an optional ``lambda cmd: str`` that serialises serial
    access via the shared OBD lock.  Falls back to direct port access when not
    provided (only safe when no other thread is querying the bus).
    """
    info = AdapterInfo()

    if locked_raw is not None:
        exchange: Callable[[str], str] = locked_raw
    else:
        port = _serial_port(connection)
        if port is None:
            log.debug("probe_adapter: no serial port accessible")
            return info
        def exchange(cmd: str) -> str:
            return raw_send(port, cmd)

    # --- STN / OBDLink ---
    # STI is an STN-specific command; genuine ELM327 returns an error.
    try:
        resp = exchange("STI")
        if resp and ("STN" in resp or "OBDLink" in resp):
            info.kind = AdapterKind.STN
            info.version = resp.splitlines()[0].strip()
            info.supports_stpx = True
            info.optimal_yield_s = 0.0
            log.info("STN/OBDLink adapter detected: %s", info.version)
            return info
    except Exception:
        log.debug("STI probe failed", exc_info=True)

    # --- ELM327 genuine vs clone ---
    try:
        resp = exchange("ATI")
        if resp:
            info.version = resp.splitlines()[0].strip()
            ver = info.version.upper()
            if "ELM327" in ver:
                genuine = any(v in ver for v in ("V1.5", "V2.0", "V2.1", "V2.2", "V2.3"))
                info.kind = (
                    AdapterKind.ELM327_GENUINE if genuine else AdapterKind.ELM327_CLONE
                )
                info.optimal_yield_s = 0.02 if genuine else 0.04
                log.info(
                    "ELM327 adapter detected: %s (%s)", info.version, info.kind.value
                )
    except Exception:
        log.debug("ATI probe failed", exc_info=True)

    return info


# ---------------------------------------------------------------------------
# STPX batch query (STN / OBDLink only)
# ---------------------------------------------------------------------------

# OBD-II Mode 01 decode table.
# Maps PID number → (python-obd command name, decoder function).
# The name matches the key used in the single-query live_data dict so the
# rest of the app can consume both paths identically.
# Decoder receives the data bytes (after mode-byte and PID-byte stripped).
_MODE1_DECODE: dict[int, tuple[str, Callable[[bytes], Any]]] = {
    0x04: ("ENGINE_LOAD",               lambda d: round(d[0] * 100 / 255, 1)),
    0x05: ("COOLANT_TEMP",              lambda d: d[0] - 40),
    0x06: ("SHORT_FUEL_TRIM_1",         lambda d: round((d[0] - 128) * 100 / 128, 1)),
    0x07: ("LONG_FUEL_TRIM_1",          lambda d: round((d[0] - 128) * 100 / 128, 1)),
    0x08: ("SHORT_FUEL_TRIM_2",         lambda d: round((d[0] - 128) * 100 / 128, 1)),
    0x09: ("LONG_FUEL_TRIM_2",          lambda d: round((d[0] - 128) * 100 / 128, 1)),
    0x0A: ("FUEL_PRESSURE",             lambda d: d[0] * 3),
    0x0B: ("INTAKE_PRESSURE",           lambda d: d[0]),
    0x0C: ("RPM",                       lambda d: round((d[0] * 256 + d[1]) / 4, 1)),
    0x0D: ("SPEED",                     lambda d: d[0]),
    0x0E: ("TIMING_ADVANCE",            lambda d: round((d[0] - 128) / 2, 1)),
    0x0F: ("INTAKE_TEMP",               lambda d: d[0] - 40),
    0x10: ("MAF",                       lambda d: round((d[0] * 256 + d[1]) / 100, 2)),
    0x11: ("THROTTLE_POS",              lambda d: round(d[0] * 100 / 255, 1)),
    0x14: ("O2_B1S1",                   lambda d: round(d[0] / 200, 3)),
    0x15: ("O2_B1S2",                   lambda d: round(d[0] / 200, 3)),
    0x16: ("O2_B1S3",                   lambda d: round(d[0] / 200, 3)),
    0x17: ("O2_B1S4",                   lambda d: round(d[0] / 200, 3)),
    0x1F: ("RUN_TIME",                  lambda d: d[0] * 256 + d[1]),
    0x21: ("DISTANCE_W_MIL",            lambda d: d[0] * 256 + d[1]),
    0x22: ("FUEL_RAIL_PRESSURE",        lambda d: round((d[0] * 256 + d[1]) * 0.079, 3)),
    0x23: ("FUEL_RAIL_PRESSURE_ABS",    lambda d: (d[0] * 256 + d[1]) * 10),
    0x2C: ("COMMANDED_EGR",             lambda d: round(d[0] * 100 / 255, 1)),
    0x2E: ("COMMANDED_EVAP_PURGE",      lambda d: round(d[0] * 100 / 255, 1)),
    0x2F: ("FUEL_LEVEL",                lambda d: round(d[0] * 100 / 255, 1)),
    0x31: ("DISTANCE_SINCE_DTC_CLEAR",  lambda d: d[0] * 256 + d[1]),
    0x33: ("BAROMETRIC_PRESSURE",       lambda d: d[0]),
    0x42: ("CONTROL_MODULE_VOLTAGE",    lambda d: round((d[0] * 256 + d[1]) / 1000, 3)),
    0x43: ("ABSOLUTE_LOAD",             lambda d: round((d[0] * 256 + d[1]) * 100 / 255, 1)),
    0x45: ("RELATIVE_THROTTLE_POS",     lambda d: round(d[0] * 100 / 255, 1)),
    0x46: ("AMBIANT_AIR_TEMP",          lambda d: d[0] - 40),
    0x47: ("THROTTLE_ACTUATOR",         lambda d: round(d[0] * 100 / 255, 1)),
    0x49: ("ACCELERATOR_POS_D",         lambda d: round(d[0] * 100 / 255, 1)),
    0x4A: ("ACCELERATOR_POS_E",         lambda d: round(d[0] * 100 / 255, 1)),
    0x4C: ("COMMANDED_THROTTLE_ACTUATOR", lambda d: round(d[0] * 100 / 255, 1)),
    0x4D: ("TIME_WITH_MIL",             lambda d: d[0] * 256 + d[1]),
    0x5A: ("RELATIVE_ACCEL_POS",        lambda d: round(d[0] * 100 / 255, 1)),
    0x5C: ("OIL_TEMP",                  lambda d: d[0] - 40),
    0x5E: ("FUEL_RATE",                 lambda d: round((d[0] * 256 + d[1]) / 20, 2)),
    0x62: ("ACTUAL_ENGINE_TORQUE_PERCENT", lambda d: d[0] - 125),
    0x63: ("ENGINE_REF_TORQUE",         lambda d: d[0] * 256 + d[1]),
    0x67: ("COOLANT_TEMP_2",            lambda d: d[1] - 40),
}

# Safely stay within a CAN frame: 8 bytes – 1 mode byte = 7 PIDs max; use 6.
_STPX_CHUNK = 6


def _parse_stpx_line(line: str) -> tuple[int, bytes] | None:
    """Parse one CAN response line from STPX output.

    Expected format (hex tokens separated by spaces):
        ``7E8 04 41 0C 1A F8``
        header  len  41  pid  data…

    Returns ``(pid, data_bytes)`` or ``None`` when the line cannot be parsed.
    """
    tokens = line.strip().split()
    if len(tokens) < 4:
        return None
    try:
        byte_vals = [int(t, 16) for t in tokens[1:]]  # skip CAN header
    except ValueError:
        return None
    # byte_vals[0]=length, [1]=0x41 (mode 01 response marker), [2]=PID, [3+]=data
    if len(byte_vals) < 3 or byte_vals[1] != 0x41:
        return None
    pid = byte_vals[2]
    data = bytes(byte_vals[3:])
    return pid, data


def batch_query_stpx(
    send_raw: Callable[[str], str],
    pid_numbers: list[int],
) -> dict[str, Any]:
    """Query *pid_numbers* (Mode 01) via STPX and return a live_data dict.

    Only STN/OBDLink adapters support the STPX command.
    PIDs absent from ``_MODE1_DECODE`` are silently skipped; the caller is
    responsible for querying them individually via python-obd if needed.

    Returns a dict with the same structure as the single-query path:
        ``{"RPM": {"value": 1234.5, "unit": ""}, ...}``
    """
    results: dict[str, Any] = {}

    for i in range(0, len(pid_numbers), _STPX_CHUNK):
        chunk = pid_numbers[i : i + _STPX_CHUNK]
        pid_hex = " ".join(f"{p:02X}" for p in chunk)
        cmd = f"STPX h:7DF, d:01 {pid_hex}"
        try:
            raw = send_raw(cmd)
        except Exception as exc:
            log.debug("STPX chunk [%s] failed: %s", pid_hex, exc)
            continue

        for line in raw.splitlines():
            parsed = _parse_stpx_line(line)
            if parsed is None:
                continue
            pid, data = parsed
            decoder_entry = _MODE1_DECODE.get(pid)
            if decoder_entry is None:
                continue
            name, decode_fn = decoder_entry
            try:
                value = decode_fn(data)
                results[name] = {"value": value, "unit": ""}
            except Exception:
                log.debug("STPX decode error for PID 0x%02X", pid)

    return results
