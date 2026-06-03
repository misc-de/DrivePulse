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

# Emissions readiness monitors python-obd exposes on a STATUS (Mode 01 PID 01)
# response — union of base + spark + compression test names (deduped, order
# preserved). Hardcoded so this works even when python-obd isn't importable.
_READINESS_TESTS: tuple[str, ...] = (
    "MISFIRE_MONITORING", "FUEL_SYSTEM_MONITORING", "COMPONENT_MONITORING",
    "CATALYST_MONITORING", "HEATED_CATALYST_MONITORING",
    "EVAPORATIVE_SYSTEM_MONITORING", "SECONDARY_AIR_SYSTEM_MONITORING",
    "OXYGEN_SENSOR_MONITORING", "OXYGEN_SENSOR_HEATER_MONITORING",
    "EGR_VVT_SYSTEM_MONITORING", "NMHC_CATALYST_MONITORING",
    "NOX_SCR_AFTERTREATMENT_MONITORING", "BOOST_PRESSURE_MONITORING",
    "EXHAUST_GAS_SENSOR_MONITORING", "PM_FILTER_MONITORING",
)

# In-use performance tracking (Mode 09 PID 08) data items in standard SAE J1979
# order for spark-ignition vehicles. Stored alongside the raw words so a
# different item count never silently mislabels values.
_IUMPR_LABELS: tuple[str, ...] = (
    "OBDCOND", "IGNCNTR",
    "CATCOMP1", "CATCOND1", "CATCOMP2", "CATCOND2",
    "O2SCOMP1", "O2SCOND1", "O2SCOMP2", "O2SCOND2",
    "EGRCOMP", "EGRCOND", "AIRCOMP", "AIRCOND", "EVAPCOMP", "EVAPCOND",
    "SO2SCOMP1", "SO2SCOND1", "SO2SCOMP2", "SO2SCOND2",
)


def _reassemble_isotp(raw: str) -> dict[str, bytes]:
    """Rebuild ISO-TP payloads (per CAN header) from a headers-on raw response.

    Handles single frames (``7E8 06 …``), first frames (``7E8 10 2B …``) and
    consecutive frames (``7E8 21 …``). Returns ``{header: payload_bytes}``.
    """
    pending: dict[str, dict[str, Any]] = {}
    done: dict[str, bytes] = {}
    for line in raw.replace("\r", "\n").split("\n"):
        tokens = line.split()
        if len(tokens) < 2:
            continue
        header = tokens[0]
        try:
            b = [int(t, 16) for t in tokens[1:]]
        except ValueError:
            continue
        if not b:
            continue
        pci = b[0] >> 4
        if pci == 0x0:  # single frame: low nibble = length
            length = b[0] & 0x0F
            done[header] = bytes(b[1:1 + length])
        elif pci == 0x1:  # first frame: 12-bit total length, data from byte 2
            total = ((b[0] & 0x0F) << 8) | b[1]
            pending[header] = {"total": total, "data": b[2:]}
        elif pci == 0x2:  # consecutive frame: data from byte 1
            buf = pending.get(header)
            if buf is not None:
                buf["data"].extend(b[1:])
    for header, buf in pending.items():
        done[header] = bytes(buf["data"][: buf["total"]])
    return done


def parse_iumpr(raw: str) -> dict[str, Any]:
    """Parse a raw Mode 09 PID 08 (IUMPR) response into per-ECU counters.

    The reply is ``49 08 <NODI> <NODI 16-bit words>``; we label the words in
    standard spark order and also keep the raw word list. Returns
    ``{header: {"values": {...}, "raw_words": [...]}}`` (usually one ECU).
    """
    out: dict[str, Any] = {}
    for header, data in _reassemble_isotp(raw).items():
        if len(data) < 3 or data[0] != 0x49 or data[1] != 0x08:
            continue
        nodi = data[2]
        body = data[3:]
        words = [
            (body[i] << 8) | body[i + 1]
            for i in range(0, min(nodi * 2, len(body) - 1), 2)
        ]
        values = {
            (_IUMPR_LABELS[i] if i < len(_IUMPR_LABELS) else f"ITEM_{i}"): w
            for i, w in enumerate(words)
        }
        out[header] = {"values": values, "raw_words": words}
    return out


def _obd_text(value: Any) -> str:
    """Decode a python-obd mode-09 string value (VIN, Cal-ID, ECU name).

    python-obd returns these as ``bytearray`` on many adapters; a plain
    ``str(bytearray)`` leaks the ``bytearray(b'…')`` repr into the stored
    profile and the scan-identity key. Decode bytes/bytearray as ASCII and
    fall back to ``str`` for already-decoded values.
    """
    if isinstance(value, (bytes, bytearray)):
        return value.decode("ascii", errors="ignore").strip()
    return str(value).strip()


class ObdScanner:
    """One-shot full-scan of a newly connected OBD adapter/vehicle."""

    def __init__(
        self,
        connection: Any,
        port: str | None,
        on_update: Callable[[dict[str, Any]], None],
        session_cache: set[str],
        force_rescan: bool = False,
        query_locked: Callable[..., Any] | None = None,
        yield_between_queries: float = 0.0,
        stop_event: threading.Event | None = None,
        obd_module: Any = None,
        raw_send_locked: Callable[..., str] | None = None,
        resync_locked: Callable[[], None] | None = None,
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
        self._resync_locked = resync_locked
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
        # post-mode-01 phases: stored/pending/permanent DTC, vehicle info,
        # monitors, readiness, IUMPR, saving.
        total_steps = max(1, len(mode1_cmds) + 8)
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
            if live_data:
                done = len(mode1_cmds)
            else:
                # STPX returned nothing (e.g. not honoured over the Bluetooth
                # pty bridge) — fall back to the reliable single-query loop so a
                # detected STN adapter never produces a poorer scan than an ELM.
                log.info("STPX batch empty, falling back to single-query scan")
                done = self._run_mode1_single(mode1_cmds, live_data, total_steps, done)
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
                    vehicle_info[name] = _obd_text(r.value)
            except Exception:
                log.exception("Could not query vehicle info command %s", name)
            if self._yield:
                time.sleep(self._yield)

        # Mode 06: on-board monitor test results (catalyst, O2, misfire, VVT…)
        done += 1
        self._emit("scanning", done / total_steps, "On-Board-Monitore")
        monitors = self._query_monitors()

        # Mode 01 STATUS: emissions readiness monitors
        done += 1
        self._emit("scanning", done / total_steps, "Readiness")
        readiness = self._query_readiness()

        # Mode 0A: permanent (non-clearable) DTCs
        done += 1
        self._emit("scanning", done / total_steps, "DTC (permanent)")
        permanent_dtcs = self._query_permanent_dtcs()

        # Mode 09 PID 08: in-use performance tracking (IUMPR)
        done += 1
        self._emit("scanning", done / total_steps, "IUMPR")
        iumpr = self._query_iumpr()

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
            "permanent_dtcs": permanent_dtcs,
            "vehicle_info": vehicle_info,
            "monitors": monitors,
            "readiness": readiness,
            "iumpr": iumpr,
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

        # Batch path
        stpx_results: dict[str, Any] = {}
        if batch_pids and self._raw_send_locked is not None:
            stpx_results = batch_query_stpx(self._raw_send_locked, batch_pids)

        # The raw STPX exchange can leave a slow multi-frame response in flight
        # on the Bluetooth pty bridge (raw_send's read window expires first) and
        # python-obd's own flushInput is unreliable over a pty — so the queries
        # that follow read stale frames and return NO DATA for everything.
        # Drain the line and restore python-obd's init before any single query.
        self._resync()

        # STPX produced nothing → not honoured on this link. Return empty so the
        # caller runs a *complete* single-query fallback; returning only the
        # partial non-decode PIDs would make run() treat the scan as done and
        # silently drop every STPX-only PID (RPM, coolant, speed, …).
        if not stpx_results:
            return {}

        live_data: dict[str, Any] = dict(stpx_results)
        self._emit("scanning", 0.7, "STPX batch done")

        # Single-query fallback for PIDs not in the decode table
        if fallback_cmds:
            self._run_mode1_single(fallback_cmds, live_data, max(1, len(fallback_cmds)), 0)

        return live_data

    def _resync(self) -> None:
        """Restore a clean, python-obd-compatible channel after a raw STPX batch.

        Delegates to the reader's locked resync (drain the serial line until
        quiet, then re-assert ATE0/ATH1/ATL0). No-op when not provided.
        """
        if self._resync_locked is None:
            return
        try:
            self._resync_locked()
        except Exception:
            log.debug("post-STPX resync failed", exc_info=True)

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
                val = _obd_text(r.value)
                return val if val else None
        except Exception:
            log.info("Could not query VIN during OBD scan", exc_info=True)
        return None

    def _query_dtc_list(self, cmd: Any, force: bool = False) -> list[dict]:
        if cmd is None:
            return []
        try:
            r = self._query_locked(cmd, force=force) if force else self._query_locked(cmd)
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

    @staticmethod
    def _scalar(v: Any) -> Any:
        """Pull a JSON-friendly number out of a (possibly pint) quantity."""
        if v is None:
            return None
        mag = getattr(v, "magnitude", v)
        try:
            return round(float(mag), 4)
        except (TypeError, ValueError):
            return str(v)

    @staticmethod
    def _unit(v: Any) -> str:
        u = getattr(v, "units", None)
        if u is None:
            return ""
        try:
            return f"{u:~}".strip()
        except (TypeError, ValueError):
            return str(u)

    def _query_monitors(self) -> dict[str, list[dict]]:
        """Mode 06 on-board monitor test results, grouped by monitor name."""
        out: dict[str, list[dict]] = {}
        cmds = sorted(
            [c for c in getattr(self.connection, "supported_commands", set())
             if getattr(c, "mode", None) == 6 and not str(getattr(c, "name", "")).startswith("MIDS")],
            key=lambda c: str(getattr(c, "name", "")),
        )
        for cmd in cmds:
            if self._stop_event is not None and self._stop_event.is_set():
                break
            try:
                r = self._query_locked(cmd)
                if r is None or r.is_null():
                    continue
                tests_obj = getattr(r.value, "tests", None)
                if tests_obj is None:
                    rows = []
                elif hasattr(tests_obj, "values"):
                    rows = list(tests_obj.values())
                else:
                    rows = list(tests_obj)
                entries = [
                    {
                        "tid": getattr(t, "tid", None),
                        "name": getattr(t, "name", None),
                        "value": self._scalar(getattr(t, "value", None)),
                        "min": self._scalar(getattr(t, "min", None)),
                        "max": self._scalar(getattr(t, "max", None)),
                        "unit": self._unit(getattr(t, "value", None)),
                        "passed": bool(getattr(t, "passed", False)),
                    }
                    for t in rows
                ]
                if entries:
                    out[str(cmd.name)] = entries
            except Exception:
                log.info("Could not query monitor %s", cmd, exc_info=True)
            if self._yield:
                time.sleep(self._yield)
        return out

    def _query_readiness(self) -> dict[str, Any]:
        """Emissions readiness from STATUS (Mode 01 PID 01): MIL, monitors."""
        cmd = getattr(self.obd.commands, "STATUS", None) if self.obd else None
        if cmd is None:
            return {}
        try:
            r = self._query_locked(cmd)
            if r is None or r.is_null():
                return {}
            st = r.value
            monitors = {}
            for name in _READINESS_TESTS:
                t = getattr(st, name, None)
                if t is not None and getattr(t, "available", False):
                    monitors[name] = {"complete": bool(getattr(t, "complete", False))}
            return {
                "MIL": bool(getattr(st, "MIL", False)),
                "dtc_count": int(getattr(st, "DTC_count", 0) or 0),
                "ignition_type": getattr(st, "ignition_type", None),
                "monitors": monitors,
            }
        except Exception:
            log.info("Could not query readiness STATUS", exc_info=True)
            return {}

    def _query_permanent_dtcs(self) -> list[dict]:
        """Mode 0A permanent DTCs — not in python-obd, queried via a custom
        command (force, since it's never in supported_commands). The Mode 03
        DTC decoder fits Mode 0A (response 0x4A + count + 2-byte codes)."""
        if self.obd is None:
            return []
        try:
            from obd.decoders import dtc as dtc_decoder
            cmd = self.obd.OBDCommand(
                "PERMANENT_DTC", "Permanent DTCs (Mode 0A)", b"0A", 0,
                dtc_decoder, self.obd.ECU.ALL, False,
            )
        except Exception:
            log.info("Could not build permanent-DTC command", exc_info=True)
            return []
        return self._query_dtc_list(cmd, force=True)

    def _query_iumpr(self) -> dict[str, Any]:
        """Mode 09 PID 08 in-use performance tracking — raw query + parse
        (python-obd has no command and its assembly dropped the reply here)."""
        if self._raw_send_locked is None:
            return {}
        try:
            raw = self._raw_send_locked("0908", timeout=4.0)
            return parse_iumpr(raw)
        except Exception:
            log.info("Could not query IUMPR (Mode 09 PID 08)", exc_info=True)
            return {}

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
