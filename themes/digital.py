"""Digital dashboard theme for DrivePulse."""
import math
from typing import Any

import cairo

THEME_TYPE = "dashboard"
LABEL = {"en": "Digital", "de": "Digital"}
CSS = """
window.dp-theme-digital .dp-gauge-bg,
window.dp-theme-digital .dp-gauge-bg > viewport,
window.dp-theme-digital .dp-gauge-bg > * {
  background-color: #000005;
}"""

from draw_helpers import _txt, _norm, _GRAD_STOPS, _draw_last_trip_strip
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


def _palette(dark: bool) -> dict:
    if dark:
        return dict(
            bg=(0.0, 0.0, 0.02),
            dim=(0.50, 0.56, 0.62),
            text=(0.93, 0.95, 1.00),
            big_text=(1.0, 1.0, 1.0),
            bar_bg=(0.14, 0.16, 0.20, 0.55),
            divider=(0.28, 0.32, 0.38, 0.35),
            row_divider=(0.22, 0.25, 0.30, 0.30),
            source=(0.45, 0.52, 0.60),
            unit=(0.65, 0.72, 0.80),
        )
    return dict(
        bg=(0.94, 0.95, 0.96),
        dim=(0.38, 0.42, 0.48),
        text=(0.08, 0.10, 0.14),
        big_text=(0.05, 0.06, 0.10),
        bar_bg=(0.80, 0.82, 0.86, 0.65),
        divider=(0.60, 0.63, 0.68, 0.55),
        row_divider=(0.68, 0.70, 0.74, 0.50),
        source=(0.42, 0.48, 0.56),
        unit=(0.30, 0.36, 0.44),
    )


def _draw_digital_metric_rows(
    cr: Any, rows: list,
    rx: float, rw: float, y_start: float, area_h: float, label_size: float,
    pal: dict,
) -> None:
    row_h = area_h / max(len(rows), 1)
    for i, (name, val_str, norm_v, color, active) in enumerate(rows):
        a = 1.0 if active else 0.26
        cy_row = y_start + i * row_h

        _txt(cr, name, rx + 2, cy_row + row_h * 0.28, label_size,
             (*pal["dim"], 0.72 * a), align="left")
        _txt(cr, val_str, rx + rw, cy_row + row_h * 0.28, label_size * 1.10,
             (*pal["text"], a), bold=True, align="right", max_w=rw * 0.55)

        if norm_v is not None:
            bar_y = cy_row + row_h * 0.58
            bar_h = row_h * 0.20
            cr.set_source_rgba(*pal["bar_bg"])
            cr.rectangle(rx, bar_y, rw, bar_h)
            cr.fill()
            fw = rw * norm_v
            if fw > 1:
                cr.set_source_rgba(*color, 0.90 * a)
                cr.rectangle(rx, bar_y, fw, bar_h)
                cr.fill()

        cr.set_source_rgba(*pal["row_divider"])
        cr.set_line_width(0.5)
        cr.move_to(rx, cy_row + row_h)
        cr.line_to(rx + rw, cy_row + row_h)
        cr.stroke()


def _draw_digital_landscape(cr: Any, width: int, height: int, d: Any, pal: dict) -> None:
    panel_w = width * 0.40
    sp_a = 1.0 if d.speed_active else 0.28

    sp_size = min(height * 0.50, panel_w * 0.72)
    if d.speed_source:
        _txt(cr, d.speed_source, panel_w * 0.50, height * 0.36 - sp_size * 0.55,
             height * 0.054, (*pal["source"], 0.65 * sp_a))
    _txt(cr, d.speed_label, panel_w * 0.50, height * 0.36, sp_size,
         (*pal["big_text"], sp_a), bold=True, max_w=panel_w * 0.90)
    _txt(cr, d.speed_unit, panel_w * 0.50, height * 0.36 + sp_size * 0.60,
         height * 0.082, (*pal["unit"], sp_a * 0.85))

    bx = panel_w * 0.07
    bw = panel_w * 0.86
    by = height * 0.72
    bh = height * 0.048
    cr.set_source_rgba(*pal["bar_bg"])
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

    cr.set_source_rgba(*pal["divider"])
    cr.set_line_width(1.0)
    cr.move_to(panel_w, height * 0.07)
    cr.line_to(panel_w, height * 0.93)
    cr.stroke()

    rx = panel_w + height * 0.06
    rw = width - rx - height * 0.06
    rows = _digital_metric_rows(d)
    _draw_digital_metric_rows(cr, rows, rx, rw, 0.0, height, height * 0.058, pal)


def _draw_digital_portrait(cr: Any, width: int, height: int, d: Any, pal: dict) -> None:
    sp_a = 1.0 if d.speed_active else 0.28
    pad = width * 0.06

    # Speed section: top ~38% of height
    sp_panel_h = height * 0.38
    sp_size = min(sp_panel_h * 0.55, width * 0.60)
    sp_cy = sp_panel_h * 0.42
    if d.speed_source:
        _txt(cr, d.speed_source, width * 0.50, sp_cy - sp_size * 0.55,
             sp_panel_h * 0.068, (*pal["source"], 0.65 * sp_a))
    _txt(cr, d.speed_label, width * 0.50, sp_cy, sp_size,
         (*pal["big_text"], sp_a), bold=True, max_w=width * 0.88)
    _txt(cr, d.speed_unit, width * 0.50, sp_cy + sp_size * 0.60,
         sp_panel_h * 0.10, (*pal["unit"], sp_a * 0.85))

    # Rainbow speed bar
    bx = pad
    bw = width - 2 * pad
    by = sp_panel_h * 0.84
    bh = sp_panel_h * 0.072
    cr.set_source_rgba(*pal["bar_bg"])
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
    cr.set_source_rgba(*pal["divider"])
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
        label_size, pal,
    )


def _draw_impl(cr: Any, width: int, height: int, data: Any, dark: bool) -> None:
    pal = _palette(dark)
    cr.set_source_rgb(*pal["bg"])
    cr.paint()
    strip_h = max(28.0, height * 0.072)
    if width >= height:
        _draw_digital_landscape(cr, width, height - strip_h, data, pal)
    else:
        _draw_digital_portrait(cr, width, height - strip_h, data, pal)
    _draw_last_trip_strip(cr, 0, height - strip_h, width, strip_h, data)


def draw(cr: Any, width: int, height: int, data: Any) -> None:
    _draw_impl(cr, width, height, data, dark=True)
