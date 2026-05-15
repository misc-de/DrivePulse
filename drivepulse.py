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
import hashlib
import json
import math
import os
import pty
import random
import signal
import socket
import struct
import subprocess
import threading
import time
from datetime import datetime, timezone
from importlib import metadata, util
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

try:
    import obd  # type: ignore
except Exception:
    obd = None

from common import (
    APP_ID,
    CONNECTION_LOG_FILE,
    DB_FILE,
    LOG_DIR,
    LOG_FILE,
    OBD_BAUDRATE,
    OBD_BT_ADDR,
    OBD_FAST,
    OBD_PORT,
    OBD_SOCKET_URL,
    OBD_TIMEOUT_SECONDS,
    POLL_INTERVAL_SECONDS,
    PROFILES_DIR,
    SETTINGS_FILE,
    THEMES_DIR,
    SUPPORTED_LANGUAGES,
    _detect_language,
    _make_label_responsive,
    _normalize_language,
    _translate,
)
from gauge import Gauge, GAUGE_THEMES, all_theme_options, load_user_themes, get_theme_css
from dashboard import DashboardCanvas, DASHBOARD_THEMES
from acceleration import AccelerationPage
from cars import CarsPage
from db import DriveDB, TripRecorder

REQUIRED_PYTHON_PACKAGES = (
    ("PyGObject", "gi", "GTK/libadwaita Python-Bindings"),
    ("pyserial", "serial", "serielle Bluetooth/USB-Port-Anbindung"),
    ("obd", "obd", "OBD-II Dongle-Anbindung"),
)


def _python_package_status(package_name: str, module_name: str) -> str:
    installed = util.find_spec(module_name) is not None
    if not installed:
        return "fehlt"

    try:
        return f"installiert ({metadata.version(package_name)})"
    except metadata.PackageNotFoundError:
        return "installiert"


def _print_required_python_packages() -> None:
    print("Benötigte Python-Pakete:")
    for package_name, module_name, description in REQUIRED_PYTHON_PACKAGES:
        status = _python_package_status(package_name, module_name)
        print(f"  - {package_name}: {status} - {description}")
    print("OBD-Konfiguration:")
    print(f"  - OBD_PORT: {OBD_PORT or 'auto (/dev/rfcomm*, /dev/ttyUSB*, /dev/ttyACM*)'}")
    print(f"  - OBD_BAUDRATE: {OBD_BAUDRATE or 'auto'}")
    print(f"  - OBD_TIMEOUT: {OBD_TIMEOUT_SECONDS:.1f}s")
    print(f"  - OBD_FAST: {'an' if OBD_FAST else 'aus'}")
    if OBD_PORT is None:
        print("  - Bluetooth-Hinweis: ELM327 koppeln und z. B. mit OBD_PORT=/dev/rfcomm0 starten.")
        print("  - Direktes BT: OBD_BT_ADDR=AA:BB:CC:DD:EE:FF (oder AA:BB:CC:DD:EE:FF:Kanal)")
        print("  - socat-Brücke: OBD_SOCKET_URL=socket://localhost:35000")


def _candidate_bt_addresses() -> list[tuple[str, int]]:
    """Parse OBD_BT_ADDR into (mac_address, rfcomm_channel) pairs."""
    if not OBD_BT_ADDR or OBD_PORT:
        return []
    result = []
    for entry in OBD_BT_ADDR.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) == 7 and parts[6].isdigit():
            result.append((":".join(parts[:6]).upper(), int(parts[6])))
        else:
            result.append((entry.upper(), 1))
    return result


def _parse_bt_port(port: str) -> tuple[str, int]:
    """Parse 'bt:AA:BB:CC:DD:EE:FF' or 'bt:AA:BB:CC:DD:EE:FF:channel' into (addr, channel)."""
    raw = port[3:]  # strip 'bt:'
    parts = raw.split(":")
    if len(parts) == 7 and parts[6].isdigit():
        return ":".join(parts[:6]).upper(), int(parts[6])
    return raw.upper(), 1


def _scan_bt_paired_devices() -> list[tuple[str, str]]:
    """Return (label, bt:ADDR) for all paired Bluetooth devices via bluetoothctl."""
    try:
        # Try modern syntax first, fall back to legacy
        for args in (["bluetoothctl", "devices", "Paired"], ["bluetoothctl", "paired-devices"]):
            result = subprocess.run(args, capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                break
        devices: list[tuple[str, str]] = []
        for line in result.stdout.strip().splitlines():
            parts = line.split(" ", 2)
            if len(parts) >= 2 and parts[0] == "Device":
                addr = parts[1].upper()
                name = parts[2].strip() if len(parts) >= 3 else addr
                devices.append((f"BT: {name} ({addr})", f"bt:{addr}"))
        return devices
    except Exception:
        return []


_OBD_CANDIDATE_PATHS = [
    "/dev/rfcomm0",
    "/dev/rfcomm1",
    "/dev/ttyUSB0",
    "/dev/ttyUSB1",
    "/dev/ttyUSB2",
    "/dev/ttyACM0",
    "/dev/ttyACM1",
]


def _scan_obd_devices() -> list[tuple[str, str]]:
    """Return (display_label, port_value) pairs of detectable and common OBD device paths.

    Existing devices come first (with their real path or descriptive by-id name).
    Common candidate paths that are not currently present are appended with a
    '(not found)' suffix so users can pre-configure a port before connecting.
    """
    devices: list[tuple[str, str]] = []
    seen_paths: set[str] = set()

    # /dev/serial/by-id/* — descriptive USB-serial names (only existing)
    for path in sorted(Path("/dev/serial/by-id").glob("*")) if Path("/dev/serial/by-id").exists() else []:
        real = str(path.resolve())
        label = f"{path.name} ({real})"
        devices.append((label, real))
        seen_paths.add(real)

    # Directly present wired / already-bound serial devices
    for pattern in ("/dev/rfcomm*", "/dev/ttyUSB*", "/dev/ttyACM*"):
        for path in sorted(Path("/").glob(pattern.lstrip("/"))):
            p = str(path)
            if p not in seen_paths:
                devices.append((p, p))
                seen_paths.add(p)

    # Paired Bluetooth devices (direct RFCOMM socket, no rfcomm bind needed)
    seen_bt: set[str] = set()
    for label, val in _scan_bt_paired_devices():
        devices.append((label, val))
        seen_bt.add(val)

    # Manually configured BT addresses from env (if not already listed)
    for addr, channel in _candidate_bt_addresses():
        val = f"bt:{addr}" if channel == 1 else f"bt:{addr}:{channel}"
        if val not in seen_bt:
            devices.append((f"BT: {addr}", val))

    if OBD_SOCKET_URL:
        devices.append((OBD_SOCKET_URL, OBD_SOCKET_URL))

    # Common candidate paths not yet present — let users pre-configure
    for candidate in _OBD_CANDIDATE_PATHS:
        if candidate not in seen_paths:
            devices.append((f"{candidate} (not found)", candidate))
            seen_paths.add(candidate)

    return devices


class BluetoothPtyBridge:
    """Bridges a Bluetooth RFCOMM socket to a PTY so pyserial/python-obd can use it."""

    _CONNECT_TIMEOUT = 10.0

    def __init__(self, addr: str, channel: int = 1) -> None:
        self.addr = addr
        self.channel = channel
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._master_fd = -1
        self._slave_fd = -1
        self.pty_path = ""
        self._open()

    def _open(self) -> None:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        try:
            sock.settimeout(self._CONNECT_TIMEOUT)
            sock.connect((self.addr, self.channel))
            sock.settimeout(None)
        except Exception:
            sock.close()
            raise
        self._sock = sock
        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd
        self._slave_fd = slave_fd
        self.pty_path = os.ttyname(slave_fd)
        threading.Thread(target=self._relay_fd_to_sock, args=(master_fd, sock), daemon=True).start()
        threading.Thread(target=self._relay_sock_to_fd, args=(sock, master_fd), daemon=True).start()

    def _relay_fd_to_sock(self, fd: int, sock: socket.socket) -> None:
        try:
            while not self._stop.is_set():
                try:
                    data = os.read(fd, 4096)
                    if not data:
                        break
                    sock.sendall(data)
                except OSError:
                    break
        finally:
            self._stop.set()

    def _relay_sock_to_fd(self, sock: socket.socket, fd: int) -> None:
        try:
            while not self._stop.is_set():
                try:
                    data = sock.recv(4096)
                    if not data:
                        break
                    os.write(fd, data)
                except OSError:
                    break
        finally:
            self._stop.set()

    def close(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        for fd in (self._master_fd, self._slave_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._master_fd = -1
        self._slave_fd = -1

    @property
    def is_alive(self) -> bool:
        return not self._stop.is_set()


class ObdScanner:
    """One-shot full-scan of a newly connected OBD adapter/vehicle.

    Runs in the ObdReader background thread. Reports progress via GLib.idle_add.
    Saves a JSON profile to PROFILES_DIR keyed by VIN (or port+command fingerprint).
    Skips silently if the profile already exists or was already scanned this session.
    """

    def __init__(
        self,
        connection: Any,
        port: str | None,
        on_update: Callable[[dict[str, Any]], None],
        session_cache: set[str],
    ) -> None:
        self.connection = connection
        self.port = port or "unknown"
        self.on_update = on_update
        self._session_cache = session_cache

    def _emit(self, status: str, progress: float, current: str = "") -> None:
        GLib.idle_add(self.on_update, {
            "source": "obd_scan",
            "scan_status": status,
            "scan_progress": progress,
            "scan_current": current,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def run(self) -> None:
        if obd is None or self.connection is None:
            return

        self._emit("scanning", 0.0, "VIN")

        vin = self._query_vin()
        if vin:
            identity = f"vin_{vin}"
        else:
            supported_names = sorted(str(c) for c in getattr(self.connection, "supported_commands", set()))
            fp = hashlib.md5(",".join(supported_names).encode()).hexdigest()[:8]
            identity = f"port_{Path(self.port).name}_{fp}"

        profile_path = PROFILES_DIR / f"{identity}.json"

        # Identität für die Trip-DB immer mitteilen, auch wenn der Scan ansonsten geskippt wird.
        self._emit_identity(vin, profile_path)

        if identity in self._session_cache:
            self._emit("skipped", 1.0)
            return

        if profile_path.exists():
            self._session_cache.add(identity)
            # Schnellscan: Cal-ID/CVN aus dem vorhandenen Profil nachreichen
            try:
                import json as _json
                cached = _json.loads(profile_path.read_text(encoding="utf-8"))
                self._emit_identity(
                    vin,
                    profile_path,
                    cal_id=(cached.get("vehicle_info") or {}).get("CALIBRATION_ID"),
                    cvn=(cached.get("vehicle_info") or {}).get("CVN"),
                    protocol=cached.get("protocol"),
                )
            except Exception:
                pass
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
            done += 1
            self._emit("scanning", done / total_steps, str(cmd))
            try:
                r = self.connection.query(cmd)
                if not r.is_null():
                    live_data[str(cmd)] = self._to_plain(r)
            except Exception as exc:
                live_data[str(cmd)] = {"error": str(exc)}

        # Mode 03: stored DTCs
        done += 1
        self._emit("scanning", done / total_steps, "DTC (gespeichert)")
        dtcs = self._query_dtc_list(getattr(obd.commands, "GET_DTC", None))

        # Mode 07: pending DTCs
        done += 1
        self._emit("scanning", done / total_steps, "DTC (ausstehend)")
        pending_dtcs = self._query_dtc_list(getattr(obd.commands, "PENDING_DTC", None))

        # Mode 09: vehicle info (VIN already done, add extras)
        done += 1
        self._emit("scanning", done / total_steps, "Fahrzeuginfo")
        vehicle_info: dict[str, Any] = {}
        if vin:
            vehicle_info["VIN"] = vin
        for name in ("CALIBRATION_ID", "CVN", "ECU_NAME"):
            cmd = getattr(obd.commands, name, None)
            if cmd is None:
                continue
            try:
                r = self.connection.query(cmd)
                if not r.is_null():
                    vehicle_info[name] = str(r.value)
            except Exception:
                pass

        # Save profile
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
        try:
            PROFILES_DIR.mkdir(parents=True, exist_ok=True)
            profile_path.write_text(
                json.dumps(profile, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            self._session_cache.add(identity)
        except Exception as exc:
            self._emit("error", 1.0, str(exc))
            return

        # Volle Identität (inkl. Cal-ID/CVN) nach dem Scan an die App schicken.
        self._emit_identity(
            vin,
            profile_path,
            cal_id=vehicle_info.get("CALIBRATION_ID"),
            cvn=vehicle_info.get("CVN"),
            protocol=profile.get("protocol"),
        )
        self._emit("complete", 1.0, str(profile_path))

    def _emit_identity(self, vin: str | None, profile_path: Path,
                       cal_id: Any = None, cvn: Any = None, protocol: Any = None) -> None:
        GLib.idle_add(self.on_update, {
            "source": "obd_scan_identity",
            "vin": vin,
            "cal_id": cal_id,
            "cvn": cvn,
            "protocol": protocol,
            "profile_path": str(profile_path),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def _query_vin(self) -> str | None:
        try:
            cmd = getattr(obd.commands, "VIN", None)
            if cmd is None:
                return None
            r = self.connection.query(cmd)
            if not r.is_null():
                val = str(r.value).strip()
                return val if val else None
        except Exception:
            pass
        return None

    def _query_dtc_list(self, cmd: Any) -> list[str]:
        if cmd is None:
            return []
        try:
            r = self.connection.query(cmd)
            if not r.is_null() and r.value:
                return [str(d) for d in r.value]
        except Exception:
            pass
        return []

    def _get_protocol(self) -> str:
        try:
            return str(self.connection.protocol_name())
        except Exception:
            return "unknown"

    def _to_plain(self, response: Any) -> Any:
        value = response.value
        try:
            return {"value": float(value.magnitude), "unit": str(value.units)}
        except Exception:
            return str(value)


class ObdReader(GObject.Object):
    """Liest OBD-II-Werte in einem Hintergrund-Thread."""

    __gtype_name__ = "ObdReader"

    # Minimum OBD() timeout for direct BT connections (ELM327 init can be slow over BT)
    _BT_OBD_TIMEOUT = 15.0

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
                    addr, ch = _parse_bt_port(self._configured_port)
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

        for addr, channel in _candidate_bt_addresses():
            if self.stop_event.is_set():
                self._connection_log("connect_aborted", reason="stop_event")
                return
            if self._try_bt_direct(addr, channel):
                return

        self.mock = True
        self.mock_reason = "kein nutzbarer Dongle gefunden"
        self._connection_log("connect_failed", reason=self.mock_reason, fallback="mock")

    def _run_vehicle_scan(self) -> None:
        if obd is None or self.connection is None or self.mock:
            return
        ObdScanner(self.connection, self.connected_port, self.on_update, self._scanned_identities).run()

    def _run(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._connect()
        self._run_vehicle_scan()

        while not self.stop_event.is_set():
            if self._force_reconnect:
                self._force_reconnect = False
                self.mock = False
                self.mock_reason = ""
                self._connect()
                self._run_vehicle_scan()
            self._maybe_reconnect_from_mock()
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

        self.next_mock_reconnect_attempt = now + 8.0
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

        commands = {
            "rpm": obd.commands.RPM,
            "speed": obd.commands.SPEED,
            "coolant_temp": obd.commands.COOLANT_TEMP,
            "throttle_pos": obd.commands.THROTTLE_POS,
            "engine_load": obd.commands.ENGINE_LOAD,
            "intake_temp": obd.commands.INTAKE_TEMP,
            "maf": obd.commands.MAF,
            "fuel_level": getattr(obd.commands, "FUEL_LEVEL", None),
            "runtime": getattr(obd.commands, "RUN_TIME", None),
            "control_module_voltage": getattr(obd.commands, "CONTROL_MODULE_VOLTAGE", None),
        }

        data: dict[str, Any] = {}
        command_count = 0
        read_error_count = 0
        for key, command in commands.items():
            if command is None:
                continue
            command_count += 1
            try:
                response = self.connection.query(command)
                data[key] = self._response_to_plain_value(response)
            except Exception as exc:
                read_error_count += 1
                data[f"{key}_error"] = str(exc)
        data["_command_count"] = command_count
        data["_read_error_count"] = read_error_count
        return data

    def _response_to_plain_value(self, response: Any) -> Any:
        if response is None or response.is_null():
            return None
        value = response.value
        try:
            magnitude = value.magnitude
            unit = str(value.units)
            return {"value": float(magnitude), "unit": unit}
        except Exception:
            return str(value)

    def _read_mock(self) -> dict[str, Any]:
        now = time.time()
        rpm = 900 + 700 * (math.sin(now / 3) + 1) + random.uniform(-80, 80)
        speed = max(0, 55 + 18 * math.sin(now / 6) + random.uniform(-3, 3))
        temp = 84 + 4 * math.sin(now / 15) + random.uniform(-0.5, 0.5)
        previous_speed = getattr(self, "_mock_previous_speed", speed)
        previous_time = getattr(self, "_mock_previous_time", now)
        dt = max(0.001, now - previous_time)
        acceleration_ms2 = ((speed - previous_speed) / 3.6) / dt
        acceleration_g = acceleration_ms2 / 9.80665
        self._mock_previous_speed = speed
        self._mock_previous_time = now

        heading = (now * 8.0) % 360.0
        return {
            "rpm": {"value": rpm, "unit": "rpm"},
            "speed": {"value": speed, "unit": "km/h"},
            "gps_speed": {"value": max(0, speed + random.uniform(-1.5, 1.5)), "unit": "km/h"},
            "gps_heading": {"value": heading, "unit": "deg"},
            "acceleration_g": {"value": acceleration_g, "unit": "g"},
            "coolant_temp": {"value": temp, "unit": "degC"},
            "fuel_level": {"value": 68 + 5 * math.sin(now / 60), "unit": "percent"},
            "throttle_pos": {"value": random.uniform(8, 42), "unit": "percent"},
            "engine_load": {"value": random.uniform(12, 68), "unit": "percent"},
            "intake_temp": {"value": 20 + random.uniform(-3, 5), "unit": "degC"},
            "control_module_voltage": {"value": 13.8 + random.uniform(-0.25, 0.25), "unit": "volt"},
        }

    def _write_log(self, payload: dict[str, Any]) -> None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass


class GpsReader:
    """Reads GPS speed from GeoClue2 (D-Bus) with GPSD as fallback."""

    # GeoClue2 D-Bus constants (same as Sensor-Suite)
    _GEOCLUE_BUS = "org.freedesktop.GeoClue2"
    _GEOCLUE_MANAGER_PATH = "/org/freedesktop/GeoClue2/Manager"
    _GEOCLUE_MANAGER_IFACE = "org.freedesktop.GeoClue2.Manager"
    _GEOCLUE_CLIENT_IFACE = "org.freedesktop.GeoClue2.Client"
    _GEOCLUE_LOCATION_IFACE = "org.freedesktop.GeoClue2.Location"
    _DBUS_PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"

    GPSD_HOST = "localhost"
    GPSD_PORT = 2947
    GPSD_RETRY_INTERVAL = 10.0

    def __init__(self, on_update: Callable[[dict[str, Any]], None]) -> None:
        self.on_update = on_update
        self.stop_event = threading.Event()
        self._gpsd_thread: threading.Thread | None = None
        self._geoclue_bus: Any = None
        self._geoclue_client: Any = None
        self._geoclue_client_path: str | None = None

    def start(self) -> None:
        GLib.idle_add(self._start_geoclue)
        self._gpsd_thread = threading.Thread(target=self._run_gpsd, name="gps-gpsd", daemon=True)
        self._gpsd_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self._geoclue_client is not None:
            try:
                self._geoclue_client.call_sync("Stop", None, Gio.DBusCallFlags.NONE, 1000, None)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # GeoClue2
    # ------------------------------------------------------------------

    def _start_geoclue(self) -> bool:
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            manager = Gio.DBusProxy.new_sync(
                bus, Gio.DBusProxyFlags.NONE, None,
                self._GEOCLUE_BUS, self._GEOCLUE_MANAGER_PATH, self._GEOCLUE_MANAGER_IFACE, None,
            )
            res = manager.call_sync("GetClient", None, Gio.DBusCallFlags.NONE, 3000, None)
            client_path = res.get_child_value(0).get_string()
            client = Gio.DBusProxy.new_sync(
                bus, Gio.DBusProxyFlags.NONE, None,
                self._GEOCLUE_BUS, client_path, self._GEOCLUE_CLIENT_IFACE, None,
            )
            self._geoclue_bus = bus
            self._geoclue_client = client
            self._geoclue_client_path = client_path
            for name, value in (
                ("DesktopId", GLib.Variant("s", APP_ID)),
                ("RequestedAccuracyLevel", GLib.Variant("u", 8)),
                ("DistanceThreshold", GLib.Variant("u", 0)),
                ("TimeThreshold", GLib.Variant("u", 1)),
            ):
                try:
                    bus.call_sync(
                        self._GEOCLUE_BUS, client_path, self._DBUS_PROPERTIES_IFACE, "Set",
                        GLib.Variant("(ssv)", (self._GEOCLUE_CLIENT_IFACE, name, value)),
                        None, Gio.DBusCallFlags.NONE, 3000, None,
                    )
                except Exception:
                    pass
            client.connect("g-signal", self._on_geoclue_signal)
            client.call_sync("Start", None, Gio.DBusCallFlags.NONE, 3000, None)
        except Exception:
            pass
        return False

    def _on_geoclue_signal(self, _proxy: Any, _sender: str, signal_name: str, params: Any) -> None:
        if signal_name != "LocationUpdated":
            return
        location_path = params.get_child_value(1).get_string()
        try:
            location = Gio.DBusProxy.new_sync(
                self._geoclue_bus, Gio.DBusProxyFlags.NONE, None,
                self._GEOCLUE_BUS, location_path, self._GEOCLUE_LOCATION_IFACE, None,
            )
            speed = self._geoclue_double(location, "Speed")
            if speed is not None and speed >= 0:
                gps_payload: dict[str, Any] = {
                    "source": "gps",
                    "gps_speed": {"value": speed * 3.6, "unit": "km/h"},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                heading = self._geoclue_double(location, "Heading")
                if heading is not None and 0 <= heading < 360:
                    gps_payload["gps_heading"] = {"value": heading, "unit": "deg"}
                lat = self._geoclue_double(location, "Latitude")
                lon = self._geoclue_double(location, "Longitude")
                if lat is not None and lon is not None:
                    gps_payload["gps_lat"] = {"value": lat, "unit": "degree"}
                    gps_payload["gps_lon"] = {"value": lon, "unit": "degree"}
                altitude = self._geoclue_double(location, "Altitude")
                if altitude is not None:
                    gps_payload["gps_altitude"] = {"value": altitude, "unit": "meter"}
                self.on_update(gps_payload)
        except Exception:
            pass

    def _geoclue_double(self, proxy: Any, name: str) -> float | None:
        value = proxy.get_cached_property(name)
        if value is None:
            return None
        result = value.get_double()
        return result if math.isfinite(result) else None

    # ------------------------------------------------------------------
    # GPSD
    # ------------------------------------------------------------------

    def _run_gpsd(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._connect_and_read_gpsd()
            except Exception:
                pass
            self.stop_event.wait(self.GPSD_RETRY_INTERVAL)

    def _connect_and_read_gpsd(self) -> None:
        import socket
        with socket.create_connection((self.GPSD_HOST, self.GPSD_PORT), timeout=5) as sock:
            sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
            buf = ""
            while not self.stop_event.is_set():
                chunk = sock.recv(4096).decode("utf-8", errors="replace")
                if not chunk:
                    break
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    self._handle_gpsd_line(line.strip())

    def _handle_gpsd_line(self, line: str) -> None:
        if not line:
            return
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return
        if data.get("class") != "TPV" or data.get("mode", 0) < 2:
            return
        speed_ms = data.get("speed")
        if speed_ms is None:
            return
        gps_payload: dict[str, Any] = {
            "source": "gps",
            "gps_speed": {"value": float(speed_ms) * 3.6, "unit": "km/h"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        track = data.get("track")
        if track is not None:
            gps_payload["gps_heading"] = {"value": float(track), "unit": "deg"}
        lat = data.get("lat")
        lon = data.get("lon")
        if lat is not None and lon is not None:
            gps_payload["gps_lat"] = {"value": float(lat), "unit": "degree"}
            gps_payload["gps_lon"] = {"value": float(lon), "unit": "degree"}
        altitude = data.get("alt")
        if altitude is not None:
            gps_payload["gps_altitude"] = {"value": float(altitude), "unit": "meter"}
        GLib.idle_add(self.on_update, gps_payload)


class SettingsDialog(Adw.PreferencesDialog):
    __gtype_name__ = "SettingsDialog"

    def __init__(
        self,
        parent: Gtk.Window,
        current_units: str,
        current_language: str,
        on_units_changed: Callable[[str], None],
        on_language_changed: Callable[[str], None],
        current_mock_mode: bool = False,
        on_mock_mode_changed: Callable[[bool], None] | None = None,
        current_obd_port: str | None = None,
        on_obd_port_changed: Callable[[str | None], None] | None = None,
        current_gauge_theme: str = "cockpit",
        on_gauge_theme_changed: Callable[[str], None] | None = None,
        current_auto_rotate: bool = True,
        on_auto_rotate_changed: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__()
        self.language = _normalize_language(current_language)
        self.on_units_changed = on_units_changed
        self.on_language_changed = on_language_changed
        self.on_mock_mode_changed = on_mock_mode_changed
        self.on_obd_port_changed = on_obd_port_changed
        self.on_gauge_theme_changed = on_gauge_theme_changed
        self.on_auto_rotate_changed = on_auto_rotate_changed
        self.set_title(_translate(self.language, "settings.title"))

        page = Adw.PreferencesPage(title=_translate(self.language, "settings.display"))
        group = Adw.PreferencesGroup(title=_translate(self.language, "settings.units"))

        self.unit_row = Adw.ComboRow(title=_translate(self.language, "settings.speed"))
        model = Gtk.StringList()
        model.append(_translate(self.language, "settings.metric"))
        model.append(_translate(self.language, "settings.imperial"))
        self.unit_row.set_model(model)
        self.unit_row.set_selected(0 if current_units == "metric" else 1)
        self.unit_row.connect("notify::selected", self._on_unit_selected)

        self.language_row = Adw.ComboRow(title=_translate(self.language, "settings.language"))
        language_model = Gtk.StringList()
        language_model.append(_translate(self.language, "settings.language.en"))
        language_model.append(_translate(self.language, "settings.language.de"))
        self.language_row.set_model(language_model)
        self.language_row.set_selected(SUPPORTED_LANGUAGES.index(self.language))
        self.language_row.connect("notify::selected", self._on_language_selected)

        self.mock_switch = Gtk.Switch()
        self.mock_switch.set_active(current_mock_mode)
        self.mock_switch.set_valign(Gtk.Align.CENTER)
        self.mock_switch.connect("notify::active", self._on_mock_changed)
        self.mock_row = Adw.ActionRow(
            title=_translate(self.language, "settings.mock_mode"),
            subtitle=_translate(self.language, "settings.mock_mode.subtitle"),
        )
        self.mock_row.add_suffix(self.mock_switch)
        self.mock_row.set_activatable_widget(self.mock_switch)

        self.auto_rotate_switch = Gtk.Switch()
        self.auto_rotate_switch.set_active(current_auto_rotate)
        self.auto_rotate_switch.set_valign(Gtk.Align.CENTER)
        self.auto_rotate_switch.connect("notify::active", self._on_auto_rotate_changed)
        self.auto_rotate_row = Adw.ActionRow(
            title=_translate(self.language, "settings.auto_rotate"),
            subtitle=_translate(self.language, "settings.auto_rotate.subtitle"),
        )
        self.auto_rotate_row.add_suffix(self.auto_rotate_switch)
        self.auto_rotate_row.set_activatable_widget(self.auto_rotate_switch)

        self._theme_options = all_theme_options(self.language)
        theme_model = Gtk.StringList()
        for _, label in self._theme_options:
            theme_model.append(label)
        self.gauge_theme_row = Adw.ComboRow(title=_translate(self.language, "settings.gauge_theme"))
        self.gauge_theme_row.set_model(theme_model)
        theme_ids = [tid for tid, _ in self._theme_options]
        selected_idx = theme_ids.index(current_gauge_theme) if current_gauge_theme in theme_ids else 0
        self.gauge_theme_row.set_selected(selected_idx)
        self.gauge_theme_row.connect("notify::selected", self._on_gauge_theme_selected)

        group.add(self.unit_row)
        group.add(self.language_row)
        group.add(self.gauge_theme_row)
        group.add(self.auto_rotate_row)
        group.add(self.mock_row)
        page.add(group)

        # OBD hardware group
        obd_devices = _scan_obd_devices()
        self._obd_port_values: list[str | None] = [None] + [val for _, val in obd_devices]
        obd_group = Adw.PreferencesGroup(title=_translate(self.language, "settings.obd"))
        dongle_model = Gtk.StringList()
        dongle_model.append(_translate(self.language, "settings.obd_dongle.auto"))
        for label, _ in obd_devices:
            dongle_model.append(label)
        self.dongle_row = Adw.ComboRow(title=_translate(self.language, "settings.obd_dongle"))
        self.dongle_row.set_model(dongle_model)
        if not obd_devices:
            self.dongle_row.set_subtitle(_translate(self.language, "settings.obd_dongle.none_found"))
        selected_idx = 0
        if current_obd_port in self._obd_port_values:
            selected_idx = self._obd_port_values.index(current_obd_port)
        self.dongle_row.set_selected(selected_idx)
        self.dongle_row.connect("notify::selected", self._on_dongle_selected)
        obd_group.add(self.dongle_row)

        page.add(obd_group)

        self.add(page)

    def _on_unit_selected(self, *_args: Any) -> None:
        self.on_units_changed("metric" if self.unit_row.get_selected() == 0 else "imperial")

    def _on_language_selected(self, *_args: Any) -> None:
        self.on_language_changed(SUPPORTED_LANGUAGES[self.language_row.get_selected()])

    def _on_mock_changed(self, *_args: Any) -> None:
        if self.on_mock_mode_changed is not None:
            self.on_mock_mode_changed(self.mock_switch.get_active())

    def _on_auto_rotate_changed(self, *_args: Any) -> None:
        if self.on_auto_rotate_changed is not None:
            self.on_auto_rotate_changed(self.auto_rotate_switch.get_active())

    def _on_dongle_selected(self, *_args: Any) -> None:
        if self.on_obd_port_changed is not None:
            idx = self.dongle_row.get_selected()
            port = self._obd_port_values[idx] if idx < len(self._obd_port_values) else None
            self.on_obd_port_changed(port)

    def _on_gauge_theme_selected(self, *_args: Any) -> None:
        if self.on_gauge_theme_changed is not None:
            idx = self.gauge_theme_row.get_selected()
            theme = self._theme_options[idx][0] if idx < len(self._theme_options) else "cockpit"
            self.on_gauge_theme_changed(theme)


class OrientationReader:
    """Reads physical device orientation from the accelerometer.

    Tries sensorfwd (com.nokia.SensorService, FuriOS/Droidian) first,
    then falls back to iio-sensor-proxy (net.hadess.SensorProxy).
    Calls on_changed(orientation_str, angle_degrees, is_landscape) on the
    GTK main thread whenever the orientation changes.
    Gracefully does nothing when neither service is available.
    """

    _MAP: dict[str, tuple[int, bool]] = {
        "normal":    (0,   False),
        "right-up":  (90,  True),
        "bottom-up": (180, False),
        "left-up":   (270, True),
    }

    # Binary protocol constants for sensorfwd socket
    _HDR   = struct.Struct("<I")        # 4 bytes: packet count
    _ACCEL = struct.Struct("<Qfffi")    # 20 bytes: ts + x + y + z + reserved (mg)

    # Axis threshold for orientation detection (mg)
    _THRESHOLD = 600

    def __init__(self, on_changed: Callable[[str, int, bool], None], enabled: bool = True) -> None:
        self.on_changed = on_changed
        self._enabled = enabled
        self._current = "normal"
        # sensorfwd state
        self._bus: Any = None
        self._session_id: int = -1
        self._sock: Any = None
        self._watch_id: int = 0
        self._buf = b""
        # iio-sensor-proxy state (fallback)
        self._iio_proxy: Any = None
        if enabled:
            GLib.idle_add(self._start)

    # ── start / stop ──────────────────────────────────────────────────────

    def _start(self) -> bool:
        if self._try_sensorfwd():
            return False
        self._try_iio_proxy()
        return False

    def _try_sensorfwd(self) -> bool:
        """Connect to com.nokia.SensorService. Returns True on success."""
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            pid = os.getpid()
            # Load accelerometer plugin
            bus.call_sync(
                "com.nokia.SensorService", "/SensorManager",
                "local.SensorManager", "loadPlugin",
                GLib.Variant("(s)", ("accelerometersensor",)),
                None, Gio.DBusCallFlags.NONE, 2000, None,
            )
            # Request session
            res = bus.call_sync(
                "com.nokia.SensorService", "/SensorManager",
                "local.SensorManager", "requestSensor",
                GLib.Variant("(sx)", ("accelerometersensor", pid)),
                GLib.VariantType.new("(i)"),
                Gio.DBusCallFlags.NONE, 2000, None,
            )
            session_id = res.get_child_value(0).get_int32()
            # 33 ms interval (~30 Hz)
            bus.call_sync(
                "com.nokia.SensorService", "/SensorManager/accelerometersensor",
                "local.AccelerometerSensor", "setInterval",
                GLib.Variant("(ii)", (session_id, 33)),
                None, Gio.DBusCallFlags.NONE, 2000, None,
            )
            # Start sensor
            bus.call_sync(
                "com.nokia.SensorService", "/SensorManager/accelerometersensor",
                "local.AccelerometerSensor", "start",
                GLib.Variant("(i)", (session_id,)),
                None, Gio.DBusCallFlags.NONE, 2000, None,
            )
            # Connect to the data socket
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect("/run/sensord.sock")
            sock.send(struct.pack("<i", session_id))
            sock.recv(1)  # handshake byte
            sock.setblocking(False)
            self._bus = bus
            self._session_id = session_id
            self._sock = sock
            self._watch_id = GLib.io_add_watch(
                sock.fileno(),
                GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP,
                self._on_socket,
            )
            return True
        except Exception:
            return False

    def _try_iio_proxy(self) -> None:
        """Fall back to iio-sensor-proxy."""
        try:
            proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, None,
                "net.hadess.SensorProxy", "/net/hadess/SensorProxy",
                "net.hadess.SensorProxy", None,
            )
            has = proxy.get_cached_property("HasAccelerometer")
            if not has or not has.get_boolean():
                return
            proxy.call_sync("ClaimAccelerometer", None, Gio.DBusCallFlags.NONE, 2000, None)
            proxy.connect("g-properties-changed", self._on_iio_props_changed)
            self._iio_proxy = proxy
            v = proxy.get_cached_property("AccelerometerOrientation")
            if v:
                self._emit(v.get_string())
        except Exception:
            pass

    # ── sensorfwd socket data ─────────────────────────────────────────────

    def _on_socket(self, _fd: int, condition: int) -> bool:
        if condition & (GLib.IO_ERR | GLib.IO_HUP):
            return False
        try:
            self._buf += self._sock.recv(4096)
            while len(self._buf) >= self._HDR.size:
                (count,) = self._HDR.unpack_from(self._buf)
                need = self._HDR.size + count * self._ACCEL.size
                if len(self._buf) < need:
                    break
                last_xyz = None
                for i in range(count):
                    _, x, y, z, _ = self._ACCEL.unpack_from(
                        self._buf, self._HDR.size + i * self._ACCEL.size
                    )
                    last_xyz = (x, y, z)
                self._buf = self._buf[need:]
                if last_xyz:
                    self._on_accel(*last_xyz)
        except BlockingIOError:
            pass
        except Exception:
            return False
        return True

    def _on_accel(self, x: float, y: float, z: float) -> None:
        """Determine orientation from raw accelerometer values (in mg)."""
        ax, ay = abs(x), abs(y)
        if ax < self._THRESHOLD and ay < self._THRESHOLD:
            return  # device lying flat — keep current orientation
        if ay >= ax:
            orientation = "normal" if y > 0 else "bottom-up"
        else:
            orientation = "left-up" if x > 0 else "right-up"
        self._emit(orientation)

    # ── iio-sensor-proxy fallback ─────────────────────────────────────────

    def _on_iio_props_changed(self, _proxy: Any, changed: Any, _invalidated: Any) -> None:
        v = changed.lookup_value("AccelerometerOrientation", None)
        if v is not None:
            self._emit(v.get_string())

    # ── shared emit / enable ──────────────────────────────────────────────

    def _emit(self, orientation: str) -> None:
        if not self._enabled or orientation == self._current:
            return
        self._current = orientation
        angle, landscape = self._MAP.get(orientation, (0, False))
        GLib.idle_add(self.on_changed, orientation, angle, landscape)

    def set_enabled(self, enabled: bool) -> None:
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if enabled:
            # Start if not already connected
            if self._sock is None and self._iio_proxy is None:
                GLib.idle_add(self._start)
            else:
                # Re-emit current orientation immediately
                angle, landscape = self._MAP.get(self._current, (0, False))
                GLib.idle_add(self.on_changed, self._current, angle, landscape)
        else:
            # Reset to upright so the UI goes back to default when disabled
            GLib.idle_add(self.on_changed, "normal", 0, False)

    def stop(self) -> None:
        if self._watch_id:
            GLib.source_remove(self._watch_id)
            self._watch_id = 0
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._bus is not None and self._session_id >= 0:
            try:
                self._bus.call_sync(
                    "com.nokia.SensorService", "/SensorManager/accelerometersensor",
                    "local.AccelerometerSensor", "stop",
                    GLib.Variant("(i)", (self._session_id,)),
                    None, Gio.DBusCallFlags.NONE, 1000, None,
                )
                self._bus.call_sync(
                    "com.nokia.SensorService", "/SensorManager",
                    "local.SensorManager", "releaseSensor",
                    GLib.Variant("(sx)", ("accelerometersensor", os.getpid())),
                    None, Gio.DBusCallFlags.NONE, 1000, None,
                )
            except Exception:
                pass
            self._bus = None
            self._session_id = -1
        if self._iio_proxy is not None:
            try:
                self._iio_proxy.call_sync(
                    "ReleaseAccelerometer", None, Gio.DBusCallFlags.NONE, 1000, None,
                )
            except Exception:
                pass
            self._iio_proxy = None


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
        footer.append(self.status_label)

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
        acceleration_scroller = Gtk.ScrolledWindow()
        acceleration_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        acceleration_scroller.set_propagate_natural_width(False)
        acceleration_scroller.set_propagate_natural_height(False)
        acceleration_scroller.set_hexpand(True)
        acceleration_scroller.set_vexpand(True)
        acceleration_scroller.set_child(self.acceleration_page)

        self.cars_page = CarsPage(self.language, db=self.db)
        self.cars_page.on_back_swipe = self._on_cars_back_swipe

        self.view_stack = Adw.ViewStack()
        self.view_stack.set_vexpand(True)
        self.view_stack.set_hexpand(True)
        self.view_stack.set_hhomogeneous(False)
        self.view_stack.set_vhomogeneous(False)
        self.view_stack.set_enable_transitions(True)
        self.view_stack.set_transition_duration(240)
        self.dashboard_stack_page = self.view_stack.add_titled_with_icon(
            dashboard_scroller,
            self.PAGE_DASHBOARD,
            _translate(self.language, "nav.gauges"),
            "view-grid-symbolic",
        )
        self.acceleration_stack_page = self.view_stack.add_titled_with_icon(
            acceleration_scroller,
            self.PAGE_ACCELERATION,
            _translate(self.language, "nav.acceleration"),
            "media-skip-forward-symbolic",
        )
        self.cars_stack_page = self.view_stack.add_titled_with_icon(
            self.cars_page,
            self.PAGE_CARS,
            _translate(self.language, "nav.cars"),
            "applications-system-symbolic",
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
        header.pack_start(self.obd_indicator["box"])
        header.pack_start(self.gps_indicator["box"])
        header.pack_end(settings_button)

        self.header = header
        self.switcher_bar = switcher_bar
        toolbar_view.add_top_bar(header)
        toolbar_view.add_bottom_bar(switcher_bar)
        toolbar_view.set_content(self.view_stack)

        self._nav_visible = True
        self._last_swipe_time = 0.0
        tap = Gtk.GestureClick()
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

        self.reader = ObdReader(self._update_from_payload, force_mock=self.mock_mode)
        self.reader._configured_port = self.obd_port
        self.reader.start()
        self.gps_reader = GpsReader(self._update_from_payload)
        self.gps_reader.start()
        self.orientation_reader = OrientationReader(self._on_orientation_changed, enabled=self.auto_rotate)

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

    def _on_content_tap(self, _gesture: Gtk.GestureClick, _n: int, _x: float, _y: float) -> None:
        if time.monotonic() - self._last_swipe_time < 0.35:
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

    def _load_settings(self) -> dict[str, Any]:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            units = data.get("units")
            language = data.get("language")
            return {
                "units": units if units in {"metric", "imperial"} else "metric",
                "language": _normalize_language(language or _detect_language()),
                "mock_mode": bool(data.get("mock_mode", False)),
                "obd_port": data.get("obd_port") or None,
                "gauge_theme": data.get("gauge_theme", "cockpit") or "cockpit",
                "auto_rotate": bool(data.get("auto_rotate", True)),
            }
        except Exception:
            return {"units": "metric", "language": _detect_language(), "mock_mode": False, "obd_port": None, "gauge_theme": "cockpit", "auto_rotate": True}

    def _load_units(self) -> str:
        return self._load_settings()["units"]

    def _save_settings(self) -> None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(
                json.dumps(
                    {
                        "units": getattr(self, "units", "metric"),
                        "language": _normalize_language(getattr(self, "language", _detect_language())),
                        "mock_mode": getattr(self, "mock_mode", False),
                        "obd_port": getattr(self, "obd_port", None),
                        "gauge_theme": getattr(self, "gauge_theme", "cockpit"),
                        "auto_rotate": getattr(self, "auto_rotate", True),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
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

        if self.gauge_box.get_visible():
            # Sensor landscape (90/270) takes precedence — handles compositors that
            # rotate the screen without changing the reported window dimensions (Phosh).
            # Portrait sensor values (0/180) are skipped so window dimensions decide:
            # on desktop or when auto-rotate is off, width/height is the right signal.
            if self._device_rotation in (90, 270):
                self._set_landscape_layout(max(width, height), min(width, height))
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
        current = self.view_stack.get_visible_child_name()
        # Wenn das Auto-Detail offen ist, übernimmt Adw.NavigationView den
        # Zurück-Swipe (Detail → Liste). Wir schalten dann nicht zusätzlich den Tab um.
        if current == self.PAGE_CARS and velocity_x > 0 and self.cars_page.is_detail_open():
            return
        pages = [self.PAGE_DASHBOARD, self.PAGE_ACCELERATION, self.PAGE_CARS]
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
        item = data.get(key)
        if item is None:
            return None
        if isinstance(item, dict):
            value = item.get("value")
        else:
            value = item
        try:
            return float(value)
        except Exception:
            return None

    def _display_speed(self, speed_kmh: float | None) -> float | None:
        if speed_kmh is None:
            return None
        return speed_kmh if self.units == "metric" else speed_kmh * 0.621371

    def _has_obd_data(self, payload: dict[str, Any]) -> bool:
        return any(self._plain_number(payload, key) is not None for key in ("rpm", "speed", "coolant_temp", "throttle_pos", "engine_load"))

    def _gps_connected_with_holdover(self, gps_speed_kmh: float | None) -> bool:
        now = time.monotonic()
        if gps_speed_kmh is not None:
            self._gps_last_seen = now
            return True
        return (now - self._gps_last_seen) < self.GPS_UNAVAIL_HOLDOVER

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
            self.trip_recorder.update_gps(
                lat=lat, lon=lon, altitude_m=altitude_m,
                heading_deg=gps_heading, gps_speed_kmh=gps_speed_kmh,
            )
            self._set_link_indicator(self.gps_indicator, self._gps_connected_with_holdover(gps_speed_kmh), False)
            self.acceleration_page.update_payload(payload, self._plain_number)
            self.cars_page.update_live(payload)
            if gps_heading is not None:
                self.dashboard_canvas.update_heading(gps_heading)
            self.dashboard_canvas.update_gps_speed(self._display_speed(gps_speed_kmh))
            self.dashboard_canvas.update_gps_pos(lat, lon, altitude_m)
            if not getattr(self, "_obd_active", False) and gps_speed_kmh is not None:
                display = self._display_speed(gps_speed_kmh)
                self.speed_gauge.set_value(display, f"{display:.0f}" if display is not None else None)
                self.dashboard_canvas.update_speed(display, f"{display:.0f}" if display is not None else None)
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

    def _handle_scan_identity(self, payload: dict[str, Any]) -> None:
        """Vom Scanner gemeldete Fahrzeug-Identität in die Trip-DB und in cars_page übernehmen."""
        from cars import _extract_inner_string, _wmi_to_brand

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


def _register_local_icon() -> None:
    """Add the local icon.png to the GTK icon theme when running from source."""
    local_icon = Path(__file__).parent / "icon.png"
    if not local_icon.exists():
        return
    try:
        import shutil
        cache_dir = Path(__file__).parent / ".icon-cache" / "hicolor" / "128x128" / "apps"
        cache_dir.mkdir(parents=True, exist_ok=True)
        dest = cache_dir / f"{APP_ID}.png"
        if not dest.exists() or dest.stat().st_mtime < local_icon.stat().st_mtime:
            shutil.copy2(local_icon, dest)
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        theme.add_search_path(str(cache_dir.parent.parent.parent))
    except Exception:
        pass


class ObdDashboardApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)
        self.window: DashboardWindow | None = None

    def do_activate(self) -> None:
        _register_local_icon()
        load_user_themes(THEMES_DIR)
        if self.window is None:
            self.window = DashboardWindow(self)
        self.window.present()


def main() -> int:
    _print_required_python_packages()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = ObdDashboardApp()
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
