"""Racing Light dashboard theme for DrivePulse — inverted palette of Racing."""
from typing import Any

THEME_TYPE = "dashboard"
LABEL = {"en": "Racing Light", "de": "Racing Hell"}
CSS = """
window.dp-theme-racing-light,
window.dp-theme-racing-light toolbarview,
window.dp-theme-racing-light scrolledwindow,
window.dp-theme-racing-light scrolledwindow > viewport,
window.dp-theme-racing-light .dp-gauge-bg,
window.dp-theme-racing-light .dp-gauge-bg > * {
  background-color: #f0f1f3;
}"""

from _dp_builtin_racing import _draw_impl


def draw(cr: Any, width: int, height: int, data: Any) -> None:
    _draw_impl(cr, width, height, data, dark=False)
