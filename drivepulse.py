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
import os
import random
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata, util
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

try:
    import obd  # type: ignore
except Exception:
    obd = None


APP_ID = "de.cais.DrivePulse"
LOG_DIR = Path(os.environ.get("OBD_LOG_DIR", Path.home() / ".local" / "state" / "drivepulse"))
LOG_FILE = LOG_DIR / "obd-log.jsonl"
CONNECTION_LOG_FILE = LOG_DIR / "connection-log.jsonl"
POLL_INTERVAL_SECONDS = float(os.environ.get("OBD_POLL_INTERVAL", "0.5"))
OBD_PORT = os.environ.get("OBD_PORT")
OBD_BAUDRATE = int(os.environ["OBD_BAUDRATE"]) if os.environ.get("OBD_BAUDRATE") else None
OBD_TIMEOUT_SECONDS = float(os.environ.get("OBD_TIMEOUT", "2.0"))
OBD_FAST = os.environ.get("OBD_FAST", "0").lower() in {"1", "true", "yes", "on"}
SETTINGS_FILE = LOG_DIR / "settings.json"
REQUIRED_PYTHON_PACKAGES = (
    ("PyGObject", "gi", "GTK/libadwaita Python-Bindings"),
    ("pyserial", "serial", "serielle Bluetooth/USB-Port-Anbindung"),
    ("obd", "obd", "OBD-II Dongle-Anbindung"),
)
SOURCE_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ("en", "de")
TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "acceleration.title": "Acceleration",
        "acceleration.subtitle": "Measure 0-30, 0-50, 0-70, 0-100, 0-150 and 0-200 km/h runs from OBD and GPS data.",
        "acceleration.ready": "Ready. Press Start and accelerate.",
        "acceleration.armed": "Armed. Timing starts when acceleration is detected.",
        "acceleration.running": "Measurement running...",
        "acceleration.done": "Measurement complete.",
        "acceleration.g": "G: {value}",
        "acceleration.g.empty": "G: --",
        "acceleration.start": "Start",
        "acceleration.reset": "Reset",
        "acceleration.best": "Best",
        "acceleration.obd": "OBD",
        "acceleration.gps": "GPS",
        "acceleration.note": "Timing starts when g-force or a rising speed signal is detected. GPS times appear when gps_speed is available.",
        "settings.title": "Settings",
        "settings.display": "Display",
        "settings.units": "Units",
        "settings.speed": "Speed",
        "settings.metric": "Metric (km/h)",
        "settings.imperial": "Imperial (mph)",
        "settings.language": "Language",
        "settings.language.en": "English",
        "settings.language.de": "Deutsch",
        "settings.mock_mode": "Mock Mode",
        "settings.mock_mode.subtitle": "Simulate OBD and GPS data without hardware",
        "gauge.rpm": "RPM",
        "gauge.speed": "Speed",
        "gauge.coolant": "Coolant",
        "status.connecting": "Connecting...",
        "status.obd": "OBD",
        "status.gps": "GPS",
        "status.log_paths": "Data log: {data_log} | Connection log: {connection_log}",
        "status.updated": "{status} | last update: {time}",
        "nav.gauges": "Gauges",
        "nav.acceleration": "Acceleration",
        "window.title": "DrivePulse",
        "settings.tooltip": "Settings",
    },
    "de": {
        "acceleration.title": "Beschleunigung",
        "acceleration.subtitle": "Misst 0-30, 0-50, 0-70, 0-100, 0-150 und 0-200 km/h mit OBD- und GPS-Daten.",
        "acceleration.ready": "Bereit. Start drücken und losfahren.",
        "acceleration.armed": "Scharf. Zeit startet bei erkannter Beschleunigung.",
        "acceleration.running": "Messung läuft...",
        "acceleration.done": "Messung abgeschlossen.",
        "acceleration.g": "G: {value}",
        "acceleration.g.empty": "G: --",
        "acceleration.start": "Start",
        "acceleration.reset": "Reset",
        "acceleration.best": "Bestzeit",
        "acceleration.obd": "OBD",
        "acceleration.gps": "GPS",
        "acceleration.note": "Startzeit wird erst gesetzt, wenn G-Kraft oder Geschwindigkeitsanstieg erkannt wird. GPS-Zeiten erscheinen nur, wenn gps_speed im Payload vorhanden ist.",
        "settings.title": "Einstellungen",
        "settings.display": "Anzeige",
        "settings.units": "Einheiten",
        "settings.speed": "Geschwindigkeit",
        "settings.metric": "Metrisch (km/h)",
        "settings.imperial": "Imperial (mph)",
        "settings.language": "Sprache",
        "settings.language.en": "English",
        "settings.language.de": "Deutsch",
        "settings.mock_mode": "Mock-Modus",
        "settings.mock_mode.subtitle": "OBD- und GPS-Daten ohne Hardware simulieren",
        "gauge.rpm": "Drehzahl",
        "gauge.speed": "Geschwindigkeit",
        "gauge.coolant": "Kühlmittel",
        "status.connecting": "Verbinde...",
        "status.obd": "OBD",
        "status.gps": "GPS",
        "status.log_paths": "Datenlog: {data_log} | Verbindungslog: {connection_log}",
        "status.updated": "{status} | letzte Aktualisierung: {time}",
        "nav.gauges": "Tachos",
        "nav.acceleration": "Beschleunigung",
        "window.title": "DrivePulse",
        "settings.tooltip": "Einstellungen",
    },
}


@dataclass
class GaugeState:
    value: float = 0.0
    label: str = "--"
    unit: str = ""
    min_value: float = 0.0
    max_value: float = 100.0


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


def _normalize_language(language: str | None) -> str:
    if not language:
        return SOURCE_LANGUAGE
    normalized = language.split(".", 1)[0].split("_", 1)[0].split("-", 1)[0].lower()
    return normalized if normalized in SUPPORTED_LANGUAGES else SOURCE_LANGUAGE


def _detect_language() -> str:
    return _normalize_language(os.environ.get("DRIVEPULSE_LANG") or os.environ.get("LANG"))


def _translate(language: str, key: str, **values: object) -> str:
    text = TRANSLATIONS.get(_normalize_language(language), {}).get(key)
    if text is None:
        text = TRANSLATIONS[SOURCE_LANGUAGE].get(key, key)
    return text.format(**values) if values else text


def _make_label_responsive(label: Gtk.Label, max_width_chars: int = 34, xalign: float = 0.0) -> Gtk.Label:
    label.set_wrap(True)
    label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    label.set_max_width_chars(max_width_chars)
    label.set_xalign(xalign)
    label.set_hexpand(True)
    return label


class Gauge(Gtk.DrawingArea):
    """Ein einfacher runder Tacho im Stil eines digitalen Cockpits."""

    __gtype_name__ = "Gauge"

    def __init__(
        self,
        title: str,
        unit: str,
        min_value: float,
        max_value: float,
        accent_rgb: tuple[float, float, float],
    ) -> None:
        super().__init__()
        self.title = title
        self.accent_rgb = accent_rgb
        self.state = GaugeState(
            value=0,
            label="--",
            unit=unit,
            min_value=min_value,
            max_value=max_value,
        )
        self.active = False
        self.set_content_width(1)
        self.set_content_height(1)
        self.set_size_request(1, 1)
        self.set_draw_func(self._draw)

    def set_value(self, value: float | None, label: str | None = None) -> None:
        if value is None or math.isnan(value):
            self.state.label = "--"
            self.state.value = self.state.min_value
            self.active = False
        else:
            self.state.value = max(self.state.min_value, min(self.state.max_value, value))
            self.state.label = label if label is not None else f"{value:.0f}"
            self.active = True
        self.queue_draw()

    def _draw(self, area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        size = min(width, height)
        cx = width / 2
        cy = height / 2
        radius = size * 0.39
        line_width = max(7, size * 0.035)

        start_angle = math.radians(135)
        end_angle = math.radians(405)
        span = end_angle - start_angle
        normalized = (self.state.value - self.state.min_value) / (self.state.max_value - self.state.min_value)
        normalized = max(0.0, min(1.0, normalized))
        value_angle = start_angle + span * normalized
        active_alpha = 1.0 if self.active else 0.34
        accent = self.accent_rgb if self.active else (0.45, 0.48, 0.50)

        # Hintergrund
        cr.set_source_rgb(0.02, 0.025, 0.03)
        cr.arc(cx, cy, radius + line_width * 1.15, 0, math.tau)
        cr.fill()

        # Äußerer Ring
        cr.set_line_width(2.0)
        cr.set_source_rgba(0.86, 0.91, 0.96, 0.85 * active_alpha)
        cr.arc(cx, cy, radius + line_width * 1.4, start_angle, end_angle)
        cr.stroke()

        # Skala dunkel
        cr.set_line_width(line_width)
        cr.set_line_cap(1)
        cr.set_source_rgba(0.35, 0.42, 0.48, 0.28 if self.active else 0.16)
        cr.arc(cx, cy, radius, start_angle, end_angle)
        cr.stroke()

        # Wertbogen
        cr.set_source_rgba(accent[0], accent[1], accent[2], 0.92 * active_alpha)
        cr.arc(cx, cy, radius, start_angle, value_angle)
        cr.stroke()

        # Marker/Ticks
        cr.set_line_width(2.0)
        for index in range(0, 11):
            angle = start_angle + span * (index / 10)
            outer = radius + line_width * 0.8
            inner = radius + line_width * (0.18 if index % 5 else -0.4)
            cr.set_source_rgba(0.95, 0.97, 1.0, (0.75 if index % 5 else 0.95) * active_alpha)
            cr.move_to(cx + math.cos(angle) * inner, cy + math.sin(angle) * inner)
            cr.line_to(cx + math.cos(angle) * outer, cy + math.sin(angle) * outer)
            cr.stroke()

        # Nadelspitze oben als optischer Bezugspunkt
        cr.set_source_rgba(1, 1, 1, 0.95 * active_alpha)
        top = -math.pi / 2
        cr.move_to(cx + math.cos(top) * (radius + line_width * 1.5), cy + math.sin(top) * (radius + line_width * 1.5))
        cr.line_to(cx + math.cos(top - 0.06) * (radius + line_width * 0.25), cy + math.sin(top - 0.06) * (radius + line_width * 0.25))
        cr.line_to(cx + math.cos(top + 0.06) * (radius + line_width * 0.25), cy + math.sin(top + 0.06) * (radius + line_width * 0.25))
        cr.close_path()
        cr.fill()

        # Text
        self._draw_center_text(cr, cx, cy, size, active_alpha)

    def _draw_text_centered(
        self,
        cr: Any,
        text: str,
        x: float,
        y: float,
        size: float,
        alpha: float = 1.0,
        bold: bool = False,
        max_width: float | None = None,
    ) -> None:
        cr.select_font_face("Cantarell", 0, 1 if bold else 0)
        cr.set_font_size(size)
        ext = cr.text_extents(text)
        if max_width is not None and ext.width > max_width:
            size = max(9, size * (max_width / max(1, ext.width)))
            cr.set_font_size(size)
            ext = cr.text_extents(text)
        cr.set_source_rgba(0.94, 0.96, 1.0, alpha)
        cr.move_to(x - ext.width / 2 - ext.x_bearing, y - ext.height / 2 - ext.y_bearing)
        cr.show_text(text)

    def _draw_center_text(self, cr: Any, cx: float, cy: float, size: int, active_alpha: float) -> None:
        value_size = max(28, size * 0.19)
        unit_size = max(14, size * 0.075)
        title_size = max(13, size * 0.062)

        text_width = size * 0.72
        self._draw_text_centered(cr, self.state.label, cx, cy - size * 0.06, value_size, active_alpha, True, text_width)
        self._draw_text_centered(cr, self.state.unit, cx, cy + size * 0.09, unit_size, 0.78 * active_alpha, True, text_width)
        self._draw_text_centered(cr, self.title, cx, cy + size * 0.26, title_size, 0.62 * active_alpha, False, text_width)


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
                # fast=False ist oft stabiler bei günstigen ELM327-Adaptern.
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
                    self._connection_log("connect_success", port=port)
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
            # Pint-Quantity aus python-OBD.
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
            # Dashboard soll auch bei Logging-Problemen weiterlaufen.
            pass


class SettingsDialog(Adw.PreferencesDialog):
    __gtype_name__ = "SettingsDialog"

    def __init__(
        self,
        parent: Gtk.Window,
        current_units: str,
        current_language: str,
        current_mock_mode: bool,
        on_units_changed: Callable[[str], None],
        on_language_changed: Callable[[str], None],
        on_mock_mode_changed: Callable[[bool], None],
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
        self.on_mock_mode_changed(self.mock_switch.get_active())


class AccelerationPage(Gtk.Box):
    __gtype_name__ = "AccelerationPage"

    SPEED_TARGETS_KMH = (30, 50, 70, 100, 150, 200)
    G_FORCE_START_THRESHOLD = 0.02

    def __init__(self, language: str = SOURCE_LANGUAGE) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.language = _normalize_language(language)
        self.set_margin_top(20)
        self.set_margin_bottom(20)
        self.set_margin_start(20)
        self.set_margin_end(20)

        self.armed = False
        self.running = False
        self.start_monotonic: float | None = None
        self.last_obd_speed: float | None = None
        self.last_speed_time: float | None = None
        self.computed_acceleration_g: float | None = None
        self.results: dict[int, dict[str, float | None]] = {
            target: {"obd": None, "gps": None} for target in self.SPEED_TARGETS_KMH
        }

        self.title_label = _make_label_responsive(Gtk.Label(label=""), 28)
        self.title_label.add_css_class("title-1")
        self.title_label.set_halign(Gtk.Align.START)

        self.subtitle_label = _make_label_responsive(Gtk.Label(label=""), 54)
        self.subtitle_label.add_css_class("dim-label")
        self.subtitle_label.set_halign(Gtk.Align.START)

        self.status_label = _make_label_responsive(Gtk.Label(label=""), 42)
        self.status_label.add_css_class("dim-label")
        self.status_label.set_halign(Gtk.Align.START)

        self.g_label = _make_label_responsive(Gtk.Label(label=""), 18, 1.0)
        self.g_label.add_css_class("title-2")
        self.g_label.set_halign(Gtk.Align.END)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.set_hexpand(True)
        header.append(self.title_label)
        header.append(self.g_label)

        intro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        intro.append(header)
        intro.append(self.subtitle_label)
        intro.append(self.status_label)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.set_halign(Gtk.Align.START)
        self.start_button = Gtk.Button(label="")
        self.start_button.add_css_class("suggested-action")
        self.start_button.connect("clicked", self.start_measurement)
        self.reset_button = Gtk.Button(label="")
        self.reset_button.connect("clicked", self.reset_measurement)
        controls.append(self.start_button)
        controls.append(self.reset_button)

        self.results_flow = Gtk.FlowBox()
        self.results_flow.set_halign(Gtk.Align.FILL)
        self.results_flow.set_hexpand(True)
        self.results_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.results_flow.set_max_children_per_line(3)
        self.results_flow.set_min_children_per_line(1)
        self.results_flow.set_column_spacing(12)
        self.results_flow.set_row_spacing(12)
        self._build_result_tiles()

        self.note_label = _make_label_responsive(Gtk.Label(label=""), 54)
        self.note_label.add_css_class("dim-label")
        self.note_label.set_halign(Gtk.Align.START)

        self.append(intro)
        self.append(controls)
        self.append(self.results_flow)
        self.append(self.note_label)

        self._refresh_texts()

    def _build_result_tiles(self) -> None:
        self.result_labels: dict[tuple[int, str], Gtk.Label] = {}
        self.best_labels: dict[int, Gtk.Label] = {}
        self.source_labels: dict[tuple[int, str], Gtk.Label] = {}
        self.source_rows: dict[tuple[int, str], Gtk.Box] = {}
        for target in self.SPEED_TARGETS_KMH:
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            tile.add_css_class("card")
            tile.set_margin_top(2)
            tile.set_margin_bottom(2)
            tile.set_margin_start(2)
            tile.set_margin_end(2)

            target_label = _make_label_responsive(Gtk.Label(label=f"0-{target} km/h"), 16)
            target_label.add_css_class("heading")
            target_label.set_halign(Gtk.Align.START)

            best_caption = _make_label_responsive(Gtk.Label(label=""), 12)
            best_caption.add_css_class("dim-label")
            best_caption.set_halign(Gtk.Align.START)
            self.best_labels[target] = best_caption

            best_label = _make_label_responsive(Gtk.Label(label="--"), 12)
            best_label.add_css_class("title-2")
            best_label.set_halign(Gtk.Align.START)
            self.result_labels[(target, "best")] = best_label

            tile.append(target_label)
            tile.append(best_caption)
            tile.append(best_label)

            for source in ("obd", "gps"):
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                source_label = _make_label_responsive(Gtk.Label(label=""), 8)
                source_label.add_css_class("dim-label")
                source_label.set_halign(Gtk.Align.START)
                value_label = _make_label_responsive(Gtk.Label(label="--"), 10, 1.0)
                value_label.set_halign(Gtk.Align.END)
                row.append(source_label)
                row.append(value_label)
                tile.append(row)
                row.set_visible(False)
                self.source_labels[(target, source)] = source_label
                self.source_rows[(target, source)] = row
                self.result_labels[(target, source)] = value_label

            self.results_flow.insert(tile, -1)

    def _refresh_texts(self) -> None:
        self.title_label.set_text(_translate(self.language, "acceleration.title"))
        self.subtitle_label.set_text(_translate(self.language, "acceleration.subtitle"))
        self.start_button.set_label(_translate(self.language, "acceleration.start"))
        self.reset_button.set_label(_translate(self.language, "acceleration.reset"))
        self.note_label.set_text(_translate(self.language, "acceleration.note"))
        if not self.armed and not self.running:
            self.status_label.set_text(_translate(self.language, "acceleration.ready"))
        for target in self.SPEED_TARGETS_KMH:
            self.best_labels[target].set_text(_translate(self.language, "acceleration.best"))
            for source in ("obd", "gps"):
                key = "acceleration.obd" if source == "obd" else "acceleration.gps"
                self.source_labels[(target, source)].set_text(_translate(self.language, key))
        self._update_best_labels()

    def set_language(self, language: str) -> None:
        self.language = _normalize_language(language)
        self._refresh_texts()

    def _set_g_text(self, active_g: float | None) -> None:
        if active_g is None:
            self.g_label.set_text(_translate(self.language, "acceleration.g.empty"))
        else:
            self.g_label.set_text(_translate(self.language, "acceleration.g", value=f"{active_g:.3f}"))

    def _update_best_labels(self) -> None:
        for target, values in self.results.items():
            measured = [value for value in values.values() if value is not None]
            best = min(measured) if measured else None
            self.result_labels[(target, "best")].set_text("--" if best is None else f"{best:.2f} s")

    def _set_source_visibility(self, obd_available: bool, gps_available: bool) -> None:
        for target in self.SPEED_TARGETS_KMH:
            for source, available in (("obd", obd_available), ("gps", gps_available)):
                has_result = self.results[target][source] is not None
                self.source_rows[(target, source)].set_visible(available or has_result)

    def start_measurement(self, *_args: Any) -> None:
        self.armed = True
        self.running = False
        self.start_monotonic = None
        self.results = {target: {"obd": None, "gps": None} for target in self.SPEED_TARGETS_KMH}
        for label in self.result_labels.values():
            label.set_text("--")
        self.status_label.set_text(_translate(self.language, "acceleration.armed"))

    def reset_measurement(self, *_args: Any) -> None:
        self.armed = False
        self.running = False
        self.start_monotonic = None
        self.last_obd_speed = None
        self.last_speed_time = None
        self.computed_acceleration_g = None
        self.results = {target: {"obd": None, "gps": None} for target in self.SPEED_TARGETS_KMH}
        for label in self.result_labels.values():
            label.set_text("--")
        self._set_g_text(None)
        self.status_label.set_text(_translate(self.language, "acceleration.ready"))

    def update_payload(self, payload: dict[str, Any], read_number: Callable[[dict[str, Any], str], float | None]) -> None:
        now = time.monotonic()
        obd_speed = read_number(payload, "speed")
        gps_speed = read_number(payload, "gps_speed")
        measured_g = read_number(payload, "acceleration_g")
        self._set_source_visibility(obd_speed is not None, gps_speed is not None)

        if obd_speed is not None and self.last_obd_speed is not None and self.last_speed_time is not None:
            dt = max(0.001, now - self.last_speed_time)
            acceleration_ms2 = ((obd_speed - self.last_obd_speed) / 3.6) / dt
            self.computed_acceleration_g = acceleration_ms2 / 9.80665

        if obd_speed is not None:
            self.last_obd_speed = obd_speed
            self.last_speed_time = now

        active_g = measured_g if measured_g is not None else self.computed_acceleration_g
        self._set_g_text(active_g)

        if self.armed and not self.running:
            speed_rising = self.computed_acceleration_g is not None and self.computed_acceleration_g > self.G_FORCE_START_THRESHOLD
            g_rising = active_g is not None and active_g > self.G_FORCE_START_THRESHOLD
            if speed_rising or g_rising:
                self.running = True
                self.start_monotonic = now
                self.status_label.set_text(_translate(self.language, "acceleration.running"))

        if not self.running or self.start_monotonic is None:
            return

        elapsed = now - self.start_monotonic
        for target in self.SPEED_TARGETS_KMH:
            row = self.results[target]
            if row["obd"] is None and obd_speed is not None and obd_speed >= target:
                row["obd"] = elapsed
                self.result_labels[(target, "obd")].set_text(f"{elapsed:.2f} s")
            if row["gps"] is None and gps_speed is not None and gps_speed >= target:
                row["gps"] = elapsed
                self.result_labels[(target, "gps")].set_text(f"{elapsed:.2f} s")
        self._update_best_labels()

        all_done = all(values["obd"] is not None or values["gps"] is not None for values in self.results.values())
        if all_done:
            self.running = False
            self.armed = False
            self.status_label.set_text(_translate(self.language, "acceleration.done"))


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

        self.reader = ObdReader(self._update_from_payload, force_mock=self.mock_mode)
        self.reader.start()

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
        dialog = SettingsDialog(self, self.units, self.language, self.mock_mode, self._set_units, self._set_language, self._set_mock_mode)
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
        # Querformat: alle Tachos nebeneinander.
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
        # Hochformat: alle Tachos untereinander.
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
        self.last_payload = payload
        source = payload.get("source", "")
        active = source in ("obd", "mock")
        rpm = self._plain_number(payload, "rpm") if active else None
        obd_speed_kmh = self._plain_number(payload, "speed") if active else None
        gps_speed_kmh = self._plain_number(payload, "gps_speed") if active else None
        speed_source_kmh = obd_speed_kmh if obd_speed_kmh is not None else gps_speed_kmh
        speed = self._display_speed(speed_source_kmh)
        temp = self._plain_number(payload, "coolant_temp") if active else None
        obd_connected = active and self._has_obd_data(payload)
        obd_connecting = bool(payload.get("obd_connecting"))
        gps_connected = gps_speed_kmh is not None and active

        self._set_link_indicator(self.obd_indicator, obd_connected, obd_connecting)
        self._set_link_indicator(self.gps_indicator, gps_connected, False)

        self.rpm_gauge.set_value(rpm, None if rpm is None else f"{rpm:.0f}")
        self.speed_gauge.set_value(speed, None if speed is None else f"{speed:.0f}")
        self.temp_gauge.set_value(temp, None if temp is None else f"{temp:.0f}")
        self.acceleration_page.update_payload(payload, self._plain_number)

        status = payload.get("connection_status") or payload.get("source", "?")
        language = _normalize_language(getattr(self, "language", _detect_language()))
        self.status_label.set_text(_translate(language, "status.updated", status=status, time=datetime.now().strftime("%H:%M:%S")))
        return False


class ObdDashboardApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)
        self.window: DashboardWindow | None = None

    def do_activate(self) -> None:
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
