"""256 × 256 JPEG thumbnail cache for car photos.

Cache mirrors the photos directory structure under THUMB_CACHE_DIR.
Thumbs are considered fresh as long as their mtime >= the source file mtime.
"""
from __future__ import annotations

import threading
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf

from drivepulse_app.common import LOG_DIR
from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)

THUMB_CACHE_DIR = LOG_DIR / "thumb_cache"
THUMB_PIXEL = 256

_lock = threading.Lock()


def thumb_path(photo_path: Path) -> Path:
    """Return expected cache path for a photo (file may not exist yet)."""
    try:
        rel = photo_path.relative_to(LOG_DIR)
        return THUMB_CACHE_DIR / rel
    except ValueError:
        return THUMB_CACHE_DIR / photo_path.name


def _is_fresh(photo_path: Path) -> bool:
    tp = thumb_path(photo_path)
    if not tp.exists():
        return False
    try:
        return tp.stat().st_mtime >= photo_path.stat().st_mtime
    except OSError:
        return False


def create_thumb(photo_path: Path) -> Path | None:
    """Create (or refresh) a 256×256 JPEG thumb. Returns cache path or None."""
    if not photo_path.exists():
        return None
    tp = thumb_path(photo_path)
    with _lock:
        if _is_fresh(photo_path):
            return tp
        try:
            tp.parent.mkdir(parents=True, exist_ok=True)
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(photo_path), THUMB_PIXEL, THUMB_PIXEL, True
            )
            pb.savev(str(tp), "jpeg", ["quality"], ["85"])
            return tp
        except Exception:
            log.debug("Could not create thumb for %s", photo_path, exc_info=True)
            return None


def get_or_create(photo_path: Path) -> Path | None:
    """Return cached thumb (instant) or create it synchronously."""
    if not photo_path.exists():
        return None
    if _is_fresh(photo_path):
        return thumb_path(photo_path)
    return create_thumb(photo_path)


def evict_to_limit(max_bytes: int) -> None:
    """Delete oldest thumb files until total cache size ≤ max_bytes."""
    if not THUMB_CACHE_DIR.exists():
        return
    try:
        files = sorted(THUMB_CACHE_DIR.rglob("*.jpg"), key=lambda p: p.stat().st_mtime)
        total = sum(f.stat().st_size for f in files)
        for f in files:
            if total <= max_bytes:
                break
            try:
                size = f.stat().st_size
                f.unlink()
                total -= size
            except OSError:
                pass
    except Exception:
        log.debug("evict_to_limit failed", exc_info=True)
