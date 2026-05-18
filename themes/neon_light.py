"""Neon Light gauge theme for DrivePulse — inverted palette of Neon."""
from typing import Any

THEME_TYPE = "gauge"
LABEL = {"en": "Neon Light", "de": "Neon Hell"}
CSS = """
window.dp-theme-neon-light,
window.dp-theme-neon-light toolbarview,
window.dp-theme-neon-light scrolledwindow,
window.dp-theme-neon-light scrolledwindow > viewport,
window.dp-theme-neon-light .dp-gauge-bg,
window.dp-theme-neon-light .dp-gauge-bg > * {
  background-color: #eef0f2;
}
.dp-accel-theme-neon-light .card {
  background-color: rgba(248, 250, 252, 0.9);
  border-radius: 4px;
}
.dp-accel-theme-neon-light .heading    { color: #1466a8; }
.dp-accel-theme-neon-light .title-1    { color: #0a4274; }
.dp-accel-theme-neon-light .title-2    { color: #1466a8; }
.dp-accel-theme-neon-light .dim-label  { color: rgba(40,80,120,0.7); }"""

from _dp_builtin_neon import _draw_impl


def draw(cr: Any, width: int, height: int, gauge: Any) -> None:
    _draw_impl(cr, width, height, gauge, dark=False)
