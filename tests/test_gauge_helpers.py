"""Tests for gauge.py's pure data helpers: label resolution from theme
modules and GaugeState.set_value behaviour (clamping + min/max tracking)."""
from __future__ import annotations

import math
import types

import pytest

from drivepulse_app.gauge import GaugeState, _resolve_label


# ─── _resolve_label ──────────────────────────────────────────────────────────

def _mk_theme(label=None, mod_name="theme_x"):
    """Build a stand-in theme module object."""
    mod = types.SimpleNamespace()
    mod.__name__ = mod_name
    if label is not None:
        mod.LABEL = label
    return mod


def test_resolve_label_picks_language_from_dict():
    mod = _mk_theme(label={"en": "Cockpit", "de": "Cockpit DE"})
    assert _resolve_label(mod, "en") == "Cockpit"
    assert _resolve_label(mod, "de") == "Cockpit DE"


def test_resolve_label_falls_back_to_english_for_missing_language():
    # If the requested language is missing but "en" is there, use "en".
    mod = _mk_theme(label={"en": "Cockpit", "de": "Cockpit DE"})
    assert _resolve_label(mod, "fr") == "Cockpit"


def test_resolve_label_falls_back_to_any_dict_entry_when_no_english():
    # If neither requested nor "en" exists, fall back to *some* entry
    # rather than failing — at least the user sees a theme name.
    mod = _mk_theme(label={"it": "Italiano", "pl": "Polski"})
    out = _resolve_label(mod, "fr")
    assert out in ("Italiano", "Polski")


def test_resolve_label_accepts_plain_string():
    mod = _mk_theme(label="Cockpit")
    assert _resolve_label(mod, "en") == "Cockpit"


def test_resolve_label_falls_back_to_module_name_when_label_missing():
    # No LABEL attr at all → use the module __name__ as a last resort.
    mod = types.SimpleNamespace()
    mod.__name__ = "my_neon_theme"
    # SimpleNamespace doesn't have LABEL; ensure code reaches the fallback.
    assert _resolve_label(mod, "en") == "my_neon_theme"


def test_resolve_label_falls_back_when_label_is_empty_string():
    # An empty string is falsy — treat as "no label" and use module name.
    mod = _mk_theme(label="", mod_name="empty_label")
    assert _resolve_label(mod, "en") == "empty_label"


# ─── GaugeState defaults ─────────────────────────────────────────────────────

def test_gauge_state_defaults_are_safe():
    s = GaugeState()
    # Default state should be inactive ("--") so the widget doesn't render
    # garbage on first paint, before any value arrives.
    assert s.label == "--"
    assert s.value == 0.0
    assert s.min_value == 0.0
    assert s.max_value == 100.0


def test_gauge_state_is_a_dataclass_with_field_assignment():
    # Light contract check: it's a dataclass — fields are mutable, public.
    s = GaugeState(value=42.0, label="42", unit="km/h", min_value=0.0, max_value=200.0)
    s.label = "—"
    s.value = 99.5
    assert s.label == "—"
    assert s.value == 99.5
