"""Scan history chart sub-page with optional cross-vehicle PID overlay."""
from __future__ import annotations

import math
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .cars_metadata import _unit_display
from .draw_helpers import _txt

_CHART_H = 220
_PAD_L = 42
_PAD_R = 14
_PAD_T = 20
_PAD_B = 24

_COLOR_MAIN    = (0.35, 0.60, 1.00)   # blue
_COLOR_OVERLAY = (1.00, 0.60, 0.20)   # orange


def _fmt(v: float) -> str:
    if abs(v) >= 100:
        return f"{v:.0f}"
    if abs(v) >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"


# ---------------------------------------------------------------------------
# Background stat computation (reusable for any car_id)
# ---------------------------------------------------------------------------

def _compute_stats_for_car(db, car_id: int) -> dict:
    from .cars_metadata import _parse_profile_pid_key
    stats: dict = {}
    raw_values: dict = {}
    try:
        scans = db.list_scans_for_car(car_id)
    except Exception:
        return {}
    for scan_meta in scans:
        ts_str = str(scan_meta["scanned_at"] or "")
        try:
            data = db.get_scan_data(int(scan_meta["id"]))
        except Exception:
            continue
        for raw_key, raw_val in (data.get("live_data") or {}).items():
            pid = _parse_profile_pid_key(raw_key)
            if not pid:
                continue
            v = raw_val.get("value") if isinstance(raw_val, dict) else raw_val
            unit = str(raw_val.get("unit", "")) if isinstance(raw_val, dict) else ""
            try:
                num = float(v)
            except (TypeError, ValueError):
                continue
            if pid not in stats:
                stats[pid] = {"min": num, "max": num, "sum": num, "count": 1, "unit": unit}
            else:
                s = stats[pid]
                s["min"] = min(s["min"], num)
                s["max"] = max(s["max"], num)
                s["sum"] += num
                s["count"] += 1
            raw_values.setdefault(pid, []).append((ts_str, num))
    for pid, s in stats.items():
        s["avg"] = s["sum"] / s["count"]
        s["values"] = sorted(raw_values.get(pid) or [], key=lambda t: t[0])
    return stats


# ---------------------------------------------------------------------------
# Chart drawing
# ---------------------------------------------------------------------------

def _draw_chart(
    cr,
    w: int,
    h: int,
    main_vals: list[float],
    main_mean: float,
    overlay_vals: list[float] | None,
) -> None:
    pl, pt = _PAD_L, _PAD_T
    plot_w = w - pl - _PAD_R
    plot_h = h - pt - _PAD_B

    try:
        dark = Adw.StyleManager.get_default().get_dark()
    except Exception:
        dark = True
    fg = (1.0, 1.0, 1.0) if dark else (0.0, 0.0, 0.0)
    axis_rgba = (*fg, 0.55)
    mean_rgba = (*fg, 0.40)
    lbl_rgba  = (*fg, 0.95)

    # Light theme: paint the plot area pure white so axes/data have a clean
    # canvas against the muted grey app background.
    if not dark:
        cr.set_source_rgb(1.0, 1.0, 1.0)
        cr.rectangle(pl, pt, plot_w, plot_h)
        cr.fill()

    # Axis lines
    cr.set_source_rgba(*axis_rgba)
    cr.set_line_width(1.0)
    cr.move_to(pl, pt)
    cr.line_to(pl, pt + plot_h)
    cr.line_to(pl + plot_w, pt + plot_h)
    cr.stroke()

    # Mean line (main series, dashed)
    if main_vals:
        mn, mx = min(main_vals), max(main_vals)
        rng = mx - mn if abs(mx - mn) > 1e-9 else 1.0
        norm_mean = (main_mean - mn) / rng if abs(mx - mn) > 1e-9 else 0.5
        mean_y = pt + plot_h * (1.0 - norm_mean)
        cr.set_source_rgba(*mean_rgba)
        cr.set_line_width(1.0)
        cr.set_dash([5.0, 4.0], 0)
        cr.move_to(pl, mean_y)
        cr.line_to(pl + plot_w, mean_y)
        cr.stroke()
        cr.set_dash([], 0)

        # Left Y-axis labels
        _txt(cr, _fmt(mx), pl - 4, pt, 9.5, rgba=lbl_rgba, align="right")
        if abs(mx - mn) > 1e-9:
            _txt(cr, _fmt(mn), pl - 4, pt + plot_h, 9.5, rgba=lbl_rgba, align="right")

    def _draw_series(vals: list[float], color: tuple, dot_r: float = 3.5) -> None:
        n = len(vals)
        if n == 0:
            return
        mn_s, mx_s = min(vals), max(vals)
        rng_s = mx_s - mn_s if abs(mx_s - mn_s) > 1e-9 else 1.0
        r, g, b = color

        def xp(i: int) -> float:
            return pl + plot_w / 2 if n == 1 else pl + i * plot_w / (n - 1)

        def yp(v: float) -> float:
            return pt + plot_h * (1.0 - (v - mn_s) / rng_s)

        if n > 1:
            cr.set_source_rgba(r, g, b, 0.28)
            cr.set_line_width(1.5)
            for i, v in enumerate(vals):
                cr.move_to(xp(i), yp(v)) if i == 0 else cr.line_to(xp(i), yp(v))
            cr.stroke()

        for i, v in enumerate(vals):
            cr.set_source_rgba(r, g, b, 0.90)
            cr.arc(xp(i), yp(v), dot_r, 0, 2 * math.pi)
            cr.fill()

    _draw_series(main_vals, _COLOR_MAIN)
    if overlay_vals:
        _draw_series(overlay_vals, _COLOR_OVERLAY)


# ---------------------------------------------------------------------------
# Main widget
# ---------------------------------------------------------------------------

class ScanChartContent(Gtk.Box):
    """Scrollable chart content for use inside a nav sub-page."""

    def __init__(
        self,
        main_label: str,
        main_pid: str,
        all_stats: dict,
        profiles: list,
        db,
        pid_labels: dict,
        language: str = "de",
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_vexpand(True)

        self._main_label = main_label
        self._main_pid = main_pid
        self._all_stats = all_stats
        self._profiles = profiles
        self._db = db
        self._pid_labels = pid_labels
        self._language = language

        self._overlay_stats: dict = {}
        self._overlay_pid: str | None = None
        self._pid_options: list[tuple[str, str]] = []
        self._car_options: list[tuple[int, str]] = []

        main_stats = all_stats.get(main_pid) or {}
        mean = main_stats.get("avg", 0.0)
        unit = main_stats.get("unit", "")
        unit_disp = _unit_display(unit, language)
        n = len(main_stats.get("values") or [])

        # ── Main series info strip ────────────────────────────────────────
        info = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        info.set_margin_start(16)
        info.set_margin_end(16)
        info.set_margin_top(12)
        info.set_margin_bottom(10)

        dot = Gtk.Label()
        dot.set_markup('<span foreground="#5999ff" size="large">⬤</span>')
        dot.set_valign(Gtk.Align.CENTER)
        info.append(dot)

        info_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_text.set_hexpand(True)

        name_lbl = Gtk.Label(label=main_label, xalign=0.0)
        name_lbl.add_css_class("heading")
        info_text.append(name_lbl)

        mean_str = f"⌀ {_fmt(mean)} {unit_disp}".strip() if unit_disp else f"⌀ {_fmt(mean)}"
        sub_text = f"{mean_str}   ·   {n} {'Messung' if n == 1 else 'Messungen'}"
        sub_lbl = Gtk.Label(label=sub_text, xalign=0.0)
        sub_lbl.add_css_class("dim-label")
        sub_lbl.add_css_class("caption")
        info_text.append(sub_lbl)

        info.append(info_text)
        self.append(info)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Chart ─────────────────────────────────────────────────────────
        self._da = Gtk.DrawingArea()
        self._da.set_content_height(_CHART_H)
        self._da.set_hexpand(True)
        self._da.set_margin_top(10)
        self._da.set_margin_bottom(10)
        self._da.set_margin_start(8)
        self._da.set_margin_end(8)
        self._da.set_draw_func(self._draw)
        self.append(self._da)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Compare section ───────────────────────────────────────────────
        compare_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        compare_outer.set_margin_start(16)
        compare_outer.set_margin_end(16)
        compare_outer.set_margin_top(14)
        compare_outer.set_margin_bottom(16)

        compare_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        overlay_dot = Gtk.Label()
        overlay_dot.set_markup('<span foreground="#ff9933" size="large">⬤</span>')
        overlay_dot.set_valign(Gtk.Align.CENTER)
        compare_hdr.append(overlay_dot)
        compare_hdr_lbl = Gtk.Label(label="Vergleich", xalign=0.0)
        compare_hdr_lbl.add_css_class("heading")
        compare_hdr.append(compare_hdr_lbl)
        compare_outer.append(compare_hdr)

        compare_list = Gtk.ListBox()
        compare_list.set_selection_mode(Gtk.SelectionMode.NONE)
        compare_list.add_css_class("boxed-list")
        compare_list.set_valign(Gtk.Align.START)

        # Car row
        car_row = Adw.ActionRow()
        car_row.set_title("Fahrzeug")

        for p in profiles:
            cid = p.get("car_id")
            if cid is None:
                continue
            disp = p.get("label") or p.get("brand") or f"Fahrzeug {cid}"
            vin = p.get("vin", "")
            if vin:
                disp = f"{disp}  …{vin[-5:]}"
            self._car_options.append((cid, disp))

        car_sl = Gtk.StringList()
        car_sl.append("—")
        for _, disp in self._car_options:
            car_sl.append(disp)

        self._car_dd = Gtk.DropDown(model=car_sl)
        self._car_dd.set_valign(Gtk.Align.CENTER)
        self._car_dd.connect("notify::selected", self._on_car_selected)
        car_row.add_suffix(self._car_dd)
        compare_list.append(car_row)

        # PID row
        self._pid_row = Adw.ActionRow()
        self._pid_row.set_title("Wert")
        self._pid_row.set_sensitive(False)

        # suffix container — we swap contents without touching ActionRow
        self._pid_suffix = Gtk.Box(spacing=4)
        self._pid_suffix.set_valign(Gtk.Align.CENTER)
        self._pid_row.add_suffix(self._pid_suffix)

        self._pid_placeholder = Gtk.Label(label="—")
        self._pid_placeholder.add_css_class("dim-label")
        self._pid_suffix.append(self._pid_placeholder)

        compare_list.append(self._pid_row)
        compare_outer.append(compare_list)
        self.append(compare_outer)

    # ── Callbacks ─────────────────────────────────────────────────────────

    def _on_car_selected(self, dd: Gtk.DropDown, _prop) -> None:
        sel = dd.get_selected()
        if sel == 0:
            self._overlay_stats = {}
            self._overlay_pid = None
            self._set_pid_suffix_placeholder("—")
            self._pid_row.set_sensitive(False)
            self._da.queue_draw()
            return

        car_id, _ = self._car_options[sel - 1]
        self._set_pid_suffix_spinner()
        self._pid_row.set_sensitive(False)
        threading.Thread(
            target=lambda: GLib.idle_add(
                self._on_stats_loaded,
                _compute_stats_for_car(self._db, car_id),
            ),
            daemon=True,
        ).start()

    def _on_stats_loaded(self, stats: dict) -> bool:
        self._overlay_stats = stats
        self._rebuild_pid_dd(stats)
        return False

    def _on_pid_selected(self, dd: Gtk.DropDown, _prop) -> None:
        sel = dd.get_selected()
        self._overlay_pid = self._pid_options[sel - 1][0] if sel > 0 else None
        self._da.queue_draw()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _clear_pid_suffix(self) -> None:
        while True:
            child = self._pid_suffix.get_first_child()
            if child is None:
                break
            self._pid_suffix.remove(child)

    def _set_pid_suffix_placeholder(self, text: str) -> None:
        self._clear_pid_suffix()
        lbl = Gtk.Label(label=text)
        lbl.add_css_class("dim-label")
        self._pid_suffix.append(lbl)

    def _set_pid_suffix_spinner(self) -> None:
        self._clear_pid_suffix()
        spinner = Gtk.Spinner()
        spinner.start()
        self._pid_suffix.append(spinner)

    def _rebuild_pid_dd(self, stats: dict) -> None:
        self._pid_options = []
        for pid, s in sorted(stats.items(), key=lambda kv: self._pid_labels.get(kv[0], kv[0])):
            if not (s.get("values") or []):
                continue
            lbl = self._pid_labels.get(pid, pid)
            ud = _unit_display(s.get("unit", ""), self._language)
            self._pid_options.append((pid, f"{lbl}  ({ud})" if ud else lbl))

        if not self._pid_options:
            self._set_pid_suffix_placeholder("Keine Daten")
            self._pid_row.set_sensitive(False)
            self._da.queue_draw()
            return

        sl = Gtk.StringList()
        sl.append("—")
        for _, disp in self._pid_options:
            sl.append(disp)

        dd = Gtk.DropDown(model=sl)
        dd.set_valign(Gtk.Align.CENTER)
        dd.connect("notify::selected", self._on_pid_selected)

        self._clear_pid_suffix()
        self._pid_suffix.append(dd)
        self._pid_row.set_sensitive(True)

        # Auto-select same PID if available in overlay car
        for i, (pid, _) in enumerate(self._pid_options):
            if pid == self._main_pid:
                dd.set_selected(i + 1)
                break

    # ── Drawing ───────────────────────────────────────────────────────────

    def _draw(self, _da, cr, w: int, h: int) -> None:
        main_stats = self._all_stats.get(self._main_pid) or {}
        main_vals = [v for _, v in (main_stats.get("values") or [])]
        main_mean = main_stats.get("avg", 0.0)

        overlay_vals: list[float] | None = None
        if self._overlay_pid and self._overlay_pid in self._overlay_stats:
            ov = [v for _, v in (self._overlay_stats[self._overlay_pid].get("values") or [])]
            overlay_vals = ov if ov else None

        _draw_chart(cr, w, h, main_vals, main_mean, overlay_vals)
