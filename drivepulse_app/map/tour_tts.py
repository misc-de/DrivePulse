"""Turn-by-turn voice guidance for the map tour.

Split out of ``tour.py``: spoken-announcement scheduling (distance thresholds
with TTS-latency look-ahead), pre-rendering of upcoming steps so audio is ready
before its trigger distance, and the language/distance text helpers. The tour
state machine in :class:`MapTourMixin` drives these via ``self`` on the composed
``MapPage``.
"""
from __future__ import annotations

from drivepulse_app.common import _translate
from drivepulse_app.map._tour_progress import tts_distance_text
from drivepulse_app.map.services import maneuver_text_key
from drivepulse_app.tts import service as tts_service
from drivepulse_app.tts.service import VoiceGender


class MapTourTtsMixin:
    """Voice-guidance scheduling, pre-rendering and text helpers."""

    # Concrete MapPage state surfaced to this mixin. See project_mixin_typing.md.
    language: str
    _tts_enabled: bool
    _tts_language: str
    _tts_voice: VoiceGender
    _tts_quality: str
    _tts_spoken_thresholds: set[int]
    _tts_last_step_idx: int
    _gps_speed_mps: float
    _tour_step_idx: int
    _tour_steps: list[dict]

    _TTS_THRESHOLDS = (300, 80)

    def _update_tts(self, step: dict, distance_m: float) -> None:
        if not self._tts_enabled:
            return
        current_idx = self._tour_step_idx
        if current_idx != self._tts_last_step_idx:
            self._tts_last_step_idx = current_idx
            self._tts_spoken_thresholds = set()
            # Don't announce immediately — the threshold loop below will fire on
            # the very next tick (or this one) at the appropriate distance.

        # Look-ahead: fire threshold early enough to compensate for TTS latency.
        # At 50 km/h and 1s latency the car travels ~14m — audible instructions
        # would otherwise describe a maneuver the driver has already reached.
        look_ahead_m = self._gps_speed_mps * tts_service.get_latency_s()
        trigger_dist = distance_m + look_ahead_m

        for threshold in self._TTS_THRESHOLDS:
            if threshold in self._tts_spoken_thresholds:
                continue
            if trigger_dist <= threshold:
                self._tts_announce(step, distance_m)
                self._tts_spoken_thresholds.add(threshold)
                break

    def _tts_effective_language(self) -> str:
        if self._tts_language != "auto":
            return self._tts_language
        return self.language if self.language in {"en", "de"} else "en"

    def _tts_distance_text(self, meters: float, lang: str) -> str:
        return tts_distance_text(meters, lang)

    def _prerender_upcoming_steps(self, from_idx: int, count: int = 5) -> None:
        """Pre-render TTS audio for the next *count* steps starting at *from_idx*.

        Uses threshold distances (300 m, 80 m) to approximate the spoken text.
        At typical speeds the 80 m threshold collapses to maneuver-text-only
        (heard_dist < 60 m), so that variant always matches exactly.
        """
        if not self._tts_enabled:
            return
        lang = self._tts_effective_language()
        gender = self._tts_voice
        for i in range(from_idx, min(from_idx + count, len(self._tour_steps))):
            step = self._tour_steps[i]
            if step.get("type") in {"depart", "arrive"}:
                continue
            maneuver_text = _translate(
                lang, maneuver_text_key(step.get("type", ""), step.get("modifier", ""))
            )
            for threshold_m in self._TTS_THRESHOLDS:
                heard_dist = float(threshold_m)
                if heard_dist > 60:
                    dist_text = self._tts_distance_text(heard_dist, lang)
                    text = _translate(lang, "tts.in_distance").format(distance=dist_text) + " " + maneuver_text
                else:
                    text = maneuver_text
                tts_service.prerender(text, lang, gender, quality=self._tts_quality)

    def _tts_announce(self, step: dict, distance_m: float) -> None:
        if not self._tts_enabled:
            return
        if step.get("type") == "depart":
            return
        lang = self._tts_effective_language()
        maneuver_text = _translate(lang, maneuver_text_key(step.get("type", ""), step.get("modifier", "")))
        # Subtract look-ahead so the spoken distance matches reality at the
        # moment the driver hears the announcement, not when it was triggered.
        look_ahead_m = self._gps_speed_mps * tts_service.get_latency_s()
        heard_dist = max(0.0, distance_m - look_ahead_m)
        if heard_dist > 60:
            dist_text = self._tts_distance_text(heard_dist, lang)
            text = _translate(lang, "tts.in_distance").format(distance=dist_text) + " " + maneuver_text
        else:
            text = maneuver_text
        tts_service.speak(text, lang, self._tts_voice, quality=self._tts_quality)
