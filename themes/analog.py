"""Analog dashboard theme for DrivePulse."""
import math
from typing import Any

THEME_TYPE = "dashboard"
ORDER = 6
LABEL = {"en": "Analog", "de": "Analog"}
CSS = """
window.dp-theme-analog,
window.dp-theme-analog toolbarview,
window.dp-theme-analog scrolledwindow,
window.dp-theme-analog scrolledwindow > viewport,
window.dp-theme-analog .dp-gauge-bg,
window.dp-theme-analog .dp-gauge-bg > * {
  background-color: #0d0d0f;
}"""

from draw_helpers import _txt, _norm, _cardinal
from common import _translate


def _analog_gauge(
    cr: Any,
    cx: float,
    cy: float,
    r: float,
    val: float,
    val_min: float,
    val_max: float,
    val_label: str,
    val_unit: str,
    title: str,
    active: bool,
    step_major: float,
    step_minor: float,
    dark: bool = True,
) -> None:
    a = 1.0 if active else 0.30
    face_col = (0.11, 0.12, 0.13) if dark else (0.92, 0.93, 0.95)
    text_col = (0.95, 0.96, 0.98) if dark else (0.08, 0.10, 0.12)
    dim_col = (0.55, 0.58, 0.62) if dark else (0.40, 0.42, 0.45)

    ARC_START = math.radians(135)
    ARC_SPAN = math.radians(270)
    ARC_END = ARC_START + ARC_SPAN

    # Face disc
    cr.set_source_rgb(*face_col)
    cr.arc(cx, cy, r, 0, math.tau)
    cr.fill()

    # Outer border ring
    cr.set_line_width(max(2.0, r * 0.025))
    cr.set_source_rgba(0.35, 0.38, 0.42, 0.70)
    cr.arc(cx, cy, r, 0, math.tau)
    cr.stroke()

    # Tick marks and scale labels
    steps_minor = int((val_max - val_min) / step_minor)
    steps_major = int((val_max - val_min) / step_major)

    tick_outer = r * 0.92
    tick_major_inner = r * 0.74
    tick_minor_inner = r * 0.84

    for s in range(steps_minor + 1):
        frac = s / steps_minor
        angle = ARC_START + ARC_SPAN * frac
        is_major = (s % round(step_major / step_minor)) == 0
        inner = tick_major_inner if is_major else tick_minor_inner
        lw = max(1.5, r * 0.016) if is_major else max(0.8, r * 0.009)
        alpha_t = 0.85 if is_major else 0.45
        cr.set_line_width(lw)
        cr.set_source_rgba(*text_col, alpha_t * a)
        cr.move_to(cx + math.cos(angle) * inner, cy + math.sin(angle) * inner)
        cr.line_to(cx + math.cos(angle) * tick_outer, cy + math.sin(angle) * tick_outer)
        cr.stroke()

    # Scale number labels at major ticks
    lbl_r = r * 0.62
    lbl_size = max(9.0, r * 0.13)
    for s in range(steps_major + 1):
        frac = s / steps_major
        angle = ARC_START + ARC_SPAN * frac
        lval = val_min + (val_max - val_min) * frac
        cr.select_font_face("Cantarell", 0, 0)
        cr.set_font_size(lbl_size)
        txt = f"{lval:.0f}"
        ext = cr.text_extents(txt)
        nx = cx + math.cos(angle) * lbl_r - ext.width / 2 - ext.x_bearing
        ny = cy + math.sin(angle) * lbl_r - ext.height / 2 - ext.y_bearing
        cr.set_source_rgba(*dim_col, 0.80 * a)
        cr.move_to(nx, ny)
        cr.show_text(txt)

    # Needle
    norm = _norm(val, val_min, val_max)
    needle_angle = ARC_START + ARC_SPAN * norm
    needle_len = r * 0.72
    tail_len = r * 0.16
    perp = r * 0.028
    tip_x = cx + math.cos(needle_angle) * needle_len
    tip_y = cy + math.sin(needle_angle) * needle_len
    tail_x = cx - math.cos(needle_angle) * tail_len
    tail_y = cy - math.sin(needle_angle) * tail_len
    p_x = -math.sin(needle_angle) * perp
    p_y = math.cos(needle_angle) * perp
    cr.set_source_rgba(0.95, 0.42, 0.08, a)
    cr.move_to(tip_x, tip_y)
    cr.line_to(cx + p_x * 2, cy + p_y * 2)
    cr.line_to(tail_x, tail_y)
    cr.line_to(cx - p_x * 2, cy - p_y * 2)
    cr.close_path()
    cr.fill()

    # Center hub
    cr.set_source_rgb(*face_col)
    cr.arc(cx, cy, r * 0.072, 0, math.tau)
    cr.fill()
    cr.set_line_width(max(1.5, r * 0.018))
    cr.set_source_rgba(0.95, 0.42, 0.08, a)
    cr.arc(cx, cy, r * 0.072, 0, math.tau)
    cr.stroke()

    # Label (title) above value, both grouped in the lower center
    title_sz = max(9.0, r * 0.13)
    val_sz = max(16.0, r * 0.28)
    unit_sz = max(10.0, r * 0.14)
    if title:
        _txt(cr, title, cx, cy + r * 0.22, title_sz, (*dim_col, 0.65 * a), max_w=r * 1.6)
        _txt(cr, val_label, cx, cy + r * 0.22 + title_sz * 0.9 + val_sz * 0.55, val_sz,
             (*text_col, a), bold=True, max_w=r * 1.4)
        _txt(cr, val_unit, cx, cy + r * 0.22 + title_sz * 0.9 + val_sz * 1.22, unit_sz,
             (*dim_col, 0.80 * a), max_w=r * 1.2)
    else:
        _txt(cr, val_label, cx, cy + r * 0.30, val_sz, (*text_col, a), bold=True, max_w=r * 1.4)
        _txt(cr, val_unit, cx, cy + r * 0.30 + val_sz * 0.72, unit_sz,
             (*dim_col, 0.80 * a), max_w=r * 1.2)


def _compass_analog(
    cr: Any,
    cx: float,
    cy: float,
    r: float,
    heading_deg: float,
    heading_str: str,
    active: bool,
    dark: bool = True,
) -> None:
    a = 1.0 if active else 0.28
    face_col = (0.11, 0.12, 0.13) if dark else (0.92, 0.93, 0.95)
    text_col = (0.95, 0.96, 0.98) if dark else (0.08, 0.10, 0.12)
    accent = (0.95, 0.42, 0.08)

    cr.set_source_rgb(*face_col)
    cr.arc(cx, cy, r, 0, math.tau)
    cr.fill()
    cr.set_line_width(max(1.5, r * 0.025))
    cr.set_source_rgba(0.35, 0.38, 0.42, 0.70)
    cr.arc(cx, cy, r, 0, math.tau)
    cr.stroke()

    # Cardinal direction ticks (N S E W)
    for label, ang in (("N", -90), ("E", 0), ("S", 90), ("W", 180)):
        a_rad = math.radians(ang)
        cr.set_line_width(max(1.5, r * 0.025))
        cr.set_source_rgba(*text_col, 0.75 * a)
        cr.move_to(cx + math.cos(a_rad) * r * 0.72, cy + math.sin(a_rad) * r * 0.72)
        cr.line_to(cx + math.cos(a_rad) * r * 0.90, cy + math.sin(a_rad) * r * 0.90)
        cr.stroke()
        lsz = max(8.0, r * 0.18)
        _txt(cr, label,
             cx + math.cos(a_rad) * r * 0.54,
             cy + math.sin(a_rad) * r * 0.54,
             lsz, (*text_col, 0.72 * a))

    # Needle
    needle_angle = math.radians(heading_deg) - math.pi / 2
    nl = r * 0.62
    tl = r * 0.20
    p = r * 0.028
    tip_x = cx + math.cos(needle_angle) * nl
    tip_y = cy + math.sin(needle_angle) * nl
    tail_x = cx - math.cos(needle_angle) * tl
    tail_y = cy - math.sin(needle_angle) * tl
    px, py = -math.sin(needle_angle) * p, math.cos(needle_angle) * p
    cr.set_source_rgba(*accent, a)
    cr.move_to(tip_x, tip_y)
    cr.line_to(cx + px * 2, cy + py * 2)
    cr.line_to(tail_x, tail_y)
    cr.line_to(cx - px * 2, cy - py * 2)
    cr.close_path()
    cr.fill()

    # Center hub
    cr.set_source_rgb(*face_col)
    cr.arc(cx, cy, r * 0.072, 0, math.tau)
    cr.fill()
    cr.set_line_width(max(1.5, r * 0.018))
    cr.set_source_rgba(*accent, a)
    cr.arc(cx, cy, r * 0.072, 0, math.tau)
    cr.stroke()

    sz = max(10.0, r * 0.20)
    _txt(cr, heading_str if active else "--",
         cx, cy + r * 0.30 + sz, sz, (*text_col, 0.80 * a), max_w=r * 1.6)


def _analog_info_bar(cr: Any, width: int, height: int, d: Any, bar_y: float) -> None:
    """Bottom info strip for analog — label above, value below."""
    items: list = []  # (label, value, active)
    if d.heading_active:
        items.append((_translate(d.language, 'dashboard.coolant'), f"{d.coolant_label} °C", d.coolant_active))
    if d.fuel_active:
        items.append((_translate(d.language, 'dashboard.fuel'), d.fuel_label, True))
    if not items or bar_y >= height * 0.96:
        return

    dim = (0.52, 0.55, 0.60)
    bright = (0.88, 0.90, 0.94)
    col_w = width / len(items)
    lbl_sz = max(9.0, height * 0.042)
    val_sz = max(11.0, height * 0.058)

    cr.set_source_rgba(0.22, 0.24, 0.28, 0.45)
    cr.set_line_width(0.5)
    cr.move_to(width * 0.08, bar_y)
    cr.line_to(width * 0.92, bar_y)
    cr.stroke()

    for i, (lbl, val, act) in enumerate(items):
        ia = 1.0 if act else 0.28
        ix = (i + 0.5) * col_w
        _txt(cr, lbl, ix, bar_y + lbl_sz * 1.1, lbl_sz, (*dim, 0.72 * ia))
        _txt(cr, val, ix, bar_y + lbl_sz * 1.1 + val_sz * 1.1, val_sz, (*bright, ia), bold=True)


def _draw_analog_landscape(cr: Any, width: int, height: int, d: Any) -> None:
    r_center = min(height * 0.37, width * 0.22)
    r_right = r_center * 0.58
    r_left = r_center * 0.46

    cy_main = height * 0.48
    cx_center = width * 0.46
    cx_right = cx_center + r_center + r_right * 1.35
    cx_left = cx_center - r_center - r_left * 1.45

    speed_step_maj = 30.0 if d.speed_unit == "km/h" else 20.0
    speed_step_min = 10.0 if d.speed_unit == "km/h" else 10.0

    _analog_gauge(cr, cx_center, cy_main, r_center,
                  d.speed, 0, d.speed_max,
                  d.speed_label, d.speed_unit, "",
                  d.speed_active, speed_step_maj, speed_step_min)

    _analog_gauge(cr, cx_right, cy_main, r_right,
                  d.rpm, 0, d.rpm_max,
                  d.rpm_label, _translate(d.language, "dashboard.rpm.unit"), _translate(d.language, "dashboard.rpm"),
                  d.rpm_active, 1000.0, 500.0)

    if d.heading_active:
        _compass_analog(cr, cx_left, cy_main, r_left,
                        d.heading_deg, d.heading_str, True)
    else:
        _analog_gauge(cr, cx_left, cy_main, r_left,
                      d.coolant, d.coolant_min, d.coolant_max,
                      d.coolant_label, "°C", _translate(d.language, "dashboard.coolant"),
                      d.coolant_active, 20.0, 10.0)

    _analog_info_bar(cr, width, height, d, cy_main + r_center + height * 0.045)


def _draw_analog_portrait(cr: Any, width: int, height: int, d: Any) -> None:
    """Speed top-center (large), RPM + Coolant/Compass below side by side."""
    r_center = min(width * 0.38, height * 0.22)
    r_side = r_center * 0.56

    cx_mid = width * 0.50
    cy_main = r_center + height * 0.04
    cy_side = cy_main + r_center + r_side + height * 0.06
    cx_left = width * 0.28
    cx_right = width * 0.72

    speed_step_maj = 30.0 if d.speed_unit == "km/h" else 20.0
    speed_step_min = 10.0 if d.speed_unit == "km/h" else 10.0

    _analog_gauge(cr, cx_mid, cy_main, r_center,
                  d.speed, 0, d.speed_max,
                  d.speed_label, d.speed_unit, "",
                  d.speed_active, speed_step_maj, speed_step_min)

    _analog_gauge(cr, cx_left, cy_side, r_side,
                  d.rpm, 0, d.rpm_max,
                  d.rpm_label, _translate(d.language, "dashboard.rpm.unit"), _translate(d.language, "dashboard.rpm"),
                  d.rpm_active, 1000.0, 500.0)

    if d.heading_active:
        _compass_analog(cr, cx_right, cy_side, r_side,
                        d.heading_deg, d.heading_str, True)
    else:
        _analog_gauge(cr, cx_right, cy_side, r_side,
                      d.coolant, d.coolant_min, d.coolant_max,
                      d.coolant_label, "°C", _translate(d.language, "dashboard.coolant"),
                      d.coolant_active, 20.0, 10.0)

    _analog_info_bar(cr, width, height, d, cy_side + r_side + height * 0.04)


def draw(cr: Any, width: int, height: int, data: Any) -> None:
    cr.set_source_rgb(0.05, 0.05, 0.06)
    cr.paint()
    if width >= height:
        _draw_analog_landscape(cr, width, height, data)
    else:
        _draw_analog_portrait(cr, width, height, data)
