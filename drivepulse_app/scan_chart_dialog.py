"""Dialog: per-PID scan history chart with up to two optional overlays."""
from __future__ import annotations

import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .cars_metadata import _unit_display
from .draw_helpers import _txt

_CHART_W = 360
_CHART_H = 190
_PAD_L = 40
_PAD_R = 14
_PAD_T = 18
_PAD_B = 22

# Blue, orange, green — main + two overlays
_COLORS: list[tuple[float, float, float]] = [
    (0.35, 0.60, 1.00),
    (1.00, 0.60, 0.20),
    (0.25, 0.82, 0.55),
]


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
        main_pid: str,
        all_stats: dict,
        pid_labels: dict,
        language: str = "de",
    ) -> None:
        super().__init__()

        self._main_pid = main_pid
        self._all_stats = all_stats
        self._pid_labels = pid_labels
        self._language = language
        self._overlay_pids: list[str | None] = [None, None]

        main_stats = all_stats.get(main_pid) or {}
        values = main_stats.get("values") or []
        mean = main_stats.get("avg", 0.0)
        unit = main_stats.get("unit", "")
        unit_disp = _unit_display(unit, language)
        n = len(values)

        self.set_heading(label)
        mean_str = f"{_fmt(mean)} {unit_disp}".strip() if unit_disp else _fmt(mean)
        count_str = "1 Messung" if n == 1 else f"{n} Messungen"
        self.set_body(f"⌀  {mean_str}   ·   {count_str}")

        # Build sorted list of PIDs available as overlays
        overlay_options: list[tuple[str, str]] = []
        for pid, s in sorted(
            all_stats.items(),
            key=lambda kv: pid_labels.get(kv[0], kv[0]),
        ):
            if pid == main_pid:
                continue
            if not (s.get("values") or []):
                continue
            lbl = pid_labels.get(pid, pid)
            ud = _unit_display(s.get("unit", ""), language)
            overlay_options.append((pid, f"{lbl}  ({ud})" if ud else lbl))

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.set_margin_top(4)
        content.set_margin_bottom(4)

        if overlay_options:
            dd_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            dd_box.set_margin_start(2)
            dd_box.set_margin_end(2)

            for idx in range(2):
                col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
                col.set_hexpand(True)

                color = _COLORS[idx + 1]
                hex_col = "#{:02x}{:02x}{:02x}".format(
                    int(color[0] * 255), int(color[1] * 255), int(color[2] * 255)
                )
                cap = Gtk.Label(xalign=0.0)
                cap.set_markup(
                    f'<span foreground="{hex_col}" size="small">⬤</span>'
                    f'<span size="small" alpha="70%">  Overlay {idx + 1}</span>'
                )
                col.append(cap)

                string_list = Gtk.StringList()
                string_list.append("—")
                for _, display_lbl in overlay_options:
                    string_list.append(display_lbl)

                dd = Gtk.DropDown(model=string_list)
                dd.set_hexpand(True)
                col.append(dd)
                dd_box.append(col)

                def _on_select(
                    _dd,
                    _prop,
                    i: int = idx,
                    opts: list = overlay_options,
                    widget: Gtk.DropDown = dd,
                ) -> None:
                    sel = widget.get_selected()
                    self._overlay_pids[i] = opts[sel - 1][0] if sel > 0 else None
                    self._da.queue_draw()

                dd.connect("notify::selected", _on_select)

            content.append(dd_box)

        self._da = Gtk.DrawingArea()
        self._da.set_content_width(_CHART_W)
        self._da.set_content_height(_CHART_H)
        self._da.set_draw_func(self._draw)
        content.append(self._da)

        self.set_extra_child(content)
        self.add_response("ok", "OK")

    def _draw(self, _da, cr, w: int, h: int) -> None:
        main_stats = self._all_stats.get(self._main_pid) or {}
        main_vals = [v for _, v in (main_stats.get("values") or [])]
        main_mean = main_stats.get("avg", 0.0)

        series: list[tuple[list[float], tuple[float, float, float]]] = []
        if main_vals:
            series.append((main_vals, _COLORS[0]))

        for i, pid in enumerate(self._overlay_pids):
            if pid and pid in self._all_stats:
                vals = [v for _, v in (self._all_stats[pid].get("values") or [])]
                if vals:
                    series.append((vals, _COLORS[i + 1]))

        _draw_chart(cr, w, h, series, main_vals, main_mean)


def _draw_chart(
    cr,
    w: int,
    h: int,
    series: list[tuple[list[float], tuple[float, float, float]]],
    main_vals: list[float],
    main_mean: float,
) -> None:
    if not series:
        return

    pl = _PAD_L
    pt = _PAD_T
    plot_w = w - pl - _PAD_R
    plot_h = h - pt - _PAD_B

    # Subtle axis lines
    cr.set_source_rgba(0.5, 0.5, 0.5, 0.15)
    cr.set_line_width(1.0)
    cr.move_to(pl, pt)
    cr.line_to(pl, pt + plot_h)
    cr.line_to(pl + plot_w, pt + plot_h)
    cr.stroke()

    # Mean line for main series (dashed, subtle)
    if main_vals:
        mn_m = min(main_vals)
        mx_m = max(main_vals)
        rng_m = mx_m - mn_m if abs(mx_m - mn_m) > 1e-9 else 1.0
        mean_norm = (main_mean - mn_m) / rng_m if abs(mx_m - mn_m) > 1e-9 else 0.5
        mean_y = pt + plot_h * (1.0 - mean_norm)

        cr.set_source_rgba(0.65, 0.65, 0.65, 0.35)
        cr.set_line_width(1.0)
        cr.set_dash([5.0, 4.0], 0)
        cr.move_to(pl, mean_y)
        cr.line_to(pl + plot_w, mean_y)
        cr.stroke()
        cr.set_dash([], 0)

        # Y-axis labels (left side, main series only)
        label_rgba = (0.65, 0.65, 0.65, 0.75)
        _txt(cr, _fmt(mx_m), pl - 4, pt, 9.5, rgba=label_rgba, align="right")
        if abs(mx_m - mn_m) > 1e-9:
            _txt(cr, _fmt(mn_m), pl - 4, pt + plot_h, 9.5, rgba=label_rgba, align="right")

    # Draw each series
    for vals, color in series:
        n = len(vals)
        if n == 0:
            continue

        mn = min(vals)
        mx = max(vals)
        rng = mx - mn if abs(mx - mn) > 1e-9 else 1.0

        def x_pos(i: int, _n: int = n) -> float:
            if _n == 1:
                return pl + plot_w / 2
            return pl + i * plot_w / (_n - 1)

        def y_pos(v: float, _mn: float = mn, _rng: float = rng) -> float:
            return pt + plot_h * (1.0 - (v - _mn) / _rng)

        r, g, b = color

        # Connecting line
        if n > 1:
            cr.set_source_rgba(r, g, b, 0.28)
            cr.set_line_width(1.5)
            for i, v in enumerate(vals):
                px, py = x_pos(i), y_pos(v)
                cr.move_to(px, py) if i == 0 else cr.line_to(px, py)
            cr.stroke()

        # Dots
        for i, v in enumerate(vals):
            px, py = x_pos(i), y_pos(v)
            cr.set_source_rgba(r, g, b, 0.9)
            cr.arc(px, py, 3.5, 0, 2 * math.pi)
            cr.fill()
