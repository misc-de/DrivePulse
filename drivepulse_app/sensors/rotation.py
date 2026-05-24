"""Rotation state provider for DrivePulse.

Two independent sources are tracked:

- sensor: physical accelerometer orientation (set by OrientationReader)
- system: compositor output transform (read live from Mutter DisplayConfig)

Consumers either lock to a specific source or follow the *active mode*:

- "follow_sensor": effective = (sensor - system) % 360
  Compensates for the compositor transform so the widget never double-
  rotates. Stays upright relative to the world.

- "follow_system": effective = 0
  Compositor already rendered the transform; widget renders normally.

`bind(cb)` (no source) follows the active mode and re-fires when the user
switches it via `set_mode(...)`. `bind(cb, source=...)` locks to that mode.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)

Source = Literal["follow_sensor", "follow_system"]
VALID_MODES: tuple[Source, ...] = ("follow_sensor", "follow_system")

# Mutter DisplayConfig encodes transform as 0..7 (4..7 = flipped variants).
_TRANSFORM_TO_ANGLE: dict[int, int] = {
    0: 0, 1: 270, 2: 180, 3: 90,
    4: 0, 5: 270, 6: 180, 7: 90,
}


class RotationProvider:
    def __init__(self, mode: Source = "follow_sensor") -> None:
        self._mode: Source = mode if mode in VALID_MODES else "follow_sensor"
        self._sensor: int = 0
        self._system: int = 0
        self._subs: list[tuple[Callable[[int], None], Source | None]] = []
        self._display_proxy: Any = None
        GLib.idle_add(self._start_display_watch)

    # ── public API ────────────────────────────────────────────────────

    @property
    def mode(self) -> Source:
        return self._mode

    def set_mode(self, mode: Source) -> None:
        if mode not in VALID_MODES or mode == self._mode:
            return
        self._mode = mode
        self._notify()

    def get(self, source: Source | None = None) -> int:
        return self._effective(source)

    def bind(self, cb: Callable[[int], None], source: Source | None = None) -> None:
        """Subscribe. If `source` is None, follows the active mode. Fires once now."""
        self._subs.append((cb, source))
        self._safe_call(cb, self._effective(source))

    def set_sensor(self, angle: int) -> None:
        angle %= 360
        if angle == self._sensor:
            return
        self._sensor = angle
        self._notify()

    def set_system(self, angle: int) -> None:
        angle %= 360
        if angle == self._system:
            return
        self._system = angle
        self._notify()

    # ── internals ─────────────────────────────────────────────────────

    def _effective(self, source: Source | None) -> int:
        eff = source if source is not None else self._mode
        if eff == "follow_sensor":
            return (self._sensor - self._system) % 360
        return 0

    def _notify(self) -> None:
        for cb, source in list(self._subs):
            self._safe_call(cb, self._effective(source))

    @staticmethod
    def _safe_call(cb: Callable[[int], None], angle: int) -> None:
        try:
            cb(angle)
        except Exception:
            log.exception("Rotation subscriber raised")

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
        # GetCurrentState returns (u, a(...), a(iiduba(ssss)a{sv}), a{sv}).
        # Logical monitors (3rd member): each is (x, y, scale, transform, primary, monitors, props).
        logical_monitors = state.get_child_value(2)
        if logical_monitors.n_children() == 0:
            return
        primary = logical_monitors.get_child_value(0)
        transform = primary.get_child_value(3).get_uint32()
        self.set_system(_TRANSFORM_TO_ANGLE.get(transform, 0))
