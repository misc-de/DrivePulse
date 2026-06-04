"""Persistence of the *active* navigation tour's progress.

A running tour (its remaining destination waypoints) lives only in RAM, so an
app restart mid-drive used to lose all progress — on restart the tour would
route back through an intermediate waypoint the driver had already reached.

This module persists just enough to resume: the list of *remaining* destination
waypoints (intermediate vias + final destination, in order) plus the saved
tour's name/id for display. The route geometry is intentionally NOT stored — on
resume it is recomputed from the current GPS position to the remaining
waypoints, which also yields a fresh route from wherever the driver now is.
"""
from __future__ import annotations

import json
import time
from typing import Any

from drivepulse_app.common import LOG_DIR
from drivepulse_app.diagnostics import atomic_write_text, get_logger

log = get_logger(__name__)

_STATE_FILE = LOG_DIR / "active_tour.json"

# A persisted tour older than this is treated as stale and ignored on load —
# resuming a half-day-old drive is almost never what the driver wants.
_MAX_AGE_S = 12 * 3600


def save_active_tour(
    remaining: list[tuple[float, float]],
    *,
    name: str | None = None,
    tour_id: int | None = None,
) -> None:
    """Persist the remaining destination waypoints of the running tour.

    Called whenever progress changes (tour begun, intermediate waypoint
    reached, a bypassed via dropped). An empty ``remaining`` clears the state
    instead, so a finished or aborted tour leaves nothing to resume.
    """
    if not remaining:
        clear_active_tour()
        return
    payload = {
        "remaining": [[float(lat), float(lon)] for lat, lon in remaining],
        "name": name,
        "id": tour_id,
        "saved_at": time.time(),
    }
    try:
        atomic_write_text(_STATE_FILE, json.dumps(payload))
    except Exception:
        log.debug("Could not persist active tour state", exc_info=True)


def load_active_tour() -> dict[str, Any] | None:
    """Return the persisted tour state, or ``None`` when there is nothing valid
    to resume (no file, malformed, no waypoints, or older than ``_MAX_AGE_S``).

    The returned dict has ``remaining`` (list of ``(lat, lon)`` tuples) plus
    ``name`` and ``id``.
    """
    try:
        raw = _STATE_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        log.debug("Could not read active tour state", exc_info=True)
        return None
    try:
        data = json.loads(raw)
        remaining = [(float(p[0]), float(p[1])) for p in data["remaining"]]
    except (ValueError, KeyError, TypeError, IndexError):
        log.debug("Malformed active tour state — ignoring", exc_info=True)
        return None
    if not remaining:
        return None
    saved_at = data.get("saved_at")
    if isinstance(saved_at, (int, float)) and time.time() - saved_at > _MAX_AGE_S:
        log.info("Persisted tour is stale (%.0f h old) — ignoring", (time.time() - saved_at) / 3600)
        return None
    return {"remaining": remaining, "name": data.get("name"), "id": data.get("id")}


def clear_active_tour() -> None:
    """Remove any persisted tour state (tour finished or aborted)."""
    try:
        _STATE_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        log.debug("Could not clear active tour state", exc_info=True)
