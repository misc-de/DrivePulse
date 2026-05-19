"""Gauge widget, built-in themes and user-theme plugin loader for DrivePulse."""
from __future__ import annotations

import importlib.util
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .diagnostics import get_logger

BUILTIN_THEMES_DIR = Path(__file__).resolve().parent.parent / "themes"
log = get_logger(__name__)


def _install_theme_import_aliases() -> None:
    """Keep existing theme files compatible after moving app code into a package."""
    from . import common, draw_helpers
    import importlib as _il

    sys.modules.setdefault("common", common)
    sys.modules.setdefault("draw_helpers", draw_helpers)
    # theme_defaults.py lives at the project root; register it so user themes
    # can do `from theme_defaults import ...` regardless of working directory.
    if "theme_defaults" not in sys.modules:
        _td_path = BUILTIN_THEMES_DIR.parent / "theme_defaults.py"
        if _td_path.exists():
            spec = _il.util.spec_from_file_location("theme_defaults", _td_path)
            if spec and spec.loader:
                mod = _il.util.module_from_spec(spec)
                sys.modules["theme_defaults"] = mod
                spec.loader.exec_module(mod)  # type: ignore[union-attr]


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


# Registry for built-in gauge themes (loaded from themes/*.py with THEME_TYPE="gauge")
_builtin_gauge_mods: dict[str, Any] = {}
# Registry for built-in dashboard themes (loaded from themes/*.py with THEME_TYPE="dashboard")
_builtin_dashboard_mods: dict[str, Any] = {}


def load_builtin_themes() -> None:
    """Scan BUILTIN_THEMES_DIR for *.py theme files and populate the builtin dicts."""
    _builtin_gauge_mods.clear()
    _builtin_dashboard_mods.clear()
    if not BUILTIN_THEMES_DIR.exists():
        return
    _install_theme_import_aliases()
    for path in sorted(BUILTIN_THEMES_DIR.glob("*.py"), key=lambda p: p.stem.lower()):
        if path.name.startswith("_"):
            continue
        stem = path.stem
        try:
            mod_name = f"_dp_builtin_{stem}"
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            theme_type = getattr(mod, "THEME_TYPE", "gauge")
            if theme_type == "dashboard":
                _builtin_dashboard_mods[stem] = mod
            else:
                _builtin_gauge_mods[stem] = mod
        except Exception:
            log.exception("Could not load built-in theme %s", path)


load_builtin_themes()

# Built-in theme identifiers (derived from loaded theme files)
GAUGE_THEMES: tuple[str, ...] = tuple(_builtin_gauge_mods.keys())
_DASHBOARD_THEME_IDS: tuple[str, ...] = tuple(_builtin_dashboard_mods.keys())

# Registry for user-supplied themes: stem -> (display_label, draw_fn | None, theme_css)
_user_themes: dict[str, tuple[str, Callable | None, str]] = {}

# ---------------------------------------------------------------------------
# Template written to THEMES_DIR on first run so the user has a starter file
# ---------------------------------------------------------------------------

_EXAMPLE_THEME = '''\
# DrivePulse – Custom Theme
# ──────────────────────────
# Filename (no leading underscore) = theme ID, e.g. "my_theme.py"
# Files with a leading underscore (_) are ignored when loading.
#
# Full documentation and minimal template:
#   <app-directory>/themes/_vorlage.py
#   <app-directory>/theme_defaults.py
#
# Required fields: THEME_TYPE, LABEL, draw()
# Optional:        CSS  (empty string = use app default)

THEME_TYPE = "gauge"
LABEL      = {"en": "My Theme", "de": "Mein Theme"}
CSS        = ""

from theme_defaults import ARC_START, ARC_SPAN, active_alpha

import math


def draw(cr, width, height, gauge):
    cx, cy, size, radius, lw, start, end, span, norm = gauge.arc_params(width, height)
    value_angle = start + span * norm
    a = active_alpha(gauge.active)
    r, g, b = gauge.accent_rgb if gauge.active else (0.45, 0.48, 0.50)

    # Background
    cr.set_source_rgb(0.05, 0.05, 0.08)
    cr.paint()

    # Arc track
    cr.set_line_width(lw)
    cr.set_line_cap(1)
    cr.set_source_rgba(0.30, 0.30, 0.35, 0.30)
    cr.arc(cx, cy, radius, start, end)
    cr.stroke()

    # Value arc
    cr.set_source_rgba(r, g, b, 0.90 * a)
    cr.arc(cx, cy, radius, start, value_angle)
    cr.stroke()

    # Text
    gauge.draw_text(cr, gauge.state.label, cx, cy - size * 0.06,
                    max(28, size * 0.19), a, True, size * 0.72)
    gauge.draw_text(cr, gauge.state.unit,  cx, cy + size * 0.09,
                    max(14, size * 0.075), 0.75 * a, True,  size * 0.72)
    gauge.draw_text(cr, gauge.title,       cx, cy + size * 0.26,
                    max(13, size * 0.062), 0.60 * a, False, size * 0.72)
'''


def _init_themes_dir(themes_dir: Path) -> None:
    """Create themes directory and write the starter template if missing."""
    themes_dir.mkdir(parents=True, exist_ok=True)
    sample = themes_dir / "_example.py"
    if not sample.exists():
        try:
            sample.write_text(_EXAMPLE_THEME, encoding="utf-8")
        except Exception:
            log.exception("Could not write sample theme file %s", sample)


def load_user_themes(themes_dir: Path, language: str = "en") -> None:
    """Scan themes_dir for *.py files (no leading underscore) and register them."""
    _init_themes_dir(themes_dir)
    _user_themes.clear()
    _install_theme_import_aliases()
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
            raw_label = getattr(mod, "LABEL", stem)
            # LABEL may be a localised dict {"en": "...", "de": "..."} or a plain string.
            if isinstance(raw_label, dict):
                label = raw_label.get(language) or raw_label.get("en") or stem
            else:
                label = str(raw_label) or stem
            draw_fn = getattr(mod, "draw", None)
            accel_css = str(getattr(mod, "CSS", "") or getattr(mod, "acceleration_css", ""))
            # Accept theme if it defines at least one of draw / CSS
            if callable(draw_fn) or accel_css:
                _user_themes[stem] = (label, draw_fn if callable(draw_fn) else None, accel_css)
        except Exception:
            log.exception("Could not load user theme %s", path)


def _resolve_label(mod: Any, language: str) -> str:
    """Read the display label from a theme module for the given language."""
    label = getattr(mod, "LABEL", None)
    if isinstance(label, dict):
        return label.get(language) or label.get("en") or next(iter(label.values()), "?")
    if isinstance(label, str) and label:
        return label
    return getattr(mod, "__name__", "?")


def all_theme_options(language: str = "en") -> list[tuple[str, str]]:
    """Return [(theme_id, display_label)] for built-in gauge + dashboard + user themes."""
    gauge = [(stem, _resolve_label(mod, language)) for stem, mod in _builtin_gauge_mods.items()]
    dash  = [(stem, _resolve_label(mod, language)) for stem, mod in _builtin_dashboard_mods.items()]
    user  = [(f"user:{stem}", info[0]) for stem, info in _user_themes.items()]
    return gauge + dash + user


def get_theme_css(theme_id: str) -> str:
    """Return full theme CSS (gauge-page background + accel-page styles). Empty = use default."""
    if theme_id.startswith("user:"):
        stem = theme_id[5:]
        return _user_themes[stem][2] if stem in _user_themes else ""
    mod = _builtin_gauge_mods.get(theme_id) or _builtin_dashboard_mods.get(theme_id)
    return getattr(mod, "CSS", "") if mod else ""


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
    source_label: str = ""


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

    def set_source_label(self, text: str) -> None:
        """Discreet source annotation (e.g. 'OBD'/'GPS') drawn over the gauge."""
        text = text or ""
        if text == self.state.source_label:
            return
        self.state.source_label = text
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
        drew_theme = False
        if self.theme.startswith("user:"):
            stem = self.theme[5:]
            if stem in _user_themes:
                draw_fn = _user_themes[stem][1]
                if draw_fn is not None:
                    try:
                        draw_fn(cr, w, h, self)
                        drew_theme = True
                    except Exception:
                        log.exception("Could not draw user gauge theme %s", self.theme)
        if not drew_theme:
            mod = _builtin_gauge_mods.get(self.theme) or _builtin_gauge_mods.get("cockpit")
            if mod:
                draw_fn = getattr(mod, "draw", None)
                if callable(draw_fn):
                    draw_fn(cr, w, h, self)
        self._draw_source_overlay(cr, w, h)

    def _draw_source_overlay(self, cr: Any, width: int, height: int) -> None:
        text = self.state.source_label
        if not text or not self.active:
            return
        size = min(width, height)
        cx = width / 2
        cy = height / 2
        font_size = max(9.0, size * 0.052)
        self._draw_text_centered(cr, text, cx, cy - size * 0.22,
                                 font_size, 0.55, False, size * 0.5)
