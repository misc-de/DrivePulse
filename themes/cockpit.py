"""Cockpit gauge theme for DrivePulse."""
import math
from typing import Any

from draw_helpers import _txt

THEME_TYPE = "gauge"
LABEL = {"en": "Cockpit", "de": "Cockpit"}
CSS = """
window.dp-theme-cockpit,
window.dp-theme-cockpit toolbarview,
window.dp-theme-cockpit scrolledwindow,
window.dp-theme-cockpit scrolledwindow > viewport,
window.dp-theme-cockpit .dp-gauge-bg,
window.dp-theme-cockpit .dp-gauge-bg > * {
  background-color: #05080f;
}
.dp-accel-theme-cockpit .card {
  background-color: rgba(8, 14, 22, 0.8);
  border-radius: 6px;
}"""


def _palette(dark: bool) -> dict:
    if dark:
        return dict(
            bg=(0.02, 0.025, 0.03),
            outer_ring=(0.86, 0.91, 0.96, 0.85),
            track=(0.35, 0.42, 0.48),
            tick=(0.95, 0.97, 1.0),
            marker=(1.0, 1.0, 1.0, 0.95),
            text=(0.94, 0.96, 1.0),
        )
    return dict(
        bg=(1.0, 1.0, 1.0),
        outer_ring=(0.30, 0.34, 0.40, 0.80),
        track=(0.65, 0.68, 0.72),
        tick=(0.18, 0.22, 0.28),
        marker=(0.10, 0.12, 0.16, 0.95),
        text=(0.06, 0.08, 0.12),
    )


def _draw_impl(cr: Any, width: int, height: int, gauge: Any, dark: bool) -> None:
    pal = _palette(dark)
    cx, cy, size, radius, line_width, start_angle, end_angle, span, normalized = gauge.arc_params(width, height)
    value_angle = start_angle + span * normalized
    active_alpha = 1.0 if gauge.active else 0.34
    accent = gauge.accent_rgb if gauge.active else (0.45, 0.48, 0.50)

    cr.set_line_width(2.0)
    o = pal["outer_ring"]
    cr.set_source_rgba(o[0], o[1], o[2], o[3] * active_alpha)
    cr.arc(cx, cy, radius + line_width * 1.4, start_angle, end_angle)
    cr.stroke()

    cr.set_line_width(line_width)
    cr.set_line_cap(1)
    cr.set_source_rgba(*pal["track"], 0.28 if gauge.active else 0.16)
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
        cr.set_source_rgba(*pal["tick"], (0.75 if index % 5 else 0.95) * active_alpha)
        cr.move_to(cx + math.cos(angle) * inner, cy + math.sin(angle) * inner)
        cr.line_to(cx + math.cos(angle) * outer, cy + math.sin(angle) * outer)
        cr.stroke()

    m = pal["marker"]
    cr.set_source_rgba(m[0], m[1], m[2], m[3] * active_alpha)
    top = -math.pi / 2
    cr.move_to(cx + math.cos(top) * (radius + line_width * 1.5), cy + math.sin(top) * (radius + line_width * 1.5))
    cr.line_to(cx + math.cos(top - 0.06) * (radius + line_width * 0.25), cy + math.sin(top - 0.06) * (radius + line_width * 0.25))
    cr.line_to(cx + math.cos(top + 0.06) * (radius + line_width * 0.25), cy + math.sin(top + 0.06) * (radius + line_width * 0.25))
    cr.close_path()
    cr.fill()

    value_size = max(28, size * 0.19)
    unit_size = max(14, size * 0.075)
    text_width = size * 0.72
    _txt(cr, gauge.state.label, cx, cy - size * 0.06, value_size,
         (*pal["text"], active_alpha), bold=True, max_w=text_width)
    _txt(cr, gauge.state.unit, cx, cy + size * 0.09, unit_size,
         (*pal["text"], 0.78 * active_alpha), bold=True, max_w=text_width)


def draw(cr: Any, width: int, height: int, gauge: Any) -> None:
    _draw_impl(cr, width, height, gauge, dark=True)
