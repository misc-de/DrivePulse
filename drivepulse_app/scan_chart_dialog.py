"""Dialog showing per-PID scan history as a simple dot chart with mean line."""
from __future__ import annotations

import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .cars_metadata import _unit_display
from .draw_helpers import _txt

_CHART_W = 340
_CHART_H = 180
_PAD_L = 40
_PAD_R = 14
_PAD_T = 16
_PAD_B = 22


def _fmt(v: float) -> str:
    if abs(v) >= 100:
        return f"{v:.0f}"
    if abs(v) >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"


class ScanChartDialog(Adw.AlertDialog):
    def __init__(
        self,
        label: str,
        stats: dict,
        language: str = "de",
    ) -> None:
        super().__init__()

        values: list[tuple[str, float]] = stats.get("values") or []
        mean: float = stats.get("avg", 0.0)
        unit: str = stats.get("unit", "")
        unit_disp = _unit_display(unit, language)
        n = len(values)

        self.set_heading(label)

        mean_str = f"{_fmt(mean)} {unit_disp}".strip() if unit_disp else _fmt(mean)
        count_str = "1 Messung" if n == 1 else f"{n} Messungen"
        self.set_body(f"⌀  {mean_str}   ·   {count_str}")

        if n > 0:
            float_vals = [v for _, v in values]
            da = Gtk.DrawingArea()
            da.set_content_width(_CHART_W)
            da.set_content_height(_CHART_H)
            da.set_draw_func(
                lambda _da, cr, w, h: _draw_chart(cr, w, h, float_vals, mean)
            )
            da.set_margin_top(4)
            da.set_margin_bottom(4)
            self.set_extra_child(da)

        self.add_response("ok", "OK")


def _draw_chart(cr, w: int, h: int, vals: list[float], mean: float) -> None:
    n = len(vals)
    if n == 0:
        return

    mn = min(vals)
    mx = max(vals)
    val_range = mx - mn if abs(mx - mn) > 1e-9 else 1.0

    pl = _PAD_L
    pt = _PAD_T
    plot_w = w - pl - _PAD_R
    plot_h = h - pt - _PAD_B

    def x_pos(i: int) -> float:
        if n == 1:
            return pl + plot_w / 2
        return pl + i * plot_w / (n - 1)

    def y_pos(v: float) -> float:
        norm = (v - mn) / val_range
        return pt + plot_h * (1.0 - norm)

    mean_y = y_pos(mean) if abs(mx - mn) > 1e-9 else pt + plot_h / 2

    # Subtle axis lines
    cr.set_source_rgba(0.5, 0.5, 0.5, 0.15)
    cr.set_line_width(1.0)
    cr.move_to(pl, pt)
    cr.line_to(pl, pt + plot_h)
    cr.line_to(pl + plot_w, pt + plot_h)
    cr.stroke()

    # Mean line (dashed, subtle)
    cr.set_source_rgba(0.65, 0.65, 0.65, 0.4)
    cr.set_line_width(1.0)
    cr.set_dash([5.0, 4.0], 0)
    cr.move_to(pl, mean_y)
    cr.line_to(pl + plot_w, mean_y)
    cr.stroke()
    cr.set_dash([], 0)

    # Connecting line between dots
    if n > 1:
        cr.set_source_rgba(0.35, 0.6, 1.0, 0.3)
        cr.set_line_width(1.5)
        for i, v in enumerate(vals):
            px, py = x_pos(i), y_pos(v)
            if i == 0:
                cr.move_to(px, py)
            else:
                cr.line_to(px, py)
        cr.stroke()

    # Dots
    for i, v in enumerate(vals):
        px, py = x_pos(i), y_pos(v)
        cr.set_source_rgba(0.35, 0.6, 1.0, 0.9)
        cr.arc(px, py, 3.5, 0, 2 * math.pi)
        cr.fill()

    # Y-axis labels (min / max)
    label_rgba = (0.65, 0.65, 0.65, 0.75)
    _txt(cr, _fmt(mx), pl - 4, pt, 9.5, rgba=label_rgba, align="right")
    if abs(mx - mn) > 1e-9:
        _txt(cr, _fmt(mn), pl - 4, pt + plot_h, 9.5, rgba=label_rgba, align="right")
