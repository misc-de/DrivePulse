"""Full-screen dashboard canvas with layout themes for DrivePulse."""
from __future__ import annotations

import math
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from drivepulse_app.common import SOURCE_LANGUAGE, _normalize_language, _translate
from drivepulse_app.ui.draw_helpers import _cardinal
from drivepulse_app.ui.gauge import _builtin_dashboard_mods

# These theme IDs trigger DashboardCanvas instead of the 3-gauge row
DASHBOARD_THEMES: tuple[str, ...] = tuple(_builtin_dashboard_mods.keys())


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------


@dataclass
class DashData:
    rpm: float = 0.0
    rpm_label: str = "--"
    rpm_active: bool = False
    rpm_max: float = 5000.0

    speed: float = 0.0
    speed_label: str = "--"
    speed_unit: str = "km/h"
    speed_max: float = 180.0
    speed_active: bool = False
    speed_source: str = ""

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

    throttle_pct: float = 0.0
    throttle_label: str = "--"
    throttle_active: bool = False

    engine_load_pct: float = 0.0
    engine_load_label: str = "--"
    engine_load_active: bool = False

    intake_c: float = 0.0
    intake_label: str = "--"
    intake_active: bool = False

    maf_gps: float = 0.0
    maf_label: str = "--"
    maf_active: bool = False

    voltage_v: float = 0.0
    voltage_label: str = "--"
    voltage_active: bool = False

    accel_g: float = 0.0
    accel_label: str = "--"
    accel_active: bool = False

    obd_speed: float = 0.0
    obd_speed_active: bool = False

    gps_speed: float = 0.0
    gps_speed_active: bool = False

    gps_lat: float = 0.0
    gps_lon: float = 0.0
    gps_altitude_m: float = 0.0
    gps_pos_active: bool = False

    # Last completed trip / live session stats (populated from DB or live tracking)
    last_trip_available: bool = False
    last_trip_rpm_min: float = 0.0
    last_trip_rpm_max: float = 0.0
    last_trip_coolant_min: float = 0.0
    last_trip_coolant_max: float = 0.0
    last_trip_speed_max: float = 0.0
    last_trip_distance_km: float = 0.0
    last_trip_duration_s: float = 0.0

    # Scan / profile data — populated once per scan, persists between OBD ticks
    scan_available: bool = False
    # Keyed by 4-char OBD PID code (uppercase), value = float or None
    scan_pids: dict = field(default_factory=dict)
    # "vin", "brand", "protocol", "cal_id", "cvn", "obd_standard"
    scan_info: dict = field(default_factory=dict)
    scan_dtcs: list = field(default_factory=list)
    scan_pending_dtcs: list = field(default_factory=list)

    language: str = SOURCE_LANGUAGE


# ---------------------------------------------------------------------------
# Canvas widget
# ---------------------------------------------------------------------------


class DashboardCanvas(Gtk.DrawingArea):
    __gtype_name__ = "DashboardCanvas"

    def __init__(self, theme: str = "racing", units: str = "metric", language: str = SOURCE_LANGUAGE) -> None:
        super().__init__()
        self.theme = theme
        self._rotation = 0  # degrees: 0, 90, 180, 270
        self.data = DashData(
            speed_unit="mph" if units == "imperial" else "km/h",
            speed_max=120.0 if units == "imperial" else 180.0,
        )
        self.data.language = _normalize_language(language)
        self._rpm_seen_max: float = 0.0
        self._speed_seen_max: float = 0.0
        self._batch_depth = 0
        self._batch_dirty = False
        self.set_content_width(1)
        self.set_content_height(1)
        self.set_size_request(1, 1)
        self.set_draw_func(self._draw)

    @contextmanager
    def batch_update(self) -> Iterator[None]:
        """Coalesce multiple data changes into one redraw."""
        self._batch_depth += 1
        try:
            yield
        finally:
            self._batch_depth -= 1
            if self._batch_depth == 0 and self._batch_dirty:
                self._batch_dirty = False
                self.queue_draw()

    def _queue_draw(self) -> None:
        if self._batch_depth:
            self._batch_dirty = True
        else:
            self.queue_draw()

    def set_theme(self, theme: str) -> None:
        self.theme = theme
        self._queue_draw()

    def set_rotation(self, angle: int) -> None:
        """Physical device rotation in degrees (0/90/180/270). Cairo drawing adapts."""
        self._rotation = angle % 360
        self._queue_draw()

    def set_units(self, units: str) -> None:
        self.data.speed_unit = "mph" if units == "imperial" else "km/h"
        self._speed_seen_max = 0.0
        self.data.speed_max = 120.0 if units == "imperial" else 180.0
        self._queue_draw()

    def set_language(self, language: str) -> None:
        self.data.language = _normalize_language(language)
        if self.data.heading_active:
            card = _cardinal(self.data.heading_deg, self.data.language)
            self.data.heading_str = f"{self.data.heading_deg:.0f}° {card}"
        self._queue_draw()

    def update_rpm(self, value: float | None, label: str | None = None) -> None:
        if value is None:
            self.data.rpm_active = False
            self.data.rpm_label = "--"
        else:
            self.data.rpm_active = True
            if value > self._rpm_seen_max:
                self._rpm_seen_max = value
                self.data.rpm_max = max(5000.0, math.ceil(value / 1000.0) * 1000.0)
            self.data.rpm = max(0.0, min(self.data.rpm_max, value))
            self.data.rpm_label = label if label is not None else f"{value:.0f}"
        self._queue_draw()

    def update_speed(self, value: float | None, label: str | None = None) -> None:
        if value is None:
            self.data.speed_active = False
            self.data.speed_label = "--"
        else:
            self.data.speed_active = True
            if value > self._speed_seen_max:
                self._speed_seen_max = value
                _min_spd = 120.0 if self.data.speed_unit == "mph" else 180.0
                self.data.speed_max = max(_min_spd, value + 20.0)
            self.data.speed = max(0.0, min(self.data.speed_max, value))
            self.data.speed_label = label if label is not None else f"{value:.0f}"
        self._queue_draw()

    def update_speed_source(self, source: str) -> None:
        if self.data.speed_source != source:
            self.data.speed_source = source
            self._queue_draw()

    def update_coolant(self, value: float | None, label: str | None = None) -> None:
        if value is None:
            self.data.coolant_active = False
            self.data.coolant_label = "--"
        else:
            self.data.coolant_active = True
            self.data.coolant = max(self.data.coolant_min, min(self.data.coolant_max, value))
            self.data.coolant_label = label if label is not None else f"{value:.0f}"
        self._queue_draw()

    def update_heading(self, deg: float | None, heading_str: str = "") -> None:
        self.data.heading_active = deg is not None
        if deg is not None:
            self.data.heading_deg = deg
            card = _cardinal(deg, self.data.language)
            self.data.heading_str = heading_str or f"{deg:.0f}° {card}"
        else:
            self.data.heading_str = ""
        self._queue_draw()

    def update_fuel(self, pct: float | None, label: str | None = None) -> None:
        self.data.fuel_active = pct is not None
        if pct is not None:
            self.data.fuel_pct = max(0.0, min(100.0, pct))
            self.data.fuel_label = label if label is not None else f"{pct:.0f}%"
        else:
            self.data.fuel_label = "--"
        self._queue_draw()

    def update_throttle(self, pct: float | None) -> None:
        self.data.throttle_active = pct is not None
        if pct is not None:
            self.data.throttle_pct = max(0.0, min(100.0, pct))
            self.data.throttle_label = f"{pct:.0f}%"
        else:
            self.data.throttle_label = "--"
        self._queue_draw()

    def update_engine_load(self, pct: float | None) -> None:
        self.data.engine_load_active = pct is not None
        if pct is not None:
            self.data.engine_load_pct = max(0.0, min(100.0, pct))
            self.data.engine_load_label = f"{pct:.0f}%"
        else:
            self.data.engine_load_label = "--"
        self._queue_draw()

    def update_intake(self, temp_c: float | None) -> None:
        self.data.intake_active = temp_c is not None
        if temp_c is not None:
            self.data.intake_c = temp_c
            self.data.intake_label = f"{temp_c:.0f}"
        else:
            self.data.intake_label = "--"
        self._queue_draw()

    def update_maf(self, maf: float | None) -> None:
        self.data.maf_active = maf is not None
        if maf is not None:
            self.data.maf_gps = maf
            self.data.maf_label = f"{maf:.1f}"
        else:
            self.data.maf_label = "--"
        self._queue_draw()

    def update_voltage(self, volts: float | None) -> None:
        self.data.voltage_active = volts is not None
        if volts is not None:
            self.data.voltage_v = volts
            self.data.voltage_label = f"{volts:.1f}"
        else:
            self.data.voltage_label = "--"
        self._queue_draw()

    def update_accel(self, accel_g: float | None) -> None:
        self.data.accel_active = accel_g is not None
        if accel_g is not None:
            self.data.accel_g = accel_g
            self.data.accel_label = f"{accel_g:+.2f}"
        else:
            self.data.accel_label = "--"
        self._queue_draw()

    def update_obd_speed(self, speed: float | None) -> None:
        self.data.obd_speed_active = speed is not None
        if speed is not None:
            self.data.obd_speed = max(0.0, speed)
        self._queue_draw()

    def update_gps_speed(self, speed: float | None) -> None:
        self.data.gps_speed_active = speed is not None
        if speed is not None:
            self.data.gps_speed = max(0.0, speed)
        self._queue_draw()

    def update_last_trip_stats(self, stats: "dict | None") -> None:
        """Letzter Trip oder laufende Session: rpm/coolant min-max, Distanz, Dauer."""
        if stats is None:
            self.data.last_trip_available = False
        else:
            self.data.last_trip_available = True
            self.data.last_trip_rpm_min = float(stats.get("min_rpm") or 0.0)
            self.data.last_trip_rpm_max = float(stats.get("max_rpm") or 0.0)
            self.data.last_trip_coolant_min = float(stats.get("min_coolant") or 0.0)
            self.data.last_trip_coolant_max = float(stats.get("max_coolant") or 0.0)
            self.data.last_trip_speed_max = float(stats.get("max_speed_kmh") or 0.0)
            self.data.last_trip_distance_km = float(stats.get("distance_km") or 0.0)
            self.data.last_trip_duration_s = float(stats.get("duration_s") or 0.0)
        self._queue_draw()

    def update_scan_data(
        self,
        pids: dict,
        info: dict,
        dtcs: list,
        pending_dtcs: list,
    ) -> None:
        """Push a completed scan snapshot into DashData for dashboard themes."""
        self.data.scan_available = True
        self.data.scan_pids = dict(pids)
        self.data.scan_info = dict(info)
        self.data.scan_dtcs = list(dtcs)
        self.data.scan_pending_dtcs = list(pending_dtcs)
        self._queue_draw()

    def update_gps_pos(self, lat: float | None, lon: float | None, altitude_m: float | None = None) -> None:
        self.data.gps_pos_active = lat is not None and lon is not None
        if lat is not None:
            self.data.gps_lat = lat
        if lon is not None:
            self.data.gps_lon = lon
        if altitude_m is not None:
            self.data.gps_altitude_m = altitude_m
        self._queue_draw()

    def _draw(self, _area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        w, h = _apply_rotation(cr, width, height, self._rotation)
        mod = _builtin_dashboard_mods.get(self.theme)
        if mod:
            draw_fn = getattr(mod, "draw", None)
            if callable(draw_fn):
                draw_fn(cr, w, h, self.data)


# ---------------------------------------------------------------------------
# Rotation helper
# ---------------------------------------------------------------------------


def _apply_rotation(cr: Any, width: int, height: int, angle: int) -> tuple[int, int]:
    """Translate+rotate Cairo context so content appears upright for the given device angle.

    Returns the effective (draw_width, draw_height) after the transform.
    The caller should use these dimensions instead of the original width/height.

    angle=90  : device rotated right (right-up)  → content needs 90° CCW
    angle=180 : device upside-down               → content needs 180°
    angle=270 : device rotated left  (left-up)   → content needs 90° CW
    """
    if angle == 90:
        cr.translate(0, height)
        cr.rotate(-math.pi / 2)
        return height, width
    if angle == 180:
        cr.translate(width, height)
        cr.rotate(math.pi)
        return width, height
    if angle == 270:
        cr.translate(width, 0)
        cr.rotate(math.pi / 2)
        return height, width
    return width, height
