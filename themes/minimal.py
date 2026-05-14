"""Minimal gauge theme for DrivePulse."""
import math
from typing import Any

THEME_TYPE = "gauge"
LABEL = {"en": "Minimal", "de": "Minimal"}
CSS = """
.dp-accel-theme-minimal .card {
  background-color: transparent;
  border-radius: 0;
  padding-top: 4px;
  padding-bottom: 4px;
}"""


def draw(cr: Any, width: int, height: int, gauge: Any) -> None:
    size = min(width, height)
    cx = width / 2
    cy = height / 2
    radius = size * 0.41
    line_width = max(4, size * 0.021)

    # Clear to transparent so the system/window background shows through
    cr.set_source_rgba(0, 0, 0, 0)
    cr.paint()

    start_angle = math.radians(145)
    end_angle = math.radians(395)
    span = end_angle - start_angle
    normalized = (gauge.state.value - gauge.state.min_value) / (gauge.state.max_value - gauge.state.min_value)
    normalized = max(0.0, min(1.0, normalized))
    value_angle = start_angle + span * normalized
    active_alpha = 1.0 if gauge.active else 0.28
    accent = gauge.accent_rgb if gauge.active else (0.5, 0.52, 0.55)

    cr.set_line_width(line_width)
    cr.set_line_cap(1)
    cr.set_source_rgba(0.45, 0.48, 0.52, 0.22)
    cr.arc(cx, cy, radius, start_angle, end_angle)
    cr.stroke()

    cr.set_source_rgba(accent[0], accent[1], accent[2], 0.88 * active_alpha)
    cr.arc(cx, cy, radius, start_angle, value_angle)
    cr.stroke()

    cr.set_line_width(1.5)
    for i in range(5):
        angle = start_angle + span * (i / 4)
        outer = radius + line_width * 1.6
        inner = radius + line_width * 0.4
        cr.set_source_rgba(0.75, 0.78, 0.82, 0.55 * active_alpha)
        cr.move_to(cx + math.cos(angle) * inner, cy + math.sin(angle) * inner)
        cr.line_to(cx + math.cos(angle) * outer, cy + math.sin(angle) * outer)
        cr.stroke()

    dot_x = cx + math.cos(value_angle) * radius
    dot_y = cy + math.sin(value_angle) * radius
    cr.set_source_rgba(accent[0], accent[1], accent[2], active_alpha)
    cr.arc(dot_x, dot_y, line_width * 0.55, 0, math.tau)
    cr.fill()

    title_size = max(12, size * 0.057)
    gauge.draw_text(cr, gauge.title, cx, cy - size * 0.20, title_size, 0.52 * active_alpha, False, size * 0.75)

    value_size = max(30, size * 0.21)
    gauge.draw_text(cr, gauge.state.label, cx, cy + size * 0.03, value_size, active_alpha, True, size * 0.75)

    unit_size = max(12, size * 0.063)
    gauge.draw_text(cr, gauge.state.unit, cx, cy + size * 0.18, unit_size, 0.65 * active_alpha, False, size * 0.75)
