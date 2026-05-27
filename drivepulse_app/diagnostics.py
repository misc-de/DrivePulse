"""Small logging helpers for DrivePulse runtime diagnostics."""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import threading
from pathlib import Path
from typing import Any

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_LOG_DIR = Path(os.environ.get("OBD_LOG_DIR", Path.home() / ".local" / "state" / "drivepulse"))
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUP_COUNT = 3
_ROOT_LOGGER_NAME = "drivepulse_app"

# Telemetry JSONL files are written from background threads at up to ~2 Hz
# (obd-log.jsonl) and rare connection events (drivepulse-log.jsonl). They
# would otherwise grow unbounded — cap them with simple size-based rotation.
_JSONL_MAX_BYTES = 10 * 1024 * 1024
_JSONL_BACKUP_COUNT = 2

_root_setup_lock = threading.Lock()
_root_configured = False
_jsonl_locks: dict[Path, threading.Lock] = {}
_jsonl_locks_guard = threading.Lock()


def _jsonl_lock_for(path: Path) -> threading.Lock:
    with _jsonl_locks_guard:
        lock = _jsonl_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _jsonl_locks[path] = lock
        return lock


def append_jsonl(
    path: Path,
    payload: dict[str, Any],
    max_bytes: int = _JSONL_MAX_BYTES,
    backup_count: int = _JSONL_BACKUP_COUNT,
) -> None:
    """Append one JSON line to *path*, rotating once it exceeds *max_bytes*.

    Rotation renames ``foo.jsonl`` → ``foo.jsonl.1`` → ``foo.jsonl.2`` and
    truncates the live file. Serialized per-path so concurrent writers from
    the reader and scanner threads don't double-rotate.
    """
    line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
    encoded = line.encode("utf-8")
    with _jsonl_lock_for(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            current = path.stat().st_size
        except FileNotFoundError:
            current = 0
        if current > 0 and current + len(encoded) > max_bytes:
            _rotate_jsonl(path, backup_count)
        with path.open("ab") as fh:
            fh.write(encoded)


def atomic_write_text(
    path: Path,
    content: str,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    """Write *content* to *path* atomically via a sibling temp file + rename.

    A plain ``Path.write_text`` truncates the live file before writing.
    A crash (signal, ENOSPC, power loss) between truncate and write leaves
    the file empty or partial, and the next read either fails or recovers
    a stale default — which loses durable state like paired-device
    fingerprints, app settings, or the migrations-done tracker.

    ``os.replace`` is atomic on POSIX and on Windows when source and
    destination live on the same filesystem.

    *mode* is applied to the temp file *before* the rename, so the live
    file is never world-readable for an instant. Use ``0o600`` for files
    holding secrets (API keys, TLS material, paired-device fingerprints).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding=encoding) as fh:
            fh.write(content)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _rotate_jsonl(path: Path, backup_count: int) -> None:
    """Shift foo.jsonl → foo.jsonl.1 → … → foo.jsonl.N, dropping the oldest."""
    oldest = path.with_suffix(path.suffix + f".{backup_count}")
    try:
        oldest.unlink(missing_ok=True)
    except OSError:
        pass
    for i in range(backup_count - 1, 0, -1):
        src = path.with_suffix(path.suffix + f".{i}")
        dst = path.with_suffix(path.suffix + f".{i + 1}")
        if src.exists():
            try:
                src.rename(dst)
            except OSError:
                pass
    if path.exists():
        try:
            path.rename(path.with_suffix(path.suffix + ".1"))
        except OSError:
            pass


def _configure_root_logger() -> None:
    """Attach a rotating file handler to the drivepulse_app parent logger once.

    Module loggers (``drivepulse_app.foo``) propagate to this parent, so a
    single file handle owns the rotation state. Without this, every
    ``get_logger`` call would attach its own handler — fine for a plain
    FileHandler but a correctness problem for RotatingFileHandler, because
    each handler would rotate independently and the others would keep
    writing to the rotated-out inode.
    """
    global _root_configured
    with _root_setup_lock:
        if _root_configured:
            return
        root = logging.getLogger(_ROOT_LOGGER_NAME)
        if not root.handlers:
            try:
                _LOG_DIR.mkdir(parents=True, exist_ok=True)
                handler: logging.Handler = logging.handlers.RotatingFileHandler(
                    _LOG_DIR / "drivepulse.log",
                    maxBytes=_LOG_MAX_BYTES,
                    backupCount=_LOG_BACKUP_COUNT,
                    encoding="utf-8",
                )
            except OSError:
                handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(_LOG_FORMAT))
            root.addHandler(handler)
        root.setLevel(logging.INFO)
        root.propagate = False
        _root_configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a child logger that propagates to the shared file handler."""
    _configure_root_logger()
    return logging.getLogger(name)


def write_diagnostic_log(
    name: str,
    level: int,
    message: str,
    *args: Any,
    exc_info: Any = None,
) -> None:
    """Write an operational diagnostic even when app logging is disabled.

    The settings switch raises the package logger level to ``CRITICAL`` so
    routine logs stay quiet. Some user-triggered failures, such as route
    calculation errors, still need a forensic breadcrumb. Building the record
    directly and handing it to the configured package logger keeps the same
    rotating file handler and format while bypassing the package-level gate.
    """
    _configure_root_logger()
    logger = logging.getLogger(name)
    record = logger.makeRecord(
        name,
        level,
        fn="",
        lno=0,
        msg=message,
        args=args,
        exc_info=exc_info,
        func=None,
        extra=None,
    )
    logging.getLogger(_ROOT_LOGGER_NAME).handle(record)


def set_log_enabled(enabled: bool) -> None:
    """Enable or disable file logging for all drivepulse_app loggers."""
    level = logging.INFO if enabled else logging.CRITICAL
    logging.getLogger(_ROOT_LOGGER_NAME).setLevel(level)
