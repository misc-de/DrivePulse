"""Racing dashboard theme for DrivePulse."""
import math
from typing import Any

THEME_TYPE = "dashboard"
LABEL = {"en": "Racing", "de": "Racing"}
CSS = """
window.dp-theme-racing,
window.dp-theme-racing toolbarview,
window.dp-theme-racing scrolledwindow,
window.dp-theme-racing scrolledwindow > viewport,
window.dp-theme-racing .dp-gauge-bg,
window.dp-theme-racing .dp-gauge-bg > * {
  background-color: #0a0803;
}"""

from draw_helpers import _txt, _norm, _arc_track, _cardinal, _ARC_START, _ARC_END, _ARC_SPAN
from common import _translate


def _ring_circle(
    cr: Any,
    cx: float,
    cy: float,
    r: float,
    lw: float,
    norm: float,
    fill_rgb: tuple,
    label: str,
    sublabel: str,
    label_size: float,
    sub_size: float,
    active: bool,
    title: str = "",
    title_size: float = 0,
) -> None:
    a = 1.0 if active else 0.28
    track = (0.18, 0.20, 0.25, 0.55)
    fill = (*fill_rgb, 0.92 * a)

    # Background disc
    cr.set_source_rgba(0.04, 0.06, 0.10, 0.70)
    cr.arc(cx, cy, r + lw * 0.9, 0, math.tau)
    cr.fill()

    _arc_track(cr, cx, cy, r, lw, _ARC_START, _ARC_END, track, fill, norm)

    # Center value
    _txt(cr, label, cx, cy - label_size * 0.12, label_size,
         (1, 1, 1, a), bold=True, max_w=r * 1.5)
    if sublabel:
        _txt(cr, sublabel, cx, cy + sub_size * 1.1, sub_size,
             (*fill_rgb, 0.80 * a), max_w=r * 1.4)
    if title:
        _txt(cr, title, cx, cy + r * 0.66 + title_size, title_size,
             (0.55, 0.60, 0.66, 0.60 * a), max_w=r * 1.8)


def _compass_circle(
    cr: Any,
    cx: float,
    cy: float,
    r: float,
    lw: float,
    deg: float,
    heading_str: str,
    accent: tuple,
    active: bool,
) -> None:
    a = 1.0 if active else 0.28
    cr.set_source_rgba(0.04, 0.06, 0.10, 0.70)
    cr.arc(cx, cy, r + lw * 0.9, 0, math.tau)
    cr.fill()

    # Thin outer ring
    cr.set_line_width(lw * 0.28)
    cr.set_source_rgba(*accent, 0.35 * a)
    cr.arc(cx, cy, r + lw * 0.28, 0, math.tau)
    cr.stroke()

    # N indicator tick at top
    cr.set_line_width(lw * 0.55)
    cr.set_source_rgba(*accent, 0.80 * a)
    cr.move_to(cx, cy - r + lw * 0.5)
    cr.line_to(cx, cy - r - lw * 0.8)
    cr.stroke()

    # Compass needle pointing toward heading_deg (north=up)
    needle_r = r * 0.60
    angle = math.radians(deg) - math.pi / 2
    # Arrow head
    tip_x = cx + math.cos(angle) * needle_r
    tip_y = cy + math.sin(angle) * needle_r
    tail_x = cx - math.cos(angle) * needle_r * 0.40
    tail_y = cy - math.sin(angle) * needle_r * 0.40
    perp_x = -math.sin(angle) * lw * 0.60
    perp_y = math.cos(angle) * lw * 0.60
    cr.set_source_rgba(*accent, 0.92 * a)
    cr.move_to(tip_x, tip_y)
    cr.line_to(cx + perp_x, cy + perp_y)
    cr.line_to(tail_x, tail_y)
    cr.line_to(cx - perp_x, cy - perp_y)
    cr.close_path()
    cr.fill()

    # Center hub
    cr.set_source_rgba(0.15, 0.18, 0.22, 1.0)
    cr.arc(cx, cy, lw * 0.65, 0, math.tau)
    cr.fill()
    cr.set_source_rgba(*accent, 0.7 * a)
    cr.arc(cx, cy, lw * 0.65, 0, math.tau)
    cr.set_line_width(1.0)
    cr.stroke()

    sz = r * 0.28
    _txt(cr, heading_str if active else "--", cx, cy + r * 0.55 + sz,
         sz, (0.85, 0.88, 0.92, 0.80 * a), max_w=r * 1.7)


def _draw_rings_landscape(
    cr: Any, width: int, height: int, d: Any,
    accent: tuple, sec_accent: tuple,
) -> None:
    """Three circles side by side: RPM — Speed(large) — Coolant/Compass."""
    r_main = min(height * 0.37, width * 0.22)
    lw_main = r_main * 0.28
    r_side = r_main * 0.56
    lw_side = r_side * 0.26

    cy = height * 0.47
    cx_main = width * 0.47
    cx_left  = cx_main - r_main - r_side * 1.30
    cx_right = cx_main + r_main + r_side * 1.30

    _ring_circle(cr, cx_left, cy, r_side, lw_side,
                 _norm(d.rpm, 0, d.rpm_max), sec_accent,
                 d.rpm_label, _translate(d.language, "dashboard.rpm.unit"), r_side * 0.68, r_side * 0.30,
                 d.rpm_active, title=_translate(d.language, "dashboard.rpm"), title_size=r_side * 0.22)

    _ring_circle(cr, cx_main, cy, r_main, lw_main,
                 _norm(d.speed, 0, d.speed_max), accent,
                 d.speed_label, d.speed_unit, r_main * 0.68, r_main * 0.28,
                 d.speed_active)

    if d.heading_active:
        _compass_circle(cr, cx_right, cy, r_side, lw_side,
                        d.heading_deg, d.heading_str, sec_accent, True)
    else:
        _ring_circle(cr, cx_right, cy, r_side, lw_side,
                     _norm(d.coolant, d.coolant_min, d.coolant_max), sec_accent,
                     d.coolant_label, "°C", r_side * 0.68, r_side * 0.30,
                     d.coolant_active, title=_translate(d.language, "dashboard.coolant"), title_size=r_side * 0.22)

    # Bottom info strip
    info_y = cy + r_main + lw_main + height * 0.065
    if info_y < height * 0.92:
        _draw_rings_info(cr, width, height, d, info_y)


def _draw_rings_portrait(
    cr: Any, width: int, height: int, d: Any,
    accent: tuple, sec_accent: tuple,
) -> None:
    """Speed circle top-center (large), RPM + Coolant/Compass below side by side."""
    # Speed circle fills upper ~45 % of height
    r_main = min(width * 0.38, height * 0.22)
    lw_main = r_main * 0.28

    r_side = r_main * 0.56
    lw_side = r_side * 0.26

    cx_mid = width * 0.50
    # Speed: vertically centered in the top half
    cy_main = r_main + lw_main + height * 0.04
    # Side circles: below speed, side by side
    cy_side = cy_main + r_main + lw_main + r_side + lw_side + height * 0.04
    cx_left  = width * 0.28
    cx_right = width * 0.72

    _ring_circle(cr, cx_mid, cy_main, r_main, lw_main,
                 _norm(d.speed, 0, d.speed_max), accent,
                 d.speed_label, d.speed_unit, r_main * 0.68, r_main * 0.28,
                 d.speed_active)

    _ring_circle(cr, cx_left, cy_side, r_side, lw_side,
                 _norm(d.rpm, 0, d.rpm_max), sec_accent,
                 d.rpm_label, _translate(d.language, "dashboard.rpm.unit"), r_side * 0.68, r_side * 0.30,
                 d.rpm_active, title=_translate(d.language, "dashboard.rpm"), title_size=r_side * 0.22)

    if d.heading_active:
        _compass_circle(cr, cx_right, cy_side, r_side, lw_side,
                        d.heading_deg, d.heading_str, sec_accent, True)
    else:
        _ring_circle(cr, cx_right, cy_side, r_side, lw_side,
                     _norm(d.coolant, d.coolant_min, d.coolant_max), sec_accent,
                     d.coolant_label, "°C", r_side * 0.68, r_side * 0.30,
                     d.coolant_active, title=_translate(d.language, "dashboard.coolant"), title_size=r_side * 0.22)

    # Info strip below side circles
    info_y = cy_side + r_side + lw_side + height * 0.04
    if info_y < height * 0.94:
        _draw_rings_info(cr, width, height, d, info_y)


def _draw_rings_info(cr: Any, width: int, height: int, d: Any, info_y: float) -> None:
    """Shared bottom info strip for both ring layouts."""
    items: list = []
    if d.heading_active:
        items.append((_translate(d.language, "dashboard.coolant"), f"{d.coolant_label} °C", d.coolant_active))
    if d.fuel_active:
        items.append((_translate(d.language, "dashboard.fuel"), d.fuel_label, True))
    if not d.heading_active:
        items.append((_translate(d.language, "dashboard.heading"), d.heading_str or "--", d.heading_active))
    col_w = width / max(len(items), 1)
    for i, (name, val, act) in enumerate(items):
        ia = 1.0 if act else 0.25
        ix = (i + 0.5) * col_w
        if name:
            _txt(cr, name, ix, info_y, height * 0.050,
                 (0.45, 0.50, 0.56, 0.65 * ia))
        _txt(cr, val, ix, info_y + height * 0.065, height * 0.062,
             (0.88, 0.91, 0.95, ia), bold=bool(val and val != "--"))


def draw(cr: Any, width: int, height: int, data: Any) -> None:
    bg = (0.04, 0.03, 0.01)
    cr.set_source_rgb(*bg)
    cr.paint()
    accent = (0.95, 0.42, 0.08)
    sec_accent = accent
    if width >= height:
        _draw_rings_landscape(cr, width, height, data, accent, sec_accent)
    else:
        _draw_rings_portrait(cr, width, height, data, accent, sec_accent)
