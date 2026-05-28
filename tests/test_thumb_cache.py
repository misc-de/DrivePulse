"""Tests for the car-photo thumbnail cache.

``thumb_path`` mirrors the photo directory structure under
``THUMB_CACHE_DIR`` and ``evict_to_limit`` keeps the cache under a size
ceiling by deleting the oldest thumbs first. The image-encoding helper
(``create_thumb``) is skipped because it needs the GdkPixbuf typelib.
"""
from __future__ import annotations

import time

from drivepulse_app.cars import thumb_cache as tc

# ── thumb_path: directory-mirroring + fallback ────────────────────────────────


def test_thumb_path_mirrors_directory_under_log_dir():
    # A photo nested under LOG_DIR keeps its relative-path structure inside
    # THUMB_CACHE_DIR — that way the cache layout is human-greppable.
    photo = tc.LOG_DIR / "photos" / "42" / "front.jpg"
    expected = tc.THUMB_CACHE_DIR / "photos" / "42" / "front.jpg"
    assert tc.thumb_path(photo) == expected


def test_thumb_path_falls_back_to_basename_for_unrelated_paths(tmp_path):
    # Photos imported from anywhere outside LOG_DIR can't be made relative,
    # so the helper must fall back to using just the file name. This keeps
    # the cache flat for foreign photos rather than raising.
    foreign = tmp_path / "elsewhere" / "img.jpg"
    expected = tc.THUMB_CACHE_DIR / "img.jpg"
    assert tc.thumb_path(foreign) == expected


# ── _is_fresh ─────────────────────────────────────────────────────────────────


def test_is_fresh_false_when_thumb_missing(monkeypatch, tmp_path):
    # No thumb file at all → must report stale so create_thumb gets invoked
    # rather than serving an invalid cache hit.
    monkeypatch.setattr(tc, "THUMB_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(tc, "LOG_DIR", tmp_path)
    photo = tmp_path / "photos" / "car.jpg"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"jpg")

    assert tc._is_fresh(photo) is False


def test_is_fresh_true_when_thumb_newer(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "THUMB_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(tc, "LOG_DIR", tmp_path)
    photo = tmp_path / "photos" / "car.jpg"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"jpg")
    # Force the photo into the past so the thumb's later mtime is clearly newer.
    past = time.time() - 100
    import os
    os.utime(photo, (past, past))

    thumb = tc.thumb_path(photo)
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"thumb")

    assert tc._is_fresh(photo) is True


def test_is_fresh_false_when_photo_was_modified_after_thumb(monkeypatch, tmp_path):
    # Photo edited / re-imported after the thumb was generated → the thumb
    # is stale and must be regenerated.
    monkeypatch.setattr(tc, "THUMB_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(tc, "LOG_DIR", tmp_path)
    photo = tmp_path / "photos" / "car.jpg"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"jpg")
    thumb = tc.thumb_path(photo)
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"thumb")
    # Bump the photo's mtime into the future so it's newer than the thumb.
    future = time.time() + 100
    import os
    os.utime(photo, (future, future))

    assert tc._is_fresh(photo) is False


# ── evict_to_limit ────────────────────────────────────────────────────────────


def test_evict_to_limit_is_noop_when_cache_dir_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "THUMB_CACHE_DIR", tmp_path / "does-not-exist")
    # Just must not raise; nothing to delete.
    tc.evict_to_limit(max_bytes=0)


def test_evict_to_limit_keeps_newest_under_size_cap(monkeypatch, tmp_path):
    # Build five 1024-byte thumbs with explicit ascending mtimes; cap the cache
    # at 3 × 1024 → 5 × 1024 = 5120 → must end up at 3 × 1024 = 3072.
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(tc, "THUMB_CACHE_DIR", cache)

    import os
    payload = b"\x00" * 1024
    files = []
    for i in range(5):
        f = cache / f"t{i}.jpg"
        f.write_bytes(payload)
        # Older = lower mtime → evicted first.
        os.utime(f, (1_000_000 + i, 1_000_000 + i))
        files.append(f)

    tc.evict_to_limit(max_bytes=3 * 1024)

    remaining = sorted(cache.glob("*.jpg"))
    assert [r.name for r in remaining] == ["t2.jpg", "t3.jpg", "t4.jpg"]


def test_evict_to_limit_keeps_all_when_already_under_cap(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(tc, "THUMB_CACHE_DIR", cache)
    (cache / "a.jpg").write_bytes(b"x" * 100)
    (cache / "b.jpg").write_bytes(b"x" * 100)

    tc.evict_to_limit(max_bytes=10_000)

    assert sorted(p.name for p in cache.glob("*.jpg")) == ["a.jpg", "b.jpg"]
