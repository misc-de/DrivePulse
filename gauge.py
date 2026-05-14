"""Gauge widget, built-in themes and user-theme plugin loader for DrivePulse."""
from __future__ import annotations

import importlib.util
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402


def _gauge_apply_rotation(cr: Any, width: int, height: int, angle: int) -> tuple[int, int]:
    """Rotate the Cairo context for a gauge DrawingArea.

    Gauges are square so 90°/270° just swaps the coordinate origin;
    the circular drawing itself is unaffected but text stays readable.
    Returns effective (w, h) — always (width, height) since gauges are square.
    """
    if angle == 90:
        cr.translate(0, height)
        cr.rotate(-math.pi / 2)
    elif angle == 180:
        cr.translate(width, height)
        cr.rotate(math.pi)
    elif angle == 270:
        cr.translate(width, 0)
        cr.rotate(math.pi / 2)
    # Gauges are always drawn square, effective size is unchanged
    return width, height


# Built-in theme identifiers (order = order in settings dropdown)
GAUGE_THEMES = ("cockpit", "neon", "minimal")

# Full theme CSS per built-in theme.
# Targets window.dp-theme-<id> for page backgrounds and
# .dp-accel-theme-<id> for acceleration-page widget styles.
_BUILTIN_THEME_CSS: dict[str, str] = {
    "cockpit": """
window.dp-theme-cockpit,
window.dp-theme-cockpit scrolledwindow,
window.dp-theme-cockpit scrolledwindow > viewport,
window.dp-theme-cockpit .dp-gauge-bg,
window.dp-theme-cockpit .dp-gauge-bg > * {
  background-color: #05080f;
}
.dp-accel-theme-cockpit .card {
  background-color: rgba(8, 14, 22, 0.8);
  border-radius: 6px;
}
""",
    "neon": """
window.dp-theme-neon,
window.dp-theme-neon scrolledwindow,
window.dp-theme-neon scrolledwindow > viewport,
window.dp-theme-neon .dp-gauge-bg,
window.dp-theme-neon .dp-gauge-bg > * {
  background-color: #000008;
}
.dp-accel-theme-neon .card {
  background-color: rgba(0, 2, 15, 0.9);
  border-radius: 4px;
}
.dp-accel-theme-neon .heading { color: #7ec8ff; }
.dp-accel-theme-neon .title-1 { color: #a4d8ff; }
.dp-accel-theme-neon .title-2 { color: #5ba8ff; }
.dp-accel-theme-neon .dim-label { color: rgba(100, 180, 255, 0.65); }
""",
    "minimal": """
.dp-accel-theme-minimal .card {
  background-color: transparent;
  border-radius: 0;
  padding-top: 4px;
  padding-bottom: 4px;
}
""",
}

# Registry for user-supplied themes: stem -> (display_label, draw_fn | None, theme_css)
_user_themes: dict[str, tuple[str, Callable | None, str]] = {}

# ---------------------------------------------------------------------------
# Template written to THEMES_DIR on first run so the user has a starter file
# ---------------------------------------------------------------------------

_EXAMPLE_THEME = '''\
# DrivePulse – Theme Vorlage
# Dateiname (ohne Unterstrich) = Theme-ID.
# Dateien mit Unterstrich am Anfang werden ignoriert.
#
# PFLICHT : LABEL             – Text der in den Einstellungen erscheint
# OPTIONAL: draw()            – Gauge-Zeichenfunktion  (Fallback: cockpit)
# OPTIONAL: acceleration_css  – CSS-String für Beschleunigungsseite (Fallback: Standard)
#
# Beide sind unabhängig – man kann nur eines oder beides definieren.
#
# ── draw() ──────────────────────────────────────────────────────────────────
# Parameter:
#   cr           – Cairo-Kontext
#   width/height – Widget-Größe in Pixeln
#   gauge        – Gauge-Objekt:
#     gauge.title               – Bezeichnung (z.B. "RPM")
#     gauge.accent_rgb          – (r, g, b) Akzentfarbe, Floats 0–1
#     gauge.active              – bool, False wenn kein Signal
#     gauge.state.value         – aktueller Wert (float)
#     gauge.state.label         – Anzeigetext (z.B. "3200")
#     gauge.state.unit          – Einheit (z.B. "rpm")
#     gauge.state.min_value / max_value
#     gauge.arc_params(w, h)    → cx, cy, size, radius, lw,
#                                  start, end, span, normalized (0–1)
#     gauge.draw_text(cr, text, x, y, size,
#                     alpha=1.0, bold=False, max_width=None)
#
# ── acceleration_css ─────────────────────────────────────────────────────────
# GTK-CSS-String. Die Beschleunigungsseite bekommt die Klasse
#   .dp-accel-theme-<dateiname>
# Damit lassen sich z.B. Karten, Farben und Schriften anpassen:
#   .dp-accel-theme-meinname .card  { background-color: rgba(0,0,0,0.8); }
#   .dp-accel-theme-meinname .heading { color: #ff4444; }

LABEL = "Mein Theme"

import math


def draw(cr, width, height, gauge):
    cx, cy, size, radius, lw, start, end, span, norm = gauge.arc_params(width, height)
    value_angle = start + span * norm
    alpha = 1.0 if gauge.active else 0.3
    r, g, b = gauge.accent_rgb if gauge.active else (0.45, 0.48, 0.50)

    # Hintergrund
    cr.set_source_rgb(0.05, 0.05, 0.08)
    cr.paint()

    # Track
    cr.set_line_width(lw)
    cr.set_line_cap(1)
    cr.set_source_rgba(0.3, 0.3, 0.35, 0.3)
    cr.arc(cx, cy, radius, start, end)
    cr.stroke()

    # Wertbogen
    cr.set_source_rgba(r, g, b, 0.9 * alpha)
    cr.arc(cx, cy, radius, start, value_angle)
    cr.stroke()

    # Text
    gauge.draw_text(cr, gauge.state.label, cx, cy - size * 0.06,
                    max(28, size * 0.19), alpha, True, size * 0.72)
    gauge.draw_text(cr, gauge.state.unit, cx, cy + size * 0.09,
                    max(14, size * 0.075), 0.75 * alpha, True, size * 0.72)
    gauge.draw_text(cr, gauge.title, cx, cy + size * 0.26,
                    max(13, size * 0.062), 0.6 * alpha, False, size * 0.72)


# Optionaler CSS-String für die Beschleunigungsseite:
acceleration_css = """
.dp-accel-theme-mein-theme .card {
  background-color: rgba(10, 10, 20, 0.8);
  border-radius: 6px;
}
.dp-accel-theme-mein-theme .heading { color: #88aaff; }
"""
'''


def _init_themes_dir(themes_dir: Path) -> None:
    """Create themes directory and write the starter template if missing."""
    themes_dir.mkdir(parents=True, exist_ok=True)
    sample = themes_dir / "_beispiel.py"
    if not sample.exists():
        try:
            sample.write_text(_EXAMPLE_THEME, encoding="utf-8")
        except Exception:
            pass


def load_user_themes(themes_dir: Path) -> None:
    """Scan themes_dir for *.py files (no leading underscore) and register them."""
    _init_themes_dir(themes_dir)
    _user_themes.clear()
    for path in sorted(themes_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        stem = path.stem
        try:
            spec = importlib.util.spec_from_file_location(f"_dp_theme_{stem}", path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            label = str(getattr(mod, "LABEL", stem))
            draw_fn = getattr(mod, "draw", None)
            accel_css = str(getattr(mod, "acceleration_css", ""))
            # Accept theme if it defines at least one of the two
            if callable(draw_fn) or accel_css:
                _user_themes[stem] = (label, draw_fn if callable(draw_fn) else None, accel_css)
        except Exception:
            pass  # silently skip broken theme files


_DASHBOARD_THEME_IDS = ("digital", "sport", "racing", "analog")


def all_theme_options(translate_fn: Callable[[str], str]) -> list[tuple[str, str]]:
    """Return [(theme_id, display_label)] for built-ins + dashboard + user themes."""
    builtin = [(t, translate_fn(f"settings.gauge_theme.{t}")) for t in GAUGE_THEMES]
    dash = [(t, translate_fn(f"settings.gauge_theme.{t}")) for t in _DASHBOARD_THEME_IDS]
    user = [(f"user:{stem}", label) for stem, (label, _, _css) in _user_themes.items()]
    return builtin + dash + user


def get_theme_css(theme_id: str) -> str:
    """Return full theme CSS (gauge-page background + accel-page styles). Empty = use default."""
    if theme_id.startswith("user:"):
        stem = theme_id[5:]
        return _user_themes[stem][2] if stem in _user_themes else ""
    return _BUILTIN_THEME_CSS.get(theme_id, "")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class GaugeState:
    value: float = 0.0
    label: str = "--"
    unit: str = ""
    min_value: float = 0.0
    max_value: float = 100.0


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class Gauge(Gtk.DrawingArea):
    __gtype_name__ = "Gauge"

    def __init__(
        self,
        title: str,
        unit: str,
        min_value: float,
        max_value: float,
        accent_rgb: tuple[float, float, float],
        theme: str = "cockpit",
    ) -> None:
        super().__init__()
        self.title = title
        self.accent_rgb = accent_rgb
        self.theme = theme
        self._rotation = 0  # degrees: 0, 90, 180, 270
        self.state = GaugeState(
            value=0,
            label="--",
            unit=unit,
            min_value=min_value,
            max_value=max_value,
        )
        self.active = False
        self.set_content_width(1)
        self.set_content_height(1)
        self.set_size_request(1, 1)
        self.set_draw_func(self._draw)

    def set_value(self, value: float | None, label: str | None = None) -> None:
        if value is None or math.isnan(value):
            self.state.label = "--"
            self.state.value = self.state.min_value
            self.active = False
        else:
            self.state.value = max(self.state.min_value, min(self.state.max_value, value))
            self.state.label = label if label is not None else f"{value:.0f}"
            self.active = True
        self.queue_draw()

    def set_theme(self, theme: str) -> None:
        self.theme = theme
        self.queue_draw()

    def set_rotation(self, angle: int) -> None:
        """Physical device rotation in degrees (0/90/180/270)."""
        self._rotation = angle % 360
        self.queue_draw()

    # ------------------------------------------------------------------
    # Public helpers exposed to user themes
    # ------------------------------------------------------------------

    def arc_params(self, width: int, height: int) -> tuple:
        return self._arc_params(width, height)

    def draw_text(
        self,
        cr: Any,
        text: str,
        x: float,
        y: float,
        size: float,
        alpha: float = 1.0,
        bold: bool = False,
        max_width: float | None = None,
    ) -> None:
        self._draw_text_centered(cr, text, x, y, size, alpha, bold, max_width)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _arc_params(self, width: int, height: int) -> tuple:
        size = min(width, height)
        cx = width / 2
        cy = height / 2
        radius = size * 0.39
        line_width = max(7, size * 0.035)
        start = math.radians(135)
        end = math.radians(405)
        span = end - start
        normalized = (self.state.value - self.state.min_value) / (self.state.max_value - self.state.min_value)
        normalized = max(0.0, min(1.0, normalized))
        return cx, cy, size, radius, line_width, start, end, span, normalized

    def _draw_text_centered(
        self,
        cr: Any,
        text: str,
        x: float,
        y: float,
        size: float,
        alpha: float = 1.0,
        bold: bool = False,
        max_width: float | None = None,
    ) -> None:
        cr.select_font_face("Cantarell", 0, 1 if bold else 0)
        cr.set_font_size(size)
        ext = cr.text_extents(text)
        if max_width is not None and ext.width > max_width:
            size = max(9, size * (max_width / max(1, ext.width)))
            cr.set_font_size(size)
            ext = cr.text_extents(text)
        cr.set_source_rgba(0.94, 0.96, 1.0, alpha)
        cr.move_to(x - ext.width / 2 - ext.x_bearing, y - ext.height / 2 - ext.y_bearing)
        cr.show_text(text)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _draw(self, area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        w, h = _gauge_apply_rotation(cr, width, height, self._rotation)
        if self.theme.startswith("user:"):
            stem = self.theme[5:]
            if stem in _user_themes:
                draw_fn = _user_themes[stem][1]
                if draw_fn is not None:
                    try:
                        draw_fn(cr, w, h, self)
                        return
                    except Exception:
                        pass  # fall through to default on error
                # draw_fn is None = only CSS theme, use cockpit for gauge
        if self.theme == "neon":
            self._draw_neon(cr, w, h)
        elif self.theme == "minimal":
            self._draw_minimal(cr, w, h)
        else:
            self._draw_cockpit(cr, w, h)

    # ------------------------------------------------------------------
    # Theme: cockpit (default)
    # ------------------------------------------------------------------

    def _draw_cockpit(self, cr: Any, width: int, height: int) -> None:
        cx, cy, size, radius, line_width, start_angle, end_angle, span, normalized = self._arc_params(width, height)
        value_angle = start_angle + span * normalized
        active_alpha = 1.0 if self.active else 0.34
        accent = self.accent_rgb if self.active else (0.45, 0.48, 0.50)

        # Fill the entire DrawingArea rectangle so no app-background bleeds through
        cr.set_source_rgb(0.02, 0.025, 0.03)
        cr.paint()
        cr.arc(cx, cy, radius + line_width * 1.15, 0, math.tau)
        cr.fill()

        cr.set_line_width(2.0)
        cr.set_source_rgba(0.86, 0.91, 0.96, 0.85 * active_alpha)
        cr.arc(cx, cy, radius + line_width * 1.4, start_angle, end_angle)
        cr.stroke()

        cr.set_line_width(line_width)
        cr.set_line_cap(1)
        cr.set_source_rgba(0.35, 0.42, 0.48, 0.28 if self.active else 0.16)
        cr.arc(cx, cy, radius, start_angle, end_angle)
        cr.stroke()

        cr.set_source_rgba(accent[0], accent[1], accent[2], 0.92 * active_alpha)
        cr.arc(cx, cy, radius, start_angle, value_angle)
        cr.stroke()

        cr.set_line_width(2.0)
        for index in range(0, 11):
            angle = start_angle + span * (index / 10)
            outer = radius + line_width * 0.8
            inner = radius + line_width * (0.18 if index % 5 else -0.4)
            cr.set_source_rgba(0.95, 0.97, 1.0, (0.75 if index % 5 else 0.95) * active_alpha)
            cr.move_to(cx + math.cos(angle) * inner, cy + math.sin(angle) * inner)
            cr.line_to(cx + math.cos(angle) * outer, cy + math.sin(angle) * outer)
            cr.stroke()

        cr.set_source_rgba(1, 1, 1, 0.95 * active_alpha)
        top = -math.pi / 2
        cr.move_to(cx + math.cos(top) * (radius + line_width * 1.5), cy + math.sin(top) * (radius + line_width * 1.5))
        cr.line_to(cx + math.cos(top - 0.06) * (radius + line_width * 0.25), cy + math.sin(top - 0.06) * (radius + line_width * 0.25))
        cr.line_to(cx + math.cos(top + 0.06) * (radius + line_width * 0.25), cy + math.sin(top + 0.06) * (radius + line_width * 0.25))
        cr.close_path()
        cr.fill()

        value_size = max(28, size * 0.19)
        unit_size = max(14, size * 0.075)
        title_size = max(13, size * 0.062)
        text_width = size * 0.72
        self._draw_text_centered(cr, self.state.label, cx, cy - size * 0.06, value_size, active_alpha, True, text_width)
        self._draw_text_centered(cr, self.state.unit, cx, cy + size * 0.09, unit_size, 0.78 * active_alpha, True, text_width)
        self._draw_text_centered(cr, self.title, cx, cy + size * 0.26, title_size, 0.62 * active_alpha, False, text_width)

    # ------------------------------------------------------------------
    # Theme: neon
    # ------------------------------------------------------------------

    def _draw_neon(self, cr: Any, width: int, height: int) -> None:
        cx, cy, size, radius, line_width, start_angle, end_angle, span, normalized = self._arc_params(width, height)
        value_angle = start_angle + span * normalized
        active_alpha = 1.0 if self.active else 0.3
        accent = self.accent_rgb if self.active else (0.35, 0.38, 0.42)
        r, g, b = accent

        cr.set_source_rgb(0.0, 0.0, 0.03)
        cr.paint()

        cr.set_line_width(1.0)
        cr.set_source_rgba(r, g, b, 0.18 * active_alpha)
        cr.arc(cx, cy, radius + line_width * 1.6, start_angle, end_angle)
        cr.stroke()

        cr.set_line_width(line_width * 0.55)
        cr.set_line_cap(1)
        cr.set_source_rgba(0.18, 0.20, 0.24, 0.7)
        cr.arc(cx, cy, radius, start_angle, end_angle)
        cr.stroke()

        for lw_mult, a_mult in ((5.0, 0.03), (3.2, 0.08), (1.8, 0.20), (0.65, 1.0)):
            cr.set_line_width(line_width * lw_mult)
            cr.set_source_rgba(r, g, b, a_mult * active_alpha)
            cr.arc(cx, cy, radius, start_angle, value_angle)
            cr.stroke()

        dot_x = cx + math.cos(value_angle) * radius
        dot_y = cy + math.sin(value_angle) * radius
        for dot_r, dot_a in ((line_width * 1.4, 0.12), (line_width * 0.7, 0.4), (line_width * 0.3, 1.0)):
            cr.set_source_rgba(1.0, 1.0, 1.0, dot_a * active_alpha)
            cr.arc(dot_x, dot_y, dot_r, 0, math.tau)
            cr.fill()

        value_size = max(30, size * 0.20)
        unit_size = max(13, size * 0.072)
        title_size = max(12, size * 0.058)
        text_width = size * 0.72

        self._draw_text_centered(cr, self.state.label, cx, cy - size * 0.05, value_size, active_alpha, True, text_width)

        cr.select_font_face("Cantarell", 0, 1)
        cr.set_font_size(unit_size)
        ext = cr.text_extents(self.state.unit)
        cr.set_source_rgba(r, g, b, 0.9 * active_alpha)
        cr.move_to(cx - ext.width / 2 - ext.x_bearing, (cy + size * 0.10) - ext.height / 2 - ext.y_bearing)
        cr.show_text(self.state.unit)

        self._draw_text_centered(cr, self.title, cx, cy + size * 0.27, title_size, 0.45 * active_alpha, False, text_width)

    # ------------------------------------------------------------------
    # Theme: minimal
    # ------------------------------------------------------------------

    def _draw_minimal(self, cr: Any, width: int, height: int) -> None:
        size = min(width, height)
        cx = width / 2
        cy = height / 2
        radius = size * 0.41
        line_width = max(4, size * 0.021)

        # Minimal uses the system/window background — clear to transparent
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()

        start_angle = math.radians(145)
        end_angle = math.radians(395)
        span = end_angle - start_angle
        normalized = (self.state.value - self.state.min_value) / (self.state.max_value - self.state.min_value)
        normalized = max(0.0, min(1.0, normalized))
        value_angle = start_angle + span * normalized
        active_alpha = 1.0 if self.active else 0.28
        accent = self.accent_rgb if self.active else (0.5, 0.52, 0.55)

        cr.set_line_width(line_width)
        cr.set_line_cap(1)
        cr.set_source_rgba(0.45, 0.48, 0.52, 0.22)
        cr.arc(cx, cy, radius, start_angle, end_angle)
        cr.stroke()

        cr.set_source_rgba(accent[0], accent[1], accent[2], 0.88 * active_alpha)
        cr.arc(cx, cy, radius, start_angle, value_angle)
        cr.stroke()

        cr.set_line_width(1.5)
        for i in range(5):
            angle = start_angle + span * (i / 4)
            outer = radius + line_width * 1.6
            inner = radius + line_width * 0.4
            cr.set_source_rgba(0.75, 0.78, 0.82, 0.55 * active_alpha)
            cr.move_to(cx + math.cos(angle) * inner, cy + math.sin(angle) * inner)
            cr.line_to(cx + math.cos(angle) * outer, cy + math.sin(angle) * outer)
            cr.stroke()

        dot_x = cx + math.cos(value_angle) * radius
        dot_y = cy + math.sin(value_angle) * radius
        cr.set_source_rgba(accent[0], accent[1], accent[2], active_alpha)
        cr.arc(dot_x, dot_y, line_width * 0.55, 0, math.tau)
        cr.fill()

        title_size = max(12, size * 0.057)
        self._draw_text_centered(cr, self.title, cx, cy - size * 0.20, title_size, 0.52 * active_alpha, False, size * 0.75)

        value_size = max(30, size * 0.21)
        self._draw_text_centered(cr, self.state.label, cx, cy + size * 0.03, value_size, active_alpha, True, size * 0.75)

        unit_size = max(12, size * 0.063)
        self._draw_text_centered(cr, self.state.unit, cx, cy + size * 0.18, unit_size, 0.65 * active_alpha, False, size * 0.75)
