"""Full-screen dashboard canvas with layout themes for DrivePulse."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import cairo
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

# These theme IDs trigger DashboardCanvas instead of the 3-gauge row
DASHBOARD_THEMES = ("digital", "sport", "racing", "analog")


def _cardinal(deg: float) -> str:
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[int((deg + 22.5) / 45) % 8]


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------


@dataclass
class DashData:
    rpm: float = 0.0
    rpm_label: str = "--"
    rpm_active: bool = False
    rpm_max: float = 7000.0

    speed: float = 0.0
    speed_label: str = "--"
    speed_unit: str = "km/h"
    speed_max: float = 240.0
    speed_active: bool = False

    coolant: float = 0.0
    coolant_label: str = "--"
    coolant_active: bool = False
    coolant_min: float = 40.0
    coolant_max: float = 130.0

    heading_deg: float = 0.0
    heading_str: str = ""
    heading_active: bool = False

    fuel_pct: float = 0.0
    fuel_label: str = "--"
    fuel_active: bool = False


# ---------------------------------------------------------------------------
# Canvas widget
# ---------------------------------------------------------------------------


class DashboardCanvas(Gtk.DrawingArea):
    __gtype_name__ = "DashboardCanvas"

    def __init__(self, theme: str = "sport", units: str = "metric") -> None:
        super().__init__()
        self.theme = theme
        self.data = DashData(
            speed_unit="mph" if units == "imperial" else "km/h",
            speed_max=150.0 if units == "imperial" else 240.0,
        )
        self.set_content_width(1)
        self.set_content_height(1)
        self.set_size_request(1, 1)
        self.set_draw_func(self._draw)

    def set_theme(self, theme: str) -> None:
        self.theme = theme
        self.queue_draw()

    def set_units(self, units: str) -> None:
        self.data.speed_unit = "mph" if units == "imperial" else "km/h"
        self.data.speed_max = 150.0 if units == "imperial" else 240.0
        self.queue_draw()

    def update_rpm(self, value: float | None, label: str | None = None) -> None:
        if value is None:
            self.data.rpm_active = False
            self.data.rpm_label = "--"
        else:
            self.data.rpm_active = True
            self.data.rpm = max(0.0, min(self.data.rpm_max, value))
            self.data.rpm_label = label if label is not None else f"{value:.0f}"
        self.queue_draw()

    def update_speed(self, value: float | None, label: str | None = None) -> None:
        if value is None:
            self.data.speed_active = False
            self.data.speed_label = "--"
        else:
            self.data.speed_active = True
            self.data.speed = max(0.0, min(self.data.speed_max, value))
            self.data.speed_label = label if label is not None else f"{value:.0f}"
        self.queue_draw()

    def update_coolant(self, value: float | None, label: str | None = None) -> None:
        if value is None:
            self.data.coolant_active = False
            self.data.coolant_label = "--"
        else:
            self.data.coolant_active = True
            self.data.coolant = max(self.data.coolant_min, min(self.data.coolant_max, value))
            self.data.coolant_label = label if label is not None else f"{value:.0f}"
        self.queue_draw()

    def update_heading(self, deg: float | None, heading_str: str = "") -> None:
        self.data.heading_active = deg is not None
        if deg is not None:
            self.data.heading_deg = deg
            card = _cardinal(deg)
            self.data.heading_str = heading_str or f"{deg:.0f}° {card}"
        else:
            self.data.heading_str = ""
        self.queue_draw()

    def update_fuel(self, pct: float | None, label: str | None = None) -> None:
        self.data.fuel_active = pct is not None
        if pct is not None:
            self.data.fuel_pct = max(0.0, min(100.0, pct))
            self.data.fuel_label = label if label is not None else f"{pct:.0f}%"
        else:
            self.data.fuel_label = "--"
        self.queue_draw()

    def _draw(self, _area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        if self.theme == "digital":
            _draw_digital(cr, width, height, self.data)
        elif self.theme == "racing":
            _draw_rings(cr, width, height, self.data, accent=(0.95, 0.42, 0.08))
        elif self.theme == "analog":
            _draw_analog(cr, width, height, self.data)
        else:  # sport
            _draw_rings(cr, width, height, self.data, accent=(0.05, 0.68, 1.0))


# ---------------------------------------------------------------------------
# Shared drawing helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Theme: digital  (inspired by design 1 – flat text + bars)
# ---------------------------------------------------------------------------

_GRAD_STOPS = [
    (0.00, (0.10, 0.92, 0.50)),
    (0.40, (0.10, 0.55, 1.00)),
    (0.70, (0.90, 0.52, 0.08)),
    (1.00, (0.92, 0.12, 0.12)),
]


def _draw_digital(cr: Any, width: int, height: int, d: DashData) -> None:
    cr.set_source_rgb(0.0, 0.0, 0.02)
    cr.paint()

    panel_w = width * 0.40
    sp_a = 1.0 if d.speed_active else 0.28

    # — Speed number ——————————————————————————————
    sp_size = min(height * 0.50, panel_w * 0.72)
    _txt(cr, d.speed_label, panel_w * 0.50, height * 0.36, sp_size,
         (1, 1, 1, sp_a), bold=True, max_w=panel_w * 0.90)
    _txt(cr, d.speed_unit, panel_w * 0.50, height * 0.36 + sp_size * 0.60,
         height * 0.082, (0.65, 0.72, 0.80, sp_a * 0.85))

    # — Rainbow speed bar ————————————————————————
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

    # — Divider ——————————————————————————————————
    cr.set_source_rgba(0.28, 0.32, 0.38, 0.35)
    cr.set_line_width(1.0)
    cr.move_to(panel_w, height * 0.07)
    cr.line_to(panel_w, height * 0.93)
    cr.stroke()

    # — Right panel: metric rows —————————————————
    rx = panel_w + height * 0.06
    rw = width - rx - height * 0.06
    rows: list[tuple] = [
        ("RPM", d.rpm_label,
         _norm(d.rpm, 0, d.rpm_max),
         (0.95, 0.50, 0.08),
         d.rpm_active),
        ("Coolant", f"{d.coolant_label} °C",
         _norm(d.coolant, d.coolant_min, d.coolant_max),
         (0.18, 0.56, 1.00),
         d.coolant_active),
    ]
    if d.heading_active:
        rows.append(("Heading", d.heading_str, None, (0.35, 0.90, 0.45), True))
    if d.fuel_active:
        rows.append(("Fuel", d.fuel_label,
                     _norm(d.fuel_pct, 0, 100),
                     (0.25, 0.85, 0.35),
                     True))

    row_h = height / max(len(rows), 1)
    for i, (name, val_str, norm_v, color, active) in enumerate(rows):
        a = 1.0 if active else 0.26
        cy_row = i * row_h

        _txt(cr, name, rx + 2, cy_row + row_h * 0.28, height * 0.058,
             (0.50, 0.56, 0.62, 0.72 * a), align="left")
        _txt(cr, val_str, rx + rw, cy_row + row_h * 0.28, height * 0.065,
             (0.93, 0.95, 1.00, a), bold=True, align="right", max_w=rw * 0.55)

        if norm_v is not None:
            bar_y = cy_row + row_h * 0.58
            bar_h = row_h * 0.20
            cr.set_source_rgba(0.13, 0.15, 0.19, 0.55)
            cr.rectangle(rx, bar_y, rw, bar_h)
            cr.fill()
            fw2 = rw * norm_v
            if fw2 > 1:
                cr.set_source_rgba(*color, 0.90 * a)
                cr.rectangle(rx, bar_y, fw2, bar_h)
                cr.fill()

        # Row separator
        cr.set_source_rgba(0.22, 0.25, 0.30, 0.30)
        cr.set_line_width(0.5)
        cr.move_to(rx, (i + 1) * row_h)
        cr.line_to(rx + rw, (i + 1) * row_h)
        cr.stroke()


# ---------------------------------------------------------------------------
# Theme: rings / sport + racing  (inspired by designs 2 & 3)
# ---------------------------------------------------------------------------

_ARC_START = math.radians(135)
_ARC_END = math.radians(405)
_ARC_SPAN = _ARC_END - _ARC_START


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


def _draw_rings(cr: Any, width: int, height: int, d: DashData, accent: tuple = (0.05, 0.68, 1.0)) -> None:
    # Background
    bg = (0.01, 0.02, 0.05) if accent[0] < 0.2 else (0.04, 0.03, 0.01)
    cr.set_source_rgb(*bg)
    cr.paint()

    sec_accent = (0.95, 0.42, 0.08) if accent[0] < 0.2 else accent

    # Layout
    cy_main = height * 0.47
    r_main = min(height * 0.37, width * 0.22)
    lw_main = r_main * 0.28

    r_side = r_main * 0.56
    lw_side = r_side * 0.26

    cx_main = width * 0.47
    cx_left = cx_main - r_main - r_side * 1.30
    cx_right = cx_main + r_main + r_side * 1.30

    # — Left circle: RPM ————————————————————————
    _ring_circle(
        cr, cx_left, cy_main, r_side, lw_side,
        _norm(d.rpm, 0, d.rpm_max),
        sec_accent,
        d.rpm_label, "rpm",
        r_side * 0.68, r_side * 0.30,
        d.rpm_active,
        title="RPM", title_size=r_side * 0.22,
    )

    # — Center circle: Speed ————————————————————
    _ring_circle(
        cr, cx_main, cy_main, r_main, lw_main,
        _norm(d.speed, 0, d.speed_max),
        accent,
        d.speed_label, d.speed_unit,
        r_main * 0.68, r_main * 0.28,
        d.speed_active,
        title="", title_size=0,
    )

    # — Right circle: Coolant / heading ————————————
    if d.heading_active:
        _compass_circle(
            cr, cx_right, cy_main, r_side, lw_side,
            d.heading_deg, d.heading_str, sec_accent, True,
        )
    else:
        _ring_circle(
            cr, cx_right, cy_main, r_side, lw_side,
            _norm(d.coolant, d.coolant_min, d.coolant_max),
            sec_accent,
            d.coolant_label, "°C",
            r_side * 0.68, r_side * 0.30,
            d.coolant_active,
            title="Coolant", title_size=r_side * 0.22,
        )

    # — Bottom info strip ————————————————————————
    info_y = cy_main + r_main + lw_main + height * 0.065
    if info_y < height * 0.92:
        items: list[tuple[str, str, bool]] = []
        if d.heading_active:
            items.append(("Coolant", f"{d.coolant_label} °C", d.coolant_active))
        if d.fuel_active:
            items.append(("Fuel", d.fuel_label, True))
        if not d.heading_active:
            items.append(("Heading", d.heading_str or "--", d.heading_active))
        items.append(("", _current_time(), True))

        col_w = width / max(len(items), 1)
        for i, (name, val, act) in enumerate(items):
            ia = 1.0 if act else 0.25
            ix = (i + 0.5) * col_w
            if name:
                _txt(cr, name, ix, info_y, height * 0.050,
                     (0.45, 0.50, 0.56, 0.65 * ia))
            _txt(cr, val, ix, info_y + height * 0.065, height * 0.062,
                 (0.88, 0.91, 0.95, ia), bold=bool(val and val != "--"))


def _current_time() -> str:
    import time as _t
    t = _t.localtime()
    return f"{t.tm_hour:02d}:{t.tm_min:02d}"


# ---------------------------------------------------------------------------
# Theme: analog  (inspired by designs 4 & 6 – needle gauges)
# ---------------------------------------------------------------------------

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

    # Value text
    val_sz = max(16.0, r * 0.28)
    _txt(cr, val_label, cx, cy + r * 0.30, val_sz, (*text_col, a), bold=True, max_w=r * 1.4)
    unit_sz = max(10.0, r * 0.14)
    _txt(cr, val_unit, cx, cy + r * 0.30 + val_sz * 0.72, unit_sz, (*dim_col, 0.80 * a), max_w=r * 1.2)

    # Title at bottom of gauge
    title_sz = max(9.0, r * 0.12)
    _txt(cr, title, cx, cy + r * 0.88, title_sz, (*dim_col, 0.65 * a), max_w=r * 1.6)


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


def _draw_analog(cr: Any, width: int, height: int, d: DashData) -> None:
    # Dark window background
    cr.set_source_rgb(0.05, 0.05, 0.06)
    cr.paint()

    # Layout: left small (compass/coolant) | center large (speed) | right medium (RPM)
    r_center = min(height * 0.37, width * 0.22)
    r_right = r_center * 0.58
    r_left = r_center * 0.46

    cy_main = height * 0.48
    cx_center = width * 0.46
    cx_right = cx_center + r_center + r_right * 1.35
    cx_left = cx_center - r_center - r_left * 1.45

    # Speed major/minor steps
    speed_step_maj = 30.0 if d.speed_unit == "km/h" else 20.0
    speed_step_min = 10.0 if d.speed_unit == "km/h" else 10.0

    # — Center: Speed ——————————————————————————————
    _analog_gauge(cr, cx_center, cy_main, r_center,
                  d.speed, 0, d.speed_max,
                  d.speed_label, d.speed_unit, "",
                  d.speed_active,
                  speed_step_maj, speed_step_min, dark=True)

    # — Right: RPM —————————————————————————————————
    _analog_gauge(cr, cx_right, cy_main, r_right,
                  d.rpm, 0, d.rpm_max,
                  d.rpm_label, "rpm", "RPM",
                  d.rpm_active,
                  1000.0, 500.0, dark=True)

    # — Left: Compass or Coolant ——————————————————
    if d.heading_active:
        _compass_analog(cr, cx_left, cy_main, r_left,
                        d.heading_deg, d.heading_str, True, dark=True)
    else:
        _analog_gauge(cr, cx_left, cy_main, r_left,
                      d.coolant, d.coolant_min, d.coolant_max,
                      d.coolant_label, "°C", "Coolant",
                      d.coolant_active,
                      20.0, 10.0, dark=True)

    # — Bottom info bar ————————————————————————————
    bar_y = cy_main + r_center + height * 0.045
    if bar_y < height * 0.94:
        items: list[tuple[str, bool]] = [(_current_time(), True)]
        if d.heading_active:
            items.insert(0, (f"Coolant  {d.coolant_label} °C", d.coolant_active))
        if d.fuel_active:
            items.insert(0, (f"Fuel  {d.fuel_label}", True))

        dim = (0.42, 0.44, 0.48)
        bright = (0.80, 0.82, 0.86)
        col_w = width / max(len(items), 1)
        txt_sz = max(10.0, height * 0.052)
        sep_col = (0.22, 0.24, 0.28, 0.45)

        # Separator line
        cr.set_source_rgba(*sep_col)
        cr.set_line_width(0.5)
        cr.move_to(width * 0.08, bar_y)
        cr.line_to(width * 0.92, bar_y)
        cr.stroke()

        for i, (txt, act) in enumerate(items):
            ia = 1.0 if act else 0.28
            ix = (i + 0.5) * col_w
            _txt(cr, txt, ix, bar_y + txt_sz * 1.1, txt_sz,
                 (*bright, ia))
