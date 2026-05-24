"""Tests for common.py helpers: env parsing, language normalisation,
translation lookup. _translate is invoked on every label in the app — a
silent regression here surfaces as either falling-back-to-key strings
visible in the UI or a KeyError mid-render."""
from __future__ import annotations

import pytest

from drivepulse_app.common import (
    _detect_language,
    _env_float,
    _env_int_or_none,
    _normalize_language,
    _translate,
)
from drivepulse_app.translations import SOURCE_LANGUAGE, SUPPORTED_LANGUAGES


# ─── _env_float / _env_int_or_none ────────────────────────────────────────────

def test_env_float_parses_valid(monkeypatch):
    monkeypatch.setenv("DP_TEST_F", "1.25")
    assert _env_float("DP_TEST_F", default=0.5) == 1.25


def test_env_float_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("DP_TEST_F", "not-a-float")
    assert _env_float("DP_TEST_F", default=0.5) == 0.5


def test_env_float_uses_default_when_unset(monkeypatch):
    monkeypatch.delenv("DP_TEST_F", raising=False)
    assert _env_float("DP_TEST_F", default=0.5) == 0.5


def test_env_int_or_none_parses_valid(monkeypatch):
    monkeypatch.setenv("DP_TEST_I", "38400")
    assert _env_int_or_none("DP_TEST_I") == 38400


def test_env_int_or_none_returns_none_when_empty(monkeypatch):
    monkeypatch.setenv("DP_TEST_I", "")
    assert _env_int_or_none("DP_TEST_I") is None


def test_env_int_or_none_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("DP_TEST_I", raising=False)
    assert _env_int_or_none("DP_TEST_I") is None


def test_env_int_or_none_returns_none_on_garbage(monkeypatch):
    monkeypatch.setenv("DP_TEST_I", "abc")
    assert _env_int_or_none("DP_TEST_I") is None


# ─── _normalize_language ──────────────────────────────────────────────────────

def test_normalize_language_passes_supported_through():
    for lang in SUPPORTED_LANGUAGES:
        assert _normalize_language(lang) == lang


def test_normalize_language_strips_region_and_encoding():
    # LANG-style values from libc: "de_DE.UTF-8" → "de"
    assert _normalize_language("de_DE.UTF-8") == "de"
    assert _normalize_language("en_GB") == "en"
    assert _normalize_language("de-AT") == "de"


def test_normalize_language_unknown_falls_back_to_source():
    # An unsupported tag should not crash the app — just speak the source
    # language until the user picks a real one.
    assert _normalize_language("klingon") == SOURCE_LANGUAGE


def test_normalize_language_none_falls_back_to_source():
    assert _normalize_language(None) == SOURCE_LANGUAGE
    assert _normalize_language("") == SOURCE_LANGUAGE


# ─── _detect_language ────────────────────────────────────────────────────────

def test_detect_language_uses_drivepulse_env_when_set(monkeypatch):
    monkeypatch.setenv("DRIVEPULSE_LANG", "de_DE.UTF-8")
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    # DRIVEPULSE_LANG wins over LANG.
    assert _detect_language() == "de"


def test_detect_language_falls_back_to_LANG(monkeypatch):
    monkeypatch.delenv("DRIVEPULSE_LANG", raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    assert _detect_language() == "en"


# ─── _translate ──────────────────────────────────────────────────────────────

def test_translate_known_key_returns_localised_text():
    # Any key that exists in SOURCE_LANGUAGE should resolve to a non-key
    # string. We don't assert the exact text (translations evolve) — just
    # that it doesn't fall through to the raw key.
    from drivepulse_app.translations import TRANSLATIONS
    sample_key = next(iter(TRANSLATIONS[SOURCE_LANGUAGE]))
    out = _translate(SOURCE_LANGUAGE, sample_key)
    assert isinstance(out, str)
    assert out != sample_key or out == TRANSLATIONS[SOURCE_LANGUAGE][sample_key]


def test_translate_missing_key_falls_back_to_key_text():
    # An unknown key never crashes; the caller gets the key back so the UI
    # at least shows *something* and the gap is debuggable in-place.
    out = _translate(SOURCE_LANGUAGE, "no.such.key.exists.anywhere")
    assert out == "no.such.key.exists.anywhere"


def test_translate_applies_format_kwargs():
    # Pick a known formattable string from the translation map. If none
    # exists, fall back to checking the path doesn't crash on kwargs.
    from drivepulse_app.translations import TRANSLATIONS
    formattable = None
    for key, text in TRANSLATIONS[SOURCE_LANGUAGE].items():
        if "{" in text and "}" in text:
            formattable = (key, text)
            break
    if formattable is None:
        pytest.skip("no formattable translation in source language to verify")
    key, raw = formattable
    # Extract one placeholder name and supply a value for it.
    import re
    placeholders = re.findall(r"\{(\w+)[^}]*\}", raw)
    if not placeholders:
        pytest.skip("formattable detection picked a non-named placeholder")
    name = placeholders[0]
    out = _translate(SOURCE_LANGUAGE, key, **{name: "TESTVAL"})
    assert "TESTVAL" in out


def test_translate_unsupported_language_falls_back_to_source():
    # Pick a key that exists in the source language.
    from drivepulse_app.translations import TRANSLATIONS
    sample_key = next(iter(TRANSLATIONS[SOURCE_LANGUAGE]))
    expected = TRANSLATIONS[SOURCE_LANGUAGE][sample_key]
    # Unknown language → falls through to source.
    assert _translate("klingon", sample_key) == expected
