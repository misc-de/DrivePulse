"""GPL-free native ELM327 backend — a drop-in fallback for python-OBD.

This module duck-types the small slice of the ``obd`` package that DrivePulse
actually uses (``obd.OBD``, ``obd.commands`` and the query/response objects),
built entirely on top of pyserial and the project's own Mode-01 decode table
(:data:`drivepulse_app.obd.adapter._MODE1_DECODE`).

It exists so the app can run — and ship in a Flatpak — without bundling
python-OBD (GPL v2), keeping the PolyForm Noncommercial license clear of GPL
copyleft. When python-OBD *is* installed (pip/AUR/source), the reader prefers
it because it covers more PIDs, protocols and adapter quirks. See CREDITS.md.

Backend selection lives in :mod:`drivepulse_app.obd.reader` (``obd_backend``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from drivepulse_app.diagnostics import get_logger
from drivepulse_app.obd.adapter import _MODE1_DECODE, raw_send

log = get_logger(__name__)

# ELM327 initialisation sequence. Echo/linefeeds/spaces off keeps responses
# compact and parser-friendly; ATH0 drops CAN headers so a Mode-01 reply is a
# bare "41 <pid> <data…>"; ATSP0 lets the adapter auto-negotiate the protocol.
_INIT_COMMANDS: tuple[str, ...] = ("ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATSP0")

# Error tokens an ELM327 may return instead of data; treat as "no value".
_ELM_ERRORS = ("NODATA", "STOPPED", "SEARCHING", "UNABLETOCONNECT", "BUSINIT", "ERROR", "?")


@dataclass(frozen=True)
class Command:
    """A queryable OBD command. Mirrors the duck type of an ``obd`` command:
    it stringifies to its name and carries the request mode/PID plus decoder."""

    name: str
    mode: int
    pid: int | None = None
    decode: Any = None  # Callable[[bytes], Any] | None

    def __str__(self) -> str:
        return self.name


class _Commands:
    """Namespace mirroring ``obd.commands`` — attributes are :class:`Command`."""


def _build_commands() -> _Commands:
    ns = _Commands()
    for pid, (name, decode_fn) in _MODE1_DECODE.items():
        setattr(ns, name, Command(name, 0x01, pid, decode_fn))
    # Mode 04 — clear stored DTCs and reset readiness monitors.
    setattr(ns, "CLEAR_DTC", Command("CLEAR_DTC", 0x04, None))  # noqa: B010 — _Commands has no static attrs
    return ns


commands = _build_commands()


@dataclass(frozen=True)
class _Quantity:
    """Minimal stand-in for a pint quantity: ``.magnitude`` + ``.units``.

    The native decoders return plain numbers in their canonical OBD units, so
    ``units`` is left empty — exactly what the STPX batch path already emits and
    what ``polling.response_to_plain_value`` expects."""

    magnitude: float
    units: str = ""


@dataclass(frozen=True)
class Response:
    """Query result duck-typing ``obd``'s response (``.is_null()`` + ``.value``)."""

    value: _Quantity | None

    def is_null(self) -> bool:
        return self.value is None


class _Interface:
    """Holds the pyserial port under ``._port`` so ``adapter._serial_port`` and
    the shared ``raw_send`` path work identically to a python-OBD connection."""

    def __init__(self, port: Any) -> None:
        self._port = port


class OBD:
    """Native ELM327 connection — duck-types the subset of ``obd.OBD`` we use."""

    def __init__(
        self,
        portstr: str | None,
        *,
        baudrate: int | None = 38400,
        timeout: float = 1.0,
        fast: bool = False,  # accepted for python-OBD signature parity; unused
        **_kwargs: Any,
    ) -> None:
        self._timeout = max(float(timeout), 0.5)
        self._protocol = ""
        self._connected = False
        self.interface: _Interface | None = None
        self.supported_commands: set[Any] = set()

        if not portstr:
            log.debug("native OBD: no port given, staying disconnected")
            return

        try:
            import serial  # pyserial — a hard dependency

            port = serial.serial_for_url(
                portstr,
                baudrate=baudrate or 38400,
                timeout=self._timeout,
                write_timeout=self._timeout,
            )
        except Exception as exc:
            log.debug("native OBD: cannot open %r: %s", portstr, exc)
            return

        self.interface = _Interface(port)
        try:
            self._connected = self._initialise(port)
        except Exception:
            log.debug("native OBD: init failed on %r", portstr, exc_info=True)
            self._connected = False
        if not self._connected:
            try:
                port.close()
            except Exception:
                pass
            self.interface = None

    # -- lifecycle -----------------------------------------------------------

    def _initialise(self, port: Any) -> bool:
        for cmd in _INIT_COMMANDS:
            raw_send(port, cmd, timeout=2.0 if cmd == "ATZ" else 1.0)
        # A successful Mode-01 supported-PID probe confirms a live ECU link.
        resp = _clean_hex(raw_send(port, "0100", timeout=self._timeout + 1.0))
        if "4100" not in resp:
            return False
        self._protocol = (raw_send(port, "ATDP", timeout=1.0) or "").strip()
        return True

    def is_connected(self) -> bool:
        return self._connected

    def status(self) -> str:
        return "Car Connected" if self._connected else "Not Connected"

    def protocol_name(self) -> str:
        return self._protocol or "unknown"

    def close(self) -> None:
        port = self.interface._port if self.interface is not None else None
        if port is not None:
            try:
                port.close()
            except Exception:
                pass
        self.interface = None
        self._connected = False

    # -- queries -------------------------------------------------------------

    def query(self, command: Command | None) -> Response:
        port = self.interface._port if self.interface is not None else None
        if not self._connected or port is None or command is None:
            return Response(None)

        if command.mode == 0x04:  # CLEAR_DTC
            resp = _clean_hex(raw_send(port, "04", timeout=self._timeout + 1.0))
            return Response(_Quantity(0.0) if "44" in resp else None)

        if command.pid is None or command.decode is None:
            return Response(None)

        resp = raw_send(port, f"01{command.pid:02X}", timeout=self._timeout)
        data = _parse_mode1(resp, command.pid)
        if data is None:
            return Response(None)
        try:
            return Response(_Quantity(float(command.decode(data))))
        except Exception:
            log.debug("native decode failed for %s", command.name, exc_info=True)
            return Response(None)


def _clean_hex(resp: str) -> str:
    """Uppercase the response and strip everything but hex digits."""
    if any(err in resp.upper().replace(" ", "") for err in _ELM_ERRORS):
        return ""
    return re.sub(r"[^0-9A-F]", "", resp.upper())


def _parse_mode1(resp: str, pid: int) -> bytes | None:
    """Extract the data bytes from a Mode-01 reply for *pid*.

    Locates the ``41 <pid>`` response marker and returns the bytes that follow,
    which is exactly what the :data:`_MODE1_DECODE` decoders consume.
    """
    cleaned = _clean_hex(resp)
    marker = f"41{pid:02X}"
    idx = cleaned.find(marker)
    if idx < 0:
        return None
    payload = cleaned[idx + len(marker):]
    payload = payload[: len(payload) - (len(payload) % 2)]  # drop dangling nibble
    if not payload:
        return None
    try:
        return bytes.fromhex(payload)
    except ValueError:
        return None
