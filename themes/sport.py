"""Sport dashboard theme for DrivePulse — rainbow L-fin HUD."""
from typing import Any

THEME_TYPE = "dashboard"
LABEL = {"en": "Sport", "de": "Sport"}
CSS = """
window.dp-theme-sport .dp-gauge-bg,
window.dp-theme-sport .dp-gauge-bg > viewport,
window.dp-theme-sport .dp-gauge-bg > * {
  background-color: #030508;
}"""

from draw_helpers import _txt, _norm
from common import _translate

_N_FINS = 48


def _palette(dark: bool) -> dict:
    if dark:
        return dict(
            bg=(0.012, 0.016, 0.024),
            src=(0.50, 0.58, 0.66),
            big=(1.0, 1.0, 1.0),
            unit=(0.52, 0.60, 0.68),
            card_bg=(0.06, 0.08, 0.11, 0.58),
            label_dim=0.85,
            inactive_fin=0.13,
        )
    return dict(
        bg=(0.94, 0.95, 0.96),
        src=(0.35, 0.42, 0.50),
        big=(0.06, 0.08, 0.12),
        unit=(0.30, 0.36, 0.44),
        card_bg=(1.0, 1.0, 1.0, 0.85),
        label_dim=0.95,
        inactive_fin=0.22,
    )


def _rainbow(frac: float) -> tuple:
    """HSV hue: yellow-green (72°) → red → purple (280°), frac 0→1."""
    h  = (72.0 - frac * 152.0) % 360.0
    hi = int(h / 60) % 6
    f  = h / 60.0 - int(h / 60.0)
    p, q, t = 0.0, 1.0 - f, f
    return [(1.0, t, p), (q, 1.0, p), (p, 1.0, t),
            (p, q, 1.0), (t, p, 1.0), (1.0, p, q)][hi]


def _l_gauge(
    cr: Any, gx: float, gy: float, gw: float, gh: float,
    arm_t: float, d: Any, pal: dict,
) -> None:
    """Rainbow L-fin gauge in rect (gx, gy, gw, gh).

    Left arm : vertical fin strip at x=gx, spans gy → gy+gh-arm_t.
    Bottom arm: horizontal fin strip at y=gy+gh-arm_t, spans gx+arm_t → gx+gw.
    Corner at (gx+arm_t, gy+gh-arm_t).  frac=0 at corner, increases outward.
    Fins are lit up to the current speed norm.
    """
    a    = 1.0 if d.speed_active else 0.32
    norm = _norm(d.speed, 0, d.speed_max)

    corner_x = gx + arm_t
    corner_y = gy + gh - arm_t
    vert_h   = gh - arm_t
    horiz_w  = gw - arm_t

    # ── Left arm: horizontal fins stacked from corner upward ─────────────────
    spacing_v = vert_h / _N_FINS
    lw_v      = max(1.5, spacing_v * 0.65)
    cr.set_line_cap(1)
    for i in range(_N_FINS):
        frac  = i / (_N_FINS - 1)
        y     = corner_y - i * spacing_v - spacing_v * 0.5
        rgb   = _rainbow(frac)
        alpha = (0.95 if frac <= norm else pal["inactive_fin"]) * a
        cr.set_line_width(lw_v)
        cr.set_source_rgba(*rgb, alpha)
        cr.move_to(gx + 3,         y)
        cr.line_to(gx + arm_t - 3, y)
        cr.stroke()

    # ── Bottom arm: vertical fins from corner rightward ───────────────────────
    spacing_h = horiz_w / _N_FINS
    lw_h      = max(1.5, spacing_h * 0.65)
    for j in range(_N_FINS):
        frac  = j / (_N_FINS - 1)
        x     = corner_x + j * spacing_h + spacing_h * 0.5
        rgb   = _rainbow(frac)
        alpha = (0.95 if frac <= norm else pal["inactive_fin"]) * a
        cr.set_line_width(lw_h)
        cr.set_source_rgba(*rgb, alpha)
        cr.move_to(x, corner_y + 3)
        cr.line_to(x, gy + gh - 3)
        cr.stroke()

    # ── Speed text: centered in the open interior of the L ───────────────────
    text_cx  = (corner_x + gx + gw) / 2
    text_cy  = (gy + corner_y) / 2
    inner_sz = min(vert_h, horiz_w)

    if d.speed_source:
        src_sz = max(8.0, inner_sz * 0.075)
        _txt(cr, d.speed_source, text_cx, text_cy - inner_sz * 0.22,
             src_sz, (*pal["src"], 0.65 * a))

    val_sz = max(28.0, min(inner_sz * 0.28, 108.0))
    _txt(cr, d.speed_label, text_cx, text_cy, val_sz,
         (*pal["big"], a), bold=True, max_w=horiz_w * 0.86)

    unit_sz = max(11.0, inner_sz * 0.085)
    _txt(cr, d.speed_unit, text_cx, text_cy + val_sz * 0.60,
         unit_sz, (*pal["unit"], 0.85 * a))


def _info_panel(cr: Any, x: float, y: float, w: float, h: float, d: Any, pal: dict) -> None:
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

        cr.set_source_rgba(*pal["card_bg"])
        cr.rectangle(x, cy0 + card_h * 0.04, w, card_h * 0.92)
        cr.fill()

        bar_w = max(3.0, w * 0.028)
        cr.set_source_rgba(*color, 0.78 * a)
        cr.rectangle(x, cy0 + card_h * 0.10, bar_w, card_h * 0.80)
        cr.fill()

        lbl_sz  = max(9.0,  min(card_h * 0.20, 28.0))
        val_sz  = max(18.0, min(card_h * 0.48, 72.0))
        cx_card = x + w * 0.55

        _txt(cr, label, cx_card, cy0 + card_h * 0.28,
             lbl_sz, (*color, 0.85 * a))
        _txt(cr, value, cx_card, cy0 + card_h * 0.72,
             val_sz, (*pal["big"], a), bold=True, max_w=w * 0.88)


# ── Landscape ─────────────────────────────────────────────────────────────────

def _draw_sport_landscape(cr: Any, width: int, height: int, d: Any, pal: dict) -> None:
    gauge_w = width * 0.62
    pad     = height * 0.04
    arm_t   = max(24.0, min(gauge_w * 0.064, height * 0.072, 52.0))

    _l_gauge(cr, 0, 0, gauge_w, height, arm_t, d, pal)

    info_x = gauge_w + pad * 0.5
    _info_panel(cr, info_x, pad, width - info_x - pad, height - 2 * pad, d, pal)


# ── Portrait ──────────────────────────────────────────────────────────────────

def _draw_sport_portrait(cr: Any, width: int, height: int, d: Any, pal: dict) -> None:
    pad     = width * 0.04
    gauge_h = height * 0.52
    arm_t   = max(20.0, min(width * 0.064, gauge_h * 0.072, 46.0))

    _l_gauge(cr, 0, 0, width, gauge_h, arm_t, d, pal)

    info_y = gauge_h + pad
    _info_panel(cr, pad, info_y, width - 2 * pad, height - info_y - pad, d, pal)


# ── Entry point ───────────────────────────────────────────────────────────────

def _draw_impl(cr: Any, width: int, height: int, data: Any, dark: bool) -> None:
    pal = _palette(dark)
    cr.set_source_rgb(*pal["bg"])
    cr.paint()
    if width >= height:
        _draw_sport_landscape(cr, width, height, data, pal)
    else:
        _draw_sport_portrait(cr, width, height, data, pal)


def draw(cr: Any, width: int, height: int, data: Any) -> None:
    _draw_impl(cr, width, height, data, dark=True)
