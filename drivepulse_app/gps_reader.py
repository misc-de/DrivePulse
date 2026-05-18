"""GPS readers for DrivePulse."""
from __future__ import annotations

import json
import math
import socket
import threading
from datetime import datetime, timezone
from typing import Any, Callable

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .common import APP_ID
from .diagnostics import get_logger


log = get_logger(__name__)


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
                log.exception("Could not stop GeoClue client")

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
                    log.exception("Could not set GeoClue property %s", name)
            client.connect("g-signal", self._on_geoclue_signal)
            client.call_sync("Start", None, Gio.DBusCallFlags.NONE, 3000, None)
        except Exception:
            log.info("GeoClue startup failed; GPSD fallback remains active", exc_info=True)
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
            lat = self._geoclue_double(location, "Latitude")
            lon = self._geoclue_double(location, "Longitude")
            # Require a valid position fix — speed alone is not sufficient.
            if lat is None or lon is None:
                return
            gps_payload: dict[str, Any] = {
                "source": "gps",
                "gps_lat": {"value": lat, "unit": "degree"},
                "gps_lon": {"value": lon, "unit": "degree"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            # Speed: GeoClue reports -1 when unavailable; only include valid values.
            speed = self._geoclue_double(location, "Speed")
            if speed is not None and speed >= 0:
                gps_payload["gps_speed"] = {"value": speed * 3.6, "unit": "km/h"}
            heading = self._geoclue_double(location, "Heading")
            if heading is not None and 0 <= heading < 360:
                gps_payload["gps_heading"] = {"value": heading, "unit": "deg"}
            altitude = self._geoclue_double(location, "Altitude")
            if altitude is not None:
                gps_payload["gps_altitude"] = {"value": altitude, "unit": "meter"}
            self.on_update(gps_payload)
        except Exception:
            log.exception("Could not process GeoClue location update")

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
                log.info("GPSD read failed; retrying in %.1fs", self.GPSD_RETRY_INTERVAL, exc_info=True)
            self.stop_event.wait(self.GPSD_RETRY_INTERVAL)

    def _connect_and_read_gpsd(self) -> None:
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
