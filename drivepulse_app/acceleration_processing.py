"""Payload processing for acceleration measurements."""
from __future__ import annotations

import math
import time
from typing import Any, Callable

from .common import _translate


class AccelerationProcessingMixin:
    def _update_lateral_g(self, heading_deg: float | None, speed_kmh: float | None, now: float) -> None:
        """Estimate lateral G from GPS heading change × speed (centripetal acceleration).

        a_lat = v · ω, where ω = d(heading)/dt. Below ~10 km/h GPS heading is too
        noisy, so the estimate falls back to 0 to avoid jitter in the display.
        """
        if heading_deg is None or speed_kmh is None or speed_kmh < 10.0:
            self._last_heading_deg = heading_deg
            self._last_heading_time = now
            self._lateral_g *= 0.6  # decay toward zero when no usable input
            return
        if self._last_heading_deg is None or self._last_heading_time is None:
            self._last_heading_deg = heading_deg
            self._last_heading_time = now
            return
        dt = max(0.05, now - self._last_heading_time)
        # Wrap heading delta into (-180, 180]
        delta = (heading_deg - self._last_heading_deg + 540.0) % 360.0 - 180.0
        omega_rad_s = math.radians(delta) / dt
        v_ms = speed_kmh / 3.6
        a_lat_ms2 = v_ms * omega_rad_s
        # Light low-pass to suppress GPS jitter; positive = right turn
        target = a_lat_ms2 / 9.80665
        self._lateral_g += (target - self._lateral_g) * 0.35
        self._last_heading_deg = heading_deg
        self._last_heading_time = now

    def update_payload(self, payload: dict[str, Any], read_number: Callable[[dict[str, Any], str], float | None]) -> None:
        now = time.monotonic()
        obd_speed = read_number(payload, "speed")
        gps_speed = read_number(payload, "gps_speed")
        measured_g = read_number(payload, "acceleration_g")
        heading = read_number(payload, "gps_heading")
        active = self._is_active()

        self._update_lateral_g(heading, gps_speed if gps_speed is not None else obd_speed, now)

        if obd_speed is not None and self.last_obd_speed is not None and self.last_speed_time is not None:
            dt = max(0.001, now - self.last_speed_time)
            acceleration_ms2 = ((obd_speed - self.last_obd_speed) / 3.6) / dt
            self.computed_acceleration_g = acceleration_ms2 / 9.80665

        if obd_speed is not None:
            self.last_obd_speed = obd_speed
            self.last_speed_time = now

        # Live displays (current G, gforce ball) keep updating regardless of
        # measurement state — they show "right now", not measurement data.
        active_g = measured_g if measured_g is not None else self.computed_acceleration_g
        self._set_g_text(active_g)
        if active_g is not None or gps_speed is not None or obd_speed is not None:
            # Y axis: longitudinal G (positive = forward acceleration)
            # X axis: lateral G (positive = right turn, computed via heading delta)
            self.gforce_canvas.update_g(self._lateral_g, active_g if active_g is not None else 0.0, 1.0)

        # Measurement-bound displays (source row visibility, Vmax/Gmax) only
        # change while a run is armed/active. Once it ends, they freeze at their
        # final value and stop flickering between OBD/GPS payloads.
        if not active:
            return

        # Sticky source visibility: a column appears as soon as that source has
        # ever produced a speed during this measurement cycle, and stays put
        # until reset, so alternating GPS/OBD payloads no longer cause flicker.
        if obd_speed is not None:
            self._obd_ever_seen = True
        if gps_speed is not None:
            self._gps_ever_seen = True
        self._set_source_visibility(self._obd_ever_seen, self._gps_ever_seen)

        # Max-G während der gesamten scharfen / laufenden Messung fortschreiben.
        # Max-Speed nur im laufenden Block (unten), damit elapsed-Zeit korrekt
        # mitgespeichert werden kann — würde man den Speed hier vorweg setzen,
        # wäre die Bedingung im laufenden Block stets False und _max_*_speed_t
        # bliebe dauerhaft None.
        if active_g is not None and (self.max_g is None or active_g > self.max_g):
            self.max_g = active_g
        self._update_maxes_label()

        if self.armed and not self.running:
            trig_g: float | None = self._raw_g_dev if self._gforce_trigger else active_g

            if trig_g is not None:
                if trig_g >= self._engage_threshold:
                    if self._engage_since is None:
                        self._engage_since = now
                else:
                    self._engage_since = None  # dropped below — reset confirm window

                if trig_g >= self.G_PRESTART_THRESHOLD:
                    if self._prestart_since is None:
                        self._prestart_since = now
                else:
                    self._prestart_since = None  # gap in gentle push — reset retroactive marker

            speed_ok = (
                (obd_speed is not None and obd_speed >= self.G_MIN_SPEED_KMH)
                or (gps_speed is not None and gps_speed >= self.G_MIN_SPEED_KMH)
            )
            sustained = (
                self._engage_since is not None
                and (now - self._engage_since) >= self.G_CONFIRM_WINDOW
            )
            if sustained and speed_ok:
                self.running = True
                # Set start time retroactively to when the gentle push began
                self.start_monotonic = self._prestart_since if self._prestart_since is not None else now
                self.status_label.set_text(_translate(self.language, "acceleration.running"))

        if not self.running or self.start_monotonic is None:
            return

        elapsed = now - self.start_monotonic
        self._run_samples.append((elapsed, active_g, self._lateral_g))

        # Track elapsed time when a new max speed is set (requires elapsed, so done here)
        if obd_speed is not None and (self.max_obd_speed is None or obd_speed > self.max_obd_speed):
            self.max_obd_speed = obd_speed
            self._max_obd_speed_t = elapsed
        if gps_speed is not None and (self.max_gps_speed is None or gps_speed > self.max_gps_speed):
            self.max_gps_speed = gps_speed
            self._max_gps_speed_t = elapsed
        self._update_vmax_row(
            obd_v=self.max_obd_speed, obd_t=self._max_obd_speed_t,
            gps_v=self.max_gps_speed, gps_t=self._max_gps_speed_t,
        )

        for target in self.SPEED_TARGETS_KMH:
            row = self.results[target]
            if row["obd"] is None and obd_speed is not None and obd_speed >= target:
                row["obd"] = elapsed
                self.result_labels[(target, "obd")].set_text(f"{elapsed:.2f} s")
            if row["gps"] is None and gps_speed is not None and gps_speed >= target:
                row["gps"] = elapsed
                self.result_labels[(target, "gps")].set_text(f"{elapsed:.2f} s")

        for lo, hi in self.RANGE_TARGETS_KMH:
            rrow = self.range_results[(lo, hi)]
            lo_obd = self.results.get(lo, {}).get("obd")
            hi_obd = self.results.get(hi, {}).get("obd")
            if rrow["obd"] is None and lo_obd is not None and hi_obd is not None:
                rrow["obd"] = hi_obd - lo_obd
                self.result_labels[((lo, hi), "obd")].set_text(f"{rrow['obd']:.2f} s")
            lo_gps = self.results.get(lo, {}).get("gps")
            hi_gps = self.results.get(hi, {}).get("gps")
            if rrow["gps"] is None and lo_gps is not None and hi_gps is not None:
                rrow["gps"] = hi_gps - lo_gps
                self.result_labels[((lo, hi), "gps")].set_text(f"{rrow['gps']:.2f} s")

        self._update_best_labels()

        all_done = all(v["obd"] is not None or v["gps"] is not None for v in self.results.values())
        if all_done:
            self.running = False
            self.armed = False
            self._saved_results = {k: dict(v) for k, v in self.results.items()}
            self._saved_range_results = {k: dict(v) for k, v in self.range_results.items()}
            self._saved_vmax_obd = self.max_obd_speed
            self._saved_vmax_obd_t = self._max_obd_speed_t
            self._saved_vmax_gps = self.max_gps_speed
            self._saved_vmax_gps_t = self._max_gps_speed_t
            self._show_replay()
            self.status_label.set_text(_translate(self.language, "acceleration.done"))
            if self.on_run_complete is not None:
                combined = {
                    "targets": {str(k): dict(v) for k, v in self.results.items()},
                    "ranges": {str(k): dict(v) for k, v in self.range_results.items()},
                    "max_obd_kmh": self.max_obd_speed,
                    "max_obd_t": self._max_obd_speed_t,
                    "max_gps_kmh": self.max_gps_speed,
                    "max_gps_t": self._max_gps_speed_t,
                    "max_g": self.max_g,
                }
                samples_list = [list(s) for s in self._run_samples]
                self.on_run_complete(combined, samples_list)
