"""Device orientation reader for DrivePulse."""
from __future__ import annotations

import os
import socket
import struct
from typing import Any, Callable

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .diagnostics import get_logger


log = get_logger(__name__)


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
        self.on_gforce: Callable[[float, float, float], None] | None = None
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
            log.info("sensorfwd orientation startup failed", exc_info=True)
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
            log.info("iio-sensor-proxy orientation startup failed", exc_info=True)

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
                    if self.on_gforce is not None:
                        x, y, z = last_xyz
                        GLib.idle_add(self.on_gforce, x / 1000.0, y / 1000.0, z / 1000.0)
        except BlockingIOError:
            pass
        except Exception:
            log.exception("Orientation sensor socket failed")
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
                log.exception("Could not close orientation sensor socket")
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
                log.exception("Could not stop sensorfwd accelerometer session")
            self._bus = None
            self._session_id = -1
        if self._iio_proxy is not None:
            try:
                self._iio_proxy.call_sync(
                    "ReleaseAccelerometer", None, Gio.DBusCallFlags.NONE, 1000, None,
                )
            except Exception:
                log.exception("Could not release iio accelerometer")
            self._iio_proxy = None
