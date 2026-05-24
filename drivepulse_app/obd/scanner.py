"""One-shot vehicle scan support for DrivePulse."""
from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

from drivepulse_app.diagnostics import get_logger
from drivepulse_app.obd.adapter import AdapterInfo, AdapterKind, batch_query_stpx, probe_adapter

log = get_logger(__name__)


class ObdScanner:
    """One-shot full-scan of a newly connected OBD adapter/vehicle."""

    def __init__(
        self,
        connection: Any,
        port: str | None,
        on_update: Callable[[dict[str, Any]], None],
        session_cache: set[str],
        force_rescan: bool = False,
        query_locked: Callable[[Any], Any] | None = None,
        yield_between_queries: float = 0.0,
        stop_event: threading.Event | None = None,
        obd_module: Any = None,
        raw_send_locked: Callable[[str], str] | None = None,
        adapter_info: AdapterInfo | None = None,
    ) -> None:
        self.connection = connection
        self.port = port or "unknown"
        self.on_update = on_update
        self._session_cache = session_cache
        self.force_rescan = force_rescan
        self.obd = obd_module
        # When provided, the scanner queries the OBD bus through this callable so
        # the reader thread can safely interleave its own queries via a shared lock.
        self._query_locked = query_locked or connection.query
        self._raw_send_locked = raw_send_locked
        self._adapter_info = adapter_info
        # Adapter-specific yield overrides the caller's default when known.
        if adapter_info is not None:
            self._yield = adapter_info.optimal_yield_s
        else:
            self._yield = max(0.0, yield_between_queries)
        self._stop_event = stop_event

    def _emit(self, status: str, progress: float, current: str = "") -> None:
        GLib.idle_add(self.on_update, {
            "source": "obd_scan",
            "scan_status": status,
            "scan_progress": progress,
            "scan_current": current,
            "timestamp": datetime.now(UTC).isoformat(),
        })

    def run(self) -> None:
        if self.obd is None or self.connection is None:
            return

        self._emit("scanning", 0.0, "VIN")

        # Probe adapter when not pre-supplied (e.g. first scan after connect).
        if self._adapter_info is None and self._raw_send_locked is not None:
            self._adapter_info = probe_adapter(
                self.connection, locked_raw=self._raw_send_locked
            )
            if self._adapter_info is not None:
                self._yield = self._adapter_info.optimal_yield_s

        vin = self._query_vin()
        if vin:
            identity = f"vin_{vin}"
        else:
            supported_names = sorted(str(c) for c in getattr(self.connection, "supported_commands", set()))
            fp = hashlib.md5(",".join(supported_names).encode()).hexdigest()[:8]
            identity = f"port_{Path(self.port).name}_{fp}"

        # Identität für die Trip-DB immer mitteilen, auch wenn der Scan ansonsten geskippt wird.
        self._emit_identity(vin, identity)

        if identity in self._session_cache and not self.force_rescan:
            self._emit("skipped", 1.0)
            return

        # Collect mode 01 supported commands (live data PIDs)
        mode1_cmds = sorted(
            [cmd for cmd in getattr(self.connection, "supported_commands", set()) if getattr(cmd, "mode", 0) == 1],
            key=lambda c: getattr(c, "pid", 0),
        )
        total_steps = max(1, len(mode1_cmds) + 4)
        done = 0

        # Mode 01: snapshot of all supported live-data PIDs.
        # STN/OBDLink: use STPX batch query (many PIDs per CAN frame → seconds not minutes).
        # ELM327: fall back to single-query loop.
        live_data: dict[str, Any] = {}
        use_batch = (
            self._adapter_info is not None
            and self._adapter_info.supports_stpx
            and self._raw_send_locked is not None
        )
        if use_batch:
            live_data = self._run_mode1_batch(mode1_cmds, total_steps)
            done = len(mode1_cmds)
        else:
            done = self._run_mode1_single(mode1_cmds, live_data, total_steps, done)

        # Mode 03: stored DTCs
        done += 1
        self._emit("scanning", done / total_steps, "DTC (stored)")
        dtcs = self._query_dtc_list(getattr(self.obd.commands, "GET_DTC", None))

        # Mode 07: pending DTCs
        done += 1
        self._emit("scanning", done / total_steps, "DTC (pending)")
        pending_dtcs = self._query_dtc_list(getattr(self.obd.commands, "PENDING_DTC", None))

        # Mode 09: vehicle info (VIN already done, add extras)
        done += 1
        self._emit("scanning", done / total_steps, "Vehicle info")
        vehicle_info: dict[str, Any] = {}
        if vin:
            vehicle_info["VIN"] = vin
        for name in ("CALIBRATION_ID", "CVN", "ECU_NAME"):
            cmd = getattr(self.obd.commands, name, None)
            if cmd is None:
                continue
            try:
                r = self._query_locked(cmd)
                if not r.is_null():
                    vehicle_info[name] = str(r.value)
            except Exception:
                log.exception("Could not query vehicle info command %s", name)
            if self._yield:
                time.sleep(self._yield)

        done += 1
        self._emit("saving", done / total_steps, "Profil speichern")
        adapter_kind = (
            self._adapter_info.kind.value if self._adapter_info else AdapterKind.UNKNOWN.value
        )
        adapter_version = self._adapter_info.version if self._adapter_info else ""
        profile = {
            "scanned_at": datetime.now(UTC).isoformat(),
            "identity": identity,
            "vin": vin,
            "port": self.port,
            "protocol": self._get_protocol(),
            "adapter_kind": adapter_kind,
            "adapter_version": adapter_version,
            "supported_pids": sorted(str(c) for c in getattr(self.connection, "supported_commands", set())),
            "live_data": live_data,
            "dtcs": dtcs,
            "pending_dtcs": pending_dtcs,
            "vehicle_info": vehicle_info,
        }
        self._session_cache.add(identity)

        # Volle Identität (inkl. Cal-ID/CVN) nach dem Scan an die App schicken.
        self._emit_identity(
            vin,
            identity,
            cal_id=vehicle_info.get("CALIBRATION_ID"),
            cvn=vehicle_info.get("CVN"),
            protocol=profile.get("protocol"),
        )
        self._emit_complete(profile)

    def _run_mode1_single(
        self,
        mode1_cmds: list[Any],
        live_data: dict[str, Any],
        total_steps: int,
        done: int,
    ) -> int:
        """Query Mode 01 PIDs one-by-one via python-obd. Returns updated *done* counter."""
        for cmd in mode1_cmds:
            if self._stop_event is not None and self._stop_event.is_set():
                return done
            done += 1
            self._emit("scanning", done / total_steps, str(cmd))
            try:
                r = self._query_locked(cmd)
                if not r.is_null():
                    live_data[str(cmd)] = self._to_plain(r)
            except Exception as exc:
                live_data[str(cmd)] = {"error": str(exc)}
            if self._yield:
                time.sleep(self._yield)
        return done

    def _run_mode1_batch(
        self,
        mode1_cmds: list[Any],
        total_steps: int,
    ) -> dict[str, Any]:
        """Query Mode 01 PIDs via STPX batch command (STN/OBDLink only).

        PIDs not covered by the STPX decode table fall back to single queries
        so the result dict is always as complete as the single-query path.
        """
        from drivepulse_app.obd.adapter import _MODE1_DECODE

        self._emit("scanning", 0.05, "STPX batch")

        # Partition PIDs: those with a known STPX decoder vs. the rest.
        batch_pids: list[int] = []
        fallback_cmds: list[Any] = []
        for cmd in mode1_cmds:
            pid = getattr(cmd, "pid", None)
            if pid is not None and pid in _MODE1_DECODE:
                batch_pids.append(pid)
            else:
                fallback_cmds.append(cmd)

        live_data: dict[str, Any] = {}

        # Batch path
        if batch_pids and self._raw_send_locked is not None:
            live_data.update(batch_query_stpx(self._raw_send_locked, batch_pids))

        self._emit("scanning", 0.7, "STPX batch done")

        # Single-query fallback for PIDs not in the decode table
        if fallback_cmds:
            done_fb = 0
            self._run_mode1_single(fallback_cmds, live_data, max(1, len(fallback_cmds)), done_fb)

        return live_data

    def _emit_identity(self, vin: str | None, identity: str,
                       cal_id: Any = None, cvn: Any = None, protocol: Any = None) -> None:
        GLib.idle_add(self.on_update, {
            "source": "obd_scan_identity",
            "vin": vin,
            "cal_id": cal_id,
            "cvn": cvn,
            "protocol": protocol,
            "profile_path": identity,
            "timestamp": datetime.now(UTC).isoformat(),
        })

    def _emit_complete(self, profile: dict[str, Any]) -> None:
        GLib.idle_add(self.on_update, {
            "source": "obd_scan",
            "scan_status": "complete",
            "scan_progress": 1.0,
            "scan_current": "",
            "scan_profile": profile,
            "timestamp": datetime.now(UTC).isoformat(),
        })

    def _query_vin(self) -> str | None:
        try:
            cmd = getattr(self.obd.commands, "VIN", None)
            if cmd is None:
                return None
            r = self._query_locked(cmd)
            if not r.is_null():
                val = str(r.value).strip()
                return val if val else None
        except Exception:
            log.info("Could not query VIN during OBD scan", exc_info=True)
        return None

    def _query_dtc_list(self, cmd: Any) -> list[dict]:
        if cmd is None:
            return []
        try:
            r = self._query_locked(cmd)
            if not r.is_null() and r.value:
                result = []
                for d in r.value:
                    if isinstance(d, (tuple, list)) and len(d) >= 2:
                        result.append({"code": str(d[0]), "description": str(d[1])})
                    else:
                        result.append({"code": str(d), "description": ""})
                return result
        except Exception:
            log.info("Could not query DTC command %s", cmd, exc_info=True)
        return []

    def _get_protocol(self) -> str:
        try:
            return str(self.connection.protocol_name())
        except Exception:
            log.info("Could not read OBD protocol name", exc_info=True)
            return "unknown"

    def _to_plain(self, response: Any) -> Any:
        value = response.value
        try:
            return {"value": float(value.magnitude), "unit": str(value.units)}
        except Exception:
            return str(value)
