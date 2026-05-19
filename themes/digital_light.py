"""Digital Light dashboard theme for DrivePulse — inverted palette of Digital."""
from typing import Any

THEME_TYPE = "dashboard"
LABEL = {"en": "Digital Light", "de": "Digital Hell"}
CSS = """
window.dp-theme-digital-light .dp-gauge-bg,
window.dp-theme-digital-light .dp-gauge-bg > viewport,
window.dp-theme-digital-light .dp-gauge-bg > * {
  background-color: #f0f1f3;
}"""

from _dp_builtin_digital import _draw_impl


def draw(cr: Any, width: int, height: int, data: Any) -> None:
    _draw_impl(cr, width, height, data, dark=False)
