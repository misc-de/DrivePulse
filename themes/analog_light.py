"""Analog Light dashboard theme for DrivePulse — inverted palette of Analog."""
from typing import Any

THEME_TYPE = "dashboard"
LABEL = {"en": "Analog Light", "de": "Analog Hell"}
CSS = """
window.dp-theme-analog_light,
window.dp-theme-analog_light toolbarview,
window.dp-theme-analog_light scrolledwindow,
window.dp-theme-analog_light scrolledwindow > viewport,
window.dp-theme-analog_light .dp-gauge-bg,
window.dp-theme-analog_light .dp-gauge-bg > * {
  background-color: #ebedf2;
}"""

from _dp_builtin_analog import _draw_impl


def draw(cr: Any, width: int, height: int, data: Any) -> None:
    _draw_impl(cr, width, height, data, dark=False)
