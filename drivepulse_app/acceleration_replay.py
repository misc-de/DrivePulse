"""Replay helpers for acceleration measurements."""
from __future__ import annotations

import time
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib  # noqa: E402

from .common import _translate


class AccelerationReplayMixin:
    def _stop_replay(self) -> None:
        if self._replay_timer_id is not None:
            GLib.source_remove(self._replay_timer_id)
            self._replay_timer_id = None
        self._replay_active = False

    def replay_measurement(self, *_args: Any) -> None:
        if self._saved_results is None or not self._run_samples:
            return
        self._stop_replay()
        self._reset_labels()
        self._set_source_visibility(self._obd_ever_seen, self._gps_ever_seen)
        self.gforce_canvas.clear()
        self._set_g_text(None)
        self._update_maxes_label()
        self._replay_active = True
        self._replay_start_mono = time.monotonic()
        self._replay_sample_idx = 0
        self.replay_button.set_label(_translate(self.language, "acceleration.replay.running"))
        self.status_label.set_text(_translate(self.language, "acceleration.running"))
        self._replay_timer_id = GLib.timeout_add(33, self._replay_tick)

    def _replay_tick(self) -> bool:
        if not self._replay_active or self._saved_results is None:
            self._replay_timer_id = None
            return GLib.SOURCE_REMOVE
        now_elapsed = time.monotonic() - self._replay_start_mono
        samples = self._run_samples
        while self._replay_sample_idx < len(samples):
            s_elapsed, active_g, lateral_g = samples[self._replay_sample_idx]
            if s_elapsed > now_elapsed:
                break
            if active_g is not None:
                self.gforce_canvas.update_g(lateral_g, active_g, 1.0)
                self._set_g_text(active_g)
            self._replay_sample_idx += 1
        for target in self.SPEED_TARGETS_KMH:
            row = self._saved_results[target]
            for source in ("obd", "gps"):
                t = row.get(source)
                if t is not None and t <= now_elapsed:
                    self.result_labels[(target, source)].set_text(f"{t:.2f} s")
        for rkey in self.RANGE_TARGETS_KMH:
            row = self._saved_range_results[rkey]
            for source in ("obd", "gps"):
                t = row.get(source)
                if t is not None and t <= now_elapsed:
                    self.result_labels[(rkey, source)].set_text(f"{t:.2f} s")
        self._update_best_labels_from_saved(now_elapsed)
        obd_v, obd_t = self._saved_vmax_obd, self._saved_vmax_obd_t
        gps_v, gps_t = self._saved_vmax_gps, self._saved_vmax_gps_t
        self._update_vmax_row(
            obd_v=obd_v if (obd_t is not None and obd_t <= now_elapsed) else None,
            obd_t=obd_t if (obd_t is not None and obd_t <= now_elapsed) else None,
            gps_v=gps_v if (gps_t is not None and gps_t <= now_elapsed) else None,
            gps_t=gps_t if (gps_t is not None and gps_t <= now_elapsed) else None,
        )
        max_elapsed = samples[-1][0] if samples else 0.0
        if now_elapsed >= max_elapsed:
            self._stop_replay()
            self._replay_timer_id = None
            self._update_best_labels()
            self._update_vmax_row(obd_v=obd_v, obd_t=obd_t, gps_v=gps_v, gps_t=gps_t)
            self.status_label.set_text(_translate(self.language, "acceleration.done"))
            self.replay_button.set_label(_translate(self.language, "acceleration.replay"))
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _update_best_labels_from_saved(self, up_to_elapsed: float) -> None:
        for target in self.SPEED_TARGETS_KMH:
            row = self._saved_results[target]
            measured = [v for v in row.values() if v is not None and v <= up_to_elapsed]
            avg = sum(measured) / len(measured) if measured else None
            self.result_labels[(target, "best")].set_text("--" if avg is None else f"{avg:.2f} s")
        for rkey in self.RANGE_TARGETS_KMH:
            row = self._saved_range_results[rkey]
            measured = [v for v in row.values() if v is not None and v <= up_to_elapsed]
            avg = sum(measured) / len(measured) if measured else None
            self.result_labels[(rkey, "best")].set_text("--" if avg is None else f"{avg:.2f} s")
