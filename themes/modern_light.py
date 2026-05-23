"""Modern Light dashboard theme for DrivePulse — inverted palette of Modern."""
from typing import Any

THEME_TYPE = "dashboard"
LABEL = {"en": "Modern Light", "de": "Modern Hell"}
CSS = """
window.dp-theme-modern-light .dp-gauge-bg,
window.dp-theme-modern-light .dp-gauge-bg > viewport,
window.dp-theme-modern-light .dp-gauge-bg > * {
  background-color: #c6c8cc;
}"""

from _dp_builtin_modern import _draw_impl


def draw(cr: Any, width: int, height: int, data: Any) -> None:
    _draw_impl(cr, width, height, data, dark=False)
