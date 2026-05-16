#!/usr/bin/env python3
"""
OBD-II Dashboard auf GTK4 / libadwaita-Basis.

Funktionen:
- Verbindung zu einem ELM327/OBD-II-Dongle via python-OBD.
- Anzeige von Drehzahl, Geschwindigkeit und Kühlmitteltemperatur als Tachos.
- Querformat: drei Tachos nebeneinander.
- Hochformat: drei Tachos untereinander.
- Zusätzliche OBD-Werte werden in JSONL geschrieben, damit sie später leicht eingebaut werden können.
- Mock-Modus, falls kein Dongle oder python-OBD verfügbar ist.

Debian/Ubuntu-Abhängigkeiten:
  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-pip
  python3 -m pip install --user obd

Start:
  python3 drivepulse.py

Optional mit Port:
  OBD_PORT=/dev/rfcomm0 python3 drivepulse.py
  OBD_PORT=/dev/ttyUSB0 python3 drivepulse.py

Bluetooth-ELM327:
  Adapter zuerst per Bluetooth koppeln und als seriellen Port binden, z. B. /dev/rfcomm0.
  Danach: OBD_PORT=/dev/rfcomm0 python3 drivepulse.py
"""

from __future__ import annotations

import atexit
import json
import math
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Register bundled icons via XDG_DATA_DIRS **before** GTK is imported so the
# icon theme engine picks them up on its first initialisation pass.
# icons/hicolor/index.theme tells GTK which sub-directories contain SVGs.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_APP_DIR = str(_PROJECT_ROOT)
_xdg_data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
if _APP_DIR not in _xdg_data_dirs.split(":"):
    os.environ["XDG_DATA_DIRS"] = f"{_APP_DIR}:{_xdg_data_dirs}"

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk  # noqa: E402

try:
    import obd  # type: ignore
except Exception:
    obd = None

from .common import (
    APP_ID,
    CONNECTION_LOG_FILE,
    DB_FILE,
    LOG_DIR,
    LOG_FILE,
    OBD_BAUDRATE,
    OBD_FAST,
    OBD_PORT,
    OBD_SOCKET_URL,
    OBD_TIMEOUT_SECONDS,
    POLL_INTERVAL_SECONDS,
    SETTINGS_FILE,
    THEMES_DIR,
    _detect_language,
    _make_label_responsive,
    _normalize_language,
    _translate,
)
from .gauge import Gauge, GAUGE_THEMES, all_theme_options, load_user_themes, get_theme_css
from .dashboard import DashboardCanvas, DASHBOARD_THEMES
from .acceleration import AccelerationPage
from .cars import CarsPage
from .cars_metadata import _extract_inner_string, _parse_profile_pid_key, _wmi_to_brand
from .cars_profiles import _load_profiles
from .db import DriveDB, TripRecorder
from .app_settings import load_settings, save_settings
from .gps_reader import GpsReader
from .orientation_reader import OrientationReader
from .obd_devices import candidate_bt_addresses, parse_bt_port
from .settings_dialog import SettingsDialog
from .icon_registry import register_local_icon
from .bluetooth_bridge import BluetoothPtyBridge
from .obd_scanner import ObdScanner
from .startup_info import print_required_python_packages
from .mock_obd import MockObdSimulator
from .obd_polling import command_map, response_to_plain_value, should_query_key
from .telemetry_utils import display_speed, has_obd_data, plain_number


class ObdReader(GObject.Object):
    """Liest OBD-II-Werte in einem Hintergrund-Thread."""

    __gtype_name__ = "ObdReader"

    # Minimum OBD() timeout for direct BT connections (ELM327 init can be slow over BT)
    _BT_OBD_TIMEOUT = 15.0
    # Periodic re-scan keeps the scan history (DTCs, PIDs) fresh while connected.
    _RESCAN_INTERVAL_S = float(os.environ.get("OBD_RESCAN_INTERVAL", "900"))
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
        # Serializes access to self.connection between the reader thread and the
        # asynchronous vehicle-scan thread so they can interleave queries safely.
        self._obd_lock = threading.Lock()
        self._scan_thread: threading.Thread | None = None
        self._obd_value_cache: dict[str, Any] = {}
        self._obd_last_query: dict[str, float] = {}
        self._mock_simulator = MockObdSimulator()
        if obd is None:
            self.mock_reason = "python-obd fehlt"
        elif force_mock:
            self.mock_reason = "Manuell aktiviert"
        else:
            self.mock_reason = ""
        self.mock = obd is None or force_mock

    def _connection_log(self, event: str, **fields: Any) -> None:
        """Schreibt jeden Verbindungsversuch sofort in ein separates Debug-Log."""
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event,
                "obd_port": OBD_PORT,
                "obd_baudrate": OBD_BAUDRATE,
                "obd_timeout": OBD_TIMEOUT_SECONDS,
                "obd_fast": OBD_FAST,
                "python_obd_available": obd is not None,
                **fields,
            }
            with CONNECTION_LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

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
                "fast": OBD_FAST,
                "timeout": max(OBD_TIMEOUT_SECONDS, self._BT_OBD_TIMEOUT),
            }
            if OBD_BAUDRATE is not None:
                connect_kwargs["baudrate"] = OBD_BAUDRATE
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
            self._obd_value_cache.clear()
            self._obd_last_query.clear()
        if self._bt_bridge is not None:
            self._bt_bridge.close()
            self._bt_bridge = None

    def set_force_mock(self, force_mock: bool) -> None:
        self.force_mock = force_mock
        if force_mock:
            self.mock = True
            self.mock_reason = "Manuell aktiviert"
        else:
            self.next_mock_reconnect_attempt = 0.0
            if obd is None:
                self.mock_reason = "python-obd fehlt"
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
            except Exception:
                pass
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
                            connect_kwargs: dict[str, Any] = {"fast": OBD_FAST, "timeout": max(OBD_TIMEOUT_SECONDS, self._BT_OBD_TIMEOUT)}
                            if OBD_BAUDRATE is not None:
                                connect_kwargs["baudrate"] = OBD_BAUDRATE
                            self._connection_log("connect_attempt", port=dev, bt_addr=addr, **connect_kwargs)
                            self.connection = obd.OBD(dev, **connect_kwargs)
                            connected = bool(self.connection and self.connection.is_connected())
                            self._connection_log("connect_result", port=dev, bt_addr=addr, connected=connected)
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
                    else:
                        # rfcomm bind unavailable — fall back to direct BT socket
                        success = self._try_bt_direct(addr, ch)
                else:
                    success = False
                    try:
                        connect_kwargs: dict[str, Any] = {"fast": OBD_FAST, "timeout": OBD_TIMEOUT_SECONDS}
                        if OBD_BAUDRATE is not None:
                            connect_kwargs["baudrate"] = OBD_BAUDRATE
                        self._connection_log("connect_attempt", port=self._configured_port, **connect_kwargs)
                        self.connection = obd.OBD(self._configured_port, **connect_kwargs)
                        connected = bool(self.connection and self.connection.is_connected())
                        self._connection_log("connect_result", port=self._configured_port, connected=connected)
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
                    self.mock_reason = f"Dongle nicht erreichbar: {self._configured_port}"
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

        def _worker() -> None:
            try:
                ObdScanner(
                    connection, port, self.on_update, self._scanned_identities,
                    force_rescan=force_rescan,
                    query_locked=self._query_locked,
                    yield_between_queries=0.04,
                    stop_event=self.stop_event,
                    obd_module=obd,
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
            return f"Mock: {self.mock_reason or 'aktiv'}"
        return f"OBD verbunden: {self.connected_port or 'auto'}"

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

    def trigger_mock_acceleration(self) -> None:
        """Start a mock 0-230 km/h acceleration run (called when Start is pressed in mock mode)."""
        self._mock_simulator.trigger_acceleration()

    def _read_mock(self) -> dict[str, Any]:
        return self._mock_simulator.read()

    def _write_log(self, payload: dict[str, Any]) -> None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass


class DashboardWindow(Adw.ApplicationWindow):
    __gtype_name__ = "DashboardWindow"

    PAGE_DASHBOARD = "dashboard"
    PAGE_ACCELERATION = "acceleration"
    PAGE_CARS = "cars"

    # Fensterbreite, unterhalb derer die Autos-Detailansicht ihre Kategorienleiste
    # auf Icon-only umschaltet (Phosh/Mobian-typische Portrait-Breiten 360–540 px).
    CARS_NARROW_BREAKPOINT = 500

    # Seconds to keep GPS shown as "available" after the last valid fix
    GPS_UNAVAIL_HOLDOVER = 1.0

    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title=_translate(_detect_language(), "window.title"))
        self.set_default_size(980, 520)
        self.settings = self._load_settings()
        self.units = self.settings["units"]
        self.language = self.settings["language"]
        self.mock_mode = self.settings["mock_mode"]
        self.obd_port: str | None = self.settings.get("obd_port")
        self.gauge_theme: str = self.settings.get("gauge_theme", "cockpit")
        self.auto_rotate: bool = self.settings.get("auto_rotate", True)
        self.last_payload: dict[str, Any] | None = None
        self._gps_last_seen: float = 0.0
        self._last_gps_lat: float | None = None
        self._last_gps_lon: float | None = None

        # Persistente Fahrten-Datenbank (cars/trips/samples) — vor allen Pages,
        # weil CarsPage sie injiziert bekommt.
        self.db = DriveDB(DB_FILE)
        self.trip_recorder = TripRecorder(self.db)
        atexit.register(self._shutdown_db)

        self.rpm_gauge = Gauge(_translate(self.language, "gauge.rpm"), "rpm", 0, 7000, (0.34, 0.62, 0.86), self.gauge_theme)
        speed_unit = "km/h" if self.units == "metric" else "mph"
        speed_max = 240 if self.units == "metric" else 150
        self.speed_gauge = Gauge(_translate(self.language, "gauge.speed"), speed_unit, 0, speed_max, (0.50, 0.72, 0.92), self.gauge_theme)
        self.temp_gauge = Gauge(_translate(self.language, "gauge.coolant"), "°C", 40, 130, (0.72, 0.32, 0.48), self.gauge_theme)

        self.status_label = _make_label_responsive(Gtk.Label(label=_translate(self.language, "status.connecting")), 36, 0.5)
        self.status_label.add_css_class("dim-label")
        self.gauge_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self.gauge_box.add_css_class("dp-gauge-bg")
        self.gauge_box.set_halign(Gtk.Align.FILL)
        self.gauge_box.set_valign(Gtk.Align.FILL)
        self.gauge_box.set_hexpand(True)
        self.gauge_box.set_vexpand(True)

        for gauge in (self.rpm_gauge, self.speed_gauge, self.temp_gauge):
            gauge.set_hexpand(True)
            gauge.set_vexpand(True)
            gauge.set_halign(Gtk.Align.FILL)
            gauge.set_valign(Gtk.Align.FILL)
            self.gauge_box.append(gauge)

        self.scan_bar = Gtk.ProgressBar()
        self.scan_bar.set_show_text(True)
        self.scan_bar.set_hexpand(True)
        self.scan_bar.set_visible(False)

        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        footer.set_halign(Gtk.Align.FILL)
        footer.set_hexpand(True)
        footer.append(self.scan_bar)

        self.footer = footer

        self.dashboard_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.dashboard_page.set_margin_top(12)
        self.dashboard_page.set_margin_bottom(12)
        self.dashboard_page.set_margin_start(12)
        self.dashboard_page.set_margin_end(12)
        self.dashboard_page.add_css_class("dp-gauge-bg")

        self.dashboard_canvas = DashboardCanvas(self.gauge_theme, self.units, self.language)
        self.dashboard_canvas.set_hexpand(True)
        self.dashboard_canvas.set_vexpand(True)
        self.dashboard_canvas.set_halign(Gtk.Align.FILL)
        self.dashboard_canvas.set_valign(Gtk.Align.FILL)

        _is_dash = self.gauge_theme in DASHBOARD_THEMES
        self.gauge_box.set_visible(not _is_dash)
        self.dashboard_canvas.set_visible(_is_dash)
        if _is_dash:
            for setter in (
                self.dashboard_page.set_margin_top,
                self.dashboard_page.set_margin_bottom,
                self.dashboard_page.set_margin_start,
                self.dashboard_page.set_margin_end,
            ):
                setter(0)

        self.dashboard_page.append(self.gauge_box)
        self.dashboard_page.append(self.dashboard_canvas)
        self.dashboard_page.append(footer)

        dashboard_scroller = Gtk.ScrolledWindow()
        dashboard_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        dashboard_scroller.set_propagate_natural_width(False)
        dashboard_scroller.set_propagate_natural_height(False)
        dashboard_scroller.set_child(self.dashboard_page)

        self.acceleration_page = AccelerationPage(self.language)
        self.acceleration_page.set_theme(self.gauge_theme)
        self.acceleration_page.set_engage_threshold(self.settings.get("engage_threshold", 0.20))
        self.acceleration_page.on_engage_threshold_changed = self._on_engage_threshold_changed
        self.acceleration_page.on_run_complete = self._on_acceleration_run_complete
        acceleration_scroller = Gtk.ScrolledWindow()
        acceleration_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        acceleration_scroller.set_propagate_natural_width(False)
        acceleration_scroller.set_propagate_natural_height(False)
        acceleration_scroller.set_hexpand(True)
        acceleration_scroller.set_vexpand(True)
        acceleration_scroller.set_child(self.acceleration_page)

        self.cars_page = CarsPage(self.language, db=self.db)
        self.cars_page.on_back_swipe = self._on_cars_back_swipe
        self.cars_page.on_forward_swipe = self._on_cars_forward_swipe
        self.cars_page.set_header_trash_fn = self.set_ctx_trash

        self.view_stack = Adw.ViewStack()
        self.view_stack.set_vexpand(True)
        self.view_stack.set_hexpand(True)
        self.view_stack.set_hhomogeneous(False)
        self.view_stack.set_vhomogeneous(False)
        self.view_stack.set_enable_transitions(True)
        self.view_stack.set_transition_duration(240)
        self.cars_stack_page = self.view_stack.add_titled_with_icon(
            self.cars_page,
            self.PAGE_CARS,
            _translate(self.language, "nav.cars"),
            "driving-symbolic",
        )
        self.dashboard_stack_page = self.view_stack.add_titled_with_icon(
            dashboard_scroller,
            self.PAGE_DASHBOARD,
            _translate(self.language, "nav.gauges"),
            "speedometer4-symbolic",
        )
        self.acceleration_stack_page = self.view_stack.add_titled_with_icon(
            acceleration_scroller,
            self.PAGE_ACCELERATION,
            _translate(self.language, "nav.acceleration"),
            "stopwatch-symbolic",
        )

        self.view_stack.connect("notify::visible-child-name", self._on_visible_page_changed)

        swipe = Gtk.GestureSwipe()
        swipe.connect("swipe", self._on_swipe)
        self.view_stack.add_controller(swipe)

        switcher_bar = Adw.ViewSwitcherBar()
        switcher_bar.set_stack(self.view_stack)
        switcher_bar.set_reveal(True)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title_label = Gtk.Label(label=_translate(self.language, "window.title"))
        header.set_title_widget(self.title_label)

        self.obd_indicator = self._build_link_indicator("network-wired-symbolic", _translate(self.language, "status.obd"))
        self.gps_indicator = self._build_link_indicator("find-location-symbolic", _translate(self.language, "status.gps"))
        settings_button = Gtk.Button(icon_name="emblem-system-symbolic")
        self.settings_button = settings_button
        settings_button.set_tooltip_text(_translate(self.language, "settings.tooltip"))
        settings_button.connect("clicked", self._open_settings)

        self._ctx_trash_btn = Gtk.Button(icon_name="user-trash-symbolic")
        self._ctx_trash_btn.add_css_class("flat")
        self._ctx_trash_btn.set_visible(False)
        self._ctx_trash_handler: int | None = None

        header.pack_start(self.obd_indicator["box"])
        header.pack_start(self.gps_indicator["box"])
        header.pack_end(settings_button)
        header.pack_end(self._ctx_trash_btn)

        self.header = header
        self.switcher_bar = switcher_bar
        toolbar_view.add_top_bar(header)
        toolbar_view.add_bottom_bar(switcher_bar)
        toolbar_view.set_content(self.view_stack)

        self._nav_visible = True
        self._last_swipe_time = 0.0
        self._tap_press_time = 0.0
        self._tap_press_x = 0.0
        self._tap_press_y = 0.0
        tap = Gtk.GestureClick()
        tap.connect("pressed", self._on_content_press)
        tap.connect("released", self._on_content_tap)
        self.view_stack.add_controller(tap)

        self.set_content(toolbar_view)
        self.connect("notify::default-width", self._on_size_changed)
        self.connect("notify::default-height", self._on_size_changed)
        self.add_tick_callback(self._layout_tick)
        GLib.idle_add(self._on_size_changed)

        self._theme_css_provider = Gtk.CssProvider()
        self.connect("realize", self._on_realize_install_css)

        self._obd_active = False
        self._device_rotation = 0

        GLib.idle_add(self._load_initial_scan_data)

        self.reader = ObdReader(self._update_from_payload, force_mock=self.mock_mode)
        self.reader._configured_port = self.obd_port
        self.acceleration_page.on_mock_start = self.reader.trigger_mock_acceleration
        self.reader.start()
        self.gps_reader = GpsReader(self._update_from_payload)
        self.gps_reader.start()
        self.orientation_reader = OrientationReader(self._on_orientation_changed, enabled=self.auto_rotate)
        self.orientation_reader.on_gforce = self.acceleration_page.update_gforce_raw

        # Idle-Erkennung + WAL-Checkpoint alle 30 s
        GLib.timeout_add_seconds(30, self._db_periodic_tick)

    def _build_link_indicator(self, icon_name: str, label_text: str) -> dict[str, Any]:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.add_css_class("dim-label")
        image = Gtk.Image(icon_name=icon_name)
        spinner = Gtk.Spinner()
        spinner.set_visible(False)
        label = Gtk.Label(label=label_text)
        box.append(spinner)
        box.append(image)
        box.append(label)
        return {"box": box, "image": image, "spinner": spinner, "label": label}

    def _set_link_indicator(self, indicator: dict[str, Any], connected: bool, connecting: bool = False) -> None:
        box = indicator["box"]
        spinner = indicator["spinner"]
        image = indicator["image"]
        box.remove_css_class("dim-label")
        box.remove_css_class("success")
        if connected:
            box.add_css_class("success")
        else:
            box.add_css_class("dim-label")
        spinner.set_visible(connecting)
        image.set_visible(not connecting)
        if connecting:
            spinner.start()
        else:
            spinner.stop()

    # Maximum duration / movement that still counts as a "short tap"
    _TAP_MAX_DURATION_S = 0.30
    _TAP_MAX_MOVE_PX = 14.0

    def _on_content_press(self, _gesture: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
        self._tap_press_time = time.monotonic()
        self._tap_press_x = x
        self._tap_press_y = y

    def _on_content_tap(self, _gesture: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
        now = time.monotonic()
        # Reject if a swipe just fired — its release event still reaches the click gesture
        if now - self._last_swipe_time < 0.35:
            return
        # Reject if the touch lasted too long (long-press) or moved too far (swipe/drag)
        duration = now - self._tap_press_time
        moved = math.hypot(x - self._tap_press_x, y - self._tap_press_y)
        if duration > self._TAP_MAX_DURATION_S or moved > self._TAP_MAX_MOVE_PX:
            return
        # Auf der Autos-Seite muss die Navigation jederzeit erreichbar bleiben,
        # damit der Anwender zurück zu Tachos/Beschleunigung kommt.
        if self.view_stack.get_visible_child_name() == self.PAGE_CARS:
            self._set_nav_visible(True)
            return
        self._set_nav_visible(not self._nav_visible)

    def _set_nav_visible(self, visible: bool) -> None:
        self._nav_visible = visible
        self.header.set_visible(visible)
        self.switcher_bar.set_visible(visible)
        self.footer.set_visible(visible)

    def _on_visible_page_changed(self, _stack: Adw.ViewStack, _pspec: Any) -> None:
        # Beim Wechsel auf Autos die Navigation erzwungen einblenden.
        if self.view_stack.get_visible_child_name() == self.PAGE_CARS and not self._nav_visible:
            self._set_nav_visible(True)

    def _on_cars_back_swipe(self) -> None:
        """Vom Autos-Tab (Liste) per Wisch nach rechts zurück zur Beschleunigung."""
        if self.view_stack.get_visible_child_name() == self.PAGE_CARS:
            self.view_stack.set_visible_child_name(self.PAGE_ACCELERATION)
            self._last_swipe_time = time.monotonic()

    def _on_cars_forward_swipe(self) -> None:
        """Vom Autos-Tab (Liste) per Wisch nach links zum Tacho."""
        if self.view_stack.get_visible_child_name() == self.PAGE_CARS:
            self.view_stack.set_visible_child_name(self.PAGE_DASHBOARD)
            self._last_swipe_time = time.monotonic()

    def _on_orientation_changed(self, orientation: str, angle: int, is_landscape: bool) -> None:
        """Called by OrientationReader when the physical device orientation changes."""
        self._device_rotation = angle
        # Rotate gauge drawings
        for gauge in (self.rpm_gauge, self.speed_gauge, self.temp_gauge):
            gauge.set_rotation(angle)
        # Rotate full-screen dashboard canvas
        self.dashboard_canvas.set_rotation(angle)
        # Re-evaluate portrait/landscape layout immediately
        self._on_size_changed()

    def _on_realize_install_css(self, *_args: Any) -> None:
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), self._theme_css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self._apply_window_theme(self.gauge_theme)

    def _apply_window_theme(self, theme: str) -> None:
        for cls in list(self.get_css_classes()):
            if cls.startswith("dp-theme-"):
                self.remove_css_class(cls)
        safe = theme.replace(":", "-").replace("_", "-")
        self.add_css_class(f"dp-theme-{safe}")
        css = get_theme_css(theme)
        self._theme_css_provider.load_from_data(css.encode() if css else b"")

    def close(self) -> bool:
        self.reader.stop()
        self.gps_reader.stop()
        self.orientation_reader.stop()
        return super().close()

    def set_ctx_trash(self, action_fn: Any) -> None:
        """Show/hide the context trash button in the header and wire up its action."""
        btn = self._ctx_trash_btn
        if self._ctx_trash_handler is not None:
            btn.disconnect(self._ctx_trash_handler)
            self._ctx_trash_handler = None
        if action_fn is not None:
            self._ctx_trash_handler = btn.connect("clicked", lambda _b: action_fn())
            btn.set_visible(True)
        else:
            btn.set_visible(False)

    def _load_settings(self) -> dict[str, Any]:
        return load_settings()

    def _load_units(self) -> str:
        return self._load_settings()["units"]

    def _save_settings(self) -> None:
        try:
            save_settings({
                "units": getattr(self, "units", "metric"),
                "language": getattr(self, "language", _detect_language()),
                "mock_mode": getattr(self, "mock_mode", False),
                "obd_port": getattr(self, "obd_port", None),
                "gauge_theme": getattr(self, "gauge_theme", "cockpit"),
                "auto_rotate": getattr(self, "auto_rotate", True),
                "engage_threshold": getattr(self, "engage_threshold", 0.20),
            })
        except Exception:
            pass

    def _on_engage_threshold_changed(self, value: float) -> None:
        self.engage_threshold = value
        self._save_settings()

    def _on_acceleration_run_complete(self, results: dict, samples: list) -> None:
        trip_recorder = getattr(self, "trip_recorder", None)
        car_id = trip_recorder.car_id if trip_recorder else None
        if car_id is None:
            return
        try:
            self.db.add_acceleration_run(
                car_id=car_id,
                results=results,
                samples=samples,
                lat=self._last_gps_lat,
                lon=self._last_gps_lon,
            )
            self.cars_page.refresh_if_showing_car(car_id)
        except Exception:
            pass

    def _save_units(self) -> None:
        self._save_settings()

    def _open_settings(self, *_args: Any) -> None:
        dialog = SettingsDialog(
            self, self.units, self.language,
            self._set_units, self._set_language,
            self.mock_mode, self._set_mock_mode,
            current_obd_port=self.obd_port,
            on_obd_port_changed=self._set_obd_port,
            current_gauge_theme=self.gauge_theme,
            on_gauge_theme_changed=self._set_gauge_theme,
            current_auto_rotate=self.auto_rotate,
            on_auto_rotate_changed=self._set_auto_rotate,
        )
        dialog.present(self)

    def _set_obd_port(self, port: str | None) -> None:
        if port == self.obd_port:
            return
        self.obd_port = port
        self._save_settings()
        self.reader.set_configured_port(port)

    def _set_units(self, units: str) -> None:
        if units == self.units:
            return
        self.units = units
        self._save_units()
        self.dashboard_canvas.set_units(units)

        if self.units == "metric":
            self.speed_gauge.state.unit = "km/h"
            self.speed_gauge.state.max_value = 240
        else:
            self.speed_gauge.state.unit = "mph"
            self.speed_gauge.state.max_value = 150

        if self.last_payload is not None:
            self._update_from_payload(self.last_payload)
        else:
            self.speed_gauge.queue_draw()

    def _set_gauge_theme(self, theme: str) -> None:
        if theme == self.gauge_theme:
            return
        self.gauge_theme = theme
        self._save_settings()
        is_dashboard = theme in DASHBOARD_THEMES
        self.gauge_box.set_visible(not is_dashboard)
        self.dashboard_canvas.set_visible(is_dashboard)
        # Dashboard themes fill the screen edge-to-edge; gauge themes need breathing room
        margin = 0 if is_dashboard else 12
        for setter in (
            self.dashboard_page.set_margin_top,
            self.dashboard_page.set_margin_bottom,
            self.dashboard_page.set_margin_start,
            self.dashboard_page.set_margin_end,
        ):
            setter(margin)
        if is_dashboard:
            self.dashboard_canvas.set_theme(theme)
        else:
            for gauge in (self.rpm_gauge, self.speed_gauge, self.temp_gauge):
                gauge.set_theme(theme)
        self.acceleration_page.set_theme(theme)
        self._apply_window_theme(theme)

    def _set_mock_mode(self, mock_mode: bool) -> None:
        if mock_mode == self.mock_mode:
            return
        self.mock_mode = mock_mode
        self._save_settings()
        self.reader.set_force_mock(mock_mode)

    def _set_auto_rotate(self, enabled: bool) -> None:
        if enabled == self.auto_rotate:
            return
        self.auto_rotate = enabled
        self._save_settings()
        self.orientation_reader.set_enabled(enabled)

    def _set_language(self, language: str) -> None:
        language = _normalize_language(language)
        if language == self.language:
            return
        self.language = language
        self._save_settings()
        self.rpm_gauge.title = _translate(self.language, "gauge.rpm")
        self.speed_gauge.title = _translate(self.language, "gauge.speed")
        self.temp_gauge.title = _translate(self.language, "gauge.coolant")
        self.title_label.set_text(_translate(self.language, "window.title"))
        self.settings_button.set_tooltip_text(_translate(self.language, "settings.tooltip"))
        self.obd_indicator["label"].set_text(_translate(self.language, "status.obd"))
        self.gps_indicator["label"].set_text(_translate(self.language, "status.gps"))
        self.dashboard_stack_page.set_title(_translate(self.language, "nav.gauges"))
        self.acceleration_stack_page.set_title(_translate(self.language, "nav.acceleration"))
        self.cars_stack_page.set_title(_translate(self.language, "nav.cars"))
        self.acceleration_page.set_language(self.language)
        self.dashboard_canvas.set_language(self.language)
        self.cars_page.set_language(self.language)
        if self.last_payload is not None:
            self._update_from_payload(self.last_payload)
        else:
            self.status_label.set_text(_translate(self.language, "status.connecting"))
        for gauge in (self.rpm_gauge, self.speed_gauge, self.temp_gauge):
            gauge.queue_draw()

    def _layout_tick(self, *_args: Any) -> bool:
        self._on_size_changed()
        return True

    def _on_size_changed(self, *_args: Any) -> bool:
        width = self.dashboard_page.get_width() or self.view_stack.get_width() or self.get_width()
        height = self.dashboard_page.get_height() or self.view_stack.get_height() or self.get_height()
        if width <= 0 or height <= 0:
            return False

        if hasattr(self, "cars_page"):
            self.cars_page.set_narrow(width < self.CARS_NARROW_BREAKPOINT)

        gauge_box_visible = self.gauge_box.get_visible()
        if gauge_box_visible is not False:
            # On Phosh / compositor-side rotation (right-up or left-up): the GTK window
            # stays portrait (e.g. 360×800) while the physical display is landscape.
            # VERTICAL layout in the GTK window appears HORIZONTAL after the compositor
            # rotates the output 90°. Pass swapped dimensions so sizing uses the physical
            # proportions (physical_w = GTK_h, physical_h = GTK_w).
            # Guard: only use this path when GTK window really is portrait (width < height),
            # so a true landscape desktop window (width > height) still gets landscape layout.
            device_rotation = getattr(self, "_device_rotation", 0)
            if device_rotation in (90, 270) and width < height:
                self._set_portrait_layout(min(width, height), max(width, height))
            elif width >= height:
                self._set_landscape_layout(width, height)
            else:
                self._set_portrait_layout(width, height)

        return False

    # Speed gauge is this factor larger than the two side gauges.
    # side + speed + side  =  side*(2 + _SPEED_SCALE) in the primary axis.
    _SPEED_SCALE = 1.45

    def _set_landscape_layout(self, width: int, height: int) -> None:
        self.gauge_box.set_orientation(Gtk.Orientation.HORIZONTAL)
        self.gauge_box.set_spacing(16)
        self.gauge_box.set_halign(Gtk.Align.CENTER)
        self.gauge_box.set_valign(Gtk.Align.CENTER)

        footer_height = max(0, self.footer.get_height()) if self.footer.get_visible() else 0
        avail_w = max(1, width - 24)
        avail_h = max(1, height - 24 - footer_height - 8)

        # Solve: side*(2 + scale) + 2*spacing = avail_w  AND  speed = side*scale ≤ avail_h
        side = int(min(
            (avail_w - 32) / (2 + self._SPEED_SCALE),
            avail_h / self._SPEED_SCALE,
        ))
        side = max(1, side)
        speed = max(1, min(int(side * self._SPEED_SCALE), avail_h))

        self._apply_gauge_sizes(side, speed)

    def _set_portrait_layout(self, width: int, height: int) -> None:
        self.gauge_box.set_orientation(Gtk.Orientation.VERTICAL)
        self.gauge_box.set_spacing(8)
        self.gauge_box.set_halign(Gtk.Align.CENTER)
        self.gauge_box.set_valign(Gtk.Align.CENTER)

        footer_height = max(0, self.footer.get_height()) if self.footer.get_visible() else 0
        avail_w = max(1, width - 24)
        avail_h = max(1, height - 24 - footer_height - 8)

        # Solve: side*(2 + scale) + 2*spacing = avail_h  AND  speed = side*scale ≤ avail_w
        side = int(min(
            (avail_h - 16) / (2 + self._SPEED_SCALE),
            avail_w / self._SPEED_SCALE,
        ))
        side = max(1, side)
        speed = max(1, min(int(side * self._SPEED_SCALE), avail_w))

        self._apply_gauge_sizes(side, speed)

    def _apply_gauge_sizes(self, side: int, speed: int) -> None:
        for gauge, sz in (
            (self.rpm_gauge,   side),
            (self.speed_gauge, speed),
            (self.temp_gauge,  side),
        ):
            gauge.set_hexpand(False)
            gauge.set_vexpand(False)
            gauge.set_halign(Gtk.Align.CENTER)
            gauge.set_valign(Gtk.Align.CENTER)
            gauge.set_size_request(sz, sz)
            gauge.set_content_width(sz)
            gauge.set_content_height(sz)

    def _on_swipe(self, _gesture: Gtk.GestureSwipe, velocity_x: float, velocity_y: float) -> None:
        ax, ay = abs(velocity_x), abs(velocity_y)

        # Vertical swipe on the gauge/dashboard page → cycle through themes
        if ay > 220 and ay > ax and self.view_stack.get_visible_child_name() == self.PAGE_DASHBOARD:
            self._last_swipe_time = time.monotonic()
            self._cycle_theme(up=velocity_y < 0)
            return

        # Horizontal swipe → switch page
        if ax < 220 or ax <= ay:
            return
        self._last_swipe_time = time.monotonic()
        current = self.view_stack.get_visible_child_name()
        # Wenn das Auto-Detail offen ist, übernimmt Adw.NavigationView den
        # Zurück-Swipe (Detail → Liste). Wir schalten dann nicht zusätzlich den Tab um.
        if current == self.PAGE_CARS and velocity_x > 0 and self.cars_page.is_detail_open():
            return
        pages = [self.PAGE_CARS, self.PAGE_DASHBOARD, self.PAGE_ACCELERATION]
        try:
            index = pages.index(current)
        except ValueError:
            index = 0
        if velocity_x < 0 and index < len(pages) - 1:
            self.view_stack.set_visible_child_name(pages[index + 1])
        elif velocity_x > 0 and index > 0:
            self.view_stack.set_visible_child_name(pages[index - 1])

    def _cycle_theme(self, up: bool) -> None:
        """Cycle to the next/previous theme via vertical swipe."""
        options = [tid for tid, _ in all_theme_options(self.language)]
        if not options:
            return
        try:
            idx = options.index(self.gauge_theme)
        except ValueError:
            idx = 0
        idx = (idx + (1 if up else -1)) % len(options)
        self._set_gauge_theme(options[idx])

    def _handle_scan_update(self, payload: dict[str, Any]) -> None:
        status = payload.get("scan_status", "")
        progress = float(payload.get("scan_progress", 0.0))
        current = str(payload.get("scan_current", ""))

        if status == "skipped":
            self.scan_bar.set_visible(False)
            return

        if status in ("scanning", "saving"):
            self.scan_bar.set_visible(True)
            self.scan_bar.set_fraction(progress)
            label = f"Fahrzeugscan: {current} ({progress * 100:.0f}%)" if current else f"Fahrzeugscan... ({progress * 100:.0f}%)"
            self.scan_bar.set_text(label)
            return

        if status == "complete":
            self.scan_bar.set_fraction(1.0)
            self.scan_bar.set_text("Fahrzeugscan abgeschlossen")
            self._save_scan_to_db(current)
            if current:
                try:
                    self._update_dashboard_from_profile(
                        json.loads(Path(current).read_text(encoding="utf-8"))
                    )
                except Exception:
                    pass
            self.cars_page.refresh_profiles()
            GLib.timeout_add(3000, self._hide_scan_bar)
            return

        if status == "error":
            self.scan_bar.set_visible(True)
            self.scan_bar.set_text(f"Scan-Fehler: {current}")
            GLib.timeout_add(6000, self._hide_scan_bar)

    def _save_scan_to_db(self, profile_path_str: str) -> None:
        car_id = getattr(self.trip_recorder, "car_id", None)
        if not profile_path_str or car_id is None:
            return
        try:
            data = json.loads(Path(profile_path_str).read_text(encoding="utf-8"))
            self.db.add_scan(car_id, data)
        except Exception:
            pass

    def _hide_scan_bar(self) -> bool:
        self.scan_bar.set_visible(False)
        return False

    def _plain_number(self, data: dict[str, Any], key: str) -> float | None:
        return plain_number(data, key)

    def _display_speed(self, speed_kmh: float | None) -> float | None:
        return display_speed(speed_kmh, self.units)

    def _has_obd_data(self, payload: dict[str, Any]) -> bool:
        return has_obd_data(payload)

    def _gps_connected_with_holdover(self, gps_speed_kmh: float | None) -> bool:
        now = time.monotonic()
        if gps_speed_kmh is not None:
            self._gps_last_seen = now
            return True
        return (now - getattr(self, "_gps_last_seen", 0.0)) < self.GPS_UNAVAIL_HOLDOVER

    def _update_from_payload(self, payload: dict[str, Any]) -> bool:
        source = payload.get("source", "")

        if source == "obd_scan":
            self._handle_scan_update(payload)
            return False

        if source == "obd_scan_identity":
            self._handle_scan_identity(payload)
            return False

        if source == "gps":
            gps_speed_kmh = self._plain_number(payload, "gps_speed")
            gps_heading = self._plain_number(payload, "gps_heading")
            lat = self._plain_number(payload, "gps_lat")
            lon = self._plain_number(payload, "gps_lon")
            altitude_m = self._plain_number(payload, "gps_altitude")
            if lat is not None:
                self._last_gps_lat = lat
            if lon is not None:
                self._last_gps_lon = lon
            trip_recorder = getattr(self, "trip_recorder", None)
            if trip_recorder is not None:
                trip_recorder.update_gps(
                    lat=lat, lon=lon, altitude_m=altitude_m,
                    heading_deg=gps_heading, gps_speed_kmh=gps_speed_kmh,
                )
            self._set_link_indicator(self.gps_indicator, self._gps_connected_with_holdover(gps_speed_kmh), False)
            self.acceleration_page.update_payload(payload, self._plain_number)
            self.cars_page.update_live(payload)
            if not getattr(self, "_obd_active", False) and gps_speed_kmh is not None:
                display = self._display_speed(gps_speed_kmh)
                src_gps = _translate(self.language, "gauge.source.gps")
                self.speed_gauge.set_value(display, f"{display:.0f}" if display is not None else None)
                self.speed_gauge.set_source_label(src_gps)
            else:
                display = None
                src_gps = ""
            with self.dashboard_canvas.batch_update():
                if gps_heading is not None:
                    self.dashboard_canvas.update_heading(gps_heading)
                self.dashboard_canvas.update_gps_speed(self._display_speed(gps_speed_kmh))
                self.dashboard_canvas.update_gps_pos(lat, lon, altitude_m)
                if src_gps:
                    self.dashboard_canvas.update_speed(display, f"{display:.0f}" if display is not None else None)
                    self.dashboard_canvas.update_speed_source(src_gps)
            return False

        self.last_payload = payload
        active = source in ("obd", "mock")
        rpm = self._plain_number(payload, "rpm") if active else None
        obd_speed_kmh = self._plain_number(payload, "speed") if active else None
        gps_speed_kmh = self._plain_number(payload, "gps_speed") if active else None
        speed_source_kmh = obd_speed_kmh if obd_speed_kmh is not None else gps_speed_kmh
        speed = self._display_speed(speed_source_kmh)
        temp = self._plain_number(payload, "coolant_temp") if active else None
        obd_connected = active and self._has_obd_data(payload)
        self._obd_active = obd_connected
        obd_connecting = bool(payload.get("obd_connecting"))
        gps_connected = self._gps_connected_with_holdover(gps_speed_kmh if active else None)

        self._set_link_indicator(self.obd_indicator, obd_connected, obd_connecting)
        self._set_link_indicator(self.gps_indicator, gps_connected, False)

        self.rpm_gauge.set_value(rpm, None if rpm is None else f"{rpm:.0f}")
        self.speed_gauge.set_value(speed, None if speed is None else f"{speed:.0f}")
        if obd_speed_kmh is not None:
            _spd_src = _translate(self.language, "gauge.source.obd")
        elif gps_speed_kmh is not None:
            _spd_src = _translate(self.language, "gauge.source.gps")
        else:
            _spd_src = ""
        self.speed_gauge.set_source_label(_spd_src)
        self.temp_gauge.set_value(temp, None if temp is None else f"{temp:.0f}")
        self.acceleration_page.update_payload(payload, self._plain_number)
        self.cars_page.update_live(payload)

        canvas_speed = self._display_speed(speed_source_kmh)
        fuel = self._plain_number(payload, "fuel_level") if active else None
        heading = self._plain_number(payload, "gps_heading") if active else None
        throttle = self._plain_number(payload, "throttle_pos") if active else None
        engine_load = self._plain_number(payload, "engine_load") if active else None
        intake = self._plain_number(payload, "intake_temp") if active else None
        maf = self._plain_number(payload, "maf") if active else None
        voltage = self._plain_number(payload, "control_module_voltage") if active else None
        accel = self._plain_number(payload, "acceleration_g") if active else None

        with self.dashboard_canvas.batch_update():
            self.dashboard_canvas.update_speed_source(_spd_src)
            self.dashboard_canvas.update_rpm(rpm, None if rpm is None else f"{rpm:.0f}")
            self.dashboard_canvas.update_speed(canvas_speed, None if canvas_speed is None else f"{canvas_speed:.0f}")
            self.dashboard_canvas.update_coolant(temp, None if temp is None else f"{temp:.0f}")
            self.dashboard_canvas.update_fuel(fuel, None if fuel is None else f"{fuel:.0f}%")
            self.dashboard_canvas.update_throttle(throttle)
            self.dashboard_canvas.update_engine_load(engine_load)
            self.dashboard_canvas.update_intake(intake)
            self.dashboard_canvas.update_maf(maf)
            self.dashboard_canvas.update_voltage(voltage)
            self.dashboard_canvas.update_accel(accel)
            self.dashboard_canvas.update_obd_speed(self._display_speed(obd_speed_kmh))
            # gps_speed appears in mock payloads; real GPS updates come via the "gps" branch
            if gps_speed_kmh is not None:
                self.dashboard_canvas.update_gps_speed(self._display_speed(gps_speed_kmh))
            if heading is not None:
                self.dashboard_canvas.update_heading(heading)

        status = payload.get("connection_status") or source or "?"
        language = _normalize_language(getattr(self, "language", _detect_language()))
        self.status_label.set_text(_translate(language, "status.updated", status=status, time=datetime.now().strftime("%H:%M:%S")))

        # Telemetrie persistieren — nur bei echter OBD-Verbindung (mock zählt nicht).
        if source == "obd" and self._has_obd_data(payload):
            self._record_obd_sample(payload)
        return False

    def _record_obd_sample(self, payload: dict[str, Any]) -> None:
        ts = time.time()
        accel = self._plain_number(payload, "acceleration_g")
        obd_speed = self._plain_number(payload, "speed")
        gps_speed = self._plain_number(payload, "gps_speed")
        speed = obd_speed if obd_speed is not None else gps_speed
        fields = {
            "speed_kmh":     speed,
            "obd_speed_kmh": obd_speed,
            "gps_speed_kmh": gps_speed,
            "rpm":           self._plain_number(payload, "rpm"),
            "coolant_c":     self._plain_number(payload, "coolant_temp"),
            "throttle_pct":  self._plain_number(payload, "throttle_pos"),
            "engine_load":   self._plain_number(payload, "engine_load"),
            "fuel_pct":      self._plain_number(payload, "fuel_level"),
            "intake_c":      self._plain_number(payload, "intake_temp"),
            "maf_gps":       self._plain_number(payload, "maf"),
            "voltage_v":     self._plain_number(payload, "control_module_voltage"),
            "accel_g":       accel,
        }
        try:
            self.trip_recorder.record_obd(ts, **fields)
        except Exception:
            pass

    def _update_dashboard_from_profile(self, data: dict[str, Any]) -> None:
        """Parse a scan profile dict and push all PID / identity / DTC data to the dashboard."""
        pids: dict[str, float | None] = {}
        for raw_key, raw_val in (data.get("live_data") or {}).items():
            pid = _parse_profile_pid_key(raw_key)
            if not pid:
                continue
            if isinstance(raw_val, dict):
                v = raw_val.get("value")
            else:
                v = raw_val
            try:
                pids[pid] = float(v) if v is not None else None
            except (TypeError, ValueError):
                pids[pid] = None

        info_src = data.get("vehicle_info") or {}
        info: dict[str, str] = {}
        vin = _extract_inner_string(info_src.get("VIN") or "")
        if vin:
            info["vin"] = vin
            brand = _wmi_to_brand(vin)
            if brand:
                info["brand"] = brand
        cal = _extract_inner_string(info_src.get("CALIBRATION_ID") or "")
        if cal:
            info["cal_id"] = cal
        cvn = _extract_inner_string(info_src.get("CVN") or "")
        if cvn:
            info["cvn"] = cvn
        if data.get("protocol"):
            info["protocol"] = str(data["protocol"])
        obd_std = pids.pop("011C", None)
        if obd_std is not None:
            info["obd_standard"] = str(int(obd_std)) if obd_std == int(obd_std) else str(obd_std)

        dtcs = [str(d) for d in (data.get("dtcs") or [])]
        pending = [str(d) for d in (data.get("pending_dtcs") or [])]

        self.dashboard_canvas.update_scan_data(pids, info, dtcs, pending)

    def _load_initial_scan_data(self) -> bool:
        """Called once after startup: push the most recent profile into the dashboard."""
        try:
            profiles = _load_profiles(self.db)
            if profiles:
                best = max(profiles, key=lambda p: p.get("last_seen") or "")
                if best.get("data"):
                    self._update_dashboard_from_profile(best["data"])
        except Exception:
            pass
        return False

    def _handle_scan_identity(self, payload: dict[str, Any]) -> None:
        """Vom Scanner gemeldete Fahrzeug-Identität in die Trip-DB und in cars_page übernehmen."""
        vin = _extract_inner_string(payload.get("vin")) or None
        cal_id = _extract_inner_string(payload.get("cal_id")) or None
        cvn = _extract_inner_string(payload.get("cvn")) or None
        protocol = payload.get("protocol") if isinstance(payload.get("protocol"), str) else None
        profile_path = payload.get("profile_path")
        brand = _wmi_to_brand(vin or "") or None
        try:
            self.trip_recorder.set_car(
                vin=vin, brand=brand, cal_id=cal_id, cvn=cvn,
                protocol=protocol, profile_path=profile_path,
            )
        except Exception:
            pass
        # Live-Header der Auto-Seite aktualisieren
        identity = {}
        if vin:
            identity["VIN"] = vin
        if cal_id:
            identity["CALIBRATION_ID"] = cal_id
        if cvn:
            identity["CVN"] = cvn
        if protocol:
            identity["protocol"] = protocol
        if identity:
            self.cars_page.set_live_identity(identity)

    def _db_periodic_tick(self) -> bool:
        # WAL-Checkpoint + Idle-Erkennung
        try:
            self.db.checkpoint()
        except Exception:
            pass
        try:
            self.trip_recorder.maybe_end_idle_trip(time.time())
        except Exception:
            pass
        return True

    def _shutdown_db(self) -> None:
        try:
            self.trip_recorder.end_trip()
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass


class ObdDashboardApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)
        self.window: DashboardWindow | None = None

    def do_activate(self) -> None:
        register_local_icon()
        load_user_themes(THEMES_DIR)
        if self.window is None:
            self.window = DashboardWindow(self)
        self.window.present()


def main() -> int:
    print_required_python_packages()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = ObdDashboardApp()
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
