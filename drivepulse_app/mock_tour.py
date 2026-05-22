"""Mock tour simulator: drives along an OSRM route geometry and emits GPS payloads."""
from __future__ import annotations

import bisect
import time
from datetime import datetime, timezone
from typing import Any, Callable

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from .diagnostics import get_logger
from .map_services import bearing, haversine


log = get_logger(__name__)

PayloadFn = Callable[[dict[str, Any]], Any]


class MockTourSimulator:
    """Walks an OSRM route at speed-zone-aware speeds and emits "gps"-source payloads.

    Speed zones are supplied as a list of (cum_dist_m, speed_kmh) breakpoints
    (sorted ascending).  When the car reaches a breakpoint the speed changes
    automatically: 40 km/h in cities, 70 km/h on rural roads, 120 km/h on
    motorways.  Falls back to *target_kmh* when no zones are provided.

    Coords are expected in OSRM geometry order: list of [lon, lat] pairs.
    """

    TICK_MS = 250  # 4 Hz, matches MapPage's max JS push rate

    # Speed applied when the simulated car is approaching or passing a turn.
    _TURN_KMH = 10.0
    # How far before the maneuver point to start slowing down.
    _TURN_APPROACH_M = 60.0
    # How far past the maneuver point before resuming normal speed.
    _TURN_CLEAR_M = 25.0

    def __init__(self, on_payload: PayloadFn, target_kmh: float = 50.0) -> None:
        self._on_payload = on_payload
        self._default_kmh = float(target_kmh)
        self._coords: list[tuple[float, float]] = []  # (lat, lon)
        self._seg_idx = 0
        self._seg_progress_m = 0.0
        self._cum_dist_m = 0.0          # total metres driven so far
        self._speed_zones: list[tuple[float, float]] = []   # (cum_dist_m, kmh)
        self._zone_starts: list[float] = []                 # parallel index for bisect
        self._maneuver_m: list[float] = []                  # sorted turn positions
        self._timeout_id: int | None = None
        self._last_tick = 0.0

    # ── current speed ─────────────────────────────────────────────────────────

    def _speed_at(self, cum_m: float) -> float:
        """Return the target speed in km/h for the given cumulative distance."""
        # Slow to _TURN_KMH within the approach/clear window of any turn maneuver.
        for turn_m in self._maneuver_m:
            if turn_m - self._TURN_APPROACH_M <= cum_m <= turn_m + self._TURN_CLEAR_M:
                return self._TURN_KMH
        if not self._speed_zones:
            return self._default_kmh
        # Find the last zone whose start is ≤ cum_m
        idx = bisect.bisect_right(self._zone_starts, cum_m) - 1
        if idx < 0:
            return self._default_kmh
        return self._speed_zones[idx][1]

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        return self._timeout_id is not None

    def start(
        self,
        coords: list[list[float]],
        speed_zones: list[tuple[float, float]] | None = None,
        maneuver_m: list[float] | None = None,
    ) -> None:
        self.stop()
        if not coords or len(coords) < 2:
            return
        # Convert OSRM [lon, lat] → (lat, lon) and drop consecutive duplicates.
        cleaned: list[tuple[float, float]] = []
        for pt in coords:
            try:
                lat = float(pt[1])
                lon = float(pt[0])
            except (TypeError, ValueError, IndexError):
                continue
            if cleaned and cleaned[-1] == (lat, lon):
                continue
            cleaned.append((lat, lon))
        if len(cleaned) < 2:
            return
        self._coords = cleaned
        self._seg_idx = 0
        self._seg_progress_m = 0.0
        self._cum_dist_m = 0.0
        self._speed_zones = sorted(speed_zones, key=lambda z: z[0]) if speed_zones else []
        self._zone_starts = [z[0] for z in self._speed_zones]
        self._maneuver_m = sorted(maneuver_m) if maneuver_m else []
        self._last_tick = time.monotonic()
        self._emit_current()
        self._timeout_id = GLib.timeout_add(self.TICK_MS, self._on_tick)

    def stop(self) -> None:
        if self._timeout_id is not None:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = None

    def resume(self) -> None:
        """Resume after a pause; segment index and progress are preserved."""
        if self._timeout_id is not None or not self._coords:
            return
        # Reset the tick clock so the first dt after resume is the tick interval,
        # not the entire pause duration (which would teleport the car ahead).
        self._last_tick = time.monotonic()
        self._emit_current()
        self._timeout_id = GLib.timeout_add(self.TICK_MS, self._on_tick)

    # ── tick ──────────────────────────────────────────────────────────────────

    def _on_tick(self) -> bool:
        now = time.monotonic()
        dt = max(0.0, now - self._last_tick)
        self._last_tick = now
        current_kmh = self._speed_at(self._cum_dist_m)
        advance_m = (current_kmh / 3.6) * dt
        finished = self._advance(advance_m)
        self._emit_current(arrived=finished)
        if finished:
            self._timeout_id = None
            return False
        return True

    def _advance(self, meters: float) -> bool:
        """Move forward by *meters* along the polyline. Returns True when the end is reached."""
        while meters > 0 and self._seg_idx < len(self._coords) - 1:
            cur = self._coords[self._seg_idx]
            nxt = self._coords[self._seg_idx + 1]
            seg_len = haversine(cur[0], cur[1], nxt[0], nxt[1])
            remain = seg_len - self._seg_progress_m
            if remain <= 0:
                self._seg_idx += 1
                self._seg_progress_m = 0.0
                continue
            if meters < remain:
                self._seg_progress_m += meters
                self._cum_dist_m += meters
                return False
            meters -= remain
            self._cum_dist_m += remain
            self._seg_idx += 1
            self._seg_progress_m = 0.0
        return self._seg_idx >= len(self._coords) - 1

    # ── position helpers ──────────────────────────────────────────────────────

    def _current_position(self) -> tuple[float, float]:
        if self._seg_idx >= len(self._coords) - 1:
            return self._coords[-1]
        cur = self._coords[self._seg_idx]
        nxt = self._coords[self._seg_idx + 1]
        seg_len = haversine(cur[0], cur[1], nxt[0], nxt[1])
        if seg_len <= 0:
            return cur
        frac = min(1.0, max(0.0, self._seg_progress_m / seg_len))
        return (
            cur[0] + (nxt[0] - cur[0]) * frac,
            cur[1] + (nxt[1] - cur[1]) * frac,
        )

    def _current_heading(self) -> float:
        if self._seg_idx >= len(self._coords) - 1:
            if len(self._coords) >= 2:
                cur = self._coords[-2]
                nxt = self._coords[-1]
            else:
                return 0.0
        else:
            cur = self._coords[self._seg_idx]
            nxt = self._coords[self._seg_idx + 1]
        return bearing(cur[0], cur[1], nxt[0], nxt[1])

    def _emit_current(self, arrived: bool = False) -> None:
        lat, lon = self._current_position()
        heading = self._current_heading()
        speed = 0.0 if arrived else self._speed_at(self._cum_dist_m)
        payload: dict[str, Any] = {
            "source": "gps",
            "gps_lat": {"value": lat, "unit": "degree"},
            "gps_lon": {"value": lon, "unit": "degree"},
            "gps_speed": {"value": speed, "unit": "km/h"},
            "gps_heading": {"value": heading, "unit": "deg"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._on_payload(payload)
        except Exception:
            log.exception("Mock tour payload dispatch failed")
