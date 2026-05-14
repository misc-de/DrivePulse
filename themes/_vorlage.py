"""
DrivePulse Theme – Minimal Template
=====================================

This file shows the absolute minimum for a working theme.
The theme displays only the sensor values as text — no arcs, no decorations.

USAGE
─────
1. Copy and rename the file (no leading underscore!), e.g.:
       themes/my_theme.py

2. Adjust THEME_TYPE, LABEL and CSS as needed.

3. Extend the draw() function as desired.

FILES WITH A LEADING UNDERSCORE (_) ARE IGNORED WHEN LOADING.

Available constants and full API reference:
    from theme_defaults import *
    → BG_DARK, TEXT_BRIGHT, TEXT_DIM, ARC_START, ARC_SPAN, active_alpha(), norm()

Drawing helpers for dashboard themes:
    from draw_helpers import _txt, _norm, _arc_track, _cardinal
"""

# ── Required fields ────────────────────────────────────────────────────────
THEME_TYPE = "gauge"           # "gauge" or "dashboard"
LABEL      = {"en": "Values only", "de": "Nur Werte"}
CSS        = ""                # Empty string = use app default


# ── Gauge theme: values only, no decoration ───────────────────────────────
# Receives: gauge.title, gauge.accent_rgb, gauge.active,
#           gauge.state.value/label/unit/min_value/max_value
#           gauge.arc_params(width, height)  → arc geometry
#           gauge.draw_text(cr, text, x, y, size, alpha, bold, max_width)
def draw(cr, width, height, gauge):
    size = min(width, height)
    cx   = width  / 2
    cy   = height / 2
    a    = 1.0 if gauge.active else 0.30

    # Background: let the system/window background show through
    cr.set_source_rgba(0, 0, 0, 0)
    cr.paint()

    # Value – large, centered
    gauge.draw_text(cr, gauge.state.label,
                    cx, cy - size * 0.05,
                    max(28, size * 0.20),
                    a, bold=True, max_width=size * 0.85)

    # Unit – centered below value
    gauge.draw_text(cr, gauge.state.unit,
                    cx, cy + size * 0.10,
                    max(13, size * 0.075),
                    0.70 * a, max_width=size * 0.75)

    # Title – small, at the bottom
    gauge.draw_text(cr, gauge.title,
                    cx, cy + size * 0.25,
                    max(12, size * 0.060),
                    0.48 * a, max_width=size * 0.75)


# ── Dashboard theme template (commented out) ─────────────────────────────
# To activate: set THEME_TYPE = "dashboard" and replace draw() below.
#
# from draw_helpers import _txt, _norm
#
# def draw(cr, width, height, data):
#     # Background
#     cr.set_source_rgb(0.05, 0.05, 0.06)
#     cr.paint()
#
#     cx = width  / 2
#     cy = height / 2
#     sz = min(width, height)
#     a  = 1.0 if data.speed_active else 0.30
#
#     # Speed – large, centered
#     _txt(cr, data.speed_label, cx, cy * 0.7,
#          sz * 0.25, (1, 1, 1, a), bold=True)
#     _txt(cr, data.speed_unit,  cx, cy * 0.7 + sz * 0.16,
#          sz * 0.07, (0.6, 0.65, 0.7, a * 0.85))
#
#     # Additional values – two rows below
#     row_y = cy * 1.1
#     row_h = sz * 0.10
#     items = [
#         (data.rpm_label,     "rpm",  data.rpm_active),
#         (data.coolant_label, "°C",   data.coolant_active),
#     ]
#     if data.fuel_active:
#         items.append((data.fuel_label, "%", True))
#
#     col_w = width / max(len(items), 1)
#     for i, (val, unit, active) in enumerate(items):
#         ia = 1.0 if active else 0.25
#         ix = (i + 0.5) * col_w
#         _txt(cr, val,  ix, row_y,          sz * 0.09,  (0.90, 0.92, 0.95, ia), bold=True)
#         _txt(cr, unit, ix, row_y + row_h,  sz * 0.055, (0.55, 0.60, 0.65, ia * 0.8))
