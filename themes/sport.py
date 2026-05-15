"""Sport dashboard theme for DrivePulse — rainbow sweep arc HUD."""
import math
from typing import Any

THEME_TYPE = "dashboard"
LABEL = {"en": "Sport", "de": "Sport"}
CSS = """
window.dp-theme-sport,
window.dp-theme-sport toolbarview,
window.dp-theme-sport scrolledwindow,
window.dp-theme-sport scrolledwindow > viewport,
window.dp-theme-sport .dp-gauge-bg,
window.dp-theme-sport .dp-gauge-bg > * {
  background-color: #030508;
}"""

from draw_helpers import _txt, _norm
from common import _translate

_ARC_START = math.radians(135)
_ARC_SPAN  = math.radians(270)
_ARC_END   = _ARC_START + _ARC_SPAN

_N_FINS = 60


def _rainbow(frac: float) -> tuple:
    """HSV hue: yellow-green (72°) → red → purple (280°), frac 0→1."""
    h  = (72.0 - frac * 152.0) % 360.0
    hi = int(h / 60) % 6
    f  = h / 60.0 - int(h / 60.0)
    p, q, t = 0.0, 1.0 - f, f
    return [(1.0, t, p), (q, 1.0, p), (p, 1.0, t),
            (p, q, 1.0), (t, p, 1.0), (1.0, p, q)][hi]


def _fin_gauge(cr: Any, cx: float, cy: float, r: float, d: Any) -> None:
    a    = 1.0 if d.speed_active else 0.32
    norm = _norm(d.speed, 0, d.speed_max)

    r_inner = r * 0.72
    r_outer = r * 0.96

    # Rainbow fins
    for i in range(_N_FINS):
        frac  = i / (_N_FINS - 1)
        angle = _ARC_START + _ARC_SPAN * frac
        rgb   = _rainbow(frac)
        alpha = (0.95 if frac <= norm + 0.005 else 0.14) * a
        major = (i % 10 == 0)
        lw    = max(2.8, r * 0.026) if major else max(1.2, r * 0.013)
        cr.set_line_width(lw)
        cr.set_line_cap(0)
        cr.set_source_rgba(*rgb, alpha)
        cr.move_to(cx + math.cos(angle) * r_inner,
                   cy + math.sin(angle) * r_inner)
        cr.line_to(cx + math.cos(angle) * r_outer,
                   cy + math.sin(angle) * r_outer)
        cr.stroke()

    # Scale labels in matching rainbow colors
    step    = 60.0 if d.speed_unit == "km/h" else 30.0
    n_steps = int(round(d.speed_max / step))
    lbl_r   = r * 1.12
    lbl_sz  = max(8.0, r * 0.11)
    for i in range(n_steps + 1):
        frac  = (i * step) / max(1.0, d.speed_max)
        angle = _ARC_START + _ARC_SPAN * frac
        rgb   = _rainbow(frac)
        txt   = str(int(i * step))
        cr.select_font_face("Cantarell", 0, 0)
        cr.set_font_size(lbl_sz)
        ext = cr.text_extents(txt)
        nx  = cx + math.cos(angle) * lbl_r - ext.width / 2 - ext.x_bearing
        ny  = cy + math.sin(angle) * lbl_r - ext.height / 2 - ext.y_bearing
        cr.set_source_rgba(*rgb, 0.90 * a)
        cr.move_to(nx, ny)
        cr.show_text(txt)

    # Inner disc for text contrast
    cr.set_source_rgba(0.03, 0.04, 0.07, 0.84)
    cr.arc(cx, cy, r_inner * 0.92, 0, math.tau)
    cr.fill()

    # Speed source
    if d.speed_source:
        _txt(cr, d.speed_source, cx, cy - r * 0.28,
             max(8.0, r * 0.11), (0.50, 0.58, 0.66, 0.65 * a))

    # Speed value
    val_sz = max(28.0, r * 0.48)
    _txt(cr, d.speed_label, cx, cy - r * 0.02, val_sz,
         (1.0, 1.0, 1.0, a), bold=True, max_w=r_inner * 1.72)

    # Unit
    unit_sz = max(11.0, r * 0.14)
    _txt(cr, d.speed_unit, cx, cy + r * 0.26, unit_sz,
         (0.52, 0.60, 0.68, 0.85 * a))


def _info_panel(cr: Any, x: float, y: float, w: float, h: float, d: Any) -> None:
    items: list[tuple] = [
        (_translate(d.language, "dashboard.rpm.unit").upper(),
         d.rpm_label, (0.95, 0.50, 0.08), d.rpm_active),
        ("°C", d.coolant_label, (0.22, 0.62, 1.00), d.coolant_active),
    ]
    if d.fuel_active:
        items.append((_translate(d.language, "dashboard.fuel").upper(),
                      d.fuel_label, (0.28, 0.88, 0.42), True))
    if d.heading_active:
        items.append(("GPS", d.heading_str or "--", (0.35, 0.90, 0.45), True))

    n      = len(items)
    card_h = h / n

    for i, (label, value, color, active) in enumerate(items):
        a   = 1.0 if active else 0.28
        cy0 = y + i * card_h

        cr.set_source_rgba(0.06, 0.08, 0.11, 0.58)
        cr.rectangle(x, cy0 + card_h * 0.04, w, card_h * 0.92)
        cr.fill()

        bar_w = max(3.0, w * 0.028)
        cr.set_source_rgba(*color, 0.78 * a)
        cr.rectangle(x, cy0 + card_h * 0.10, bar_w, card_h * 0.80)
        cr.fill()

        lbl_sz  = max(9.0, min(card_h * 0.20, 28.0))
        val_sz  = max(18.0, min(card_h * 0.48, 72.0))
        cx_card = x + w * 0.55

        _txt(cr, label, cx_card, cy0 + card_h * 0.28,
             lbl_sz, (*color, 0.85 * a))
        _txt(cr, value, cx_card, cy0 + card_h * 0.72,
             val_sz, (1.0, 1.0, 1.0, a), bold=True, max_w=w * 0.88)


# ── Landscape ─────────────────────────────────────────────────────────────────

def _draw_sport_landscape(cr: Any, width: int, height: int, d: Any) -> None:
    gauge_w = width * 0.62
    pad     = height * 0.04
    r       = min(height * 0.38, gauge_w * 0.50)
    # Horizontally center the arc bounding box within gauge_w
    cx      = (gauge_w + r * 0.293) / 2
    # Vertically center the arc bounding box within height
    cy      = height / 2 + r * 0.146

    _fin_gauge(cr, cx, cy, r, d)

    info_x = gauge_w + pad * 0.5
    _info_panel(cr, info_x, pad, width - info_x - pad, height - 2 * pad, d)


# ── Portrait ──────────────────────────────────────────────────────────────────

def _draw_sport_portrait(cr: Any, width: int, height: int, d: Any) -> None:
    pad = width * 0.04
    r   = min(width * 0.40, height * 0.26)
    # Horizontally center the arc bounding box
    cx  = (width + r * 0.293) / 2
    # Top of topmost scale label at pad from top edge
    cy  = r * 1.12 + pad

    _fin_gauge(cr, cx, cy, r, d)

    # Info starts just below the lowest arc point (at 135°: cy + r*sin(135°)*1.12)
    info_y = cy + r * 0.83 + pad
    _info_panel(cr, pad, info_y, width - 2 * pad, height - info_y - pad, d)


# ── Entry point ───────────────────────────────────────────────────────────────

def draw(cr: Any, width: int, height: int, data: Any) -> None:
    cr.set_source_rgb(0.012, 0.016, 0.024)
    cr.paint()
    if width >= height:
        _draw_sport_landscape(cr, width, height, data)
    else:
        _draw_sport_portrait(cr, width, height, data)
