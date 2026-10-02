"""Pure helpers and the cairo drawing routine for the scan-chart sub-page.

These functions are side-effect free (modulo the cairo context they paint
into) and are unit-tested separately via ``tests/test_scan_chart_helpers.py``.

The ``_prefs_load`` / ``_prefs_save`` / ``_PREFS_FILE`` triple stays in
``scan_chart`` proper because tests monkeypatch ``_PREFS_FILE`` on that
module and the load/save functions look it up via module globals.
"""
from __future__ import annotations

import colorsys
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


def _color_in(
    color: tuple[float, float, float],
    used: list[tuple[float, float, float]],
) -> bool:
    """Gleiche Farbe auf 8-bit-Ebene (gespeicherte Prefs sind Floats)."""
    return any(_rgb_to_hex(color) == _rgb_to_hex(u) for u in used)


def _next_free_color(
    used: list[tuple[float, float, float]],
) -> tuple[float, float, float]:
    """Erste Palettenfarbe, die noch kein Fahrzeug im Diagramm trägt; ist die
    Palette erschöpft, weitere Farbtöne im Goldener-Winkel-Abstand."""
    for c in _DEFAULT_COMPARE_COLORS:
        if not _color_in(c, used):
            return c
    for i in range(1, 360):
        hue = (0.11 + i * 0.381966) % 1.0
        c = colorsys.hsv_to_rgb(hue, 0.65 if i % 2 else 0.85, 0.95)
        if not _color_in(c, used):
            return c
    return _DEFAULT_COMPARE_COLORS[0]


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
            if not rows:
                continue
            # Nullpunkt = Scan-Start; liegen Samples davor (scanned_at erst am
            # Ende gesetzt), zählt das früheste Sample — sonst wären die
            # Zeiten negativ und Vergleiche gegeneinander verschoben.
            first_ts = min(float(row["ts"]) for row in rows)
            t0 = first_ts if scan_start_ts is None else min(scan_start_ts, first_ts)
            pid_pts: dict[str, list[tuple[float, float]]] = {}
            for row in rows:
                _pid = str(row["pid"])
                rel_s = float(row["ts"]) - t0
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

def _x_fracs(
    vals: list[float], times: list[float] | None, t_max: float,
) -> list[float]:
    """X-Position jedes Punkts als Anteil 0…1 der vollen (ungezoomten) Breite."""
    n = len(vals)
    if t_max > 0 and times is not None and len(times) == n:
        return [max(0.0, t) / t_max for t in times]
    if n == 1:
        return [0.5]
    return [i / (n - 1) for i in range(n)]


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
    view: tuple[float, float] = (0.0, 1.0),
) -> tuple[float, float]:
    """
    series_groups: each entry is {
        'color': (r,g,b),
        'val1': list[float] | None,
        'val2': list[float] | None,
        't1': list[float] | None,   # Sekunden ab Scan-Start, parallel zu val1
        't2': list[float] | None,   # Sekunden ab Scan-Start, parallel zu val2
    }

    Haben Serien Zeitstempel, teilen sie sich eine Zeitachse 0 … längste
    Laufzeit: eine 4-min-Fahrt belegt dann nur die ersten 4 min neben einer
    10-min-Fahrt, statt auf die volle Breite gestreckt zu werden.

    view: sichtbarer X-Ausschnitt als Anteil (start, end) der vollen Breite
    (Zoom). Die Y-Achsen skalieren auf die im Ausschnitt sichtbaren Werte.

    Gibt (plot_left, plot_width) zurück, damit Gesten Pixel in Anteile
    umrechnen können.
    """
    pl = _PAD_L
    pr = _PAD_R_VAL2 if has_val2 else _PAD_R
    pt = _PAD_T
    plot_w = max(1.0, float(w - pl - pr))
    plot_h = max(1.0, float(h - pt - _PAD_B))
    vx0, vx1 = view
    vspan = max(1e-9, vx1 - vx0)
    zoomed = vx0 > 1e-9 or vx1 < 1.0 - 1e-9

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

    # Gemeinsame Zeitachse über alle Serien mit Zeitstempeln
    t_max = 0.0
    for g in series_groups:
        for key in ("t1", "t2"):
            ts_list = g.get(key)
            if ts_list:
                t_max = max(t_max, ts_list[-1])

    # X-Anteile je Serie vorab, damit der Wertebereich dem Ausschnitt folgt
    lines: list[tuple[list[float], list[float], tuple[float, float, float], bool]] = []
    val1_all: list[float] = []
    val2_all: list[float] = []
    for g in series_groups:
        color = g.get("color") or _COLOR_MAIN
        for vkey, tkey, bucket, dashed in (
            ("val1", "t1", val1_all, False),
            ("val2", "t2", val2_all, True),
        ):
            vals = g.get(vkey) or []
            if not vals:
                continue
            fr = _x_fracs(vals, g.get(tkey), t_max)
            lines.append((vals, fr, color, dashed))
            visible = [v for v, f in zip(vals, fr, strict=True) if vx0 <= f <= vx1]
            bucket.extend(visible if visible else vals)

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

    # X axis: gemeinsame Zeitachse, sonst Datum bzw. Labels der Hauptserie
    ty_x = pt + plot_h + 14
    if t_max > 0:
        for f, align in ((0.0, "left"), (0.5, "center"), (1.0, "right")):
            _txt(cr, _fmt_rel_s((vx0 + f * vspan) * t_max), pl + f * plot_w, ty_x, 9.5,
                 rgba=lbl_rgba, align=align)
        cr.set_source_rgba(*grid_rgba)
        cr.set_line_width(1.0)
        cr.set_dash([2.0, 3.0], 0)
        cr.move_to(pl + plot_w / 2, pt)
        cr.line_to(pl + plot_w / 2, pt + plot_h)
        cr.stroke()
        cr.set_dash([], 0)
    elif main_ts:
        # Labels der Hauptserie am Rand des sichtbaren Ausschnitts
        last_i = len(main_ts) - 1
        first_ts = main_ts[round(vx0 * last_i)]
        last_ts = main_ts[round(vx1 * last_i)]
        if first_ts == last_ts:
            _txt(cr, first_ts, pl + plot_w / 2, ty_x, 9.5, rgba=lbl_rgba, align="center")
        else:
            _txt(cr, first_ts, pl, ty_x, 9.5, rgba=lbl_rgba, align="left")
            _txt(cr, last_ts, pl + plot_w, ty_x, 9.5, rgba=lbl_rgba, align="right")
    if zoomed:
        _txt(cr, f"{1.0 / vspan:.1f}×", pl + plot_w, pt - 10 if not has_val2 else pt + 8,
             9.0, rgba=(*fg, 0.6), align="right")

    def xp(f: float) -> float:
        return pl + (f - vx0) / vspan * plot_w

    def _draw_line(
        vals: list[float],
        fracs: list[float],
        mn: float,
        mx: float,
        color: tuple[float, float, float],
        dashed: bool,
    ) -> None:
        n = len(vals)
        rng = mx - mn if abs(mx - mn) > 1e-9 else 1.0
        r, g, b = color

        def yp(v: float) -> float:
            return pt + plot_h * (1.0 - (v - mn) / rng)

        if n > 1:
            cr.set_source_rgba(r, g, b, 0.50)
            cr.set_line_width(1.6)
            if dashed:
                cr.set_dash([5.0, 4.0], 0)
            for i, v in enumerate(vals):
                cr.move_to(xp(fracs[i]), yp(v)) if i == 0 else cr.line_to(xp(fracs[i]), yp(v))
            cr.stroke()
            if dashed:
                cr.set_dash([], 0)

        cr.set_source_rgba(r, g, b, 0.92)
        dot_r = 2.6
        for i, v in enumerate(vals):
            if vx0 - 0.05 * vspan <= fracs[i] <= vx1 + 0.05 * vspan:
                cr.arc(xp(fracs[i]), yp(v), dot_r, 0, 2 * math.pi)
                cr.fill()

    # Gezoomt: Linien auf die Plotfläche begrenzen (Punkte dürfen den Rand
    # minimal überragen, sonst werden sie an den Achsen halbiert).
    cr.save()
    cr.rectangle(pl - 3, pt - 3, plot_w + 6, plot_h + 6)
    cr.clip()
    for vals, fr, color, dashed in lines:
        if dashed:
            _draw_line(vals, fr, v2_mn, v2_mx, color, dashed=True)
        else:
            _draw_line(vals, fr, v1_mn, v1_mx, color, dashed=False)
    cr.restore()
    return float(pl), plot_w
