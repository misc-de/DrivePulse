"""Interactive metric/time chart used by the trip detail card.

Returns a ``Gtk.DrawingArea`` plus pointer + scrub gestures that drive the
shared ``cursor_state`` dict — the GPS-track widget and the chart share the
cursor so a touch on either highlights the same sample.
"""
from __future__ import annotations

import math
import os
from collections.abc import Callable
from typing import Any

from gi.repository import Adw, Gtk

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)

_CHART_DEBUG = bool(os.environ.get("DRIVEPULSE_CHART_DEBUG"))


def _is_dark() -> bool:
    try:
        return Adw.StyleManager.get_default().get_dark()
    except Exception:
        log.debug("Adw.StyleManager.get_dark failed, defaulting to dark", exc_info=True)
        return True


def _build_chart_widget(
    chart_state: dict,
    cursor_state: dict,
    on_cursor_change: Callable,
    height: int = 180,
) -> Gtk.DrawingArea:
    """Generic metric/time chart. chart_state holds current pts, unit, color, fmt.
    pts = list of (ts, value|None, lat|None, lon|None).
    cursor_state['idx'] = active index into pts (-1 = none).
    """
    PAD_L, PAD_R, PAD_T, PAD_B = 40, 12, 30, 24
    area = Gtk.DrawingArea()
    area.set_content_height(height)
    area.set_hexpand(True)

    def _idx_from_px(px: float, w: float) -> int:
        pts = chart_state.get("pts") or []
        if not pts:
            return -1
        iw = max(1.0, w - PAD_L - PAD_R)
        ts0 = pts[0][0]
        t_span = max(1e-6, pts[-1][0] - ts0)
        target = ts0 + max(0.0, min(1.0, (px - PAD_L) / iw)) * t_span
        best, best_d = 0, abs(pts[0][0] - target)
        for i, (ts, *_) in enumerate(pts):
            d = abs(ts - target)
            if d < best_d:
                best_d = d
                best = i
        return best

    def _set_cursor(px: float, w: float) -> None:
        idx = _idx_from_px(px, w)
        if idx != cursor_state.get("idx", -1):
            cursor_state["idx"] = idx
            area.queue_draw()
            on_cursor_change()

    def _clear_cursor() -> None:
        if cursor_state.get("idx", -1) != -1:
            cursor_state["idx"] = -1
            area.queue_draw()
            on_cursor_change()

    def draw_cb(area: Gtk.DrawingArea, cr: Any, w: int, h: int) -> None:
        pts = chart_state.get("pts") or []
        if len(pts) < 2:
            return
        valid_vals = [p[1] for p in pts if isinstance(p[1], (int, float)) and math.isfinite(p[1])]
        if not valid_vals:
            return

        dark = _is_dark()
        iw = max(1, w - PAD_L - PAD_R)
        ih = max(1, h - PAD_T - PAD_B)
        grid_rgba = (1.0, 1.0, 1.0, 0.45) if dark else (0.0, 0.0, 0.0, 0.45)
        text_rgba = (1.0, 1.0, 1.0, 0.95) if dark else (0.0, 0.0, 0.0, 0.95)
        color = chart_state.get("color", (0.34, 0.62, 0.86))
        fmt = chart_state.get("fmt", "{:.0f}")
        unit = chart_state.get("unit", "")

        ts0 = pts[0][0]
        t_span = max(1e-6, pts[-1][0] - ts0)
        v_min = min(valid_vals)
        v_max = max(valid_vals)
        v_pad = max(1e-6, v_max - v_min) * 0.08
        v_lo = v_min - v_pad
        v_hi = v_max + v_pad
        v_range = max(1e-6, v_hi - v_lo)

        def _vy(v: float) -> float:
            return PAD_T + ih - ((v - v_lo) / v_range) * ih

        def _tx(ts: float) -> float:
            return PAD_L + ((ts - ts0) / t_span) * iw

        # Grid lines
        cr.set_line_width(1.0)
        cr.set_source_rgba(*grid_rgba)
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = PAD_T + ih * (1.0 - frac)
            cr.move_to(PAD_L, y)
            cr.line_to(PAD_L + iw, y)
            cr.stroke()

        # Y-axis labels
        cr.set_source_rgba(*text_rgba)
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(10)
        for frac in (0.0, 0.5, 1.0):
            lbl_val = v_lo + frac * v_range
            if not math.isfinite(lbl_val):
                continue
            lbl = fmt.format(lbl_val)
            y = PAD_T + ih * (1.0 - frac) + 4
            cr.move_to(4, y)
            cr.show_text(lbl)

        # Build draw segments (skip None/NaN gaps)
        segments: list[list[tuple[float, float]]] = []
        seg: list[tuple[float, float]] = []
        for ts, v, *_ in pts:
            if not (isinstance(v, (int, float)) and math.isfinite(v)):
                if seg:
                    segments.append(seg)
                    seg = []
            else:
                seg.append((_tx(ts), _vy(v)))
        if seg:
            segments.append(seg)

        # Fill
        fill_rgba = (*color, 0.22)
        for seg in segments:
            if len(seg) < 2:
                continue
            cr.set_source_rgba(*fill_rgba)
            cr.move_to(seg[0][0], PAD_T + ih)
            for x, y in seg:
                cr.line_to(x, y)
            cr.line_to(seg[-1][0], PAD_T + ih)
            cr.close_path()
            cr.fill()

        # Line
        for seg in segments:
            if len(seg) < 2:
                continue
            cr.set_source_rgb(*color)
            cr.set_line_width(2.0)
            cr.move_to(*seg[0])
            for x, y in seg[1:]:
                cr.line_to(x, y)
            cr.stroke()

        # Cursor
        idx = cursor_state.get("idx", -1)
        if 0 <= idx < len(pts):
            ts_c, v_c, *_ = pts[idx]
            if v_c is not None:
                cx = _tx(ts_c)
                cy_dot = _vy(v_c)

                cr.set_source_rgba(1.0, 0.82, 0.1, 0.9)
                cr.set_line_width(1.5)
                cr.move_to(cx, PAD_T)
                cr.line_to(cx, PAD_T + ih)
                cr.stroke()

                cr.set_source_rgb(1.0, 0.82, 0.1)
                cr.arc(cx, cy_dot, 4, 0, 6.2832)
                cr.fill()

                cursor_lbl = fmt.format(v_c) + (" " + unit if unit else "")
                extra_fn = chart_state.get("cursor_extra_fn")
                extra_lbl = extra_fn(ts_c) if extra_fn else None
                cr.set_font_size(11)
                te = cr.text_extents(cursor_lbl)
                te2 = cr.text_extents(extra_lbl) if extra_lbl else None
                box_w = (max(te.width, te2.width) if te2 else te.width) + 6
                line_h = te.height + 4
                box_h = line_h + (te2.height + 3 if te2 else 0) + 1
                lx = min(cx + 6, w - box_w - 3)
                ly = max(PAD_T + te.height + 4, cy_dot - 4)
                bg = (0.0, 0.0, 0.0, 0.6) if dark else (1.0, 1.0, 1.0, 0.82)
                cr.set_source_rgba(*bg)
                cr.rectangle(lx - 3, ly - te.height - 1, box_w, box_h)
                cr.fill()
                fg = (1.0, 1.0, 1.0) if dark else (0.0, 0.0, 0.0)
                cr.set_source_rgb(*fg)
                cr.move_to(lx, ly)
                cr.show_text(cursor_lbl)
                if extra_lbl:
                    cr.move_to(lx, ly + line_h)
                    cr.show_text(extra_lbl)

    area.set_draw_func(draw_cb)

    # Pointer hover (mouse / stylus). Touch and pressed-pointer drag go
    # through the scrub gesture below — EventControllerMotion only reliably
    # fires for non-pressed pointer motion (hover).
    _hovering = [False]
    motion_ctl = Gtk.EventControllerMotion()

    def _on_pointer_enter(_c: Any, _x: float, _y: float) -> None:
        _hovering[0] = True

    def _on_pointer_motion(_c: Any, x: float, _y: float) -> None:
        _set_cursor(x, area.get_width())

    def _on_pointer_leave(_c: Any) -> None:
        _hovering[0] = False
        _clear_cursor()

    motion_ctl.connect("enter", _on_pointer_enter)
    motion_ctl.connect("motion", _on_pointer_motion)
    motion_ctl.connect("leave", _on_pointer_leave)
    area.add_controller(motion_ctl)

    # Scrub gesture for tap + drag, touch + pointer. Using the base
    # Gtk.GestureSingle directly gives us begin/update/end signals from
    # Gtk.Gesture that fire for EVERY event of the sequence with NO
    # threshold — unlike Gtk.GestureDrag (8 px) or Gtk.GestureClick (may
    # self-cancel on drag). Claiming the sequence in begin keeps a parent
    # ScrolledWindow's kinetic-scroll gesture from stealing horizontal or
    # vertical drag motion.
    scrub_ctl = Gtk.GestureSingle()
    scrub_ctl.set_button(0)  # any pointer button + touch
    scrub_ctl.set_touch_only(False)
    scrub_ctl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

    def _track(gesture: Any, sequence: Any, phase: str) -> None:
        ok, x, y = gesture.get_point(sequence)
        if _CHART_DEBUG:
            log.info("chart-scrub %s ok=%s x=%.1f y=%.1f w=%d",
                     phase, ok, x if ok else -1, y if ok else -1, area.get_width())
        if ok:
            _set_cursor(x, area.get_width())

    def _on_scrub_begin(gesture: Any, sequence: Any) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        _track(gesture, sequence, "begin")

    def _on_scrub_update(gesture: Any, sequence: Any) -> None:
        _track(gesture, sequence, "update")

    def _on_scrub_end(gesture: Any, sequence: Any) -> None:
        if _CHART_DEBUG:
            log.info("chart-scrub end")

    def _on_scrub_cancel(gesture: Any, sequence: Any) -> None:
        if _CHART_DEBUG:
            log.info("chart-scrub cancel (sequence stolen?)")

    scrub_ctl.connect("begin", _on_scrub_begin)
    scrub_ctl.connect("update", _on_scrub_update)
    scrub_ctl.connect("end", _on_scrub_end)
    scrub_ctl.connect("cancel", _on_scrub_cancel)
    area.add_controller(scrub_ctl)

    return area
