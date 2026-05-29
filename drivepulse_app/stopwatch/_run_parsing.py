"""Pure parsing of persisted stopwatch-run blobs.

``StopWatchPage.load_persisted_run`` restores a saved acceleration run. The
stored shape is loose — range keys appear as either ``"(100, 200)"`` or
``"100-200"``, target/range values may be missing or wrong-typed, and sample
rows come either as canonical ``[elapsed, active_g, lateral_g]`` triplets or as
richer mock-seeder dicts. These functions normalise that into the page's
internal structures and are kept here, free of GTK, so the type-guard maze is
unit-testable.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def parse_range_key(raw: Any) -> tuple[int, int] | None:
    """Parse a range key in either ``"(100, 200)"`` or ``"100-200"`` form."""
    s = str(raw).strip().lstrip("(").rstrip(")")
    sep = "," if "," in s else ("-" if "-" in s else None)
    if sep is None:
        return None
    try:
        lo_s, hi_s = s.split(sep, 1)
        return int(lo_s.strip()), int(hi_s.strip())
    except (TypeError, ValueError):
        return None


def _obd_gps_pair(val: dict[str, Any]) -> dict[str, float | None]:
    return {
        "obd": val.get("obd") if isinstance(val.get("obd"), (int, float)) else None,
        "gps": val.get("gps") if isinstance(val.get("gps"), (int, float)) else None,
    }


def parse_target_results(
    results_blob: dict[str, Any],
    speed_targets: Iterable[int],
) -> dict[int, dict[str, float | None]]:
    """Normalise the ``targets`` blob into ``{speed_kmh: {"obd", "gps"}}``.

    Only keys matching a known target are kept; missing or non-numeric obd/gps
    values become ``None``.
    """
    new_results: dict[int, dict[str, float | None]] = {
        target: {"obd": None, "gps": None} for target in speed_targets
    }
    for key, val in (results_blob.get("targets") or {}).items():
        try:
            tgt = int(str(key))
        except (TypeError, ValueError):
            continue
        if tgt not in new_results or not isinstance(val, dict):
            continue
        new_results[tgt] = _obd_gps_pair(val)
    return new_results


def parse_range_results(
    results_blob: dict[str, Any],
    range_targets: Iterable[tuple[int, int]],
) -> dict[tuple[int, int], dict[str, float | None]]:
    """Normalise the ``ranges`` blob into ``{(lo, hi): {"obd", "gps"}}``."""
    new_ranges: dict[tuple[int, int], dict[str, float | None]] = {
        r: {"obd": None, "gps": None} for r in range_targets
    }
    for key, val in (results_blob.get("ranges") or {}).items():
        parsed = parse_range_key(key)
        if parsed is None or parsed not in new_ranges or not isinstance(val, dict):
            continue
        new_ranges[parsed] = _obd_gps_pair(val)
    return new_ranges


def parse_run_samples(
    samples_blob: Iterable[Any],
) -> list[tuple[float, float | None, float]]:
    """Normalise sample rows into ``(elapsed, active_g, lateral_g)`` triplets.

    Accepts both the canonical triplet list/tuple form and the richer
    mock-seeder dict (``ts``/``elapsed``, ``accel_g``/``active_g``, ``lateral_g``).
    Rows missing a timestamp or with unparseable numbers are skipped.
    """
    triplets: list[tuple[float, float | None, float]] = []
    for s in samples_blob:
        if isinstance(s, dict):
            ts = s.get("ts") or s.get("elapsed")
            active_g = s.get("accel_g") or s.get("active_g")
            lat_g = s.get("lateral_g", 0.0) or 0.0
            if ts is None:
                continue
            try:
                triplets.append((float(ts), None if active_g is None else float(active_g), float(lat_g)))
            except (TypeError, ValueError):
                continue
        elif isinstance(s, (list, tuple)) and len(s) >= 3:
            try:
                triplets.append((float(s[0]), None if s[1] is None else float(s[1]), float(s[2])))
            except (TypeError, ValueError):
                continue
    return triplets
