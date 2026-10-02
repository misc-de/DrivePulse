"""Kurzer Kontakt zum OBD-Dongle: erreichbar? Motor an?

Der Dongle hängt meist dauerhaft an der OBD-Buchse (Dauerplus) und ist damit
auch bei geparktem Auto per Bluetooth erreichbar. Ob gefahren wird, verrät die
Bordspannung: läuft der Motor, lädt die Lichtmaschine und hebt sie über
~13,2 V; ein stehendes Auto hält ~12,0–12,8 V.

``ATRV`` misst der Adapter selbst an seinem Versorgungspin — kein Steuergerät
wird angesprochen, der CAN-Bus des Autos bleibt also schlafen. Die
Drehzahlabfrage (``010C``) weckt dagegen den Bus und läuft deshalb nur bei
starken Hinweisen (Phone lädt, Auto-Bluetooth verbunden), nie im Raster.
"""
from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)

# Ab dieser Spannung gilt der Motor als laufend (Lichtmaschine lädt).
ENGINE_ON_VOLTAGE = 13.2
# Smarte Lichtmaschinen laden im Fahrbetrieb teils nur auf ~12,8 V — ein
# Anstieg gegenüber der zuletzt gemessenen Ruhespannung zählt deshalb auch.
ENGINE_ON_RISE_V = 0.3
# Drehzahl, ab der der Motor sicher läuft (Leerlauf liegt deutlich darüber).
ENGINE_ON_RPM = 300.0

_CONNECT_TIMEOUT_S = 8.0
_VOLTAGE_RE = re.compile(r"(\d{1,2}(?:[.,]\d{1,2})?)\s*V", re.IGNORECASE)
_RPM_RE = re.compile(r"41\s*0C\s*([0-9A-F]{2})\s*([0-9A-F]{2})", re.IGNORECASE)


@dataclass
class ProbeResult:
    reachable: bool
    voltage: float | None = None
    rpm: float | None = None
    error: str = ""


def parse_voltage(text: str) -> float | None:
    m = _VOLTAGE_RE.search(text or "")
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    # Plausibel nur zwischen 6 und 18 V (12-V-Bordnetz)
    return v if 6.0 <= v <= 18.0 else None


def parse_rpm(text: str) -> float | None:
    m = _RPM_RE.search((text or "").replace("\r", " ").replace("\n", " "))
    if not m:
        return None
    return (int(m.group(1), 16) * 256 + int(m.group(2), 16)) / 4.0


def engine_running(
    result: ProbeResult, rest_voltage: float | None,
) -> bool:
    """Entscheidung aus einem Probe-Ergebnis und der letzten Ruhespannung."""
    if not result.reachable:
        return False
    if result.rpm is not None and result.rpm >= ENGINE_ON_RPM:
        return True
    v = result.voltage
    if v is None:
        return False
    if v >= ENGINE_ON_VOLTAGE:
        return True
    return rest_voltage is not None and v - rest_voltage >= ENGINE_ON_RISE_V


def _read_until_prompt(sock: socket.socket, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline:
        sock.settimeout(max(0.05, deadline - time.monotonic()))
        try:
            chunk = sock.recv(256)
        except TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
        if b">" in chunk:
            break
    return buf.decode("ascii", errors="replace")


def _cmd(sock: socket.socket, cmd: str, timeout: float = 3.0) -> str:
    sock.sendall(cmd.encode("ascii") + b"\r")
    return _read_until_prompt(sock, timeout)


def probe_dongle(addr: str, channel: int = 1, *, check_rpm: bool = False) -> ProbeResult:
    """Verbinden, Spannung (und optional Drehzahl) lesen, sofort wieder trennen."""
    try:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    except (OSError, AttributeError) as exc:
        return ProbeResult(False, error=f"socket: {exc}")
    try:
        sock.settimeout(_CONNECT_TIMEOUT_S)
        try:
            sock.connect((addr, channel))
        except OSError as exc:
            return ProbeResult(False, error=str(exc))
        # Evtl. halb gesendete Zeile aus einer früheren Sitzung verwerfen
        _cmd(sock, "", timeout=1.0)
        _cmd(sock, "ATE0")
        voltage = parse_voltage(_cmd(sock, "ATRV"))
        rpm = None
        if check_rpm:
            # Protokollsuche (SEARCHING...) kann einige Sekunden dauern
            rpm = parse_rpm(_cmd(sock, "010C", timeout=12.0))
        return ProbeResult(True, voltage=voltage, rpm=rpm)
    except OSError as exc:
        return ProbeResult(True, error=str(exc))
    finally:
        try:
            sock.close()
        except OSError:
            pass
