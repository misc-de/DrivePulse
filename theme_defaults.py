"""
DrivePulse – Default values and theme API
==========================================

Import with:
    from theme_defaults import *          # all constants
    from theme_defaults import BG_DARK, ARC_START, active_alpha

Every theme file in themes/ must define:
    THEME_TYPE  = "gauge" | "dashboard"
    LABEL       = {"en": "My Theme", "de": "Mein Theme"}
    CSS         = "..."    # GTK CSS, optional (empty string = use app default)
    def draw(cr, width, height, data): ...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GAUGE THEME  (THEME_TYPE = "gauge")
─────────────────────────────────────
def draw(cr, width, height, gauge):

    gauge.title           – display name, e.g. "RPM"
    gauge.accent_rgb      – (r, g, b) accent colour, floats 0–1
    gauge.active          – bool; False = no OBD signal

    gauge.state.value     – current value (float)
    gauge.state.label     – display text, e.g. "3 200"
    gauge.state.unit      – unit string, e.g. "rpm"
    gauge.state.min_value – scale minimum
    gauge.state.max_value – scale maximum

    gauge.arc_params(width, height)
        → cx, cy, size, radius, lw, arc_start, arc_end, arc_span, norm
          (norm = 0.0 … 1.0, pointer value)

    gauge.draw_text(cr, text, x, y, size,
                    alpha=1.0, bold=False, max_width=None)
        Centred text; auto-shrinks when max_width is exceeded.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DASHBOARD THEME  (THEME_TYPE = "dashboard")
────────────────────────────────────────────
def draw(cr, width, height, data):

    data.rpm              – current value (float)
    data.rpm_label        – display text
    data.rpm_active       – bool
    data.rpm_max          – scale maximum (default 7000)

    data.speed            – current value
    data.speed_label      – display text
    data.speed_unit       – "km/h" or "mph"
    data.speed_max        – scale maximum
    data.speed_active     – bool

    data.coolant          – current value (°C)
    data.coolant_label    – display text
    data.coolant_active   – bool
    data.coolant_min / coolant_max

    data.heading_deg      – heading angle 0–360
    data.heading_str      – formatted text, e.g. "270° W"
    data.heading_active   – bool

    data.fuel_pct         – fuel level 0–100
    data.fuel_label       – display text
    data.fuel_active      – bool

    data.language         – current language: "en" | "de"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations
import math

# ── Background colours ────────────────────────────────────────────────────
BG_BLACK   = (0.00, 0.00, 0.00)
BG_DARK    = (0.05, 0.05, 0.06)
BG_COCKPIT = (0.02, 0.025, 0.03)
BG_NEON    = (0.00, 0.00, 0.03)

# ── Text colours ──────────────────────────────────────────────────────────
TEXT_BRIGHT   = (0.94, 0.96, 1.00)  # primary value
TEXT_DIM      = (0.55, 0.60, 0.66)  # label / unit
INACTIVE_RGB  = (0.45, 0.48, 0.50)  # accent colour when no signal

# ── Arc geometry (gauge default: 135° … 405°, span of 270°) ──────────────
ARC_START = math.radians(135)
ARC_END   = math.radians(405)
ARC_SPAN  = ARC_END - ARC_START     # math.radians(270)

# ── Helper functions ──────────────────────────────────────────────────────

def active_alpha(active: bool, full: float = 1.0, dim: float = 0.30) -> float:
    """Returns *full* when active, otherwise *dim*."""
    return full if active else dim


def norm(value: float, min_value: float, max_value: float) -> float:
    """Normalises *value* to the range 0.0 … 1.0."""
    return max(0.0, min(1.0, (value - min_value) / max(max_value - min_value, 1e-9)))
