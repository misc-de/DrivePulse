"""Rotation state provider for DrivePulse.

Two independent sources are tracked:

- sensor: physical accelerometer orientation (set by OrientationReader)
- system: compositor output transform (read live from Mutter DisplayConfig)

Consumers bind to one of two modes:

- "follow_sensor": widget rotation = (sensor - system) % 360
  Keeps the widget upright relative to the world. Compensates for the
  compositor transform so the widget never double-rotates.

- "follow_system": widget rotation = 0
  The compositor already applied its transform to the framebuffer, so the
  widget just renders normally and follows along.
"""
from __future__ import annotations

from typing import Any, Callable, Literal

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .diagnostics import get_logger


log = get_logger(__name__)

Source = Literal["follow_sensor", "follow_system"]

# Mutter DisplayConfig encodes transform as 0..7 (4..7 = flipped variants).
# We only care about the rotation portion.
_TRANSFORM_TO_ANGLE: dict[int, int] = {
    0: 0, 1: 90, 2: 180, 3: 270,
    4: 0, 5: 90, 6: 180, 7: 270,
}


class RotationProvider:
    """Holds sensor + system rotation and notifies subscribers per source."""

    def __init__(self) -> None:
        self._sensor: int = 0
        self._system: int = 0
        self._subs: dict[Source, list[Callable[[int], None]]] = {
            "follow_sensor": [],
            "follow_system": [],
        }
        self._display_proxy: Any = None
        GLib.idle_add(self._start_display_watch)

    # ── public API ────────────────────────────────────────────────────

    def get(self, source: Source) -> int:
        """Return the effective rotation angle for the given mode."""
        if source == "follow_sensor":
            return (self._sensor - self._system) % 360
        return 0

    def bind(self, source: Source, cb: Callable[[int], None]) -> None:
        """Subscribe to changes. Fires immediately with the current value."""
        self._subs[source].append(cb)
        cb(self.get(source))

    def set_sensor(self, angle: int) -> None:
        angle %= 360
        if angle == self._sensor:
            return
        self._sensor = angle
        self._notify("follow_sensor")

    def set_system(self, angle: int) -> None:
        angle %= 360
        if angle == self._system:
            return
        self._system = angle
        # follow_sensor depends on system, so it changes too.
        self._notify("follow_system")
        self._notify("follow_sensor")

    # ── Mutter DisplayConfig watcher ──────────────────────────────────

    def _start_display_watch(self) -> bool:
        try:
            proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None,
                "org.gnome.Mutter.DisplayConfig",
                "/org/gnome/Mutter/DisplayConfig",
                "org.gnome.Mutter.DisplayConfig", None,
            )
            proxy.connect("g-signal", self._on_display_signal)
            self._display_proxy = proxy
            self._refresh_system()
        except Exception:
            log.info("Mutter DisplayConfig unavailable; system rotation stays at 0", exc_info=True)
        return False

    def _on_display_signal(self, _proxy: Any, _sender: str, signal: str, _params: Any) -> None:
        if signal == "MonitorsChanged":
            self._refresh_system()

    def _refresh_system(self) -> None:
        if self._display_proxy is None:
            return
        try:
            state = self._display_proxy.call_sync(
                "GetCurrentState", None, Gio.DBusCallFlags.NONE, 2000, None,
            )
        except Exception:
            log.exception("Failed to query Mutter DisplayConfig state")
            return
        # GetCurrentState returns (u, a(...), a(iiduba(ssss)a{sv}), a{sv})
        # Logical monitors are the 3rd member. Each entry: (x, y, scale, transform, primary, monitors, props)
        logical_monitors = state.get_child_value(2)
        if logical_monitors.n_children() == 0:
            return
        primary = logical_monitors.get_child_value(0)
        transform = primary.get_child_value(3).get_uint32()
        self.set_system(_TRANSFORM_TO_ANGLE.get(transform, 0))

    # ── notification ──────────────────────────────────────────────────

    def _notify(self, source: Source) -> None:
        angle = self.get(source)
        for cb in list(self._subs[source]):
            try:
                cb(angle)
            except Exception:
                log.exception("Rotation subscriber raised")
