"""Full-screen dashboard canvas with layout themes for DrivePulse."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from common import SOURCE_LANGUAGE, _normalize_language, _translate
from draw_helpers import _cardinal
from gauge import _builtin_dashboard_mods

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
    rpm_max: float = 7000.0

    speed: float = 0.0
    speed_label: str = "--"
    speed_unit: str = "km/h"
    speed_max: float = 240.0
    speed_active: bool = False

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
            speed_max=150.0 if units == "imperial" else 240.0,
        )
        self.data.language = _normalize_language(language)
        self.set_content_width(1)
        self.set_content_height(1)
        self.set_size_request(1, 1)
        self.set_draw_func(self._draw)

    def set_theme(self, theme: str) -> None:
        self.theme = theme
        self.queue_draw()

    def set_rotation(self, angle: int) -> None:
        """Physical device rotation in degrees (0/90/180/270). Cairo drawing adapts."""
        self._rotation = angle % 360
        self.queue_draw()

    def set_units(self, units: str) -> None:
        self.data.speed_unit = "mph" if units == "imperial" else "km/h"
        self.data.speed_max = 150.0 if units == "imperial" else 240.0
        self.queue_draw()

    def set_language(self, language: str) -> None:
        self.data.language = _normalize_language(language)
        if self.data.heading_active:
            card = _cardinal(self.data.heading_deg, self.data.language)
            self.data.heading_str = f"{self.data.heading_deg:.0f}° {card}"
        self.queue_draw()

    def update_rpm(self, value: float | None, label: str | None = None) -> None:
        if value is None:
            self.data.rpm_active = False
            self.data.rpm_label = "--"
        else:
            self.data.rpm_active = True
            self.data.rpm = max(0.0, min(self.data.rpm_max, value))
            self.data.rpm_label = label if label is not None else f"{value:.0f}"
        self.queue_draw()

    def update_speed(self, value: float | None, label: str | None = None) -> None:
        if value is None:
            self.data.speed_active = False
            self.data.speed_label = "--"
        else:
            self.data.speed_active = True
            self.data.speed = max(0.0, min(self.data.speed_max, value))
            self.data.speed_label = label if label is not None else f"{value:.0f}"
        self.queue_draw()

    def update_coolant(self, value: float | None, label: str | None = None) -> None:
        if value is None:
            self.data.coolant_active = False
            self.data.coolant_label = "--"
        else:
            self.data.coolant_active = True
            self.data.coolant = max(self.data.coolant_min, min(self.data.coolant_max, value))
            self.data.coolant_label = label if label is not None else f"{value:.0f}"
        self.queue_draw()

    def update_heading(self, deg: float | None, heading_str: str = "") -> None:
        self.data.heading_active = deg is not None
        if deg is not None:
            self.data.heading_deg = deg
            card = _cardinal(deg, self.data.language)
            self.data.heading_str = heading_str or f"{deg:.0f}° {card}"
        else:
            self.data.heading_str = ""
        self.queue_draw()

    def update_fuel(self, pct: float | None, label: str | None = None) -> None:
        self.data.fuel_active = pct is not None
        if pct is not None:
            self.data.fuel_pct = max(0.0, min(100.0, pct))
            self.data.fuel_label = label if label is not None else f"{pct:.0f}%"
        else:
            self.data.fuel_label = "--"
        self.queue_draw()

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
