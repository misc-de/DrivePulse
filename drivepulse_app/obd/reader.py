"""Background OBD reader, connection management and mock fallback."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject

try:
    import obd
except Exception:
    obd = None

from drivepulse_app.common import (
    CONNECTION_LOG_FILE,
    LOG_DIR,
    LOG_FILE,
    OBD_BAUDRATE,
    OBD_FAST,
    OBD_PORT,
    OBD_SOCKET_URL,
    OBD_TIMEOUT_SECONDS,
    POLL_INTERVAL_SECONDS,
)
from drivepulse_app.diagnostics import append_jsonl, get_logger
from drivepulse_app.obd.adapter import AdapterInfo, _serial_port, probe_adapter, raw_send
from drivepulse_app.obd.devices import candidate_bt_addresses, parse_bt_port
from drivepulse_app.obd.mock import MockObdSimulator, MockUdsSimulator
from drivepulse_app.obd.polling import (
    OBD_COMMAND_ATTRS,
    command_map,
    response_to_plain_value,
    should_query_key,
)
from drivepulse_app.obd.scanner import ObdScanner
from drivepulse_app.sensors.bluetooth import BluetoothPtyBridge

log = get_logger(__name__)


def _extract_speed_kmh(value: Any) -> float | None:
    """Pull a km/h scalar out of the normalized OBD payload value.

    ``response_to_plain_value`` returns either ``{"value": float, "unit": str}``
    or a stringified value. Vehicle speed PIDs report km/h on every adapter we
    care about, so a unit conversion isn't needed — we just need the number.
    """
    if isinstance(value, dict):
        v = value.get("value")
        if isinstance(v, int | float):
            return float(v)
    return None


class ObdReader(GObject.Object):
    """Liest OBD-II-Werte in einem Hintergrund-Thread."""

    __gtype_name__ = "ObdReader"

    # Minimum OBD() timeout for direct BT connections (ELM327 init can be slow over BT)
    _BT_OBD_TIMEOUT = 15.0
    # Periodic re-scan keeps the scan history (DTCs, PIDs) fresh while connected.
    _RESCAN_INTERVAL_S = float(os.environ.get("OBD_RESCAN_INTERVAL", "90"))
    # How often to probe for a real dongle while in mock fallback. Lower = faster
    # pickup when the car is started, at the cost of more failed connect attempts.
    _MOCK_RECONNECT_INTERVAL_S = float(os.environ.get("OBD_MOCK_RECONNECT_INTERVAL", "3"))
    # Idle backoff: when the vehicle has been below _IDLE_MOTION_KMH for at
    # least _IDLE_HOLD_S, raise the minimum polling interval for all PIDs to
    # _IDLE_MIN_INTERVAL_S. Fast PIDs (rpm/speed/coolant) normally hit every
    # 500 ms tick — at a standstill that's 2 Hz of Bluetooth traffic for
    # values that aren't changing. The backoff drops it to ~0.5 Hz instead.
    _IDLE_MOTION_KMH = 3.0
    _IDLE_HOLD_S = 10.0
    _IDLE_MIN_INTERVAL_S = float(os.environ.get("OBD_IDLE_MIN_INTERVAL", "2.0"))

    def __init__(self, on_update: Callable[[dict[str, Any]], None], force_mock: bool = False) -> None:
        super().__init__()
        self.on_update = on_update
        self.force_mock = force_mock
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.connection: Any = None
        self.connected_port: str | None = None
        self.failed_read_count = 0
        self.next_mock_reconnect_attempt = 0.0
        self._bt_bridge: BluetoothPtyBridge | None = None
        self._configured_port: str | None = None
        self._force_reconnect = False
        self._scanned_identities: set[str] = set()
        self._last_scan_monotonic: float = 0.0
        self._adapter_info: AdapterInfo | None = None
        # Set while a read-only UDS diagnostic session (Car Lab) owns the bus.
        # The live poll loop and vehicle scan pause so module-addressed UDS
        # traffic isn't interleaved with the 7DF functional broadcast.
        self._diagnostic_active = False
        # Serializes access to self.connection between the reader thread and the
        # asynchronous vehicle-scan thread so they can interleave queries safely.
        self._obd_lock = threading.Lock()
        self._scan_thread: threading.Thread | None = None
        self._obd_value_cache: dict[str, Any] = {}
        self._obd_last_query: dict[str, float] = {}
        # Tracks the last monotonic time the vehicle was observed in motion
        # (speed >= _IDLE_MOTION_KMH). When the vehicle has been parked for
        # longer than _IDLE_HOLD_S, fast-PID polling backs off from "every
        # tick" to _IDLE_MIN_INTERVAL_S — quietens the BT radio while parked.
        self._last_motion_monotonic: float = time.monotonic()
        self._obd_log_enabled: bool = True
        self._mock_simulator = MockObdSimulator()
        # Simulated UDS module for the Car Lab when no real adapter is present.
        self._mock_uds = MockUdsSimulator()
        if obd is None:
            self.mock_reason = "python-obd missing"
        elif force_mock:
            self.mock_reason = "Manually enabled"
        else:
            self.mock_reason = ""
        self.mock = obd is None or force_mock

    def set_obd_log_enabled(self, enabled: bool) -> None:
        self._obd_log_enabled = enabled

    def _connection_log(self, event: str, **fields: Any) -> None:
        """Schreibt jeden Verbindungsversuch sofort in ein separates Debug-Log."""
        if not self._obd_log_enabled or self.mock:
            return
        try:
            payload = {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": event,
                "obd_port": self._configured_port or OBD_PORT,
                "obd_baudrate": OBD_BAUDRATE,
                "obd_timeout": OBD_TIMEOUT_SECONDS,
                "obd_fast": OBD_FAST,
                "python_obd_available": obd is not None,
                **fields,
            }
            append_jsonl(CONNECTION_LOG_FILE, payload)
        except Exception:
            log.exception("Could not write OBD connection log event=%s", event)

    def start(self) -> None:
        self._connection_log("reader_start")
        self.thread = threading.Thread(target=self._run, name="obd-reader", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.5)
        if self.connection:
            self._close_connection()

    def _candidate_ports(self) -> list[str | None]:
        if OBD_PORT:
            return [OBD_PORT]

        candidates: list[str | None] = []
        for pattern in ("/dev/rfcomm*", "/dev/ttyUSB*", "/dev/ttyACM*", "/dev/serial/by-id/*"):
            candidates.extend(str(path) for path in sorted(Path("/").glob(pattern.lstrip("/"))))
        if OBD_SOCKET_URL:
            candidates.append(OBD_SOCKET_URL)
        return [*candidates, None]

    def _try_bt_direct(self, addr: str, channel: int) -> bool:
        """Try direct Bluetooth RFCOMM socket without rfcomm bind. Returns True on success."""
        self._connection_log("bt_direct_attempt", bt_addr=addr, channel=channel)
        bridge: BluetoothPtyBridge | None = None
        try:
            bridge = BluetoothPtyBridge(addr, channel)
            connect_kwargs: dict[str, Any] = {
                "fast": False,
                "timeout": max(OBD_TIMEOUT_SECONDS, self._BT_OBD_TIMEOUT),
                "baudrate": OBD_BAUDRATE if OBD_BAUDRATE is not None else 38400,
            }
            self._connection_log("connect_attempt", port=bridge.pty_path, bt_addr=addr, **connect_kwargs)
            self.connection = obd.OBD(bridge.pty_path, **connect_kwargs)
            connected = bool(self.connection and self.connection.is_connected())
            self._connection_log("connect_result", port=bridge.pty_path, bt_addr=addr, connected=connected)
            if connected:
                self._bt_bridge = bridge
                self.mock = False
                self.mock_reason = ""
                self.connected_port = f"bt:{addr}"
                self.failed_read_count = 0
                supported = sorted(str(c) for c in getattr(self.connection, "supported_commands", set()))
                self._connection_log("connect_success", port=self.connected_port, supported_commands=supported)
                self._probe_adapter()
                return True
            self._close_connection()
            bridge.close()
            return False
        except Exception as exc:
            self._connection_log("bt_direct_exception", bt_addr=addr, channel=channel, error=repr(exc), error_type=type(exc).__name__)
            self._close_connection()
            if bridge is not None:
                bridge.close()
            return False

    def _close_connection(self) -> None:
        try:
            if self.connection:
                self.connection.close()
        except Exception as exc:
            self._connection_log("connect_close_error", port=self.connected_port, error=str(exc))
        finally:
            self.connection = None
            self.connected_port = None
            self._adapter_info = None
            self._obd_value_cache.clear()
            self._obd_last_query.clear()
        if self._bt_bridge is not None:
            self._bt_bridge.close()
            self._bt_bridge = None

    def _send_raw_locked(self, cmd: str) -> str:
        """Send a raw AT/ST command while holding the shared OBD serial lock."""
        port = _serial_port(self.connection)
        if port is None:
            return ""
        with self._obd_lock:
            return raw_send(port, cmd)

    def _probe_adapter(self) -> None:
        """Detect adapter type after a successful connection and cache the result."""
        if self.connection is None or self.mock:
            return
        self._adapter_info = probe_adapter(self.connection, locked_raw=self._send_raw_locked)

    def set_force_mock(self, force_mock: bool) -> None:
        self.force_mock = force_mock
        if force_mock:
            self.mock = True
            self.mock_reason = "Manually enabled"
        else:
            self._force_reconnect = True
            if obd is None:
                self.mock_reason = "python-obd missing"
            else:
                self.mock_reason = ""

    def set_configured_port(self, port: str | None) -> None:
        self._configured_port = port
        self._force_reconnect = True

    def _rfcomm_bind(self, addr: str, channel: int) -> str | None:
        """Bind a Bluetooth address to an rfcomm device node. Returns device path or None."""
        # Find a free rfcomm slot (0-9)
        slot = 0
        for i in range(10):
            if not Path(f"/dev/rfcomm{i}").exists():
                slot = i
                break
        dev = f"/dev/rfcomm{slot}"
        release_cmd = ["rfcomm", "release", str(slot)]
        bind_cmd = ["rfcomm", "bind", str(slot), addr, str(channel)]
        self._connection_log("rfcomm_bind_attempt", addr=addr, channel=channel, dev=dev)
        # Release any stale binding first (ignore errors)
        for prefix in ([], ["pkexec"]):
            try:
                subprocess.run([*prefix, *release_cmd], capture_output=True, timeout=5, check=False)
            except Exception as exc:
                self._connection_log("rfcomm_release_error", addr=addr, error=str(exc))
            break
        # Try bind without sudo, then with pkexec (GUI password dialog)
        for cmd in (bind_cmd, ["pkexec", *bind_cmd]):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
                if result.returncode == 0:
                    self._connection_log("rfcomm_bind_ok", addr=addr, dev=dev, cmd=cmd[0])
                    return dev
                self._connection_log(
                    "rfcomm_bind_failed",
                    addr=addr, dev=dev, cmd=cmd[0],
                    returncode=result.returncode,
                    stderr=result.stderr.strip()[-200:],
                )
            except FileNotFoundError:
                self._connection_log("rfcomm_bind_not_found", addr=addr)
                return None
            except subprocess.TimeoutExpired:
                self._connection_log("rfcomm_bind_timeout", addr=addr)
                return None
            except Exception as exc:
                self._connection_log("rfcomm_bind_error", addr=addr, error=str(exc))
        return None

    def _connect(self) -> None:
        if self.force_mock:
            self.mock = True
            self.mock_reason = "Manuell aktiviert"
            self._connection_log("connect_skipped", reason="force_mock")
            return
        self._connection_log("connect_begin")
        GLib.idle_add(
            self.on_update,
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "source": "status",
                "obd_connecting": True,
                "connection_status": "Connecting to OBD...",
                "obd_port": self.connected_port,
            },
        )

        if obd is None:
            self.mock = True
            self.mock_reason = "python-obd nicht importierbar"
            self._connection_log("connect_failed", reason=self.mock_reason, fallback="mock")
            return

        self._close_connection()

        # Settings-configured port takes priority over auto-scan
        if self._configured_port:
            if not self.stop_event.is_set():
                if self._configured_port.startswith("bt:"):
                    addr, ch = parse_bt_port(self._configured_port)
                    # Try rfcomm bind first (creates /dev/rfcommN, most reliable)
                    dev = self._rfcomm_bind(addr, ch)
                    if dev:
                        success = False
                        try:
                            connect_kwargs: dict[str, Any] = {
                                "fast": False,
                                "timeout": max(OBD_TIMEOUT_SECONDS, self._BT_OBD_TIMEOUT),
                                "baudrate": OBD_BAUDRATE if OBD_BAUDRATE is not None else 38400,
                            }
                            self._connection_log("connect_attempt", port=dev, bt_addr=addr, **connect_kwargs)
                            self.connection = obd.OBD(dev, **connect_kwargs)
                            connected = bool(self.connection and self.connection.is_connected())
                            self._connection_log("connect_result", port=dev, bt_addr=addr, connected=connected,
                                                 status=str(getattr(self.connection, "status", lambda: "unknown")()))
                            if connected:
                                self.mock = False
                                self.mock_reason = ""
                                self.connected_port = dev
                                self.failed_read_count = 0
                                supported = sorted(str(c) for c in getattr(self.connection, "supported_commands", set()))
                                self._connection_log("connect_success", port=dev, supported_commands=supported)
                                self._probe_adapter()
                                success = True
                            else:
                                self._close_connection()
                        except Exception as exc:
                            self._close_connection()
                            self._connection_log("connect_exception", port=dev, error=repr(exc))
                        if not success:
                            # rfcomm port open but OBD handshake failed — ELM clone fallback
                            self._connection_log("bt_rfcomm_obd_failed_trying_direct", bt_addr=addr, dev=dev)
                            success = self._try_bt_direct(addr, ch)
                    else:
                        # rfcomm bind unavailable — fall back to direct BT socket
                        success = self._try_bt_direct(addr, ch)
                else:
                    is_rfcomm = self._configured_port.startswith("/dev/rfcomm")
                    success = False
                    try:
                        connect_kwargs = {
                            "fast": False if is_rfcomm else OBD_FAST,
                            "timeout": max(OBD_TIMEOUT_SECONDS, self._BT_OBD_TIMEOUT) if is_rfcomm else OBD_TIMEOUT_SECONDS,
                        }
                        if OBD_BAUDRATE is not None:
                            connect_kwargs["baudrate"] = OBD_BAUDRATE
                        elif is_rfcomm:
                            connect_kwargs["baudrate"] = 38400
                        self._connection_log("connect_attempt", port=self._configured_port, **connect_kwargs)
                        self.connection = obd.OBD(self._configured_port, **connect_kwargs)
                        connected = bool(self.connection and self.connection.is_connected())
                        self._connection_log("connect_result", port=self._configured_port, connected=connected,
                                             status=str(getattr(self.connection, "status", lambda: "unknown")()))
                        if connected:
                            self.mock = False
                            self.mock_reason = ""
                            self.connected_port = self._configured_port
                            self.failed_read_count = 0
                            supported = sorted(str(c) for c in getattr(self.connection, "supported_commands", set()))
                            self._connection_log("connect_success", port=self._configured_port, supported_commands=supported)
                            self._probe_adapter()
                            success = True
                        else:
                            self._close_connection()
                    except Exception as exc:
                        self._close_connection()
                        self._connection_log("connect_exception", port=self._configured_port, error=repr(exc), error_type=type(exc).__name__)
                if not success:
                    self.mock = True
                    self.mock_reason = f"Dongle unreachable: {self._configured_port}"
                    self._connection_log("connect_failed", reason=self.mock_reason, port=self._configured_port, fallback="mock")
            return

        # No configured port: auto-scan all candidates
        for port in self._candidate_ports():
            if self.stop_event.is_set():
                self._connection_log("connect_aborted", reason="stop_event")
                return

            try:
                connect_kwargs = {
                    "fast": OBD_FAST,
                    "timeout": OBD_TIMEOUT_SECONDS,
                }
                if OBD_BAUDRATE is not None:
                    connect_kwargs["baudrate"] = OBD_BAUDRATE
                self._connection_log("connect_attempt", port=port, **connect_kwargs)
                self.connection = obd.OBD(port, **connect_kwargs)
                connected = bool(self.connection and self.connection.is_connected())
                self._connection_log(
                    "connect_result",
                    port=port,
                    connected=connected,
                    status=str(getattr(self.connection, "status", lambda: "unknown")()),
                )
                if connected:
                    self.mock = False
                    self.mock_reason = ""
                    self.connected_port = port
                    self.failed_read_count = 0
                    supported = sorted(str(c) for c in getattr(self.connection, "supported_commands", set()))
                    self._connection_log("connect_success", port=port, supported_commands=supported)
                    self._probe_adapter()
                    return

                self._close_connection()
            except Exception as exc:
                self._close_connection()
                self._connection_log("connect_exception", port=port, error=repr(exc), error_type=type(exc).__name__)

        for addr, channel in candidate_bt_addresses():
            if self.stop_event.is_set():
                self._connection_log("connect_aborted", reason="stop_event")
                return
            if self._try_bt_direct(addr, channel):
                return

        self.mock = True
        self.mock_reason = "kein nutzbarer Dongle gefunden"
        self._connection_log("connect_failed", reason=self.mock_reason, fallback="mock")

    def _query_locked(self, command: Any) -> Any:
        """Run an OBD query through the shared lock so the reader and scanner
        threads cannot interleave bytes on the serial line."""
        with self._obd_lock:
            return self.connection.query(command)

    def clear_dtcs(self) -> bool:
        """Send OBD Mode 04 (CLEAR_DTC). Returns True if the ECU acknowledged.

        Note for callers: this clears stored *and* pending DTCs, the freeze
        frame, and resets the emissions readiness monitors. The caller is
        responsible for confirming the action with the user before
        invoking this method.
        """
        if obd is None or self.connection is None or self.mock:
            return False
        cmd = getattr(obd.commands, "CLEAR_DTC", None)
        if cmd is None:
            return False
        try:
            response = self._query_locked(cmd)
            return not response.is_null()
        except Exception:
            log.exception("CLEAR_DTC query failed")
            return False

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

        if obd is None or self.connection is None or self.mock:
            return None
        port = _serial_port(self.connection)
        if port is None:
            return None

        self._diagnostic_active = True
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
            return self._mock_uds.discover(tx, rx)

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

    def uds_snapshot(
        self, tx: str, rx: str, dids: list[int], protocol: str = "6"
    ) -> dict[int, str]:
        """Read *dids* from one module once; return ``{did: hex_string}`` positives."""
        from drivepulse_app.obd.uds import did_payload

        if self.mock:
            return self._mock_uds.snapshot(dids)

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
            return self._mock_uds.scan_modules(candidates)
        if obd is None or self.connection is None:
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

    def _run_vehicle_scan(self, force_rescan: bool = False) -> None:
        """Start the vehicle scan in a background thread so the live read loop
        is not blocked. The scan can take 30+ seconds over Bluetooth; running it
        asynchronously lets gauges update within the first poll cycle after
        connect instead of after the full scan."""
        if obd is None or self.connection is None or self.mock:
            return
        if self._scan_thread is not None and self._scan_thread.is_alive():
            return  # a scan is already in progress
        connection = self.connection
        port = self.connected_port
        # Mark scan time at start so periodic re-scans don't pile up.
        self._last_scan_monotonic = time.monotonic()

        adapter_info = self._adapter_info

        def _worker() -> None:
            try:
                ObdScanner(
                    connection, port, self.on_update, self._scanned_identities,
                    force_rescan=force_rescan,
                    query_locked=self._query_locked,
                    yield_between_queries=0.04,
                    stop_event=self.stop_event,
                    obd_module=obd,
                    raw_send_locked=self._send_raw_locked,
                    adapter_info=adapter_info,
                ).run()
            except Exception as exc:
                self._connection_log("scan_thread_error", error=repr(exc), error_type=type(exc).__name__)

        self._scan_thread = threading.Thread(target=_worker, name="obd-scan", daemon=True)
        self._scan_thread.start()

    def _maybe_periodic_rescan(self) -> None:
        if self.mock or self.connection is None or self._RESCAN_INTERVAL_S <= 0:
            return
        if self._last_scan_monotonic <= 0:
            return
        if time.monotonic() - self._last_scan_monotonic < self._RESCAN_INTERVAL_S:
            return
        if self._scan_thread is not None and self._scan_thread.is_alive():
            return
        self._run_vehicle_scan(force_rescan=True)

    def _run(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not self.force_mock:
            self._connect()
            self._run_vehicle_scan()

        while not self.stop_event.is_set():
            if self._diagnostic_active:
                # A UDS diagnostic session owns the bus — don't poll or rescan.
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            if self._force_reconnect:
                self._force_reconnect = False
                if not self.force_mock:
                    self.mock = False
                    self.mock_reason = ""
                    self._connect()
                    self._run_vehicle_scan()
            self._maybe_reconnect_from_mock()
            self._maybe_periodic_rescan()
            payload = self._read_mock() if self.mock else self._read_obd()
            payload["timestamp"] = datetime.now(UTC).isoformat()
            payload["source"] = ("mock" if self.force_mock else "mock_fallback") if self.mock else "obd"
            payload["obd_connecting"] = False
            payload["connection_status"] = self._connection_status()
            payload["obd_port"] = self.connected_port
            if self.mock_reason:
                payload["mock_reason"] = self.mock_reason
            self._write_log(payload)
            GLib.idle_add(self.on_update, payload)
            self._maybe_reconnect_after_read(payload)
            time.sleep(POLL_INTERVAL_SECONDS)

    def _maybe_reconnect_from_mock(self) -> None:
        if self.force_mock or not self.mock or obd is None:
            return

        now = time.monotonic()
        if now < self.next_mock_reconnect_attempt:
            return

        self.next_mock_reconnect_attempt = now + self._MOCK_RECONNECT_INTERVAL_S
        self._connection_log("mock_reconnect_probe")
        self._connect()
        self._run_vehicle_scan()

    def _connection_status(self) -> str:
        if self.mock:
            return f"Mock: {self.mock_reason or 'active'}"
        return f"OBD connected: {self.connected_port or 'auto'}"

    def _maybe_reconnect_after_read(self, payload: dict[str, Any]) -> None:
        if self.mock:
            return

        command_count = int(payload.get("_command_count", 0))
        read_error_count = int(payload.get("_read_error_count", 0))
        disconnected = bool(self.connection and not self.connection.is_connected())
        bt_dead = self._bt_bridge is not None and not self._bt_bridge.is_alive
        failed_read = disconnected or bt_dead or (command_count > 0 and read_error_count >= command_count)
        self.failed_read_count = self.failed_read_count + 1 if failed_read else 0
        if self.failed_read_count < 3:
            return

        self._connection_log("reconnect_begin", reason="wiederholte Lesefehler", failed_reads=self.failed_read_count)
        self.mock = False
        self.mock_reason = ""
        self._connect()
        self._run_vehicle_scan()

    def _read_obd(self) -> dict[str, Any]:
        assert obd is not None
        assert self.connection is not None
        if self._diagnostic_active:
            # Bus is owned by a UDS session; serve the last known values.
            return {**self._obd_value_cache, "_command_count": 0, "_read_error_count": 0}
        if self._adapter_info is not None and self._adapter_info.supports_stpx:
            return self._read_obd_batch()
        return self._read_obd_single()

    def _read_obd_batch(self) -> dict[str, Any]:
        """Read live PIDs via a single STPX batch round-trip (STN/OBDLink).

        One CAN request returns several PIDs at once, replacing the per-PID
        Bluetooth round-trips of the single-query path. PIDs not due this tick
        are served from cache; PIDs without an STPX decoder — or that the
        adapter fails to answer in the batch — fall back to an individual
        python-obd query so a gauge never goes blank.
        """
        from drivepulse_app.obd.adapter import _MODE1_DECODE, batch_query_stpx

        commands = command_map(obd)
        name_to_key = {attr: key for key, attr in OBD_COMMAND_ATTRS}

        data: dict[str, Any] = {}
        now = time.monotonic()
        idle_min = self._idle_min_interval(now)

        due_batch: dict[int, str] = {}        # pid -> key (decodable, due this tick)
        due_single: list[tuple[str, Any]] = []  # (key, command) without STPX decoder
        for key, command in commands.items():
            if command is None:
                continue
            if not self._should_query_obd_key(key, now, idle_min):
                if key in self._obd_value_cache:
                    data[key] = self._obd_value_cache[key]
                continue
            pid = getattr(command, "pid", None)
            if pid is not None and pid in _MODE1_DECODE:
                due_batch[pid] = key
            else:
                due_single.append((key, command))

        command_count = 0
        read_error_count = 0

        if due_batch:
            command_count += 1
            try:
                batched = batch_query_stpx(self._send_raw_locked, list(due_batch))
            except Exception as exc:
                batched = {}
                log.debug("STPX live batch failed: %s", exc)
            if not batched:
                read_error_count += 1
            answered: set[str] = set()
            for name, value in batched.items():
                mapped_key = name_to_key.get(name)
                if mapped_key is None:
                    continue
                data[mapped_key] = value
                self._obd_value_cache[mapped_key] = value
                self._obd_last_query[mapped_key] = now
                answered.add(mapped_key)
                if mapped_key == "speed":
                    self._note_speed_for_idle(value, now)
            # Demote any due PID the adapter didn't answer to a single query.
            for pid, key in due_batch.items():
                if key not in answered:
                    due_single.append((key, commands[key]))

        for key, command in due_single:
            command_count += 1
            try:
                with self._obd_lock:
                    response = self.connection.query(command)
                value = self._response_to_plain_value(response)
                data[key] = value
                self._obd_value_cache[key] = value
                self._obd_last_query[key] = now
                if key == "speed":
                    self._note_speed_for_idle(value, now)
            except Exception as exc:
                read_error_count += 1
                data[f"{key}_error"] = str(exc)

        data["_command_count"] = command_count
        data["_read_error_count"] = read_error_count
        return data

    def _read_obd_single(self) -> dict[str, Any]:
        assert obd is not None
        assert self.connection is not None

        commands = command_map(obd)

        data: dict[str, Any] = {}
        command_count = 0
        read_error_count = 0
        now = time.monotonic()
        idle_min = self._idle_min_interval(now)
        for key, command in commands.items():
            if command is None:
                continue
            if not self._should_query_obd_key(key, now, idle_min):
                if key in self._obd_value_cache:
                    data[key] = self._obd_value_cache[key]
                continue
            command_count += 1
            try:
                with self._obd_lock:
                    response = self.connection.query(command)
                value = self._response_to_plain_value(response)
                data[key] = value
                self._obd_value_cache[key] = value
                self._obd_last_query[key] = now
                if key == "speed":
                    self._note_speed_for_idle(value, now)
            except Exception as exc:
                read_error_count += 1
                data[f"{key}_error"] = str(exc)
        data["_command_count"] = command_count
        data["_read_error_count"] = read_error_count
        return data

    def _should_query_obd_key(self, key: str, now: float, min_interval: float = 0.0) -> bool:
        return should_query_key(key, now, self._obd_last_query, min_interval)

    def _idle_min_interval(self, now: float) -> float:
        """Return the minimum polling interval to apply right now.

        Zero while the vehicle is moving (or recently moved) — fast PIDs run
        every tick as before. Raised to ``_IDLE_MIN_INTERVAL_S`` once the car
        has been below ``_IDLE_MOTION_KMH`` for ``_IDLE_HOLD_S``.
        """
        if now - self._last_motion_monotonic < self._IDLE_HOLD_S:
            return 0.0
        return self._IDLE_MIN_INTERVAL_S

    def _note_speed_for_idle(self, value: Any, now: float) -> None:
        """Update the motion timestamp from a freshly-read speed value."""
        speed_kmh = _extract_speed_kmh(value)
        if speed_kmh is None:
            # Unknown reading — treat conservatively as motion so we don't
            # back off based on a single noisy sample.
            self._last_motion_monotonic = now
        elif speed_kmh >= self._IDLE_MOTION_KMH:
            self._last_motion_monotonic = now

    def _response_to_plain_value(self, response: Any) -> Any:
        return response_to_plain_value(response)

    def trigger_mock_stopwatch(self) -> None:
        """Start a mock 0-230 km/h stopwatch run (called when Start is pressed in mock mode)."""
        self._mock_simulator.trigger_acceleration()

    def _read_mock(self) -> dict[str, Any]:
        return self._mock_simulator.read()

    def _write_log(self, payload: dict[str, Any]) -> None:
        if not self._obd_log_enabled or self.mock:
            return
        try:
            append_jsonl(LOG_FILE, payload)
        except Exception:
            log.exception("Could not write OBD payload log")
