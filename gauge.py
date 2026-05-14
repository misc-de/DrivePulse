"""Gauge widget and state for DrivePulse."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

GAUGE_THEMES = ("cockpit", "neon", "minimal")


@dataclass
class GaugeState:
    value: float = 0.0
    label: str = "--"
    unit: str = ""
    min_value: float = 0.0
    max_value: float = 100.0


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
        theme: str = "cockpit",
    ) -> None:
        super().__init__()
        self.title = title
        self.accent_rgb = accent_rgb
        self.theme = theme
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

    def set_theme(self, theme: str) -> None:
        self.theme = theme
        self.queue_draw()

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _arc_params(self, width: int, height: int) -> tuple[float, float, float, float, float, float, float, float, float]:
        """Returns cx, cy, size, radius, line_width, start, end, span, normalized."""
        size = min(width, height)
        cx = width / 2
        cy = height / 2
        radius = size * 0.39
        line_width = max(7, size * 0.035)
        start = math.radians(135)
        end = math.radians(405)
        span = end - start
        normalized = (self.state.value - self.state.min_value) / (self.state.max_value - self.state.min_value)
        normalized = max(0.0, min(1.0, normalized))
        return cx, cy, size, radius, line_width, start, end, span, normalized

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

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _draw(self, area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        if self.theme == "neon":
            self._draw_neon(cr, width, height)
        elif self.theme == "minimal":
            self._draw_minimal(cr, width, height)
        else:
            self._draw_cockpit(cr, width, height)

    # ------------------------------------------------------------------
    # Theme: cockpit (default)
    # ------------------------------------------------------------------

    def _draw_cockpit(self, cr: Any, width: int, height: int) -> None:
        cx, cy, size, radius, line_width, start_angle, end_angle, span, normalized = self._arc_params(width, height)
        value_angle = start_angle + span * normalized
        active_alpha = 1.0 if self.active else 0.34
        accent = self.accent_rgb if self.active else (0.45, 0.48, 0.50)

        cr.set_source_rgb(0.02, 0.025, 0.03)
        cr.arc(cx, cy, radius + line_width * 1.15, 0, math.tau)
        cr.fill()

        cr.set_line_width(2.0)
        cr.set_source_rgba(0.86, 0.91, 0.96, 0.85 * active_alpha)
        cr.arc(cx, cy, radius + line_width * 1.4, start_angle, end_angle)
        cr.stroke()

        cr.set_line_width(line_width)
        cr.set_line_cap(1)
        cr.set_source_rgba(0.35, 0.42, 0.48, 0.28 if self.active else 0.16)
        cr.arc(cx, cy, radius, start_angle, end_angle)
        cr.stroke()

        cr.set_source_rgba(accent[0], accent[1], accent[2], 0.92 * active_alpha)
        cr.arc(cx, cy, radius, start_angle, value_angle)
        cr.stroke()

        cr.set_line_width(2.0)
        for index in range(0, 11):
            angle = start_angle + span * (index / 10)
            outer = radius + line_width * 0.8
            inner = radius + line_width * (0.18 if index % 5 else -0.4)
            cr.set_source_rgba(0.95, 0.97, 1.0, (0.75 if index % 5 else 0.95) * active_alpha)
            cr.move_to(cx + math.cos(angle) * inner, cy + math.sin(angle) * inner)
            cr.line_to(cx + math.cos(angle) * outer, cy + math.sin(angle) * outer)
            cr.stroke()

        cr.set_source_rgba(1, 1, 1, 0.95 * active_alpha)
        top = -math.pi / 2
        cr.move_to(cx + math.cos(top) * (radius + line_width * 1.5), cy + math.sin(top) * (radius + line_width * 1.5))
        cr.line_to(cx + math.cos(top - 0.06) * (radius + line_width * 0.25), cy + math.sin(top - 0.06) * (radius + line_width * 0.25))
        cr.line_to(cx + math.cos(top + 0.06) * (radius + line_width * 0.25), cy + math.sin(top + 0.06) * (radius + line_width * 0.25))
        cr.close_path()
        cr.fill()

        self._draw_cockpit_center(cr, cx, cy, size, active_alpha)

    def _draw_cockpit_center(self, cr: Any, cx: float, cy: float, size: int, active_alpha: float) -> None:
        value_size = max(28, size * 0.19)
        unit_size = max(14, size * 0.075)
        title_size = max(13, size * 0.062)
        text_width = size * 0.72
        self._draw_text_centered(cr, self.state.label, cx, cy - size * 0.06, value_size, active_alpha, True, text_width)
        self._draw_text_centered(cr, self.state.unit, cx, cy + size * 0.09, unit_size, 0.78 * active_alpha, True, text_width)
        self._draw_text_centered(cr, self.title, cx, cy + size * 0.26, title_size, 0.62 * active_alpha, False, text_width)

    # ------------------------------------------------------------------
    # Theme: neon
    # ------------------------------------------------------------------

    def _draw_neon(self, cr: Any, width: int, height: int) -> None:
        cx, cy, size, radius, line_width, start_angle, end_angle, span, normalized = self._arc_params(width, height)
        value_angle = start_angle + span * normalized
        active_alpha = 1.0 if self.active else 0.3
        accent = self.accent_rgb if self.active else (0.35, 0.38, 0.42)
        r, g, b = accent

        # Deep black background
        cr.set_source_rgb(0.0, 0.0, 0.03)
        cr.paint()

        # Subtle outer ring
        cr.set_line_width(1.0)
        cr.set_source_rgba(r, g, b, 0.18 * active_alpha)
        cr.arc(cx, cy, radius + line_width * 1.6, start_angle, end_angle)
        cr.stroke()

        # Track
        cr.set_line_width(line_width * 0.55)
        cr.set_line_cap(1)
        cr.set_source_rgba(0.18, 0.20, 0.24, 0.7)
        cr.arc(cx, cy, radius, start_angle, end_angle)
        cr.stroke()

        # Glow layers: outer halo → core
        for lw_mult, a_mult in ((5.0, 0.03), (3.2, 0.08), (1.8, 0.20), (0.65, 1.0)):
            cr.set_line_width(line_width * lw_mult)
            cr.set_source_rgba(r, g, b, a_mult * active_alpha)
            cr.arc(cx, cy, radius, start_angle, value_angle)
            cr.stroke()

        # Bright dot at current value position
        dot_x = cx + math.cos(value_angle) * radius
        dot_y = cy + math.sin(value_angle) * radius
        for dot_r, dot_a in ((line_width * 1.4, 0.12), (line_width * 0.7, 0.4), (line_width * 0.3, 1.0)):
            cr.set_source_rgba(1.0, 1.0, 1.0, dot_a * active_alpha)
            cr.arc(dot_x, dot_y, dot_r, 0, math.tau)
            cr.fill()

        # Center text — value large, unit in accent, title dim
        self._draw_neon_center(cr, cx, cy, size, active_alpha, accent)

    def _draw_neon_center(self, cr: Any, cx: float, cy: float, size: int, active_alpha: float, accent: tuple) -> None:
        value_size = max(30, size * 0.20)
        unit_size = max(13, size * 0.072)
        title_size = max(12, size * 0.058)
        text_width = size * 0.72

        # Value in white
        self._draw_text_centered(cr, self.state.label, cx, cy - size * 0.05, value_size, active_alpha, True, text_width)

        # Unit in accent color
        cr.select_font_face("Cantarell", 0, 1)
        cr.set_font_size(unit_size)
        ext = cr.text_extents(self.state.unit)
        cr.set_source_rgba(accent[0], accent[1], accent[2], 0.9 * active_alpha)
        cr.move_to(cx - ext.width / 2 - ext.x_bearing, (cy + size * 0.10) - ext.height / 2 - ext.y_bearing)
        cr.show_text(self.state.unit)

        self._draw_text_centered(cr, self.title, cx, cy + size * 0.27, title_size, 0.45 * active_alpha, False, text_width)

    # ------------------------------------------------------------------
    # Theme: minimal
    # ------------------------------------------------------------------

    def _draw_minimal(self, cr: Any, width: int, height: int) -> None:
        size = min(width, height)
        cx = width / 2
        cy = height / 2
        radius = size * 0.41
        line_width = max(4, size * 0.021)

        start_angle = math.radians(145)
        end_angle = math.radians(395)
        span = end_angle - start_angle
        normalized = (self.state.value - self.state.min_value) / (self.state.max_value - self.state.min_value)
        normalized = max(0.0, min(1.0, normalized))
        value_angle = start_angle + span * normalized
        active_alpha = 1.0 if self.active else 0.28
        accent = self.accent_rgb if self.active else (0.5, 0.52, 0.55)

        # Track arc
        cr.set_line_width(line_width)
        cr.set_line_cap(1)
        cr.set_source_rgba(0.45, 0.48, 0.52, 0.22)
        cr.arc(cx, cy, radius, start_angle, end_angle)
        cr.stroke()

        # Value arc
        cr.set_source_rgba(accent[0], accent[1], accent[2], 0.88 * active_alpha)
        cr.arc(cx, cy, radius, start_angle, value_angle)
        cr.stroke()

        # 5 major tick marks only
        cr.set_line_width(1.5)
        for i in range(5):
            angle = start_angle + span * (i / 4)
            outer = radius + line_width * 1.6
            inner = radius + line_width * 0.4
            cr.set_source_rgba(0.75, 0.78, 0.82, 0.55 * active_alpha)
            cr.move_to(cx + math.cos(angle) * inner, cy + math.sin(angle) * inner)
            cr.line_to(cx + math.cos(angle) * outer, cy + math.sin(angle) * outer)
            cr.stroke()

        # End-cap dot at value position
        dot_x = cx + math.cos(value_angle) * radius
        dot_y = cy + math.sin(value_angle) * radius
        cr.set_source_rgba(accent[0], accent[1], accent[2], active_alpha)
        cr.arc(dot_x, dot_y, line_width * 0.55, 0, math.tau)
        cr.fill()

        # Title (small, above center)
        title_size = max(12, size * 0.057)
        self._draw_text_centered(cr, self.title, cx, cy - size * 0.20, title_size, 0.52 * active_alpha, False, size * 0.75)

        # Value (large, center)
        value_size = max(30, size * 0.21)
        self._draw_text_centered(cr, self.state.label, cx, cy + size * 0.03, value_size, active_alpha, True, size * 0.75)

        # Unit (small, below value)
        unit_size = max(12, size * 0.063)
        self._draw_text_centered(cr, self.state.unit, cx, cy + size * 0.18, unit_size, 0.65 * active_alpha, False, size * 0.75)
