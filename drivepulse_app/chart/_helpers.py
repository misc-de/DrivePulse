"""Pure helpers and the cairo drawing routine for the scan-chart sub-page.

These functions are side-effect free (modulo the cairo context they paint
into) and are unit-tested separately via ``tests/test_scan_chart_helpers.py``.

The ``_prefs_load`` / ``_prefs_save`` / ``_PREFS_FILE`` triple stays in
``scan_chart`` proper because tests monkeypatch ``_PREFS_FILE`` on that
module and the load/save functions look it up via module globals.
"""
from __future__ import annotations

import json
import math
import sqlite3

from gi.repository import Adw

from drivepulse_app.diagnostics import get_logger
from drivepulse_app.ui.draw_helpers import _txt

_log = get_logger(__name__)

_CHART_H = 260
_PAD_L = 48
_PAD_R = 16
_PAD_R_VAL2 = 56
_PAD_T = 26
_PAD_B = 36

_COLOR_MAIN = (0.35, 0.60, 1.00)  # main vehicle = blue
_DEFAULT_COMPARE_COLORS: list[tuple[float, float, float]] = [
    (1.00, 0.60, 0.20),  # orange
    (0.30, 0.80, 0.45),  # green
    (0.75, 0.45, 0.95),  # violet
    (1.00, 0.85, 0.30),  # yellow
    (0.95, 0.40, 0.50),  # pink
    (0.40, 0.85, 0.85),  # cyan
]


def _fmt(v: float) -> str:
    if abs(v) >= 100:
        return f"{v:.0f}"
    if abs(v) >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"


def _fmt_ts(ts: str) -> str:
    return ts[:10] if len(ts) >= 10 else ts


def _fmt_rel_s(s: float) -> str:
    """Format relative seconds as '0s', '1m23s', etc. for intra-scan X-axis."""
    s = round(s)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m{s % 60:02d}s"


def _rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    r, g, b = (max(0, min(255, round(c * 255))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _safe_pids_count(scan_meta) -> int:
    try:
        return int(scan_meta["pids_count"] or 0)
    except (KeyError, TypeError, ValueError):
        return 0


def _fmt_scan_label(ts: str) -> str:
    # ISO 8601 → "YYYY-MM-DD HH:MM"
    if len(ts) >= 16:
        return ts[:16].replace("T", " ")
    return ts


def _lookup_card_bg(widget) -> tuple[float, float, float] | None:
    try:
        ok, rgba = widget.get_style_context().lookup_color("card_bg_color")
    except Exception:
        _log.debug("Could not look up card_bg_color", exc_info=True)
        return None
    if not ok:
        return None
    return (rgba.red, rgba.green, rgba.blue)


# ---------------------------------------------------------------------------
# Background stat computation (reusable for any car_id)
# ---------------------------------------------------------------------------

def _compute_stats_for_car(db, car_id: int) -> dict:
    from drivepulse_app.cars.metadata import _parse_profile_pid_key
    stats: dict = {}
    raw_values: dict = {}
    try:
        scans = db.list_scans_for_car(car_id)
    except Exception:
        # Aggregator must never raise — empty stats is the documented fallback.
        _log.warning("Could not list scans for car_id=%s", car_id, exc_info=True)
        return {}
    for scan_meta in scans:
        ts_str = str(scan_meta["scanned_at"] or "")
        try:
            data = db.get_scan_data(int(scan_meta["id"]))
        except (sqlite3.Error, json.JSONDecodeError, ValueError):
            _log.debug("Could not load scan_data for id=%s", scan_meta.get("id"), exc_info=True)
            continue
        for raw_key, raw_val in (data.get("live_data") or {}).items():
            pid = _parse_profile_pid_key(raw_key)
            if not pid:
                continue
            v = raw_val.get("value") if isinstance(raw_val, dict) else raw_val
            unit = str(raw_val.get("unit", "")) if isinstance(raw_val, dict) else ""
            if v is None:
                continue
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
        s["intra_series"] = {}

    # Intra-scan time series
    for scan_meta in scans:
        scan_id = int(scan_meta["id"])
        try:
            if not db.scan_has_series(scan_id):
                continue
            scan_start_ts: float | None = None
            try:
                from datetime import datetime as _dt
                scan_start_ts = _dt.fromisoformat(
                    str(scan_meta["scanned_at"]).replace("Z", "+00:00")
                ).timestamp()
            except (ValueError, TypeError):
                _log.debug("Unparseable scanned_at for scan_id=%s", scan_id, exc_info=True)
            rows = db.get_scan_samples(scan_id)
            pid_pts: dict[str, list[tuple[float, float]]] = {}
            for row in rows:
                _pid = str(row["pid"])
                rel_s = float(row["ts"]) - (scan_start_ts or float(row["ts"]))
                pid_pts.setdefault(_pid, []).append((rel_s, float(row["value"])))
            for _pid, pts in pid_pts.items():
                if _pid not in stats:
                    stats[_pid] = {"min": 0.0, "max": 0.0, "sum": 0.0,
                                   "count": 0, "unit": "", "values": [],
                                   "intra_series": {}}
                stats[_pid]["intra_series"][scan_id] = sorted(pts, key=lambda t: t[0])
        except Exception:
            # Optional enrichment — never let a broken intra-series block the stats result.
            _log.debug("Could not load intra-scan samples for scan_id=%s", scan_id, exc_info=True)

    return stats


# ---------------------------------------------------------------------------
# Chart drawing — multi-series with up to two value axes
# ---------------------------------------------------------------------------

def _draw_chart(
    cr,
    w: int,
    h: int,
    series_groups: list[dict],
    val1_unit: str,
    val2_unit: str,
    has_val2: bool,
    main_ts: list[str] | None,
    bg_rgb: tuple[float, float, float] | None = None,
) -> None:
    """
    series_groups: each entry is {
        'color': (r,g,b),
        'val1': list[float] | None,
        'val2': list[float] | None,
    }
    """
    pl = _PAD_L
    pr = _PAD_R_VAL2 if has_val2 else _PAD_R
    pt = _PAD_T
    plot_w = max(1.0, float(w - pl - pr))
    plot_h = max(1.0, float(h - pt - _PAD_B))

    try:
        dark = Adw.StyleManager.get_default().get_dark()
    except Exception:
        _log.debug("StyleManager.get_dark failed, defaulting to dark", exc_info=True)
        dark = True
    fg = (1.0, 1.0, 1.0) if dark else (0.0, 0.0, 0.0)
    axis_rgba = (*fg, 0.55)
    grid_rgba = (*fg, 0.16)
    lbl_rgba  = (*fg, 0.95)

    if not dark and bg_rgb is not None:
        cr.set_source_rgb(*bg_rgb)
        cr.rectangle(0, 0, w, h)
        cr.fill()

    # Value range per axis across all series
    val1_all: list[float] = []
    val2_all: list[float] = []
    for g in series_groups:
        if g.get("val1"):
            val1_all.extend(g["val1"])
        if g.get("val2"):
            val2_all.extend(g["val2"])

    v1_mn, v1_mx = (min(val1_all), max(val1_all)) if val1_all else (0.0, 1.0)
    v2_mn, v2_mx = (min(val2_all), max(val2_all)) if val2_all else (0.0, 1.0)
    v1_same = abs(v1_mx - v1_mn) <= 1e-9
    v2_same = abs(v2_mx - v2_mn) <= 1e-9

    # Grid lines (1/3, 2/3) + L-axis
    tick_fracs = (0.0, 1.0 / 3.0, 2.0 / 3.0, 1.0)
    cr.set_source_rgba(*grid_rgba)
    cr.set_line_width(1.0)
    cr.set_dash([2.0, 3.0], 0)
    for f in tick_fracs[1:-1]:
        ty = pt + plot_h * (1.0 - f)
        cr.move_to(pl, ty)
        cr.line_to(pl + plot_w, ty)
        cr.stroke()
    cr.set_dash([], 0)

    cr.set_source_rgba(*axis_rgba)
    cr.set_line_width(1.0)
    cr.move_to(pl, pt)
    cr.line_to(pl, pt + plot_h)
    cr.line_to(pl + plot_w, pt + plot_h)
    cr.stroke()

    # Left Y axis: value 1
    if val1_all:
        for f in tick_fracs:
            val = v1_mn + f * (v1_mx - v1_mn)
            ty = pt + plot_h * (1.0 - f)
            _txt(cr, _fmt(val), pl - 5, ty, 9.5, rgba=lbl_rgba, align="right")
            if v1_same:
                break
    if val1_unit:
        _txt(cr, val1_unit, pl, pt - 10, 9.0, rgba=lbl_rgba, align="left")

    # Right Y axis: value 2
    if has_val2:
        cr.set_source_rgba(*axis_rgba)
        cr.set_line_width(1.0)
        cr.move_to(pl + plot_w, pt)
        cr.line_to(pl + plot_w, pt + plot_h)
        cr.stroke()
        if val2_all:
            for f in tick_fracs:
                val = v2_mn + f * (v2_mx - v2_mn)
                ty = pt + plot_h * (1.0 - f)
                _txt(cr, _fmt(val), pl + plot_w + 5, ty, 9.5, rgba=lbl_rgba, align="left")
                if v2_same:
                    break
        if val2_unit:
            _txt(cr, val2_unit, pl + plot_w, pt - 10, 9.0, rgba=lbl_rgba, align="right")

    # X axis: relative time labels in intra mode, otherwise date
    if main_ts:
        ty_x = pt + plot_h + 14
        # Intra mode: labels are already formatted as "0s", "1m23s"
        first_ts = main_ts[0]
        last_ts = main_ts[-1]
        if first_ts == last_ts:
            _txt(cr, first_ts, pl + plot_w / 2, ty_x, 9.5, rgba=lbl_rgba, align="center")
        else:
            _txt(cr, first_ts, pl, ty_x, 9.5, rgba=lbl_rgba, align="left")
            _txt(cr, last_ts, pl + plot_w, ty_x, 9.5, rgba=lbl_rgba, align="right")

    def _draw_line(
        vals: list[float],
        mn: float,
        mx: float,
        color: tuple[float, float, float],
        dashed: bool,
    ) -> None:
        n = len(vals)
        if n == 0:
            return
        rng = mx - mn if abs(mx - mn) > 1e-9 else 1.0
        r, g, b = color

        def xp(i: int) -> float:
            return pl + plot_w / 2 if n == 1 else pl + i * plot_w / (n - 1)

        def yp(v: float) -> float:
            return pt + plot_h * (1.0 - (v - mn) / rng)

        if n > 1:
            cr.set_source_rgba(r, g, b, 0.50)
            cr.set_line_width(1.6)
            if dashed:
                cr.set_dash([5.0, 4.0], 0)
            for i, v in enumerate(vals):
                cr.move_to(xp(i), yp(v)) if i == 0 else cr.line_to(xp(i), yp(v))
            cr.stroke()
            if dashed:
                cr.set_dash([], 0)

        cr.set_source_rgba(r, g, b, 0.92)
        dot_r = 2.6
        for i, v in enumerate(vals):
            cr.arc(xp(i), yp(v), dot_r, 0, 2 * math.pi)
            cr.fill()

    for g in series_groups:
        color = g.get("color") or _COLOR_MAIN
        v1 = g.get("val1") or []
        v2 = g.get("val2") or []
        if v1:
            _draw_line(v1, v1_mn, v1_mx, color, dashed=False)
        if v2:
            _draw_line(v2, v2_mn, v2_mx, color, dashed=True)
