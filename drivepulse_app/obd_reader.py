"""Background OBD reader, connection management and mock fallback."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject  # noqa: E402

try:
    import obd  # type: ignore
except Exception:
    obd = None

from .common import (
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
from .bluetooth_bridge import BluetoothPtyBridge
from .diagnostics import append_jsonl, get_logger
from .mock_obd import MockObdSimulator
from .obd_adapter import AdapterInfo, probe_adapter, raw_send, _serial_port
from .obd_devices import candidate_bt_addresses, parse_bt_port
from .obd_polling import command_map, response_to_plain_value, should_query_key
from .obd_scanner import ObdScanner


log = get_logger(__name__)


class ObdReader(GObject.Object):
    """Liest OBD-II-Werte in einem Hintergrund-Thread."""

    __gtype_name__ = "ObdReader"

    # Minimum OBD() timeout for direct BT connections (ELM327 init can be slow over BT)
    _BT_OBD_TIMEOUT = 15.0
    # Periodic re-scan keeps the scan history (DTCs, PIDs) fresh while connected.
    _RESCAN_INTERVAL_S = float(os.environ.get("OBD_RESCAN_INTERVAL", "60"))
    # How often to probe for a real dongle while in mock fallback. Lower = faster
    # pickup when the car is started, at the cost of more failed connect attempts.
    _MOCK_RECONNECT_INTERVAL_S = float(os.environ.get("OBD_MOCK_RECONNECT_INTERVAL", "3"))

    def __init__(self, on_update: Callable[[dict[str, Any]], None], force_mock: bool = False) -> None:
        super().__init__()
        self.on_update = on_update
        self.force_mock = force_mock
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.connection = None
        self.connected_port: str | None = None
        self.failed_read_count = 0
        self.next_mock_reconnect_attempt = 0.0
        self._bt_bridge: BluetoothPtyBridge | None = None
        self._configured_port: str | None = None
        self._force_reconnect = False
        self._scanned_identities: set[str] = set()
        self._last_scan_monotonic: float = 0.0
        self._adapter_info: AdapterInfo | None = None
        # Serializes access to self.connection between the reader thread and the
        # asynchronous vehicle-scan thread so they can interleave queries safely.
        self._obd_lock = threading.Lock()
        self._scan_thread: threading.Thread | None = None
        self._obd_value_cache: dict[str, Any] = {}
        self._obd_last_query: dict[str, float] = {}
        self._obd_log_enabled: bool = True
        self._mock_simulator = MockObdSimulator()
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
                "timestamp": datetime.now(timezone.utc).isoformat(),
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
        return candidates + [None]

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
                subprocess.run(prefix + release_cmd, capture_output=True, timeout=5)
            except Exception as exc:
                self._connection_log("rfcomm_release_error", addr=addr, error=str(exc))
            break
        # Try bind without sudo, then with pkexec (GUI password dialog)
        for cmd in (bind_cmd, ["pkexec"] + bind_cmd):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
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
                "timestamp": datetime.now(timezone.utc).isoformat(),
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
                        connect_kwargs: dict[str, Any] = {
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
            payload["timestamp"] = datetime.now(timezone.utc).isoformat()
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

        commands = command_map(obd)

        data: dict[str, Any] = {}
        command_count = 0
        read_error_count = 0
        now = time.monotonic()
        for key, command in commands.items():
            if command is None:
                continue
            if not self._should_query_obd_key(key, now):
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
            except Exception as exc:
                read_error_count += 1
                data[f"{key}_error"] = str(exc)
        data["_command_count"] = command_count
        data["_read_error_count"] = read_error_count
        return data

    def _should_query_obd_key(self, key: str, now: float) -> bool:
        return should_query_key(key, now, self._obd_last_query)

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
