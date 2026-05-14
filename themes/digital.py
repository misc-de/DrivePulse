"""Digital dashboard theme for DrivePulse."""
import math
from typing import Any

import cairo

THEME_TYPE = "dashboard"
ORDER = 4
LABEL = {"en": "Digital", "de": "Digital"}
CSS = """
window.dp-theme-digital,
window.dp-theme-digital toolbarview,
window.dp-theme-digital scrolledwindow,
window.dp-theme-digital scrolledwindow > viewport,
window.dp-theme-digital .dp-gauge-bg,
window.dp-theme-digital .dp-gauge-bg > * {
  background-color: #000005;
}"""

from draw_helpers import _txt, _norm, _GRAD_STOPS
from common import _translate


def _digital_metric_rows(d: Any) -> list:
    lang = d.language
    rows: list = [
        (_translate(lang, "dashboard.rpm"), d.rpm_label,
         _norm(d.rpm, 0, d.rpm_max),
         (0.95, 0.50, 0.08),
         d.rpm_active),
        (_translate(lang, "dashboard.coolant"), f"{d.coolant_label} °C",
         _norm(d.coolant, d.coolant_min, d.coolant_max),
         (0.18, 0.56, 1.00),
         d.coolant_active),
    ]
    if d.heading_active:
        rows.append((_translate(lang, "dashboard.heading"), d.heading_str, None, (0.35, 0.90, 0.45), True))
    if d.fuel_active:
        rows.append((_translate(lang, "dashboard.fuel"), d.fuel_label,
                     _norm(d.fuel_pct, 0, 100),
                     (0.25, 0.85, 0.35),
                     True))
    return rows


def _draw_digital_metric_rows(
    cr: Any, rows: list,
    rx: float, rw: float, y_start: float, area_h: float, label_size: float,
) -> None:
    row_h = area_h / max(len(rows), 1)
    for i, (name, val_str, norm_v, color, active) in enumerate(rows):
        a = 1.0 if active else 0.26
        cy_row = y_start + i * row_h

        _txt(cr, name, rx + 2, cy_row + row_h * 0.28, label_size,
             (0.50, 0.56, 0.62, 0.72 * a), align="left")
        _txt(cr, val_str, rx + rw, cy_row + row_h * 0.28, label_size * 1.10,
             (0.93, 0.95, 1.00, a), bold=True, align="right", max_w=rw * 0.55)

        if norm_v is not None:
            bar_y = cy_row + row_h * 0.58
            bar_h = row_h * 0.20
            cr.set_source_rgba(0.13, 0.15, 0.19, 0.55)
            cr.rectangle(rx, bar_y, rw, bar_h)
            cr.fill()
            fw = rw * norm_v
            if fw > 1:
                cr.set_source_rgba(*color, 0.90 * a)
                cr.rectangle(rx, bar_y, fw, bar_h)
                cr.fill()

        cr.set_source_rgba(0.22, 0.25, 0.30, 0.30)
        cr.set_line_width(0.5)
        cr.move_to(rx, cy_row + row_h)
        cr.line_to(rx + rw, cy_row + row_h)
        cr.stroke()


def _draw_digital_landscape(cr: Any, width: int, height: int, d: Any) -> None:
    panel_w = width * 0.40
    sp_a = 1.0 if d.speed_active else 0.28

    sp_size = min(height * 0.50, panel_w * 0.72)
    _txt(cr, d.speed_label, panel_w * 0.50, height * 0.36, sp_size,
         (1, 1, 1, sp_a), bold=True, max_w=panel_w * 0.90)
    _txt(cr, d.speed_unit, panel_w * 0.50, height * 0.36 + sp_size * 0.60,
         height * 0.082, (0.65, 0.72, 0.80, sp_a * 0.85))

    bx = panel_w * 0.07
    bw = panel_w * 0.86
    by = height * 0.72
    bh = height * 0.048
    cr.set_source_rgba(0.14, 0.16, 0.20, 0.55)
    cr.rectangle(bx, by, bw, bh)
    cr.fill()
    fw = bw * _norm(d.speed, 0, d.speed_max)
    if fw > 1:
        pat = cairo.LinearGradient(bx, 0, bx + bw, 0)
        for pos, col in _GRAD_STOPS:
            pat.add_color_stop_rgb(pos, *col)
        cr.set_source(pat)
        cr.rectangle(bx, by, fw, bh)
        cr.fill()

    cr.set_source_rgba(0.28, 0.32, 0.38, 0.35)
    cr.set_line_width(1.0)
    cr.move_to(panel_w, height * 0.07)
    cr.line_to(panel_w, height * 0.93)
    cr.stroke()

    rx = panel_w + height * 0.06
    rw = width - rx - height * 0.06
    rows = _digital_metric_rows(d)
    _draw_digital_metric_rows(cr, rows, rx, rw, 0.0, height, height * 0.058)


def _draw_digital_portrait(cr: Any, width: int, height: int, d: Any) -> None:
    sp_a = 1.0 if d.speed_active else 0.28
    pad = width * 0.06

    # Speed section: top ~38% of height
    sp_panel_h = height * 0.38
    sp_size = min(sp_panel_h * 0.55, width * 0.60)
    sp_cy = sp_panel_h * 0.42
    _txt(cr, d.speed_label, width * 0.50, sp_cy, sp_size,
         (1, 1, 1, sp_a), bold=True, max_w=width * 0.88)
    _txt(cr, d.speed_unit, width * 0.50, sp_cy + sp_size * 0.60,
         sp_panel_h * 0.10, (0.65, 0.72, 0.80, sp_a * 0.85))

    # Rainbow speed bar
    bx = pad
    bw = width - 2 * pad
    by = sp_panel_h * 0.84
    bh = sp_panel_h * 0.072
    cr.set_source_rgba(0.14, 0.16, 0.20, 0.55)
    cr.rectangle(bx, by, bw, bh)
    cr.fill()
    fw = bw * _norm(d.speed, 0, d.speed_max)
    if fw > 1:
        pat = cairo.LinearGradient(bx, 0, bx + bw, 0)
        for pos, col in _GRAD_STOPS:
            pat.add_color_stop_rgb(pos, *col)
        cr.set_source(pat)
        cr.rectangle(bx, by, fw, bh)
        cr.fill()

    # Horizontal divider
    div_y = sp_panel_h
    cr.set_source_rgba(0.28, 0.32, 0.38, 0.35)
    cr.set_line_width(1.0)
    cr.move_to(pad, div_y)
    cr.line_to(width - pad, div_y)
    cr.stroke()

    # Metric rows fill the remaining height
    rows = _digital_metric_rows(d)
    label_size = min(height * 0.046, width * 0.052)
    _draw_digital_metric_rows(
        cr, rows,
        pad, width - 2 * pad,
        div_y, height - div_y,
        label_size,
    )


def draw(cr: Any, width: int, height: int, data: Any) -> None:
    cr.set_source_rgb(0.0, 0.0, 0.02)
    cr.paint()
    if width >= height:
        _draw_digital_landscape(cr, width, height, data)
    else:
        _draw_digital_portrait(cr, width, height, data)
