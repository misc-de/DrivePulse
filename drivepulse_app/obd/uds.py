"""Read-only UDS (ISO 14229) client over an ELM327/STN raw-command channel.

This module is the foundation for *exploring* manufacturer-specific control
modules (instrument cluster, body/comfort module, …) where features such as
ambient lighting or display options live. It only ever **reads** — there is no
write, no SecurityAccess key calculation, nothing that changes the vehicle.

How it works:
  * A control module is addressed by its CAN request/response IDs (e.g. tx
    ``7E0`` / rx ``7E8``). Unlike the functional broadcast ``7DF`` used for
    standard OBD-II, UDS talks to one module at a time.
  * Requests/responses use ISO-TP (ISO 15765-2) framing. With CAN auto
    formatting (``ATCAF1``, the ELM default) the adapter sends flow-control
    frames and reassembles multi-frame responses for us, so we only deal with
    the assembled message bytes.
  * The only services used here are read-only: ReadDataByIdentifier (0x22) and,
    optionally, DiagnosticSessionControl (0x10) to enter an extended session
    when a DID is not readable in the default session.

Transport assumptions (CAF on / headers off, the robust ELM default) are
documented on :func:`parse_isotp_response`. A genuine STN adapter (OBDLink MX+)
is recommended because it handles flow control and timing far more reliably
than cheap ELM clones, but any ISO-TP-capable adapter should work.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)

SendRaw = Callable[[str], str]

# Adapter replies that mean "no usable answer" rather than a UDS message.
_ERROR_TOKENS = (
    "NO DATA",
    "CAN ERROR",
    "BUFFER FULL",
    "BUS BUSY",
    "BUS ERROR",
    "FB ERROR",
    "DATA ERROR",
    "ERR",
    "STOPPED",
    "UNABLE TO CONNECT",
    "?",
)

# ISO 14229-1 negative-response codes worth understanding while exploring.
NRC_NAMES: dict[int, str] = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x14: "responseTooLong",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x25: "noResponseFromSubnetComponent",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted",
    0x78: "requestCorrectlyReceived-ResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}

# Standardised identification DIDs (ISO 14229-1 Annex C). These are the safest,
# most useful starting point for collecting data — most modules answer several
# of them in the default session and the values are human-readable.
IDENTIFICATION_DIDS: dict[int, str] = {
    0xF180: "BootSoftwareIdentification",
    0xF181: "ApplicationSoftwareIdentification",
    0xF182: "ApplicationDataIdentification",
    0xF183: "BootSoftwareFingerprint",
    0xF184: "ApplicationSoftwareFingerprint",
    0xF185: "ApplicationDataFingerprint",
    0xF186: "ActiveDiagnosticSession",
    0xF187: "VehicleManufacturerSparePartNumber",
    0xF188: "VehicleManufacturerECUSoftwareNumber",
    0xF189: "VehicleManufacturerECUSoftwareVersionNumber",
    0xF18A: "SystemSupplierIdentifier",
    0xF18B: "ECUManufacturingDate",
    0xF18C: "ECUSerialNumber",
    0xF18D: "SupportedFunctionalUnits",
    0xF190: "VIN",
    0xF191: "VehicleManufacturerECUHardwareNumber",
    0xF192: "SystemSupplierECUHardwareNumber",
    0xF193: "SystemSupplierECUHardwareVersionNumber",
    0xF194: "SystemSupplierECUSoftwareNumber",
    0xF195: "SystemSupplierECUSoftwareVersionNumber",
    0xF197: "SystemNameOrEngineType",
    0xF198: "RepairShopCodeOrTesterSerialNumber",
    0xF199: "ProgrammingDate",
    0xF19D: "ECUInstallationDate",
}

# VAG (VW/Audi/Seat/Škoda) UDS module CAN IDs, 11-bit, as routed through the
# gateway over the OBD connector (ISO 15765-4, ELM protocol "6"). For modules in
# the 0x7xx range the response ID is the request + 0x6A; the legislated
# powertrain ECUs use the standard request + 8. Other modules can be derived
# from their diagnostic address with the same +0x6A rule — verify against the
# raw output, since fitment varies by model/year.
VAG_MODULES: dict[str, tuple[str, str]] = {
    "engine":            ("7E0", "7E8"),  # addr 01
    "transmission":      ("7E1", "7E9"),  # addr 02
    "abs":               ("713", "77D"),  # addr 03
    "airbag":            ("715", "77F"),  # addr 15
    "instruments":       ("714", "77E"),  # addr 17 — Kombiinstrument
    "steering":          ("712", "77C"),  # addr 44
    "central_electrics": ("70E", "778"),  # addr 09 — Bordnetz / BCM
    "comfort":           ("73B", "7A5"),  # addr 46 — Komfortsteuergerät
    "gateway":           ("710", "77A"),  # addr 19
    "mmi":               ("773", "7DD"),  # addr 5F — Information electronics / MMI
}

# VAG-specific ReadDataByIdentifier targets useful while exploring coding.
# The long-coding bytes (read in an extended session) live at DID 0x0600 on
# UDS-era VAG modules; adaptation values are individual DIDs above 0x0600.
VAG_CODING_DID = 0x0600


@dataclass(frozen=True)
class ModuleAddr:
    """A candidate control-module address for an auto module scan."""

    name: str
    tx: str
    rx: str
    brand: str = ""


def standard_ecu_modules() -> list[ModuleAddr]:
    """The legislated 11-bit OBD-II ECU addresses (ISO 15765-4).

    Brand-independent: every OBD-II/UDS vehicle answers in this range for its
    emissions-relevant ECUs. Request 0x7E0..0x7E7 → response request + 8.
    """
    names = {0x7E0: "engine", 0x7E1: "transmission"}
    return [
        ModuleAddr(names.get(tx, f"ecu_{tx:03X}"), f"{tx:03X}", f"{tx + 8:03X}")
        for tx in range(0x7E0, 0x7E8)
    ]


def candidate_modules() -> list[ModuleAddr]:
    """All addresses an auto module scan probes: the universal legislated ECUs
    plus the known VAG body modules (deduplicated)."""
    modules = standard_ecu_modules()
    seen = {(m.tx, m.rx) for m in modules}
    for name, (tx, rx) in VAG_MODULES.items():
        if (tx, rx) not in seen:
            modules.append(ModuleAddr(name, tx, rx, brand="vag"))
            seen.add((tx, rx))
    return modules

_NUMBERED_LINE = re.compile(r"^\s*([0-9A-Fa-f]+)\s*:\s*(.*)$")
_HEX_PAIR = re.compile(r"[0-9A-Fa-f]{2}")


class UdsError(Exception):
    """Raised when the adapter returns no usable UDS message."""


@dataclass(frozen=True)
class NegativeResponse:
    """A UDS negative response (``7F <service> <nrc>``)."""

    service: int
    nrc: int

    @property
    def name(self) -> str:
        return NRC_NAMES.get(self.nrc, f"unknown(0x{self.nrc:02X})")

    def __str__(self) -> str:
        return f"NRC 0x{self.nrc:02X} {self.name} (service 0x{self.service:02X})"


@dataclass(frozen=True)
class UdsResponse:
    """A parsed UDS response message (already ISO-TP reassembled)."""

    data: bytes
    negative: NegativeResponse | None = None

    @property
    def positive(self) -> bool:
        return self.negative is None

    @property
    def service(self) -> int:
        return self.data[0] if self.data else -1


def _hex_bytes(text: str) -> bytes:
    """Collect 2-digit hex pairs from *text*, ignoring spaces and junk."""
    return bytes(int(tok, 16) for tok in _HEX_PAIR.findall(text))


def _looks_like_error(lines: list[str]) -> str | None:
    upper = " ".join(lines).upper()
    for token in _ERROR_TOKENS:
        if token in upper:
            return token
    return None


def parse_isotp_response(raw: str) -> bytes:
    """Reassemble an ELM/STN response into the UDS message bytes.

    Targets the robust default of CAN auto-formatting on (``ATCAF1``) with
    headers off (``ATH0``), where the adapter strips ISO-TP PCI bytes for us:

      * single frame  → one hex line, e.g. ``62 F1 90 57 30 4C ...``
      * multi frame   → numbered rows the adapter prints for long messages::

            0014
            0: 62 F1 90 57 30 4C 31
            1: 32 33 34 35 36 37 38

        The lone length line is ignored; numbered rows are concatenated in
        sequence order.

    Raises :class:`UdsError` when the adapter reports NO DATA / CAN ERROR / etc.
    """
    lines = [ln.strip() for ln in raw.replace(">", "").splitlines() if ln.strip()]
    if not lines:
        raise UdsError("empty response")
    err = _looks_like_error(lines)
    if err:
        raise UdsError(err)

    numbered: dict[int, bytes] = {}
    for ln in lines:
        m = _NUMBERED_LINE.match(ln)
        if m:
            numbered[int(m.group(1), 16)] = _hex_bytes(m.group(2))
    if numbered:
        return b"".join(numbered[k] for k in sorted(numbered))

    return b"".join(_hex_bytes(ln) for ln in lines)


def interpret(data: bytes) -> UdsResponse:
    """Wrap reassembled *data* as a positive or negative :class:`UdsResponse`."""
    if not data:
        raise UdsError("empty UDS message")
    if data[0] == 0x7F:
        service = data[1] if len(data) > 1 else 0
        nrc = data[2] if len(data) > 2 else 0
        return UdsResponse(data=data, negative=NegativeResponse(service, nrc))
    return UdsResponse(data=data)


def did_payload(response: UdsResponse, did: int) -> bytes | None:
    """Return the value bytes from a ReadDataByIdentifier (0x22) response.

    Verifies the echoed DID; returns ``None`` if the response isn't a positive
    0x22 reply for *did*.
    """
    d = response.data
    if not response.positive or len(d) < 3 or d[0] != 0x62:
        return None
    if (d[1] << 8 | d[2]) != did:
        return None
    return bytes(d[3:])


def as_ascii(data: bytes) -> str | None:
    """Render *data* as ASCII when it is fully printable, else ``None``."""
    if not data:
        return None
    if all(0x20 <= b < 0x7F for b in data):
        return data.decode("ascii")
    return None


class UdsClient:
    """Drives read-only UDS exchanges through a raw ELM/STN command sender.

    *send_raw* must write a command and return the adapter's text reply (the
    same contract as :func:`drivepulse_app.obd.adapter.raw_send`). The client
    never issues a write service.
    """

    # How long to keep waiting through 0x78 "response pending" replies.
    _PENDING_RETRIES = 4
    _PENDING_DELAY_S = 0.1

    def __init__(self, send_raw: SendRaw) -> None:
        self._send = send_raw
        self._open = False

    def open(self, tx_header: str, rx_filter: str, protocol: str = "6") -> None:
        """Configure the adapter to talk to one module.

        *tx_header* / *rx_filter* are hex strings (11-bit e.g. ``"7E0"``/``"7E8"``
        or 29-bit e.g. ``"18DA40F1"``/``"18DAF140"``). *protocol* selects the
        ELM CAN protocol (``"6"`` = ISO 15765-4 11-bit/500k, ``"7"`` = 29-bit).
        """
        self.init_adapter(protocol)
        self.set_target(tx_header, rx_filter)
        self._open = True

    def init_adapter(self, protocol: str = "6") -> None:
        """Adapter-wide setup (protocol + framing), independent of the target.

        Call once before probing many modules with :meth:`set_target`.
        """
        for cmd in (
            f"ATSP{protocol}",
            "ATE0",   # echo off
            "ATL0",   # linefeeds off
            "ATS1",   # spaces on (stable token parsing)
            "ATH0",   # headers off
            "ATCAF1",  # CAN auto formatting → ISO-TP handled by adapter
        ):
            self._send(cmd)

    def set_target(self, tx_header: str, rx_filter: str) -> None:
        """Point the adapter at one module (request/response CAN ids)."""
        self._send(f"ATSH{tx_header.upper()}")
        self._send(f"ATCRA{rx_filter.upper()}")
        self._open = True

    def is_present(self) -> bool:
        """True if the currently targeted module answers at all.

        Any UDS reply — positive *or* negative — proves a module is there; only a
        missing answer (NO DATA / timeout) means absent. Tries TesterPresent then
        a VIN read so modules that suppress one still get detected.
        """
        for payload in (b"\x3e\x00", b"\x22\xf1\x90"):
            try:
                self.request(payload)
                return True
            except UdsError:
                continue
        return False

    def close(self) -> None:
        """Restore functional-broadcast defaults so a shared adapter is reusable."""
        if not self._open:
            return
        for cmd in ("ATAR", "ATSH7DF", "ATH0"):
            self._send(cmd)
        self._open = False

    def request(self, payload: bytes) -> UdsResponse:
        """Send a raw UDS request and return the parsed response.

        Transparently waits out 0x78 "response pending" negative replies.
        """
        cmd = payload.hex().upper()
        for _ in range(self._PENDING_RETRIES):
            raw = self._send(cmd)
            response = interpret(parse_isotp_response(raw))
            if response.negative and response.negative.nrc == 0x78:
                time.sleep(self._PENDING_DELAY_S)
                continue
            return response
        return response

    def enter_session(self, session: int = 0x03) -> UdsResponse:
        """DiagnosticSessionControl (0x10). Default 0x03 = extended session."""
        return self.request(bytes([0x10, session]))

    def read_data_by_identifier(self, did: int) -> UdsResponse:
        """ReadDataByIdentifier (0x22) for a 16-bit *did*."""
        return self.request(bytes([0x22, (did >> 8) & 0xFF, did & 0xFF]))

    def scan_dids(
        self,
        dids: Iterable[int],
        on_result: Callable[[int, UdsResponse], None] | None = None,
    ) -> Iterator[tuple[int, UdsResponse]]:
        """Read each DID in *dids*, yielding ``(did, response)`` as it goes.

        Errors per DID are swallowed and reported as a negative-style response
        so a single unreadable DID never aborts the sweep.
        """
        for did in dids:
            try:
                response = self.read_data_by_identifier(did)
            except UdsError as exc:
                log.debug("DID 0x%04X: %s", did, exc)
                response = UdsResponse(data=b"", negative=NegativeResponse(0x22, 0x10))
            if on_result is not None:
                on_result(did, response)
            yield did, response
