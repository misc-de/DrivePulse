"""Cockpit Light gauge theme for DrivePulse — inverted palette of Cockpit."""
from typing import Any

THEME_TYPE = "gauge"
LABEL = {"en": "Cockpit Light", "de": "Cockpit Hell"}
CSS = """
window.dp-theme-cockpit-light .dp-main-nav,
window.dp-theme-cockpit-light .dp-main-nav scrolledwindow,
window.dp-theme-cockpit-light .dp-main-nav scrolledwindow > viewport,
window.dp-theme-cockpit-light .dp-gauge-bg,
window.dp-theme-cockpit-light .dp-gauge-bg > * {
  background-color: #ffffff;
}
.dp-accel-theme-cockpit-light .card {
  background-color: rgba(245, 246, 248, 0.85);
  border-radius: 6px;
}"""

from _dp_builtin_cockpit import _draw_impl


def draw(cr: Any, width: int, height: int, gauge: Any) -> None:
    _draw_impl(cr, width, height, gauge, dark=False)
