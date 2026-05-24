"""Continuous OBD sensor recording during a drive session.

Listens to the existing on_update payload stream (no new OBD queries).
Starts automatically when the engine is running (rpm > 0), stops when
stop() is called (e.g. on OBD disconnect / app exit).
Writes to scan_samples in a background writer thread.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import TYPE_CHECKING, Any

from drivepulse_app.diagnostics import get_logger

if TYPE_CHECKING:
    from drivepulse_app.db import DriveDB

_log = get_logger(__name__)

# Mapping: on_update payload key → 4-digit PID hex code.
# Only keys actually polled by OBD_COMMAND_ATTRS are listed.
_KEY_TO_PID: dict[str, str] = {
    "rpm":                      "010C",
    "speed":                    "010D",
    "coolant_temp":             "0105",
    "throttle_pos":             "0111",
    "engine_load":              "0104",
    "intake_temp":              "010F",
    "maf":                      "0110",
    "fuel_level":               "012F",
    "runtime":                  "011F",
    "control_module_voltage":   "0142",
}

_SENTINEL = None  # writer-thread stop signal


class ObdRecorder:
    """Records OBD sensor values into scan_samples during a drive.

    Lifecycle:
        recorder = ObdRecorder(scan_id, db)
        # Call handle_payload() for each on_update payload
        recorder.stop()   # called on disconnect / app shutdown
    """

    def __init__(self, scan_id: int, db: DriveDB) -> None:
        self._scan_id = scan_id
        self._db = db
        self._active = False
        self._started = False  # True once first engine-on payload seen
        self._samples_written = 0
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._writer = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer.start()

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def scan_id(self) -> int:
        return self._scan_id

    @property
    def samples_written(self) -> int:
        return self._samples_written

    def handle_payload(self, payload: dict[str, Any]) -> None:
        """Called on every on_update payload (GTK main thread). Non-blocking."""
        if not self._active:
            return
        source = payload.get("source", "")
        if source not in ("obd", "mock", "mock_fallback"):
            return

        # Auto-start: only begin recording once engine is running.
        if not self._started:
            rpm_raw = payload.get("rpm")
            rpm = self._to_float(rpm_raw)
            if rpm is None or rpm <= 0:
                return
            self._started = True

        ts = time.time()
        rows: list[dict] = []
        for key, pid in _KEY_TO_PID.items():
            raw = payload.get(key)
            value = self._to_float(raw)
            if value is None:
                continue
            unit = raw.get("unit", "") if isinstance(raw, dict) else ""
            rows.append({"ts": ts, "pid": pid, "value": value, "unit": unit})

        if rows:
            self._queue.put(rows)

    def start(self) -> None:
        self._active = True

    def stop(self) -> None:
        self._active = False
        self._queue.put(_SENTINEL)
        self._writer.join(timeout=5.0)
        _log.info("ObdRecorder stopped — %d samples written for scan %d",
                  self._samples_written, self._scan_id)

    # ── Internal ──────────────────────────────────────────────────────────

    def _writer_loop(self) -> None:
        buf: list[dict] = []
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                if buf:
                    self._flush(buf)
                break
            buf.extend(item)
            if len(buf) >= 100:
                self._flush(buf)
                buf = []

    def _flush(self, rows: list[dict]) -> None:
        try:
            n = self._db.add_scan_samples(self._scan_id, rows)
            self._samples_written += n
        except Exception:
            _log.exception("ObdRecorder flush failed for scan_id=%d", self._scan_id)

    @staticmethod
    def _to_float(raw: Any) -> float | None:
        if raw is None:
            return None
        if isinstance(raw, dict):
            raw = raw.get("value")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
