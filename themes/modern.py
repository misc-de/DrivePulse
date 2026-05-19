"""Modern dashboard theme for DrivePulse — automotive HUD style."""
import math
import cairo
from typing import Any

THEME_TYPE = "dashboard"
LABEL = {"en": "Modern", "de": "Modern"}
CSS = """
window.dp-theme-modern .dp-gauge-bg,
window.dp-theme-modern .dp-gauge-bg > viewport,
window.dp-theme-modern .dp-gauge-bg > * {
  background-color: #09090d;
}"""

from draw_helpers import _txt, _norm, _draw_last_trip_strip
from common import _translate

_CYAN   = (0.22, 0.74, 0.92)
_WHITE  = (0.96, 0.97, 1.00)
_DIM    = (0.42, 0.46, 0.52)
_ORANGE = (0.95, 0.42, 0.08)
_RED    = (0.90, 0.12, 0.08)

_ARC_START = math.radians(135)
_ARC_SPAN  = math.radians(270)
_ARC_END   = _ARC_START + _ARC_SPAN


def _palette(dark: bool) -> dict:
    if dark:
        return dict(
            bg=(0.036, 0.036, 0.052),
            ring_grad=((0.13, 0.14, 0.18, 1.0), (0.03, 0.04, 0.05, 1.0)),
            chrome=(0.78, 0.82, 0.88, 0.60),
            chrome_hi=(1.0, 1.0, 1.0, 0.18),
            track=(0.10, 0.12, 0.16, 0.90),
            tick=(0.82, 0.85, 0.90),
            face_grad=((0.16, 0.17, 0.20, 1.0), (0.05, 0.06, 0.08, 1.0)),
            face_ring=(0.30, 0.34, 0.40, 0.50),
            dim=_DIM,
            big=_WHITE,
            accent=_CYAN,
            card_bg=(0.07, 0.09, 0.12, 0.55),
        )
    return dict(
        bg=(0.94, 0.95, 0.96),
        ring_grad=((0.96, 0.97, 0.99, 1.0), (0.82, 0.84, 0.88, 1.0)),
        chrome=(0.45, 0.48, 0.54, 0.70),
        chrome_hi=(0.30, 0.34, 0.40, 0.22),
        track=(0.78, 0.80, 0.84, 0.90),
        tick=(0.30, 0.34, 0.40),
        face_grad=((1.0, 1.0, 1.0, 1.0), (0.90, 0.92, 0.95, 1.0)),
        face_ring=(0.55, 0.58, 0.62, 0.60),
        dim=(0.35, 0.40, 0.46),
        big=(0.06, 0.08, 0.12),
        accent=(0.10, 0.45, 0.70),
        card_bg=(1.0, 1.0, 1.0, 0.80),
    )


# ── Speed ring ────────────────────────────────────────────────────────────────

def _speed_ring(cr: Any, cx: float, cy: float, r: float, d: Any, pal: dict) -> None:
    a    = 1.0 if d.speed_active else 0.32
    norm = _norm(d.speed, 0, d.speed_max)
    val_angle = _ARC_START + _ARC_SPAN * norm

    # Outer disc with radial depth gradient
    grad_bg = cairo.RadialGradient(cx, cy - r * 0.25, r * 0.08, cx, cy, r)
    grad_bg.add_color_stop_rgba(0.0, *pal["ring_grad"][0])
    grad_bg.add_color_stop_rgba(1.0, *pal["ring_grad"][1])
    cr.set_source(grad_bg)
    cr.arc(cx, cy, r, 0, math.tau)
    cr.fill()

    # Outer chrome ring
    cr.set_line_cap(0)
    cr.set_line_width(max(2.5, r * 0.024))
    ch = pal["chrome"]
    cr.set_source_rgba(ch[0], ch[1], ch[2], ch[3] * a)
    cr.arc(cx, cy, r * 0.974, 0, math.tau)
    cr.stroke()

    # Inner chrome highlight
    cr.set_line_width(max(1.0, r * 0.010))
    chi = pal["chrome_hi"]
    cr.set_source_rgba(chi[0], chi[1], chi[2], chi[3] * a)
    cr.arc(cx, cy, r * 0.952, 0, math.tau)
    cr.stroke()

    # Arc track (background)
    arc_r = r * 0.865
    lw    = r * 0.076
    cr.set_line_width(lw)
    cr.set_line_cap(1)
    cr.set_source_rgba(*pal["track"])
    cr.arc(cx, cy, arc_r, _ARC_START, _ARC_END)
    cr.stroke()

    # Filled arc: orange → red gradient along the sweep
    if norm > 0.004:
        sx = cx + math.cos(_ARC_START) * arc_r
        sy = cy + math.sin(_ARC_START) * arc_r
        ex = cx + math.cos(_ARC_END)   * arc_r
        ey = cy + math.sin(_ARC_END)   * arc_r
        grad_arc = cairo.LinearGradient(sx, sy, ex, ey)
        grad_arc.add_color_stop_rgba(0.00, *_ORANGE, 0.95 * a)
        grad_arc.add_color_stop_rgba(0.60, 0.95, 0.28, 0.06, 0.95 * a)
        grad_arc.add_color_stop_rgba(1.00, *_RED,    0.95 * a)
        cr.set_source(grad_arc)
        cr.arc(cx, cy, arc_r, _ARC_START, val_angle)
        cr.stroke()

    # Tick marks
    for s in range(51):
        frac  = s / 50
        angle = _ARC_START + _ARC_SPAN * frac
        major = (s % 5 == 0)
        outer_r = r * 0.942
        inner_r = r * (0.864 if major else 0.905)
        cr.set_line_width(max(0.8, r * (0.018 if major else 0.008)))
        cr.set_line_cap(0)
        cr.set_source_rgba(*pal["tick"], (0.80 if major else 0.38) * a)
        cr.move_to(cx + math.cos(angle) * inner_r, cy + math.sin(angle) * inner_r)
        cr.line_to(cx + math.cos(angle) * outer_r, cy + math.sin(angle) * outer_r)
        cr.stroke()

    # Scale numbers
    lbl_r  = r * 0.718
    lbl_sz = max(7.0, r * 0.112)
    step   = 60.0 if d.speed_unit == "km/h" else 30.0
    n_steps = int(round(d.speed_max / step))
    for i in range(n_steps + 1):
        frac  = (i * step) / max(1, d.speed_max)
        angle = _ARC_START + _ARC_SPAN * frac
        txt   = str(int(i * step))
        cr.select_font_face("Cantarell", 0, 0)
        cr.set_font_size(lbl_sz)
        ext = cr.text_extents(txt)
        nx = cx + math.cos(angle) * lbl_r - ext.width / 2 - ext.x_bearing
        ny = cy + math.sin(angle) * lbl_r - ext.height / 2 - ext.y_bearing
        cr.set_source_rgba(*pal["dim"], 0.80 * a)
        cr.move_to(nx, ny)
        cr.show_text(txt)

    # Inner face disc with radial gradient
    face_r = r * 0.620
    grad_face = cairo.RadialGradient(cx, cy - face_r * 0.28, face_r * 0.04,
                                     cx, cy, face_r)
    grad_face.add_color_stop_rgba(0.0, *pal["face_grad"][0])
    grad_face.add_color_stop_rgba(1.0, *pal["face_grad"][1])
    cr.set_source(grad_face)
    cr.arc(cx, cy, face_r, 0, math.tau)
    cr.fill()

    cr.set_line_width(max(1.2, r * 0.014))
    cr.set_source_rgba(*pal["face_ring"])
    cr.arc(cx, cy, face_r, 0, math.tau)
    cr.stroke()

    # GPS / OBD source
    if d.speed_source:
        src_sz = max(8.0, r * 0.128)
        _txt(cr, d.speed_source, cx, cy - r * 0.33, src_sz, (*pal["accent"], 0.70 * a))

    # Speed value
    val_sz = max(24.0, r * 0.42)
    _txt(cr, d.speed_label, cx, cy - r * 0.02, val_sz,
         (*pal["big"], a), bold=True, max_w=face_r * 1.72)

    # Unit
    unit_sz = max(11.0, r * 0.160)
    _txt(cr, d.speed_unit, cx, cy + r * 0.26, unit_sz, (*pal["accent"], 0.88 * a))


# ── Info column ───────────────────────────────────────────────────────────────

def _info_col(cr: Any, x: float, y: float, w: float, h: float, d: Any, pal: dict) -> None:
    """Right-side digital readout column matching the HUD style."""
    items: list[tuple[str, str, bool]] = [
        (_translate(d.language, "dashboard.rpm.unit").upper(),
         d.rpm_label, d.rpm_active),
        ("°C",
         d.coolant_label, d.coolant_active),
    ]
    if d.fuel_active:
        items.append((_translate(d.language, "dashboard.fuel").upper(),
                      d.fuel_label, True))
    if d.heading_active:
        items.append(("GPS", d.heading_str or "--", True))

    n     = len(items)
    row_h = h / n

    for i, (label, value, active) in enumerate(items):
        a      = 1.0 if active else 0.28
        cy_top = y + i * row_h

        # Row background
        cr.set_source_rgba(*pal["card_bg"])
        cr.rectangle(x, cy_top + row_h * 0.04, w, row_h * 0.92)
        cr.fill()

        # Accent left bar
        bar_w = max(2.0, w * 0.022)
        cr.set_source_rgba(*pal["accent"], 0.70 * a)
        cr.rectangle(x, cy_top + row_h * 0.12, bar_w, row_h * 0.76)
        cr.fill()

        lbl_sz = max(9.0,  row_h * 0.21)
        val_sz = max(18.0, row_h * 0.46)

        label_cx = x + w * 0.54
        _txt(cr, label, label_cx, cy_top + row_h * 0.28,
             lbl_sz, (*pal["accent"], 0.85 * a))
        _txt(cr, value, label_cx, cy_top + row_h * 0.72 - 4,
             val_sz, (*pal["big"], a), bold=True, max_w=w * 0.88)


# ── Landscape layout ──────────────────────────────────────────────────────────

def _draw_modern_landscape(cr: Any, width: int, height: int, d: Any, pal: dict) -> None:
    ring_share = 0.60
    ring_w = width * ring_share
    info_w = width - ring_w

    r  = min(ring_w * 0.44, height * 0.44)
    cx = ring_w * 0.50
    cy = height * 0.50

    _speed_ring(cr, cx, cy, r, d, pal)

    pad = height * 0.06
    _info_col(cr, ring_w, pad, info_w - pad * 0.5, height - pad * 2, d, pal)


# ── Portrait layout ───────────────────────────────────────────────────────────

def _draw_modern_portrait(cr: Any, width: int, height: int, d: Any, pal: dict) -> None:
    r  = min(width * 0.44, height * 0.28)
    cx = width * 0.50
    cy = r + height * 0.04

    _speed_ring(cr, cx, cy, r, d, pal)

    pad    = width * 0.05
    info_y = cy + r + height * 0.035
    info_h = height - info_y - pad
    _info_col(cr, pad, info_y, width - 2 * pad, info_h, d, pal)


# ── Entry point ───────────────────────────────────────────────────────────────

def _draw_impl(cr: Any, width: int, height: int, data: Any, dark: bool) -> None:
    pal = _palette(dark)
    cr.set_source_rgb(*pal["bg"])
    cr.paint()
    strip_h = max(28.0, height * 0.072)
    if width >= height:
        _draw_modern_landscape(cr, width, height - strip_h, data, pal)
    else:
        _draw_modern_portrait(cr, width, height - strip_h, data, pal)
    _draw_last_trip_strip(cr, 0, height - strip_h, width, strip_h, data)


def draw(cr: Any, width: int, height: int, data: Any) -> None:
    _draw_impl(cr, width, height, data, dark=True)
