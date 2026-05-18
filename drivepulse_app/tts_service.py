"""Text-to-speech service for navigation guidance using espeak-ng."""
from __future__ import annotations

import subprocess
import threading
from typing import Literal

from .diagnostics import get_logger

log = get_logger(__name__)

VoiceGender = Literal["male", "female"]
TtsLanguage = Literal["auto", "en", "de"]

_lock = threading.Lock()
_current_proc: subprocess.Popen | None = None


def _espeak_voice(language: str, gender: VoiceGender) -> str:
    """Return the espeak-ng voice identifier."""
    lang = language if language in {"en", "de"} else "en"
    suffix = "+f3" if gender == "female" else ""
    return f"{lang}{suffix}"


def speak(
    text: str,
    language: str,
    gender: VoiceGender = "female",
    speed: int = 150,
) -> None:
    """Speak *text* asynchronously via espeak-ng, cancelling any ongoing utterance."""
    threading.Thread(target=_speak_sync, args=(text, language, gender, speed), daemon=True).start()


def _speak_sync(text: str, language: str, gender: VoiceGender, speed: int) -> None:
    global _current_proc
    with _lock:
        if _current_proc is not None:
            try:
                _current_proc.terminate()
            except Exception:
                pass
            _current_proc = None

        voice = _espeak_voice(language, gender)
        try:
            proc = subprocess.Popen(
                ["espeak-ng", "-v", voice, "-s", str(speed), text],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _current_proc = proc
        except FileNotFoundError:
            log.warning("espeak-ng not found — install it for voice navigation")
            return
        except Exception as exc:
            log.warning("TTS error: %s", exc)
            return

    proc.wait()
    with _lock:
        if _current_proc is proc:
            _current_proc = None


def stop() -> None:
    """Stop any currently playing utterance."""
    global _current_proc
    with _lock:
        if _current_proc is not None:
            try:
                _current_proc.terminate()
            except Exception:
                pass
            _current_proc = None
