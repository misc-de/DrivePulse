"""Pure helpers for the live OBD session view in CarsPage.

``CarsPage.update_live`` receives a telemetry payload on every poll and folds
its values into two dicts: ``_latest_live`` (the most recent value per key) and
``_live_session_stats`` (running min/max per tracked PID). That folding is pure
dict maths, so it lives here where it can be unit-tested without GTK.
"""

from __future__ import annotations

from collections.abc import Container
from typing import Any

# Payload keys that are transport/metadata, not telemetry values.
META_KEYS = frozenset({"source", "timestamp", "connection_status", "mock_reason"})


def extract_session_number(v: Any) -> float | None:
    """Coerce a telemetry value (raw number or ``{"value": ...}`` dict) to float."""
    if isinstance(v, dict) and "value" in v:
        try:
            return float(v["value"])
        except (TypeError, ValueError):
            return None
    if isinstance(v, (int, float)):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return None


def fold_live_payload(
    payload: dict[str, Any],
    latest_live: dict[str, Any],
    session_stats: dict[str, dict[str, Any]],
    live_keys: Container[str],
) -> None:
    """Fold a telemetry ``payload`` into ``latest_live`` and ``session_stats``.

    Both target dicts are mutated in place. Private (``_``-prefixed) and metadata
    keys are ignored. For keys in ``live_keys`` the running ``min``/``max`` (and
    last-seen ``unit``) are tracked across the session.
    """
    for k, v in payload.items():
        if k.startswith("_") or k in META_KEYS:
            continue
        latest_live[k] = v
        if k in live_keys:
            num = extract_session_number(v)
            if num is not None:
                stats = session_stats.setdefault(k, {})
                stats["unit"] = v.get("unit", "") if isinstance(v, dict) else ""
                stats["min"] = num if "min" not in stats else min(stats["min"], num)
                stats["max"] = num if "max" not in stats else max(stats["max"], num)
