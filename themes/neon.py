"""Neon gauge theme for DrivePulse."""
import math
from typing import Any

from draw_helpers import _txt

THEME_TYPE = "gauge"
LABEL = {"en": "Neon", "de": "Neon"}
CSS = """
window.dp-theme-neon,
window.dp-theme-neon toolbarview,
window.dp-theme-neon scrolledwindow,
window.dp-theme-neon scrolledwindow > viewport,
window.dp-theme-neon .dp-gauge-bg,
window.dp-theme-neon .dp-gauge-bg > * {
  background-color: #000008;
}
.dp-accel-theme-neon .card {
  background-color: rgba(0, 2, 15, 0.9);
  border-radius: 4px;
}
.dp-accel-theme-neon .heading    { color: #7ec8ff; }
.dp-accel-theme-neon .title-1    { color: #a4d8ff; }
.dp-accel-theme-neon .title-2    { color: #5ba8ff; }
.dp-accel-theme-neon .dim-label  { color: rgba(100,180,255,0.65); }"""


def _palette(dark: bool) -> dict:
    if dark:
        return dict(
            bg=(0.0, 0.0, 0.03),
            track=(0.18, 0.20, 0.24, 0.7),
            dot=(1.0, 1.0, 1.0),
            text=(0.94, 0.96, 1.0),
        )
    return dict(
        bg=(0.93, 0.94, 0.96),
        track=(0.65, 0.68, 0.72, 0.7),
        dot=(0.08, 0.10, 0.14),
        text=(0.06, 0.08, 0.12),
    )


def _draw_impl(cr: Any, width: int, height: int, gauge: Any, dark: bool) -> None:
    pal = _palette(dark)
    cx, cy, size, radius, line_width, start_angle, end_angle, span, normalized = gauge.arc_params(width, height)
    value_angle = start_angle + span * normalized
    active_alpha = 1.0 if gauge.active else 0.3
    accent = gauge.accent_rgb if gauge.active else (0.35, 0.38, 0.42)
    r, g, b = accent

    cr.set_line_width(1.0)
    cr.set_source_rgba(r, g, b, 0.18 * active_alpha)
    cr.arc(cx, cy, radius + line_width * 1.6, start_angle, end_angle)
    cr.stroke()

    cr.set_line_width(line_width * 0.55)
    cr.set_line_cap(1)
    cr.set_source_rgba(*pal["track"])
    cr.arc(cx, cy, radius, start_angle, end_angle)
    cr.stroke()

    for lw_mult, a_mult in ((5.0, 0.03), (3.2, 0.08), (1.8, 0.20), (0.65, 1.0)):
        cr.set_line_width(line_width * lw_mult)
        cr.set_source_rgba(r, g, b, a_mult * active_alpha)
        cr.arc(cx, cy, radius, start_angle, value_angle)
        cr.stroke()

    dot_x = cx + math.cos(value_angle) * radius
    dot_y = cy + math.sin(value_angle) * radius
    for dot_r, dot_a in ((line_width * 1.4, 0.12), (line_width * 0.7, 0.4), (line_width * 0.3, 1.0)):
        cr.set_source_rgba(*pal["dot"], dot_a * active_alpha)
        cr.arc(dot_x, dot_y, dot_r, 0, math.tau)
        cr.fill()

    value_size = max(30, size * 0.20)
    unit_size = max(13, size * 0.072)
    text_width = size * 0.72

    _txt(cr, gauge.state.label, cx, cy - size * 0.05, value_size,
         (*pal["text"], active_alpha), bold=True, max_w=text_width)
    _txt(cr, gauge.state.unit, cx, cy + size * 0.10, unit_size,
         (r, g, b, 0.9 * active_alpha), bold=True)


def draw(cr: Any, width: int, height: int, gauge: Any) -> None:
    _draw_impl(cr, width, height, gauge, dark=True)

