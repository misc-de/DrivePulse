"""Speed-limit zones and over-speed warning for the map tour.

Split out of ``tour.py``: building the (cumulative-distance, speed) breakpoint
table from OSRM step data, the background Overpass pre-fetch that refines it
with real per-segment limits, the on-screen speed-limit sign, and the
over-speed warning beep. Driven from :class:`MapTourMixin` via ``self`` on the
composed ``MapPage``.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from gi.repository import GLib

from drivepulse_app.diagnostics import get_logger
from drivepulse_app.map._tour_progress import build_speed_zones
from drivepulse_app.map.services import fetch_overpass_speed_zones

log = get_logger(__name__)


class MapTourSpeedMixin:
    """Speed-zone breakpoints, Overpass refinement, sign overlay and beep."""

    # Concrete MapPage state surfaced to this mixin. See project_mixin_typing.md.
    _tour_steps: list[dict]
    _step_cum_m: list[float]
    _tour_coords: list[list[float]]
    _tour_active: bool
    _route_gen: int
    _speed_zones: list[tuple[float, float]]
    _speed_zone_overlay: Any
    _speed_zone_lbl: Any
    _GPS_MAX_STALE_S: float
    _OBD_SPEED_STALE_S: float

    # Defined in MapTourMixin (core); read here.
    _gps_progress_m: Callable[[], float]

    def _build_speed_zones(self) -> list[tuple[float, float]]:
        """Build (cum_dist_m, speed_kmh) breakpoints.

        Prefers Valhalla's real ``speed_limit`` values.  Falls back to the
        ref-tag heuristic (A* → 120, B* → 70, urban → 40) so the sign is
        always shown during mock-mode tours where Valhalla data may be absent.
        """
        return build_speed_zones(self._tour_steps, self._step_cum_m)

    def _start_overpass_speed_fetch(self) -> None:
        """Kick off a background thread that pre-fetches per-segment speed limits."""
        coords = list(self._tour_coords) if self._tour_coords else []
        if not coords:
            return
        gen = self._route_gen
        t = threading.Thread(
            target=self._overpass_speed_bg,
            args=(coords, gen),
            daemon=True,
        )
        t.start()

    def _overpass_speed_bg(
        self, coords: list[list[float]], gen: int
    ) -> None:
        try:
            zones = fetch_overpass_speed_zones(coords)
        except Exception:
            log.exception("Overpass speed fetch failed")
            zones = []
        GLib.idle_add(self._apply_overpass_speed_zones, zones, gen)

    def _apply_overpass_speed_zones(
        self, zones: list[tuple[float, float]], gen: int
    ) -> bool:
        if gen != self._route_gen or not self._tour_active:
            return False
        if zones:
            self._speed_zones = zones
            self._speed_zones_from_overpass = True
            log.debug("Overpass speed zones: %d breakpoints loaded", len(zones))
        return False

    def _update_speed_zone_overlay(self) -> None:
        if self._speed_zone_overlay is None or self._speed_zone_lbl is None:
            return
        if not self._speed_zones:
            self._speed_zone_overlay.set_visible(False)
            return
        # Only post a number we actually trust: real per-segment limits from
        # Overpass. Without them the zones are just the urban/ref heuristic
        # (e.g. 40 km/h for any unref'd street), which is plain wrong in
        # Tempo-30 areas — better to show no sign than a confidently-wrong one.
        # The mock-mode simulator has no Overpass, so it keeps its heuristic.
        if not getattr(self, "_speed_zones_from_overpass", False) and not getattr(
            self, "mock_mode", False
        ):
            self._speed_zone_overlay.set_visible(False)
            return
        progress_m = self._gps_progress_m()
        speed: float | None = None
        for cum_m, spd in self._speed_zones:
            if cum_m <= progress_m:
                speed = spd
            else:
                break
        if speed is None:
            self._speed_zone_overlay.set_visible(False)
            return
        self._speed_zone_lbl.set_text(str(int(speed)))
        self._speed_zone_overlay.set_visible(True)

        # Speed-limit warning beep — only with Overpass data, only once per step.
        if (
            getattr(self, "_speed_warn_enabled", True)
            and getattr(self, "_speed_zones_from_overpass", False)
            and not getattr(self, "_speed_warn_fired", False)
            and self._tour_active
        ):
            import time as _time
            _now = _time.monotonic()
            _gps_age = _now - getattr(self, "_gps_filt_time", 0.0)
            if _gps_age < self._GPS_MAX_STALE_S:
                vehicle_kmh = getattr(self, "_gps_filt_speed_kmh", 0.0) or 0.0
            else:
                _obd_age = _now - getattr(self, "_obd_speed_time", 0.0)
                vehicle_kmh = (
                    getattr(self, "_obd_speed_kmh", None) or 0.0
                    if _obd_age < self._OBD_SPEED_STALE_S
                    else 0.0
                )
            if vehicle_kmh >= speed * 1.30:
                self._speed_warn_fired = True
                self._play_speed_beep(long_double=True)
            elif vehicle_kmh >= speed * 1.15:
                self._speed_warn_fired = True
                self._play_speed_beep(long_double=False)

    def _play_speed_beep(self, long_double: bool) -> None:
        import io
        import math
        import struct
        import subprocess
        import wave

        def _do() -> None:
            rate = 22050
            freq = 880.0
            volume = 0.65
            # short: 160 ms single tone  |  long-double: 280 ms + 120 ms gap + 280 ms
            segments = (
                [(280, True), (120, False), (280, True)] if long_double else [(160, True)]
            )
            frames: list[bytes] = []
            for ms, on in segments:
                n = int(rate * ms / 1000)
                for i in range(n):
                    v = int(32767 * volume * math.sin(2 * math.pi * freq * i / rate)) if on else 0
                    frames.append(struct.pack("<h", v))
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(b"".join(frames))
            try:
                proc = subprocess.Popen(
                    ["aplay", "-q"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                proc.communicate(input=buf.getvalue(), timeout=3)
            except (OSError, subprocess.SubprocessError):
                log.debug("aplay maneuver beep failed", exc_info=True)

        threading.Thread(target=_do, daemon=True).start()
