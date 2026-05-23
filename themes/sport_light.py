"""Sport Light dashboard theme for DrivePulse — inverted palette of Sport."""
from typing import Any

THEME_TYPE = "dashboard"
LABEL = {"en": "Sport Light", "de": "Sport Hell"}
CSS = """
window.dp-theme-sport-light .dp-gauge-bg,
window.dp-theme-sport-light .dp-gauge-bg > viewport,
window.dp-theme-sport-light .dp-gauge-bg > * {
  background-color: #c6c8cc;
}"""

from _dp_builtin_sport import _draw_impl


def draw(cr: Any, width: int, height: int, data: Any) -> None:
    _draw_impl(cr, width, height, data, dark=False)
