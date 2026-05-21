"""One-shot vehicle scan support for DrivePulse."""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from .diagnostics import get_logger


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
    ) -> None:
        self.connection = connection
        self.port = port or "unknown"
        self.on_update = on_update
        self._session_cache = session_cache
        self.force_rescan = force_rescan
        self.obd = obd_module
        # When provided, the scanner queries the OBD bus through this callable so
        # the reader thread can safely interleave its own queries via a shared lock.
        self._query_locked = query_locked or (lambda cmd: connection.query(cmd))
        self._yield = max(0.0, yield_between_queries)
        self._stop_event = stop_event

    def _emit(self, status: str, progress: float, current: str = "") -> None:
        GLib.idle_add(self.on_update, {
            "source": "obd_scan",
            "scan_status": status,
            "scan_progress": progress,
            "scan_current": current,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def run(self) -> None:
        if self.obd is None or self.connection is None:
            return

        self._emit("scanning", 0.0, "VIN")

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

        # Mode 01: snapshot of all supported live-data PIDs
        live_data: dict[str, Any] = {}
        for cmd in mode1_cmds:
            if self._stop_event is not None and self._stop_event.is_set():
                return
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
        profile = {
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "identity": identity,
            "vin": vin,
            "port": self.port,
            "protocol": self._get_protocol(),
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

    def _emit_identity(self, vin: str | None, identity: str,
                       cal_id: Any = None, cvn: Any = None, protocol: Any = None) -> None:
        GLib.idle_add(self.on_update, {
            "source": "obd_scan_identity",
            "vin": vin,
            "cal_id": cal_id,
            "cvn": cvn,
            "protocol": protocol,
            "profile_path": identity,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def _emit_complete(self, profile: dict[str, Any]) -> None:
        GLib.idle_add(self.on_update, {
            "source": "obd_scan",
            "scan_status": "complete",
            "scan_progress": 1.0,
            "scan_current": "",
            "scan_profile": profile,
            "timestamp": datetime.now(timezone.utc).isoformat(),
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

    def _query_dtc_list(self, cmd: Any) -> list[str]:
        if cmd is None:
            return []
        try:
            r = self._query_locked(cmd)
            if not r.is_null() and r.value:
                return [str(d) for d in r.value]
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
