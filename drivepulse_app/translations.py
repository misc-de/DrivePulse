"""Translation catalog for DrivePulse.

Translations live in ``lang/<code>.json`` at the project root. Every JSON file
is a flat ``{key: text}`` mapping. To add a new language, drop a new
``lang/<code>.json`` file in there — no code change required. Each file should
include a ``language.name`` entry holding the language's own name (endonym),
which is what the language picker shows.
"""
from __future__ import annotations

import json
from pathlib import Path

from .diagnostics import get_logger


log = get_logger(__name__)


SOURCE_LANGUAGE = "en"

_LANG_DIR = Path(__file__).resolve().parent.parent / "lang"


def _load_translations() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if not _LANG_DIR.is_dir():
        log.warning("Language directory not found: %s", _LANG_DIR)
        return out
    for path in sorted(_LANG_DIR.glob("*.json")):
        code = path.stem.strip().lower()
        if not code:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Could not parse language file %s", path)
            continue
        if not isinstance(data, dict):
            log.warning("Language file %s is not a JSON object — skipping", path)
            continue
        out[code] = {str(k): str(v) for k, v in data.items()}
    return out


def _make_supported(translations: dict[str, dict[str, str]]) -> tuple[str, ...]:
    """Source language first, others alphabetical. Falls back to (SOURCE,) when empty."""
    codes = list(translations.keys())
    if not codes:
        return (SOURCE_LANGUAGE,)
    others = sorted(c for c in codes if c != SOURCE_LANGUAGE)
    if SOURCE_LANGUAGE in codes:
        return tuple([SOURCE_LANGUAGE] + others)
    return tuple(sorted(codes))


TRANSLATIONS: dict[str, dict[str, str]] = _load_translations()
SUPPORTED_LANGUAGES: tuple[str, ...] = _make_supported(TRANSLATIONS)


def language_name(code: str) -> str:
    """Return the endonym (the language's own name) for a given code."""
    entries = TRANSLATIONS.get(code, {})
    return entries.get("language.name") or code
