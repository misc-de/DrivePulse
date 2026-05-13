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

import json
import math
import random
import signal
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
    LOG_DIR,
    LOG_FILE,
    OBD_BAUDRATE,
    OBD_FAST,
    OBD_PORT,
    OBD_TIMEOUT_SECONDS,
    POLL_INTERVAL_SECONDS,
    SETTINGS_FILE,
    SUPPORTED_LANGUAGES,
    _detect_language,
    _make_label_responsive,
    _normalize_language,
    _translate,
)
from gauge import Gauge
from acceleration import AccelerationPage

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


class ObdReader(GObject.Object):
    """Liest OBD-II-Werte in einem Hintergrund-Thread."""

    __gtype_name__ = "ObdReader"

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
        return candidates + [None]

    def _close_connection(self) -> None:
        try:
            if self.connection:
                self.connection.close()
        except Exception as exc:
            self._connection_log("connect_close_error", port=self.connected_port, error=str(exc))
        finally:
            self.connection = None
            self.connected_port = None

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

        self.mock = True
        self.mock_reason = "kein nutzbarer Dongle gefunden"
        self._connection_log("connect_failed", reason=self.mock_reason, fallback="mock")

    def _run(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._connect()

        while not self.stop_event.is_set():
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
        failed_read = disconnected or (command_count > 0 and read_error_count >= command_count)
        self.failed_read_count = self.failed_read_count + 1 if failed_read else 0
        if self.failed_read_count < 3:
            return

        self._connection_log("reconnect_begin", reason="wiederholte Lesefehler", failed_reads=self.failed_read_count)
        self.mock = False
        self.mock_reason = ""
        self._connect()

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

        return {
            "rpm": {"value": rpm, "unit": "rpm"},
            "speed": {"value": speed, "unit": "km/h"},
            "gps_speed": {"value": max(0, speed + random.uniform(-1.5, 1.5)), "unit": "km/h"},
            "acceleration_g": {"value": acceleration_g, "unit": "g"},
            "coolant_temp": {"value": temp, "unit": "degC"},
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
                self.on_update({
                    "source": "gps",
                    "gps_speed": {"value": speed * 3.6, "unit": "km/h"},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
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
        GLib.idle_add(self.on_update, {
            "source": "gps",
            "gps_speed": {"value": float(speed_ms) * 3.6, "unit": "km/h"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


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
    ) -> None:
        super().__init__()
        self.language = _normalize_language(current_language)
        self.on_units_changed = on_units_changed
        self.on_language_changed = on_language_changed
        self.on_mock_mode_changed = on_mock_mode_changed
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

        group.add(self.unit_row)
        group.add(self.language_row)
        group.add(self.mock_row)
        page.add(group)
        self.add(page)

    def _on_unit_selected(self, *_args: Any) -> None:
        self.on_units_changed("metric" if self.unit_row.get_selected() == 0 else "imperial")

    def _on_language_selected(self, *_args: Any) -> None:
        self.on_language_changed(SUPPORTED_LANGUAGES[self.language_row.get_selected()])

    def _on_mock_changed(self, *_args: Any) -> None:
        if self.on_mock_mode_changed is not None:
            self.on_mock_mode_changed(self.mock_switch.get_active())


class DashboardWindow(Adw.ApplicationWindow):
    __gtype_name__ = "DashboardWindow"

    PAGE_DASHBOARD = "dashboard"
    PAGE_ACCELERATION = "acceleration"

    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title=_translate(_detect_language(), "window.title"))
        self.set_default_size(980, 520)
        self.settings = self._load_settings()
        self.units = self.settings["units"]
        self.language = self.settings["language"]
        self.mock_mode = self.settings["mock_mode"]
        self.last_payload: dict[str, Any] | None = None

        self.rpm_gauge = Gauge(_translate(self.language, "gauge.rpm"), "rpm", 0, 7000, (0.34, 0.62, 0.86))
        speed_unit = "km/h" if self.units == "metric" else "mph"
        speed_max = 240 if self.units == "metric" else 150
        self.speed_gauge = Gauge(_translate(self.language, "gauge.speed"), speed_unit, 0, speed_max, (0.50, 0.72, 0.92))
        self.temp_gauge = Gauge(_translate(self.language, "gauge.coolant"), "°C", 40, 130, (0.72, 0.32, 0.48))

        self.status_label = _make_label_responsive(Gtk.Label(label=_translate(self.language, "status.connecting")), 36, 0.5)
        self.status_label.add_css_class("dim-label")
        self.log_label = _make_label_responsive(
            Gtk.Label(label=_translate(self.language, "status.log_paths", data_log=LOG_FILE, connection_log=CONNECTION_LOG_FILE)),
            42,
            0.5,
        )
        self.log_label.add_css_class("dim-label")

        self.gauge_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
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

        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        footer.set_halign(Gtk.Align.CENTER)
        footer.append(self.status_label)
        footer.append(self.log_label)

        self.footer = footer

        self.dashboard_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.dashboard_page.set_margin_top(12)
        self.dashboard_page.set_margin_bottom(12)
        self.dashboard_page.set_margin_start(12)
        self.dashboard_page.set_margin_end(12)
        self.dashboard_page.append(self.gauge_box)
        self.dashboard_page.append(footer)

        dashboard_scroller = Gtk.ScrolledWindow()
        dashboard_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        dashboard_scroller.set_propagate_natural_width(False)
        dashboard_scroller.set_propagate_natural_height(False)
        dashboard_scroller.set_child(self.dashboard_page)

        self.acceleration_page = AccelerationPage(self.language)
        acceleration_scroller = Gtk.ScrolledWindow()
        acceleration_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        acceleration_scroller.set_propagate_natural_width(False)
        acceleration_scroller.set_propagate_natural_height(False)
        acceleration_scroller.set_child(self.acceleration_page)

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
            "dashboard-symbolic",
        )
        self.acceleration_stack_page = self.view_stack.add_titled_with_icon(
            acceleration_scroller,
            self.PAGE_ACCELERATION,
            _translate(self.language, "nav.acceleration"),
            "view-statistics-symbolic",
        )

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

        toolbar_view.add_top_bar(header)
        toolbar_view.add_bottom_bar(switcher_bar)
        toolbar_view.set_content(self.view_stack)

        self.set_content(toolbar_view)
        self.connect("notify::default-width", self._on_size_changed)
        self.connect("notify::default-height", self._on_size_changed)
        self.add_tick_callback(self._layout_tick)
        GLib.idle_add(self._on_size_changed)

        self._obd_active = False
        self.reader = ObdReader(self._update_from_payload, force_mock=self.mock_mode)
        self.reader.start()
        self.gps_reader = GpsReader(self._update_from_payload)
        self.gps_reader.start()

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

    def close(self) -> bool:
        self.reader.stop()
        self.gps_reader.stop()
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
            }
        except Exception:
            return {"units": "metric", "language": _detect_language(), "mock_mode": False}

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
        dialog = SettingsDialog(self, self.units, self.language, self._set_units, self._set_language, self.mock_mode, self._set_mock_mode)
        dialog.present(self)

    def _set_units(self, units: str) -> None:
        if units == self.units:
            return
        self.units = units
        self._save_units()

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

    def _set_mock_mode(self, mock_mode: bool) -> None:
        if mock_mode == self.mock_mode:
            return
        self.mock_mode = mock_mode
        self._save_settings()
        self.reader.set_force_mock(mock_mode)

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
        self.log_label.set_text(_translate(self.language, "status.log_paths", data_log=LOG_FILE, connection_log=CONNECTION_LOG_FILE))
        self.acceleration_page.set_language(self.language)
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

        if width >= height:
            self._set_landscape_layout(width, height)
        else:
            self._set_portrait_layout(width, height)

        return False

    def _set_landscape_layout(self, width: int, height: int) -> None:
        self.gauge_box.set_orientation(Gtk.Orientation.HORIZONTAL)
        self.gauge_box.set_spacing(16)
        self.gauge_box.set_halign(Gtk.Align.FILL)
        self.gauge_box.set_valign(Gtk.Align.CENTER)

        footer_height = max(0, self.footer.get_height())
        available_width = max(1, width - 24)
        available_height = max(1, height - 24 - footer_height - 8)
        gauge_size = max(1, min(available_height, (available_width - 32) // 3))

        for gauge in (self.rpm_gauge, self.speed_gauge, self.temp_gauge):
            gauge.set_hexpand(True)
            gauge.set_vexpand(True)
            gauge.set_halign(Gtk.Align.CENTER)
            gauge.set_valign(Gtk.Align.CENTER)
            gauge.set_size_request(gauge_size, gauge_size)
            gauge.set_content_width(gauge_size)
            gauge.set_content_height(gauge_size)

    def _set_portrait_layout(self, width: int, height: int) -> None:
        self.gauge_box.set_orientation(Gtk.Orientation.VERTICAL)
        self.gauge_box.set_spacing(8)
        self.gauge_box.set_halign(Gtk.Align.CENTER)
        self.gauge_box.set_valign(Gtk.Align.CENTER)

        footer_height = max(0, self.footer.get_height())
        available_width = max(1, width - 24)
        available_height = max(1, height - 24 - footer_height - 8)
        gauge_size = max(1, min(available_width, (available_height - 16) // 3))

        for gauge in (self.rpm_gauge, self.speed_gauge, self.temp_gauge):
            gauge.set_hexpand(False)
            gauge.set_vexpand(False)
            gauge.set_halign(Gtk.Align.CENTER)
            gauge.set_valign(Gtk.Align.CENTER)
            gauge.set_size_request(gauge_size, gauge_size)
            gauge.set_content_width(gauge_size)
            gauge.set_content_height(gauge_size)

    def _on_swipe(self, _gesture: Gtk.GestureSwipe, velocity_x: float, velocity_y: float) -> None:
        if abs(velocity_x) < 220 or abs(velocity_x) <= abs(velocity_y):
            return

        current = self.view_stack.get_visible_child_name()
        pages = [self.PAGE_DASHBOARD, self.PAGE_ACCELERATION]
        try:
            index = pages.index(current)
        except ValueError:
            index = 0

        if velocity_x < 0 and index < len(pages) - 1:
            self.view_stack.set_visible_child_name(pages[index + 1])
        elif velocity_x > 0 and index > 0:
            self.view_stack.set_visible_child_name(pages[index - 1])

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

    def _update_from_payload(self, payload: dict[str, Any]) -> bool:
        source = payload.get("source", "")

        if source == "gps":
            gps_speed_kmh = self._plain_number(payload, "gps_speed")
            self._set_link_indicator(self.gps_indicator, gps_speed_kmh is not None, False)
            self.acceleration_page.update_payload(payload, self._plain_number)
            if not getattr(self, "_obd_active", False) and gps_speed_kmh is not None:
                display = self._display_speed(gps_speed_kmh)
                self.speed_gauge.set_value(display, f"{display:.0f}" if display is not None else None)
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
        gps_connected = gps_speed_kmh is not None and active

        self._set_link_indicator(self.obd_indicator, obd_connected, obd_connecting)
        self._set_link_indicator(self.gps_indicator, gps_connected, False)

        self.rpm_gauge.set_value(rpm, None if rpm is None else f"{rpm:.0f}")
        self.speed_gauge.set_value(speed, None if speed is None else f"{speed:.0f}")
        self.temp_gauge.set_value(temp, None if temp is None else f"{temp:.0f}")
        self.acceleration_page.update_payload(payload, self._plain_number)

        status = payload.get("connection_status") or source or "?"
        language = _normalize_language(getattr(self, "language", _detect_language()))
        self.status_label.set_text(_translate(language, "status.updated", status=status, time=datetime.now().strftime("%H:%M:%S")))
        return False


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
