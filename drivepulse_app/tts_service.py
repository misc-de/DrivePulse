"""Text-to-speech service for navigation guidance.

Backends (in preference order when configured):
  piper    — neural TTS via the `piper` CLI binary + ONNX voice models
  espeak   — espeak-ng, lightweight but robotic
"""
from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path
from typing import Literal

from .diagnostics import get_logger

log = get_logger(__name__)

VoiceGender = Literal["male", "female"]
TtsLanguage = Literal["auto", "en", "de"]

_lock = threading.Lock()
_current_proc: subprocess.Popen | None = None

# Active backend — changed by set_backend() when the user picks one in Settings.
_backend: str = "espeak"

# Standard directories where piper model files (.onnx) are searched.
_PIPER_DIRS: list[Path] = [
    Path.home() / ".local" / "share" / "piper",
    Path("/usr/share/piper"),
    Path("/usr/local/share/piper"),
]

# Preferred model name per (language, gender) combination.
_PIPER_MODELS: dict[tuple[str, str], str] = {
    ("de", "female"): "de_DE-kerstin-low",
    ("de", "male"):   "de_DE-thorsten-high",
    ("en", "female"): "en_US-lessac-high",
    ("en", "male"):   "en_US-ryan-high",
}

# Public flag: True when the `piper` binary is on PATH.
PIPER_AVAILABLE: bool = shutil.which("piper") is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _effective_lang(language: str) -> str:
    return language if language in {"en", "de"} else "en"


def _espeak_voice(language: str, gender: VoiceGender) -> str:
    lang = _effective_lang(language)
    suffix = "+f3" if gender == "female" else ""
    return f"{lang}{suffix}"


def _piper_model_path(language: str, gender: str) -> Path | None:
    """Return the first found .onnx model path for this lang/gender, or None."""
    key = (_effective_lang(language), gender)
    model_name = _PIPER_MODELS.get(key)
    if not model_name:
        return None
    for d in _PIPER_DIRS:
        p = d / f"{model_name}.onnx"
        if p.exists():
            return p
    return None


def piper_model_available(language: str, gender: str) -> bool:
    """True when piper binary and the matching model file are both present."""
    return PIPER_AVAILABLE and _piper_model_path(language, gender) is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def set_backend(backend: str) -> None:
    """Switch active backend.  Unknown values fall back to 'espeak'."""
    global _backend
    _backend = backend if backend in {"espeak", "piper"} else "espeak"


def speak(
    text: str,
    language: str,
    gender: VoiceGender = "female",
    speed: int = 150,
) -> None:
    """Speak *text* asynchronously, cancelling any ongoing utterance."""
    threading.Thread(
        target=_speak_sync,
        args=(text, language, gender, speed, _backend),
        daemon=True,
    ).start()


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


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _speak_sync(
    text: str,
    language: str,
    gender: VoiceGender,
    speed: int,
    backend: str,
) -> None:
    global _current_proc

    # Stop whatever is playing.
    with _lock:
        if _current_proc is not None:
            try:
                _current_proc.terminate()
            except Exception:
                pass
            _current_proc = None

    if backend == "piper" and PIPER_AVAILABLE:
        proc = _launch_piper(text, language, gender)
        if proc is None:
            log.warning("Piper model not found for %s/%s — falling back to espeak-ng", language, gender)
            proc = _launch_espeak(text, language, gender, speed)
    else:
        proc = _launch_espeak(text, language, gender, speed)

    if proc is None:
        return

    with _lock:
        _current_proc = proc

    proc.wait()

    with _lock:
        if _current_proc is proc:
            _current_proc = None


def _launch_espeak(
    text: str,
    language: str,
    gender: VoiceGender,
    speed: int,
) -> subprocess.Popen | None:
    voice = _espeak_voice(language, gender)
    try:
        return subprocess.Popen(
            ["espeak-ng", "-v", voice, "-s", str(speed), text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        log.warning("espeak-ng not found — install it for voice navigation")
    except Exception as exc:
        log.warning("TTS espeak error: %s", exc)
    return None


def _launch_piper(
    text: str,
    language: str,
    gender: str,
) -> subprocess.Popen | None:
    model = _piper_model_path(language, gender)
    if model is None:
        return None
    try:
        # piper reads text from stdin, writes raw PCM to stdout;
        # pipe into aplay for immediate playback.
        echo = subprocess.Popen(
            ["echo", text],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        piper_proc = subprocess.Popen(
            ["piper", "--model", str(model), "--output-raw"],
            stdin=echo.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        echo.stdout.close()  # let echo exit when piper closes the pipe
        aplay = subprocess.Popen(
            ["aplay", "-r", "22050", "-f", "S16_LE", "-c", "1", "-q"],
            stdin=piper_proc.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        piper_proc.stdout.close()
        # Return aplay as the "current process" — terminating it stops audio.
        return aplay
    except FileNotFoundError as exc:
        log.warning("Piper TTS process not found: %s", exc)
    except Exception as exc:
        log.warning("TTS piper error: %s", exc)
    return None
