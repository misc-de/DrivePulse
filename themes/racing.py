"""Racing dashboard theme for DrivePulse."""
import math
from typing import Any

THEME_TYPE = "dashboard"
LABEL = {"en": "Racing", "de": "Racing"}
CSS = """
window.dp-theme-racing .dp-gauge-bg,
window.dp-theme-racing .dp-gauge-bg > viewport,
window.dp-theme-racing .dp-gauge-bg > * {
  background-color: #0a0803;
}"""

from draw_helpers import _txt, _norm, _arc_track, _cardinal, _ARC_START, _ARC_END, _ARC_SPAN, _draw_last_trip_strip
from common import _translate


def _palette(dark: bool) -> dict:
    if dark:
        return dict(
            bg=(0.04, 0.03, 0.01),
            disc=(0.04, 0.06, 0.10, 0.70),
            track=(0.18, 0.20, 0.25, 0.55),
            big=(1.0, 1.0, 1.0),
            dim=(0.55, 0.60, 0.66),
            info_label=(0.45, 0.50, 0.56),
            info_value=(0.88, 0.91, 0.95),
        )
    return dict(
        bg=(0.94, 0.95, 0.96),
        disc=(1.0, 1.0, 1.0, 0.85),
        track=(0.78, 0.80, 0.84, 0.75),
        big=(0.06, 0.08, 0.12),
        dim=(0.35, 0.40, 0.46),
        info_label=(0.38, 0.42, 0.48),
        info_value=(0.08, 0.10, 0.14),
    )


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
    pal: dict,
    title: str = "",
    title_size: float = 0,
    source: str = "",
) -> None:
    a = 1.0 if active else 0.28
    fill = (*fill_rgb, 0.92 * a)

    # Background disc
    cr.set_source_rgba(*pal["disc"])
    cr.arc(cx, cy, r + lw * 0.9, 0, math.tau)
    cr.fill()

    _arc_track(cr, cx, cy, r, lw, _ARC_START, _ARC_END, pal["track"], fill, norm)

    # Speed source (OBD / GPS) above center
    if source:
        src_sz = max(8.0, r * 0.18)
        _txt(cr, source, cx, cy - r * 0.52, src_sz,
             (*pal["dim"], 0.55 * a), max_w=r * 1.4)

    # Center value
    _txt(cr, label, cx, cy - label_size * 0.12, label_size,
         (*pal["big"], a), bold=True, max_w=r * 1.5)
    if sublabel:
        _txt(cr, sublabel, cx, cy + sub_size * 2.2, sub_size,
             (*fill_rgb, 0.80 * a), max_w=r * 1.4)
    if title:
        _txt(cr, title, cx, cy + r * 0.66 + title_size, title_size,
             (*pal["dim"], 0.60 * a), max_w=r * 1.8)




def _draw_rings_landscape(
    cr: Any, width: int, height: int, d: Any,
    accent: tuple, sec_accent: tuple, pal: dict,
) -> None:
    """Three circles side by side: RPM — Speed(large) — Coolant/Compass."""
    r_main = min(height * 0.37, width * 0.22)
    lw_main = r_main * 0.28
    r_side = r_main * 0.56
    lw_side = r_side * 0.26

    cy = height * 0.47
    cx_main = width * 0.50
    cx_left  = cx_main - r_main - r_side * 1.30
    cx_right = cx_main + r_main + r_side * 1.30

    _ring_circle(cr, cx_left, cy, r_side, lw_side,
                 _norm(d.rpm, 0, d.rpm_max), sec_accent,
                 d.rpm_label, _translate(d.language, "dashboard.rpm.unit"), r_side * 0.68, r_side * 0.30,
                 d.rpm_active, pal)

    _ring_circle(cr, cx_main, cy, r_main, lw_main,
                 _norm(d.speed, 0, d.speed_max), accent,
                 d.speed_label, d.speed_unit, r_main * 0.68, r_main * 0.28,
                 d.speed_active, pal, source=d.speed_source)

    _ring_circle(cr, cx_right, cy, r_side, lw_side,
                 _norm(d.coolant, 0.0, 130.0), sec_accent,
                 d.coolant_label, "°C", r_side * 0.68, r_side * 0.30,
                 d.coolant_active, pal)

    # Bottom info strip
    info_y = cy + r_main + lw_main + height * 0.065
    if info_y < height * 0.92:
        _draw_rings_info(cr, width, height, d, info_y, pal)


def _draw_rings_portrait(
    cr: Any, width: int, height: int, d: Any,
    accent: tuple, sec_accent: tuple, pal: dict,
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
                 d.speed_active, pal, source=d.speed_source)

    _ring_circle(cr, cx_left, cy_side, r_side, lw_side,
                 _norm(d.rpm, 0, d.rpm_max), sec_accent,
                 d.rpm_label, _translate(d.language, "dashboard.rpm.unit"), r_side * 0.68, r_side * 0.30,
                 d.rpm_active, pal)

    _ring_circle(cr, cx_right, cy_side, r_side, lw_side,
                 _norm(d.coolant, 0.0, 130.0), sec_accent,
                 d.coolant_label, "°C", r_side * 0.68, r_side * 0.30,
                 d.coolant_active, pal)

    # Info strip below side circles
    info_y = cy_side + r_side + lw_side + height * 0.04
    if info_y < height * 0.94:
        _draw_rings_info(cr, width, height, d, info_y, pal)


def _draw_rings_info(cr: Any, width: int, height: int, d: Any, info_y: float, pal: dict) -> None:
    """Shared bottom info strip for both ring layouts."""
    items: list = []
    if d.fuel_active:
        items.append((_translate(d.language, "dashboard.fuel"), d.fuel_label, True))
    col_w = width / max(len(items), 1)
    for i, (name, val, act) in enumerate(items):
        ia = 1.0 if act else 0.25
        ix = (i + 0.5) * col_w
        if name:
            _txt(cr, name, ix, info_y, height * 0.050,
                 (*pal["info_label"], 0.65 * ia))
        _txt(cr, val, ix, info_y + height * 0.065, height * 0.062,
             (*pal["info_value"], ia), bold=bool(val and val != "--"))


def _draw_impl(cr: Any, width: int, height: int, data: Any, dark: bool) -> None:
    pal = _palette(dark)
    cr.set_source_rgb(*pal["bg"])
    cr.paint()
    accent = (0.95, 0.42, 0.08)
    sec_accent = accent
    strip_h = max(28.0, height * 0.072)
    if width >= height:
        _draw_rings_landscape(cr, width, height - strip_h, data, accent, sec_accent, pal)
    else:
        _draw_rings_portrait(cr, width, height - strip_h, data, accent, sec_accent, pal)
    _draw_last_trip_strip(cr, 0, height - strip_h, width, strip_h, data)


def draw(cr: Any, width: int, height: int, data: Any) -> None:
    _draw_impl(cr, width, height, data, dark=True)
