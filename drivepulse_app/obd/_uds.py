"""Read-only UDS diagnostics (Car Lab) for :class:`ObdReader`.

Module discovery, DID sweeps and snapshots run over the reader's live
connection. Every session pauses the live poll loop (``_diagnostic_active``)
and holds the OBD lock, so module-addressed traffic never interleaves with
the 7DF functional broadcast. Without a real connection, only an explicitly
chosen mock mode serves simulated modules.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from drivepulse_app.diagnostics import get_logger
from drivepulse_app.obd.adapter import _serial_port, raw_send
from drivepulse_app.obd.mock import MockUdsSimulator

log = get_logger(__name__)


class ObdUdsMixin:
    # Concrete ObdReader state surfaced to this mixin. See
    # project_mixin_typing.md.
    connection: Any
    mock: bool
    force_mock: bool
    stop_event: threading.Event
    _obd_lock: threading.Lock
    _diagnostic_active: bool
    _mock_uds: MockUdsSimulator

    def run_uds_session(
        self,
        tx: str,
        rx: str,
        fn: Callable[[Any], Any],
        protocol: str = "6",
    ) -> Any:
        """Run *fn(client)* against one control module over a read-only UDS session.

        Pauses live polling (``_diagnostic_active``) and holds the OBD lock for the
        whole session so module-addressed traffic never interleaves with the live
        loop. The adapter's CAN header is restored to the functional broadcast on
        exit. Returns ``fn``'s result, or ``None`` when no real connection exists.
        """
        from drivepulse_app.obd.uds import UdsClient

        if self.connection is None or self.mock:
            return None
        port = _serial_port(self.connection)
        if port is None:
            return None

        self._diagnostic_active = True
        t0 = time.monotonic()
        log.info(
            "UDS session begin: port=%s proto=%s tx=%s rx=%s", port, protocol, tx.upper(), rx.upper()
        )
        try:
            with self._obd_lock:
                client = UdsClient(lambda cmd: raw_send(port, cmd))
                client.open(tx, rx, protocol=protocol)
                try:
                    return fn(client)
                finally:
                    client.close()
        except Exception:
            log.exception("UDS session failed (tx=%s rx=%s)", tx, rx)
            return None
        finally:
            self._diagnostic_active = False
            log.info("UDS session end (%.1fs)", time.monotonic() - t0)

    def discover_module(self, tx: str, rx: str, protocol: str = "6") -> dict[str, Any]:
        """Read-only inventory of one module: identification DIDs + VAG coding DID.

        Returns a JSON-friendly dict suitable for ``DriveDB.add_discovery``.
        """
        from drivepulse_app.obd.uds import (
            IDENTIFICATION_DIDS,
            VAG_CODING_DID,
            as_ascii,
            did_payload,
        )

        if self.mock:
            # Simulated data only in explicit mock mode; a no-dongle fallback
            # has nothing real to read (see scan_modules).
            return self._mock_uds.discover(tx, rx) if self.force_mock else {}

        def work(client: Any) -> dict[str, Any]:
            out: dict[str, Any] = {
                "created_at": datetime.now(UTC).isoformat(),
                "tx": tx.upper(), "rx": rx.upper(),
                "identification": {}, "coding": {}, "did_responses": {},
            }
            for did, resp in client.scan_dids([*IDENTIFICATION_DIDS, VAG_CODING_DID]):
                key = f"{did:04X}"
                payload = did_payload(resp, did)
                if payload is not None:
                    entry: dict[str, Any] = {"hex": payload.hex().upper()}
                    ascii_val = as_ascii(payload)
                    if ascii_val is not None:
                        entry["ascii"] = ascii_val
                    out["did_responses"][key] = entry
                    if did in IDENTIFICATION_DIDS:
                        out["identification"][IDENTIFICATION_DIDS[did]] = entry
                    if did == VAG_CODING_DID:
                        out["coding"][key] = entry
                elif resp.negative is not None:
                    out["did_responses"][key] = {
                        "nrc": f"{resp.negative.nrc:02X}",
                        "nrc_name": resp.negative.name,
                    }
            return out

        return self.run_uds_session(tx, rx, work, protocol) or {}

    def sweep_module(
        self,
        tx: str,
        rx: str,
        ranges: list[tuple[int, int]] | None = None,
        protocol: str = "6",
    ) -> dict[str, Any]:
        """Deep read-only sweep of a module's DID space (discovery-shaped dict).

        Reads every DID in *ranges* (default :data:`DISCOVERY_SWEEP_RANGES`) and
        records: every positive value, plus negatives that are NOT
        ``requestOutOfRange`` (0x31). A 0x31 means "no such DID" — pure noise
        over a big sweep — while any other NRC (securityAccessDenied,
        conditionsNotCorrect, session…) means the DID *exists* but isn't
        readable right now, which is exactly what's worth knowing.
        """
        from drivepulse_app.obd.uds import (
            DISCOVERY_SWEEP_RANGES,
            IDENTIFICATION_DIDS,
            VAG_CODING_DID,
            as_ascii,
            did_payload,
            expand_ranges,
        )

        dids = expand_ranges(ranges or list(DISCOVERY_SWEEP_RANGES))

        if self.mock:
            return self._mock_uds.sweep(tx, rx, dids) if self.force_mock else {}

        def work(client: Any) -> dict[str, Any]:
            log.info(
                "deep DID sweep start: tx=%s rx=%s dids=%d", tx.upper(), rx.upper(), len(dids)
            )
            t0 = time.monotonic()
            out: dict[str, Any] = {
                "created_at": datetime.now(UTC).isoformat(),
                "tx": tx.upper(), "rx": rx.upper(), "sweep": True,
                "did_count": len(dids),
                "identification": {}, "coding": {}, "did_responses": {},
            }
            positive = gated = 0
            for did, resp in client.scan_dids(dids, log_each=True):
                key = f"{did:04X}"
                payload = did_payload(resp, did)
                if payload is not None:
                    entry: dict[str, Any] = {"hex": payload.hex().upper()}
                    ascii_val = as_ascii(payload)
                    if ascii_val is not None:
                        entry["ascii"] = ascii_val
                    out["did_responses"][key] = entry
                    positive += 1
                    if did in IDENTIFICATION_DIDS:
                        out["identification"][IDENTIFICATION_DIDS[did]] = entry
                    if did == VAG_CODING_DID:
                        out["coding"][key] = entry
                elif resp.negative is not None and resp.negative.nrc != 0x31:
                    out["did_responses"][key] = {
                        "nrc": f"{resp.negative.nrc:02X}",
                        "nrc_name": resp.negative.name,
                        "gated": True,
                    }
                    gated += 1
            log.info(
                "deep DID sweep done: %d positive, %d gated of %d DIDs in %.1fs",
                positive, gated, len(dids), time.monotonic() - t0,
            )
            return out

        return self.run_uds_session(tx, rx, work, protocol) or {}

    def uds_snapshot(
        self, tx: str, rx: str, dids: list[int], protocol: str = "6"
    ) -> dict[int, str]:
        """Read *dids* from one module once; return ``{did: hex_string}`` positives."""
        from drivepulse_app.obd.uds import did_payload

        if self.mock:
            # Simulated data only in explicit mock mode; a no-dongle fallback
            # has nothing real to read (see scan_modules).
            return self._mock_uds.snapshot(dids) if self.force_mock else {}

        def work(client: Any) -> dict[int, str]:
            out: dict[int, str] = {}
            for did, resp in client.scan_dids(dids):
                payload = did_payload(resp, did)
                if payload is not None:
                    out[did] = payload.hex().upper()
            return out

        return self.run_uds_session(tx, rx, work, protocol) or {}

    def scan_modules(self, protocol: str = "6") -> list[dict[str, str]]:
        """Probe known module addresses; return those that answer (read-only).

        Brand-independent: the legislated 0x7E0–0x7E7 ECUs answer on every
        OBD-II/UDS vehicle, plus the known VAG body modules. Each entry is
        ``{"name", "tx", "rx"}``.
        """
        from drivepulse_app.obd.uds import UdsClient, candidate_modules

        candidates = candidate_modules()
        if self.mock:
            # Only an explicitly chosen mock mode serves simulated modules. An
            # automatic no-dongle fallback (mock without force_mock) has no real
            # bus, so it must report nothing rather than fabricate control units.
            return self._mock_uds.scan_modules(candidates) if self.force_mock else []
        if self.connection is None:
            return []
        port = _serial_port(self.connection)
        if port is None:
            return []

        found: list[dict[str, str]] = []
        self._diagnostic_active = True
        try:
            with self._obd_lock:
                client = UdsClient(lambda cmd: raw_send(port, cmd))
                client.init_adapter(protocol)
                for mod in candidates:
                    if self.stop_event.is_set():
                        break
                    client.set_target(mod.tx, mod.rx)
                    if client.is_present():
                        found.append({"name": mod.name, "tx": mod.tx, "rx": mod.rx})
                client.close()
        except Exception:
            log.exception("Module scan failed")
        finally:
            self._diagnostic_active = False
        return found

    def mock_uds_toggle(self) -> None:
        """Flip the simulated coding bit (Car Lab mock) so the next capture diffs."""
        self._mock_uds.toggle_function()
