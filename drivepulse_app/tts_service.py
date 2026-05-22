"""Text-to-speech service for navigation guidance.

Backends (in preference order when configured):
  piper    — neural TTS via the `piper` CLI binary + ONNX voice models
  espeak   — espeak-ng, lightweight but robotic
"""
from __future__ import annotations

import atexit
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Literal

try:
    import requests as _requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

try:
    from gi.repository import GLib as _GLib
    _GLIB_OK = True
except ImportError:
    _GLib = None  # type: ignore[assignment]
    _GLIB_OK = False

from .diagnostics import get_logger

log = get_logger(__name__)

VoiceGender = Literal["male", "female"]
TtsLanguage = Literal["auto", "en", "de"]

_lock = threading.Lock()
_current_proc: subprocess.Popen | None = None

# Active backend — changed by set_backend() when the user picks one in Settings.
_backend: str = "espeak"

# Pre-rendered audio cache: text-hash → Path to raw PCM file.
# Populated by prerender(); consumed (and file deleted) by speak() on cache hit.
_audio_cache: dict[str, Path] = {}
_cache_lock = threading.Lock()
_CACHE_LIMIT = 40

# Tracks which pre-renders are currently running to avoid duplicate work.
_prerender_active: set[str] = set()
_prerender_set_lock = threading.Lock()

# Measured TTS launch latency in seconds (time from speak() call to audible output).
# espeak is fast (~0.2s); piper needs ONNX inference (~1.0-2.0s on first call).
# Updated via exponential moving average from actual measurements.
_tts_latency_s: float = 1.0


def get_latency_s() -> float:
    """Return the estimated seconds from speak() call to audible output start."""
    return _tts_latency_s


def _record_launch_latency(seconds: float) -> None:
    global _tts_latency_s
    # EMA: weight recent measurement 30%, prior estimate 70%.
    _tts_latency_s = 0.3 * seconds + 0.7 * _tts_latency_s

# Standard directories where piper model files (.onnx) are searched.
_PIPER_DIRS: list[Path] = [
    Path.home() / ".local" / "share" / "piper",
    Path("/usr/share/piper"),
    Path("/usr/local/share/piper"),
]

# Preferred model name per (language, gender, quality) combination.
# kerstin is only published in "low"; all quality values map to it.
_PIPER_MODELS: dict[tuple[str, str, str], str] = {
    ("de", "female", "low"):    "de_DE-kerstin-low",
    ("de", "female", "medium"): "de_DE-kerstin-low",
    ("de", "female", "high"):   "de_DE-kerstin-low",
    ("de", "male",   "low"):    "de_DE-thorsten-low",
    ("de", "male",   "medium"): "de_DE-thorsten-medium",
    ("de", "male",   "high"):   "de_DE-thorsten-high",
    ("en", "female", "low"):    "en_US-lessac-low",
    ("en", "female", "medium"): "en_US-lessac-medium",
    ("en", "female", "high"):   "en_US-lessac-high",
    ("en", "male",   "low"):    "en_US-ryan-low",
    ("en", "male",   "medium"): "en_US-ryan-medium",
    ("en", "male",   "high"):   "en_US-ryan-high",
}

# Public flag: True when the `piper` binary is on PATH.
PIPER_AVAILABLE: bool = shutil.which("piper") is not None

# Hugging Face base URL for rhasspy/piper-voices.
_PIPER_HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# Models currently being downloaded — prevents duplicate fetches.
_download_in_progress: set[str] = set()
_download_lock = threading.Lock()

# Per-download cancel events (keyed by model_name).
_download_cancel_events: dict[str, threading.Event] = {}

# Optional UI callback: called with (model_name, fraction).
# fraction: 0.0–1.0 = progress, -1.0 = cancelled/error, 2.0 = done
_progress_cb: "Callable[[str, float], None] | None" = None


# ---------------------------------------------------------------------------
# Download progress public API
# ---------------------------------------------------------------------------

def set_download_callback(cb: "Callable[[str, float], None] | None") -> None:
    """Register a callback that receives (model_name, fraction) progress updates.

    fraction meanings:
      0.0 – 1.0  → download progress
      -1.0       → cancelled or error
       2.0       → download complete
    """
    global _progress_cb
    _progress_cb = cb


def cancel_download(model_name: str) -> None:
    """Signal an in-progress download to abort."""
    event = _download_cancel_events.get(model_name)
    if event:
        event.set()


def active_downloads() -> list[str]:
    """Return a list of model names currently being downloaded."""
    with _download_lock:
        return list(_download_in_progress)


def _emit_progress(model_name: str, fraction: float) -> None:
    """Emit a progress update via the registered callback (thread-safe)."""
    cb = _progress_cb
    if cb is None:
        return
    if _GLIB_OK and _GLib is not None:
        _GLib.idle_add(cb, model_name, fraction)
    else:
        try:
            cb(model_name, fraction)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Model auto-download
# ---------------------------------------------------------------------------

def _model_hf_url(model_name: str, suffix: str = "") -> str:
    """Construct the Hugging Face download URL for a piper model file.

    Model name format: ``{lang_region}-{voice}-{quality}``  e.g. ``de_DE-kerstin-low``
    Maps to: ``{base}/{lang_code}/{lang_region}/{voice}/{quality}/{model_name}.onnx``
    """
    parts = model_name.split("-", 2)   # ["de_DE", "kerstin", "low"]
    lang_region, voice, quality = parts[0], parts[1], parts[2]
    lang_code = lang_region.split("_")[0].lower()
    return f"{_PIPER_HF_BASE}/{lang_code}/{lang_region}/{voice}/{quality}/{model_name}.onnx{suffix}"


def _download_piper_model(model_name: str) -> None:
    """Download model .onnx + .onnx.json into the first writable piper dir.

    Progress is reported via the registered callback (see set_download_callback):
      - 0.0       → starting
      - 0.0–0.9   → .onnx file streaming progress
      - 0.9–1.0   → .onnx.json download (tiny)
      - 2.0       → complete
      - -1.0      → cancelled or error
    """
    with _download_lock:
        if model_name in _download_in_progress:
            return
        _download_in_progress.add(model_name)

    cancel_event = threading.Event()
    _download_cancel_events[model_name] = cancel_event

    target_dir = _PIPER_DIRS[0]  # ~/.local/share/piper
    tmp: "Path | None" = None
    try:
        _emit_progress(model_name, 0.0)

        if not _REQUESTS_OK:
            log.warning("Cannot auto-download Piper model — 'requests' not installed")
            _emit_progress(model_name, -1.0)
            return

        target_dir.mkdir(parents=True, exist_ok=True)

        # .onnx = 0.0 → 0.9, .onnx.json = 0.9 → 1.0
        file_specs = [
            ("", 0.0, 0.9),
            (".json", 0.9, 1.0),
        ]
        for suffix, frac_start, frac_end in file_specs:
            if cancel_event.is_set():
                log.info("Piper download cancelled: %s", model_name)
                _emit_progress(model_name, -1.0)
                return

            url = _model_hf_url(model_name, suffix)
            dest = target_dir / f"{model_name}.onnx{suffix}"
            if dest.exists():
                _emit_progress(model_name, frac_end)
                continue

            log.info("Downloading Piper model %s%s …", model_name, suffix)
            resp = _requests.get(url, stream=True, timeout=120)
            resp.raise_for_status()

            total = int(resp.headers.get("content-length", 0) or 0)
            downloaded = 0
            tmp = dest.with_suffix(".tmp")
            cancelled = False
            with tmp.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if cancel_event.is_set():
                        cancelled = True
                        break
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        ratio = downloaded / total
                        fraction = frac_start + ratio * (frac_end - frac_start)
                        _emit_progress(model_name, fraction)

            if cancelled:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                log.info("Piper download cancelled: %s", model_name)
                _emit_progress(model_name, -1.0)
                return

            tmp.rename(dest)
            tmp = None
            _emit_progress(model_name, frac_end)

        log.info("Piper model ready: %s", model_name)
        _emit_progress(model_name, 2.0)

    except Exception as exc:
        log.warning("Piper model download failed (%s): %s", model_name, exc)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        _emit_progress(model_name, -1.0)
    finally:
        _download_cancel_events.pop(model_name, None)
        with _download_lock:
            _download_in_progress.discard(model_name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _effective_lang(language: str) -> str:
    return language if language in {"en", "de"} else "en"


def _espeak_voice(language: str, gender: VoiceGender) -> str:
    lang = _effective_lang(language)
    suffix = "+f3" if gender == "female" else ""
    return f"{lang}{suffix}"


def _piper_model_path(language: str, gender: str, quality: str = "high") -> Path | None:
    """Return the first found .onnx model path for this lang/gender/quality, or None."""
    key = (_effective_lang(language), gender, quality)
    model_name = _PIPER_MODELS.get(key)
    if not model_name:
        return None
    for d in _PIPER_DIRS:
        p = d / f"{model_name}.onnx"
        if p.exists():
            return p
    return None


def piper_model_available(language: str, gender: str, quality: str = "high") -> bool:
    """True when piper binary and the matching model file are both present."""
    return PIPER_AVAILABLE and _piper_model_path(language, gender, quality) is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _cache_key(text: str, language: str, gender: str, quality: str = "high") -> str:
    return hashlib.md5(f"{language}:{gender}:{quality}:{text}".encode()).hexdigest()


def clear_audio_cache() -> None:
    """Delete all pre-rendered PCM files and clear the cache (e.g. on tour end)."""
    with _cache_lock:
        for p in _audio_cache.values():
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        _audio_cache.clear()


atexit.register(clear_audio_cache)


def prerender(text: str, language: str, gender: VoiceGender = "female", speed: int = 150, quality: str = "high") -> None:
    """Pre-render *text* to a cached PCM file in the background (piper only).

    A subsequent speak() call with the same text/language/gender/quality will find the
    file in cache and skip piper entirely — just aplay, which starts instantly.
    No-op when piper is unavailable or the entry is already cached/in-progress.
    """
    if _backend != "piper" or not PIPER_AVAILABLE:
        return
    key = _cache_key(text, language, gender, quality)
    with _cache_lock:
        if key in _audio_cache:
            return
    with _prerender_set_lock:
        if key in _prerender_active:
            return
        _prerender_active.add(key)
    threading.Thread(
        target=_prerender_sync,
        args=(text, language, gender, quality, key),
        daemon=True,
        name="tts-prerender",
    ).start()


def _prerender_sync(text: str, language: str, gender: VoiceGender, quality: str, key: str) -> None:
    try:
        model = _piper_model_path(language, gender, quality)
        if model is None:
            return
        fd, tmp_path_str = tempfile.mkstemp(suffix=".pcm", prefix="drivepulse_tts_")
        os.close(fd)
        tmp = Path(tmp_path_str)
        try:
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
            echo.stdout.close()
            data = piper_proc.stdout.read()
            piper_proc.wait()
            if piper_proc.returncode == 0 and data:
                tmp.write_bytes(data)
                with _cache_lock:
                    if len(_audio_cache) >= _CACHE_LIMIT:
                        oldest_key = next(iter(_audio_cache))
                        old_path = _audio_cache.pop(oldest_key)
                        try:
                            old_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                    _audio_cache[key] = tmp
                log.debug("TTS pre-render cached: %.40s…", text)
                return
        except Exception as exc:
            log.debug("TTS pre-render subprocess error: %s", exc)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    except Exception as exc:
        log.debug("TTS pre-render error: %s", exc)
    finally:
        with _prerender_set_lock:
            _prerender_active.discard(key)


def _play_cached_file(path: Path) -> "subprocess.Popen | None":
    """Play a pre-rendered raw PCM file via aplay (instant — no piper needed)."""
    try:
        return subprocess.Popen(
            ["aplay", "-r", "22050", "-f", "S16_LE", "-c", "1", "-q", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log.warning("TTS cached play error: %s", exc)
    return None


def set_backend(backend: str) -> None:
    """Switch active backend.  Unknown values fall back to 'espeak'."""
    global _backend
    _backend = backend if backend in {"espeak", "piper"} else "espeak"


def ensure_models(language: str, gender: str, quality: str = "high") -> None:
    """Pre-fetch the Piper model for *language*/*gender*/*quality* if not already present.

    Safe to call from the UI thread — download runs in a daemon thread.
    No-op when piper binary is missing or model already exists.
    """
    if not PIPER_AVAILABLE:
        return
    key = (_effective_lang(language), gender, quality)
    model_name = _PIPER_MODELS.get(key)
    if not model_name:
        return
    if _piper_model_path(language, gender, quality) is not None:
        return  # already on disk
    with _download_lock:
        if model_name in _download_in_progress:
            return  # already downloading
    threading.Thread(
        target=_download_piper_model,
        args=(model_name,),
        daemon=True,
        name="piper-dl",
    ).start()


def speak(
    text: str,
    language: str,
    gender: VoiceGender = "female",
    speed: int = 150,
    quality: str = "high",
) -> None:
    """Speak *text* asynchronously, cancelling any ongoing utterance."""
    threading.Thread(
        target=_speak_sync,
        args=(text, language, gender, speed, _backend, quality),
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
    quality: str = "high",
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

    t0 = time.monotonic()
    cached_path: Path | None = None

    if backend == "piper" and PIPER_AVAILABLE:
        key = _cache_key(text, language, gender, quality)
        with _cache_lock:
            cached_path = _audio_cache.pop(key, None)

        if cached_path is not None and cached_path.exists():
            proc = _play_cached_file(cached_path)
            log.debug("TTS cache hit: %.40s…", text)
        else:
            cached_path = None
            proc = _launch_piper(text, language, gender, quality)
            if proc is None:
                piper_key = (_effective_lang(language), gender, quality)
                model_name = _PIPER_MODELS.get(piper_key, "")
                if model_name not in _download_in_progress:
                    log.info("Piper model not yet available for %s/%s/%s — using espeak-ng", language, gender, quality)
                proc = _launch_espeak(text, language, gender, speed)
    else:
        proc = _launch_espeak(text, language, gender, speed)

    if proc is None:
        if cached_path is not None:
            try:
                cached_path.unlink(missing_ok=True)
            except Exception:
                pass
        return

    with _lock:
        _current_proc = proc

    # Record how long the subprocess pipeline took to launch.  This is a proxy
    # for the TTS startup latency (time until the first audio sample plays).
    _record_launch_latency(time.monotonic() - t0)

    proc.wait()

    with _lock:
        if _current_proc is proc:
            _current_proc = None

    if cached_path is not None:
        try:
            cached_path.unlink(missing_ok=True)
        except Exception:
            pass


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
    quality: str = "high",
) -> subprocess.Popen | None:
    model = _piper_model_path(language, gender, quality)
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
