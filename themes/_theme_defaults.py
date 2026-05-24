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

    ── Core gauges ───────────────────────────────────────────────────────────

    data.rpm              – current value (float)
    data.rpm_label        – display text, e.g. "3 200"
    data.rpm_active       – bool; False = no OBD signal
    data.rpm_max          – scale maximum (default 7000)

    data.speed            – current value in display units
    data.speed_label      – display text
    data.speed_unit       – "km/h" or "mph"
    data.speed_max        – scale maximum (240 metric / 150 imperial)
    data.speed_active     – bool

    data.coolant          – coolant temperature (°C)
    data.coolant_label    – display text
    data.coolant_active   – bool
    data.coolant_min      – scale minimum (default 40 °C)
    data.coolant_max      – scale maximum (default 130 °C)

    data.heading_deg      – compass heading 0–360
    data.heading_str      – formatted text, e.g. "270° W"
    data.heading_active   – bool (False when GPS has no fix)

    data.fuel_pct         – fuel level 0–100 %
    data.fuel_label       – display text, e.g. "68%"
    data.fuel_active      – bool

    ── Extended OBD channels ─────────────────────────────────────────────────

    data.throttle_pct     – throttle position 0–100 %
    data.throttle_label   – display text, e.g. "32%"
    data.throttle_active  – bool

    data.engine_load_pct  – calculated engine load 0–100 %
    data.engine_load_label
    data.engine_load_active

    data.intake_c         – intake air temperature (°C)
    data.intake_label     – display text, e.g. "22"
    data.intake_active    – bool

    data.maf_gps          – mass air flow (g/s)
    data.maf_label        – display text, e.g. "14.3"
    data.maf_active       – bool

    data.voltage_v        – control module / battery voltage (V)
    data.voltage_label    – display text, e.g. "13.8"
    data.voltage_active   – bool

    data.accel_g          – longitudinal acceleration (g, signed: + = forward)
    data.accel_label      – display text, e.g. "+0.12"
    data.accel_active     – bool

    ── Speed breakdown ───────────────────────────────────────────────────────

    data.obd_speed        – OBD vehicle speed in display units (km/h or mph)
    data.obd_speed_active – bool

    data.gps_speed        – GPS speed in display units (km/h or mph)
    data.gps_speed_active – bool

    ── Speed breakdown ───────────────────────────────────────────────────────

    data.obd_speed        – OBD vehicle speed in display units (km/h or mph)
    data.obd_speed_active – bool

    data.gps_speed        – GPS speed in display units (km/h or mph)
    data.gps_speed_active – bool

    ── GPS position ──────────────────────────────────────────────────────────

    data.gps_lat          – latitude in decimal degrees
    data.gps_lon          – longitude in decimal degrees
    data.gps_altitude_m   – altitude in metres
    data.gps_pos_active   – bool; True only when lat+lon are valid

    ── Scan / profile snapshot ───────────────────────────────────────────────
    Populated once when a scan completes or on app startup from the last
    saved profile.  Not updated every OBD tick — values reflect the most
    recent full vehicle scan.

    data.scan_available       – bool; False until first scan loads

    data.scan_info            – dict with string entries:
        "vin"           Vehicle identification number
        "brand"         Manufacturer derived from VIN WMI
        "protocol"      OBD protocol string, e.g. "ISO 15765-4 (CAN 11/500)"
        "cal_id"        ECU software / calibration ID
        "cvn"           Calibration verification number
        "obd_standard"  OBD standard code (numeric string)

    data.scan_dtcs            – list[str] of stored fault codes, e.g. ["P0420"]
    data.scan_pending_dtcs    – list[str] of pending fault codes

    data.scan_pids            – dict[str, float | None]
        Key = 4-char uppercase OBD PID hex code.  Value = float or None.
        Units are SI originals (°C, km/h, %, g/s, kPa, V, s, km …).

        ── Vehicle ──────────────────────────────────
        "011C"  OBD standard (numeric code)

        ── Engine ───────────────────────────────────
        "010C"  RPM (rev/min)
        "0104"  Engine load – calculated (%)
        "0143"  Engine load – absolute (%)
        "010E"  Ignition timing advance (°)
        "011F"  Engine run time since start (s)
        "0142"  Board / control module voltage (V)

        ── Drive ────────────────────────────────────
        "010D"  Vehicle speed (km/h)
        "0131"  Distance since DTC clear (km)
        "0121"  Distance driven with MIL on (km)
        "0130"  Warm-up cycles since DTC clear (count)

        ── Temperatures ─────────────────────────────
        "0105"  Coolant temperature (°C)
        "010F"  Intake air temperature (°C)
        "0146"  Ambient air temperature (°C)
        "013C"  Catalyst temperature Bank 1 Sensor 1 (°C)

        ── Throttle / pedal ─────────────────────────
        "0111"  Throttle position (%)
        "0145"  Relative throttle position (%)
        "0147"  Throttle position sensor B (%)
        "0149"  Accelerator pedal sensor D (%)
        "014A"  Accelerator pedal sensor E (%)
        "014C"  Commanded throttle actuator (%)

        ── Mixture / lambda ─────────────────────────
        "0103"  Fuel system status (numeric code)
        "0106"  Short-term fuel trim Bank 1 (%)
        "0107"  Long-term fuel trim Bank 1 (%)
        "0156"  Long-term fuel trim secondary Bank 1 (%)
        "0134"  Lambda Bank 1 Sensor 1 (ratio)
        "0144"  Commanded lambda (ratio)
        "0115"  O₂ sensor Bank 1 Sensor 2 (V)

        ── Fuel system ──────────────────────────────
        "0110"  MAF air mass flow (g/s)
        "012F"  Fuel level (%)
        "0123"  Fuel rail pressure (kPa)
        "012E"  EVAP purge commanded (%)
        "0133"  Barometric pressure (kPa)

        ── Diagnostics ──────────────────────────────
        "0141"  Monitor status this drive cycle (numeric)

    Example usage:
        ltft = data.scan_pids.get("0107")   # long-term fuel trim %
        vin  = data.scan_info.get("vin", "")
        has_faults = bool(data.scan_dtcs)

    ── Locale ────────────────────────────────────────────────────────────────

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
