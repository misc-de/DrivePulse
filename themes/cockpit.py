"""Cockpit gauge theme for DrivePulse."""
import math
from typing import Any

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


def draw(cr: Any, width: int, height: int, gauge: Any) -> None:
    cx, cy, size, radius, line_width, start_angle, end_angle, span, normalized = gauge.arc_params(width, height)
    value_angle = start_angle + span * normalized
    active_alpha = 1.0 if gauge.active else 0.34
    accent = gauge.accent_rgb if gauge.active else (0.45, 0.48, 0.50)

    # Fill the entire DrawingArea rectangle so no app-background bleeds through
    cr.set_source_rgb(0.02, 0.025, 0.03)
    cr.paint()
    cr.arc(cx, cy, radius + line_width * 1.15, 0, math.tau)
    cr.fill()

    cr.set_line_width(2.0)
    cr.set_source_rgba(0.86, 0.91, 0.96, 0.85 * active_alpha)
    cr.arc(cx, cy, radius + line_width * 1.4, start_angle, end_angle)
    cr.stroke()

    cr.set_line_width(line_width)
    cr.set_line_cap(1)
    cr.set_source_rgba(0.35, 0.42, 0.48, 0.28 if gauge.active else 0.16)
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

    value_size = max(28, size * 0.19)
    unit_size = max(14, size * 0.075)
    title_size = max(13, size * 0.062)
    text_width = size * 0.72
    gauge.draw_text(cr, gauge.state.label, cx, cy - size * 0.06, value_size, active_alpha, True, text_width)
    gauge.draw_text(cr, gauge.state.unit, cx, cy + size * 0.09, unit_size, 0.78 * active_alpha, True, text_width)
