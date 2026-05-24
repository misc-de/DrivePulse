"""Shared Cairo drawing utilities for DrivePulse theme files."""
from __future__ import annotations

import math
from typing import Any

from drivepulse_app.common import SOURCE_LANGUAGE, _normalize_language, _translate

_GRAD_STOPS = [
    (0.00, (0.10, 0.92, 0.50)),
    (0.40, (0.10, 0.55, 1.00)),
    (0.70, (0.90, 0.52, 0.08)),
    (1.00, (0.92, 0.12, 0.12)),
]

_ARC_START = math.radians(135)
_ARC_END   = math.radians(405)
_ARC_SPAN  = _ARC_END - _ARC_START


def _txt(
    cr: Any,
    text: str,
    x: float,
    y: float,
    size: float,
    rgba: tuple = (1.0, 1.0, 1.0, 1.0),
    bold: bool = False,
    align: str = "center",
    max_w: float | None = None,
) -> None:
    cr.select_font_face("Cantarell", 0, 1 if bold else 0)
    cr.set_font_size(size)
    ext = cr.text_extents(text)
    if max_w is not None and ext.width > max_w:
        size = max(8.0, size * max_w / max(1.0, ext.width))
        cr.set_font_size(size)
        ext = cr.text_extents(text)
    if align == "center":
        tx = x - ext.width / 2 - ext.x_bearing
    elif align == "right":
        tx = x - ext.width - ext.x_bearing
    else:
        tx = x - ext.x_bearing
    ty = y - ext.height / 2 - ext.y_bearing
    cr.set_source_rgba(*rgba)
    cr.move_to(tx, ty)
    cr.show_text(text)


def _norm(val: float, lo: float, hi: float) -> float:
    return max(0.0, min(1.0, (val - lo) / max(hi - lo, 1e-9)))


def _arc_track(
    cr: Any,
    cx: float,
    cy: float,
    r: float,
    lw: float,
    start: float,
    end: float,
    track: tuple,
    fill: tuple,
    norm: float,
) -> None:
    cr.set_line_width(lw)
    cr.set_line_cap(1)
    cr.set_source_rgba(*track)
    cr.arc(cx, cy, r, start, end)
    cr.stroke()
    value_ang = start + (end - start) * norm
    cr.set_source_rgba(*fill)
    cr.arc(cx, cy, r, start, value_ang)
    cr.stroke()


def _cardinal(deg: float, language: str = SOURCE_LANGUAGE) -> str:
    keys = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return _translate(language, f"dashboard.cardinal.{keys[int((deg + 22.5) / 45) % 8]}")


def _draw_last_trip_strip(
    cr: Any,
    x: float,
    y: float,
    w: float,
    h: float,
    d: Any,
    dark: bool = True,
) -> None:
    """Zeichnet eine schmale Leiste mit Last-Trip-Stats (RPM-Range + Temp-Range).

    x/y: obere linke Ecke, w/h: Breite/Höhe der Leiste.
    d: DashData (muss last_trip_available == True sein).
    """
    if not getattr(d, "last_trip_available", False):
        return

    rpm_min = d.last_trip_rpm_min
    rpm_max = d.last_trip_rpm_max
    cool_min = d.last_trip_coolant_min
    cool_max = d.last_trip_coolant_max

    bg_a = 0.45 if dark else 0.15
    cr.set_source_rgba(0.0, 0.0, 0.0, bg_a)
    cr.rectangle(x, y, w, h)
    cr.fill()

    sz_lbl = max(8.0, h * 0.28)
    sz_val = max(10.0, h * 0.42)
    col_lbl = (0.55, 0.60, 0.66, 0.65) if dark else (0.35, 0.38, 0.42, 0.75)
    col_val = (0.90, 0.93, 0.96, 0.88) if dark else (0.10, 0.12, 0.16, 0.90)

    lang = getattr(d, "language", SOURCE_LANGUAGE)
    label_rpm  = _translate(lang, "dashboard.rpm.unit")
    label_temp = "°C"

    rpm_str  = f"{rpm_min:.0f} – {rpm_max:.0f}"
    temp_str = f"{cool_min:.0f} – {cool_max:.0f}"

    half = w / 2
    cy_lbl = y + h * 0.28
    cy_val = y + h * 0.72

    _txt(cr, label_rpm,  x + half * 0.50, cy_lbl, sz_lbl, col_lbl)
    _txt(cr, rpm_str,    x + half * 0.50, cy_val, sz_val, col_val, bold=True, max_w=half * 0.90)

    _txt(cr, label_temp, x + half * 1.50, cy_lbl, sz_lbl, col_lbl)
    _txt(cr, temp_str,   x + half * 1.50, cy_val, sz_val, col_val, bold=True, max_w=half * 0.90)
