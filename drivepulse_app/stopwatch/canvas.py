"""G-force canvas used by the StopWatch page."""
from __future__ import annotations

import math
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402


class GForceCanvas(Gtk.DrawingArea):
    """2D G-Force visualization: dot inside a ring grid (lateral × longitudinal).

    Style and scale follow the Sensor-Suite reference
    (https://github.com/misc-de/Sensor-Suite).
    """

    __gtype_name__ = "GForceCanvas"

    MAX_G = 2.0
    _SMOOTH = 0.30

    def __init__(self) -> None:
        super().__init__()
        self._target_x = 0.0
        self._target_y = 0.0
        self._target_z = 1.0
        self._x = 0.0
        self._y = 0.0
        self._z = 1.0
        self._has_data = False
        self._light_mode = False
        self.set_draw_func(self._draw)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_content_width(220)
        self.set_content_height(220)

    def update_g(self, x_g: float | None, y_g: float | None, z_g: float | None = None) -> None:
        if x_g is None and y_g is None and z_g is None:
            return
        if x_g is not None:
            self._target_x = float(x_g)
        if y_g is not None:
            self._target_y = float(y_g)
        if z_g is not None:
            self._target_z = float(z_g)
        self._has_data = True
        self._x += (self._target_x - self._x) * self._SMOOTH
        self._y += (self._target_y - self._y) * self._SMOOTH
        self._z += (self._target_z - self._z) * self._SMOOTH
        self.queue_draw()

    def set_light_mode(self, enabled: bool) -> None:
        self._light_mode = enabled
        self.queue_draw()

    def clear(self) -> None:
        self._target_x = self._target_y = 0.0
        self._target_z = 1.0
        self._x = self._y = 0.0
        self._z = 1.0
        self._has_data = False
        self.queue_draw()

    @staticmethod
    def _text_center(cr: Any, text: str, x: float, y: float) -> None:
        ext = cr.text_extents(text)
        cr.move_to(x - ext.width / 2 - ext.x_bearing, y - ext.height / 2 - ext.y_bearing)
        cr.show_text(text)

    def _draw(self, _area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        cx = width / 2
        cy = height / 2
        margin = min(width, height) * 0.165
        radius = min(width, height) / 2 - margin
        if radius < 18:
            return

        # Magnitude (deviation from 1g of gravity) drives colour
        mag = math.sqrt(self._x ** 2 + self._y ** 2 + self._z ** 2)
        dev = abs(mag - 1.0)
        if not self._has_data:
            r, g, b = 0.45, 0.48, 0.52
        elif dev < 0.10:
            r, g, b = 0.20, 0.78, 0.34
        elif dev < 0.45:
            r, g, b = 0.95, 0.72, 0.10
        else:
            r, g, b = 0.90, 0.22, 0.16

        font_value = max(11.0, radius * 0.16)
        font_ring  = max(8.0,  radius * 0.095)
        label_pad  = margin * 0.55

        if self._light_mode:
            bg = (0.98, 0.98, 0.97, 0.96)
            grid = (0.12, 0.13, 0.15)
            text = (0.03, 0.03, 0.03)
            highlight = (0.0, 0.0, 0.0, 0.18)
        else:
            bg = (0.08, 0.09, 0.11, 0.95)
            grid = (0.60, 0.62, 0.66)
            text = (0.92, 0.93, 0.95)
            highlight = (1.0, 1.0, 1.0, 0.30)

        # Background disc + outer ring
        cr.arc(cx, cy, radius, 0, math.tau)
        cr.set_source_rgba(*bg)
        cr.fill_preserve()
        cr.set_source_rgba(r, g, b, 0.40)
        cr.set_line_width(2.2)
        cr.stroke()

        # Inner rings at 0.5g and 1.0g with labels
        for ring_g, alpha, lw in ((0.5, 0.20, 1.0), (1.0, 0.45, 1.4)):
            rpx = (ring_g / self.MAX_G) * radius
            cr.arc(cx, cy, rpx, 0, math.tau)
            cr.set_source_rgba(r, g, b, alpha)
            cr.set_line_width(lw)
            cr.stroke()
            cr.select_font_face("Cantarell", 0, 0)
            cr.set_font_size(font_ring)
            cr.set_source_rgba(*grid, 0.75)
            self._text_center(cr, f"{ring_g:.1f}g", cx + rpx * 0.70, cy - rpx * 0.70)

        # Cross-hairs through centre
        cr.set_line_width(1.0)
        cr.set_source_rgba(*grid, 0.40)
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            cr.move_to(cx + dx * radius * 0.06, cy + dy * radius * 0.06)
            cr.line_to(cx + dx * radius * 0.94, cy + dy * radius * 0.94)
        cr.stroke()

        # G-force dot (clamped to ring)
        dot_r = max(6.0, radius * 0.11)
        nx = self._x / self.MAX_G
        ny = self._y / self.MAX_G
        dist = math.sqrt(nx * nx + ny * ny)
        limit = 1.0 - dot_r / radius
        if dist > limit and dist > 0:
            nx *= limit / dist
            ny *= limit / dist
        # X axis: right = positive (right turn / right G)
        # Y axis: up = positive (forward acceleration)
        dot_x = cx + nx * radius
        dot_y = cy - ny * radius
        cr.arc(dot_x, dot_y, dot_r, 0, math.tau)
        cr.set_source_rgba(r, g, b, 0.30 if self._has_data else 0.18)
        cr.fill()
        cr.arc(dot_x, dot_y, dot_r, 0, math.tau)
        cr.set_source_rgba(r, g, b, 0.95 if self._has_data else 0.5)
        cr.set_line_width(2.2)
        cr.stroke()
        if self._has_data:
            cr.arc(dot_x, dot_y, dot_r * 0.32, 0, math.tau)
            cr.set_source_rgba(*highlight)
            cr.fill()

        # Axis labels around the ring (top = longitudinal, right = lateral, bottom = magnitude)
        cr.select_font_face("Cantarell", 0, 0)
        cr.set_font_size(font_value)
        cr.set_source_rgba(*text, 0.95 if self._has_data else 0.55)
        self._text_center(cr, f"{self._y:+.1f}g", cx, cy - radius - label_pad)
        self._text_center(cr, f"{self._x:+.1f}g", cx + radius + label_pad, cy)
        self._text_center(cr, f"{mag:.1f}g",       cx, cy + radius + label_pad)
