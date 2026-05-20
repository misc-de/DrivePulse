"""Telemetry, scan and trip handlers for DashboardWindow."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from gi.repository import GLib

from .common import _detect_language, _normalize_language, _translate
from .cars_profiles import _load_profiles
from .dashboard_data import obd_sample_fields, scan_identity_from_payload, scan_profile_dashboard_data
from .diagnostics import get_logger
from .telemetry_utils import display_speed, has_obd_data, plain_number


log = get_logger(__name__)


class DashboardTelemetryMixin:
    _scan_is_new_car: bool = False
    _pending_new_car_id: "int | None" = None
    def _known_car_id_for_vin(self, vin: str | None) -> int | None:
        if not vin:
            return None
        try:
            for row in self.db.list_cars():
                if (row["vin"] or "") == vin:
                    return int(row["id"])
        except Exception:
            log.exception("Could not look up car by VIN")
        return None

    def _add_live_vehicle_from_identity(self, identity: dict[str, str]) -> int | None:
        vin = identity.get("VIN")
        if not vin:
            return None
        try:
            car_id = self.trip_recorder.set_car(
                vin=vin,
                brand=identity.get("brand"),
                cal_id=identity.get("CALIBRATION_ID"),
                cvn=identity.get("CVN"),
                protocol=identity.get("protocol"),
                profile_path=identity.get("profile_path"),
            )
            self.cars_page.refresh_profiles()
            self._refresh_last_trip_stats(car_id)
            return car_id
        except Exception:
            log.exception("Could not add live vehicle from identity")
            return None

    def _handle_scan_update(self, payload: dict[str, Any]) -> None:
        status = payload.get("scan_status", "")
        progress = float(payload.get("scan_progress", 0.0))
        current = str(payload.get("scan_current", ""))

        if status == "skipped":
            self.scan_bar.set_visible(False)
            return

        if status in ("scanning", "saving"):
            self.scan_bar.set_visible(True)
            self.scan_bar.set_fraction(progress)
            label = f"Fahrzeugscan: {current} ({progress * 100:.0f}%)" if current else f"Fahrzeugscan... ({progress * 100:.0f}%)"
            self.scan_bar.set_text(label)
            return

        if status == "complete":
            self.scan_bar.set_fraction(1.0)
            self.scan_bar.set_text("Fahrzeugscan abgeschlossen")
            profile = payload.get("scan_profile")
            if profile:
                self._save_scan_to_db(profile)
                self._update_dashboard_from_profile(profile)
            self.cars_page.refresh_profiles()
            if self._scan_is_new_car and self._pending_new_car_id is not None:
                new_car_id = self._pending_new_car_id
                self._scan_is_new_car = False
                self._pending_new_car_id = None
                GLib.idle_add(self.cars_page.open_car, new_car_id)
            GLib.timeout_add(3000, self._hide_scan_bar)
            return

        if status == "error":
            self.scan_bar.set_visible(True)
            self.scan_bar.set_text(f"Scan-Fehler: {current}")
            GLib.timeout_add(6000, self._hide_scan_bar)

    def _save_scan_to_db(self, profile: dict) -> None:
        car_id = getattr(self.trip_recorder, "car_id", None)
        if car_id is None:
            return
        try:
            self.db.add_scan(car_id, profile)
        except Exception:
            log.exception("Could not save scan profile to database")

    def _hide_scan_bar(self) -> bool:
        self.scan_bar.set_visible(False)
        return False

    def _plain_number(self, data: dict[str, Any], key: str) -> float | None:
        return plain_number(data, key)

    def _display_speed(self, speed_kmh: float | None) -> float | None:
        return display_speed(speed_kmh, self.units)

    def _has_obd_data(self, payload: dict[str, Any]) -> bool:
        return has_obd_data(payload)

    def _gps_connected_with_holdover(self, gps_has_fix: bool) -> bool:
        now = time.monotonic()
        if gps_has_fix:
            self._gps_last_seen = now
            return True
        return (now - getattr(self, "_gps_last_seen", 0.0)) < self.GPS_UNAVAIL_HOLDOVER

    def _update_from_payload(self, payload: dict[str, Any]) -> bool:
        source = payload.get("source", "")

        if source == "obd_scan":
            self._handle_scan_update(payload)
            return False

        if source == "obd_scan_identity":
            self._handle_scan_identity(payload)
            return False

        if source == "gps":
            gps_speed_kmh = self._plain_number(payload, "gps_speed")
            gps_heading = self._plain_number(payload, "gps_heading")
            lat = self._plain_number(payload, "gps_lat")
            lon = self._plain_number(payload, "gps_lon")
            altitude_m = self._plain_number(payload, "gps_altitude")
            if lat is not None:
                self._last_gps_lat = lat
            if lon is not None:
                self._last_gps_lon = lon
            # Connected when we have a position fix — speed may be absent at standstill.
            gps_has_fix = lat is not None
            gps_active = gps_has_fix or gps_speed_kmh is not None
            if gps_active:
                self._gps_last_seen = time.monotonic()
            if gps_speed_kmh is not None:
                self._last_gps_speed_kmh = gps_speed_kmh
            elif not gps_active:
                self._last_gps_speed_kmh = None
            self._set_link_indicator(self.gps_indicator, gps_active, False)
            trip_recorder = getattr(self, "trip_recorder", None)
            if trip_recorder is not None:
                trip_recorder.update_gps(
                    lat=lat, lon=lon, altitude_m=altitude_m,
                    heading_deg=gps_heading, gps_speed_kmh=gps_speed_kmh,
                )
            self.stopwatch_page.update_payload(payload, self._plain_number)
            self.cars_page.update_live(payload)
            if hasattr(self, "map_page"):
                self.map_page.update_gps(lat, lon, gps_heading, gps_speed_kmh)
            if hasattr(self, "dashcam_page"):
                self.dashcam_page.update_gps(lat, lon, gps_speed_kmh)
            if not getattr(self, "_obd_active", False) and gps_active:
                held_kmh = self._last_gps_speed_kmh if self._last_gps_speed_kmh is not None else 0.0
                display = self._display_speed(held_kmh)
                src_gps = _translate(self.language, "gauge.source.gps")
                self.speed_gauge.set_value(display, f"{display:.0f}" if display is not None else None)
                self.speed_gauge.set_source_label(src_gps)
            else:
                display = None
                src_gps = ""
            with self.dashboard_canvas.batch_update():
                if gps_heading is not None:
                    self.dashboard_canvas.update_heading(gps_heading)
                self.dashboard_canvas.update_gps_speed(self._display_speed(gps_speed_kmh))
                self.dashboard_canvas.update_gps_pos(lat, lon, altitude_m)
                if src_gps:
                    self.dashboard_canvas.update_speed(display, f"{display:.0f}" if display is not None else None)
                    self.dashboard_canvas.update_speed_source(src_gps)
            return False

        self.last_payload = payload
        active = source in ("obd", "mock")
        rpm = self._plain_number(payload, "rpm") if active else None
        obd_speed_kmh = self._plain_number(payload, "speed") if active else None
        gps_speed_kmh = self._plain_number(payload, "gps_speed") if active else None
        speed_source_kmh = obd_speed_kmh if obd_speed_kmh is not None else gps_speed_kmh
        speed = self._display_speed(speed_source_kmh)
        temp = self._plain_number(payload, "coolant_temp") if active else None
        obd_connected = active and self._has_obd_data(payload)
        was_obd_active = getattr(self, "_obd_active", False)
        self._obd_active = obd_connected
        if was_obd_active and not obd_connected:
            self.cars_page.clear_live_session()
        obd_connecting = bool(payload.get("obd_connecting"))
        gps_connected = self._gps_connected_with_holdover(gps_speed_kmh is not None if active else False)
        _prev_gps_connected = getattr(self, "_gps_was_connected", False)
        self._gps_was_connected = gps_connected

        self._set_link_indicator(self.obd_indicator, obd_connected, obd_connecting)
        self._set_link_indicator(self.gps_indicator, gps_connected, False)

        self.rpm_gauge.set_value(rpm, None if rpm is None else f"{rpm:.0f}")
        if obd_connected:
            # Only update speed gauge from OBD payloads when OBD data is actually present.
            # When OBD is disconnected, the GPS branch owns the speed gauge.
            if speed is not None:
                _gauge_speed = speed
                if obd_speed_kmh is not None:
                    _spd_src = _translate(self.language, "gauge.source.obd")
                elif gps_speed_kmh is not None:
                    _spd_src = _translate(self.language, "gauge.source.gps")
                else:
                    _spd_src = ""
            elif gps_connected:
                # OBD has no speed but GPS is active — show GPS held speed to avoid
                # blanking the gauge while the OBD dongle has no vehicle speed (e.g.
                # engine-off queries returning partial data).
                _held = self._last_gps_speed_kmh if self._last_gps_speed_kmh is not None else 0.0
                _gauge_speed = self._display_speed(_held)
                _spd_src = _translate(self.language, "gauge.source.gps")
            else:
                _gauge_speed = None
                _spd_src = ""
            self.speed_gauge.set_value(_gauge_speed, None if _gauge_speed is None else f"{_gauge_speed:.0f}")
            self.speed_gauge.set_source_label(_spd_src)
        elif not gps_connected:
            # GPS truly gone (holdover expired) — clear speed gauge once on transition.
            if _prev_gps_connected:
                self._last_gps_speed_kmh = None
                self.speed_gauge.set_value(None, None)
                self.speed_gauge.set_source_label("")
            _spd_src = ""
        else:
            _spd_src = ""
        self.temp_gauge.set_value(temp, None if temp is None else f"{temp:.0f}")
        self.stopwatch_page.update_payload(payload, self._plain_number)
        self.cars_page.update_live(payload)

        canvas_speed = self._display_speed(speed_source_kmh)
        fuel = self._plain_number(payload, "fuel_level") if active else None
        heading = self._plain_number(payload, "gps_heading") if active else None
        throttle = self._plain_number(payload, "throttle_pos") if active else None
        engine_load = self._plain_number(payload, "engine_load") if active else None
        intake = self._plain_number(payload, "intake_temp") if active else None
        maf = self._plain_number(payload, "maf") if active else None
        voltage = self._plain_number(payload, "control_module_voltage") if active else None
        accel = self._plain_number(payload, "acceleration_g") if active else None

        with self.dashboard_canvas.batch_update():
            self.dashboard_canvas.update_rpm(rpm, None if rpm is None else f"{rpm:.0f}")
            # Speed and source: OBD owns when connected; GPS branch owns when GPS is active.
            # When neither is active, clear the display.
            if obd_connected:
                # Use same speed as the gauge (includes GPS fallback when OBD has no speed).
                self.dashboard_canvas.update_speed(_gauge_speed, None if _gauge_speed is None else f"{_gauge_speed:.0f}")
                self.dashboard_canvas.update_speed_source(_spd_src)
            elif not gps_connected:
                self.dashboard_canvas.update_speed(None, None)
                self.dashboard_canvas.update_speed_source("")
            # else: GPS branch already set speed and source — leave them intact.
            self.dashboard_canvas.update_coolant(temp, None if temp is None else f"{temp:.0f}")
            self.dashboard_canvas.update_fuel(fuel, None if fuel is None else f"{fuel:.0f}%")
            self.dashboard_canvas.update_throttle(throttle)
            self.dashboard_canvas.update_engine_load(engine_load)
            self.dashboard_canvas.update_intake(intake)
            self.dashboard_canvas.update_maf(maf)
            self.dashboard_canvas.update_voltage(voltage)
            self.dashboard_canvas.update_accel(accel)
            self.dashboard_canvas.update_obd_speed(self._display_speed(obd_speed_kmh))
            # gps_speed appears in mock payloads; real GPS updates come via the "gps" branch
            if gps_speed_kmh is not None:
                self.dashboard_canvas.update_gps_speed(self._display_speed(gps_speed_kmh))
            if heading is not None:
                self.dashboard_canvas.update_heading(heading)

        status = payload.get("connection_status") or source or "?"
        language = _normalize_language(getattr(self, "language", _detect_language()))
        self.status_label.set_text(_translate(language, "status.updated", status=status, time=datetime.now().strftime("%H:%M:%S")))

        # Telemetrie persistieren — nur bei echter OBD-Verbindung (mock zählt nicht).
        if source == "obd" and self._has_obd_data(payload):
            self._record_obd_sample(payload)
        return False

    def _record_obd_sample(self, payload: dict[str, Any]) -> None:
        ts = time.time()
        fields = obd_sample_fields(payload, self._plain_number)
        trip_recorder = getattr(self, "trip_recorder", None)
        if trip_recorder is None:
            return
        try:
            trip_recorder.record_obd(ts, **fields)
        except Exception:
            log.exception("Could not record OBD sample")

        self._update_live_trip_stats(fields)

    def _update_live_trip_stats(self, fields: dict) -> None:
        """Min/Max während des laufenden Trips verfolgen und Dashboard aktualisieren."""
        trip_recorder = getattr(self, "trip_recorder", None)
        if trip_recorder is None:
            return
        current_trip_id = getattr(trip_recorder, "trip_id", None)

        # Neuer Trip gestartet → Live-Tracking zurücksetzen
        if current_trip_id is not None and current_trip_id != self._live_trip_id:
            self._live_trip_id = current_trip_id
            self._live_rpm_min = None
            self._live_rpm_max = None
            self._live_coolant_min = None
            self._live_coolant_max = None
            self._live_speed_max = None

        if current_trip_id is None:
            return

        rpm = fields.get("rpm")
        coolant = fields.get("coolant_c")
        speed = fields.get("speed_kmh")

        def _upd_min(cur: "float | None", v: float) -> float:
            return v if cur is None else min(cur, v)

        def _upd_max(cur: "float | None", v: float) -> float:
            return v if cur is None else max(cur, v)

        changed = False
        if rpm is not None:
            new_min = _upd_min(self._live_rpm_min, rpm)
            new_max = _upd_max(self._live_rpm_max, rpm)
            if new_min != self._live_rpm_min or new_max != self._live_rpm_max:
                self._live_rpm_min = new_min
                self._live_rpm_max = new_max
                changed = True
        if coolant is not None:
            new_min = _upd_min(self._live_coolant_min, coolant)
            new_max = _upd_max(self._live_coolant_max, coolant)
            if new_min != self._live_coolant_min or new_max != self._live_coolant_max:
                self._live_coolant_min = new_min
                self._live_coolant_max = new_max
                changed = True
        if speed is not None:
            new_max = _upd_max(self._live_speed_max, speed)
            if new_max != self._live_speed_max:
                self._live_speed_max = new_max
                changed = True

        if changed and (self._live_rpm_max or self._live_coolant_max):
            self.dashboard_canvas.update_last_trip_stats({
                "min_rpm": self._live_rpm_min or 0.0,
                "max_rpm": self._live_rpm_max or 0.0,
                "min_coolant": self._live_coolant_min or 0.0,
                "max_coolant": self._live_coolant_max or 0.0,
                "max_speed_kmh": self._live_speed_max or 0.0,
                "distance_km": None,
                "duration_s": None,
            })

    def _update_dashboard_from_profile(self, data: dict[str, Any]) -> None:
        """Parse a scan profile dict and push all PID / identity / DTC data to the dashboard."""
        pids, info, dtcs, pending = scan_profile_dashboard_data(data)
        self.dashboard_canvas.update_scan_data(pids, info, dtcs, pending)

    def _load_initial_scan_data(self) -> bool:
        """Called once after startup: push the most recent profile into the dashboard."""
        try:
            profiles = _load_profiles(self.db)
            if profiles:
                best = max(profiles, key=lambda p: p.get("last_seen") or "")
                if best.get("data"):
                    self._update_dashboard_from_profile(best["data"])
        except Exception:
            log.exception("Could not load initial scan data")
        return False

    def _handle_scan_identity(self, payload: dict[str, Any]) -> None:
        """Vom Scanner gemeldete Fahrzeug-Identität in die Live-Ansicht übernehmen."""
        scan_identity = scan_identity_from_payload(payload)
        identity = scan_identity["identity"]
        if scan_identity.get("brand"):
            identity["brand"] = scan_identity["brand"]
        if scan_identity.get("profile_path"):
            identity["profile_path"] = str(scan_identity["profile_path"])
        if identity:
            self.cars_page.set_live_identity(identity)

        car_id = self._known_car_id_for_vin(scan_identity["vin"])
        if car_id is not None:
            self._scan_is_new_car = False
            try:
                self.trip_recorder.set_car(
                    vin=scan_identity["vin"],
                    brand=scan_identity["brand"],
                    cal_id=scan_identity["cal_id"],
                    cvn=scan_identity["cvn"],
                    protocol=scan_identity["protocol"],
                    profile_path=scan_identity["profile_path"],
                )
            except Exception:
                log.exception("Could not set known trip recorder identity from scan payload")
        elif scan_identity.get("vin"):
            # Neues, unbekanntes Fahrzeug → sofort in DB anlegen
            new_id = self._add_live_vehicle_from_identity(identity)
            if new_id is not None:
                self._scan_is_new_car = True
                self._pending_new_car_id = new_id
                car_id = new_id

        # Letzten abgeschlossenen Trip laden und im Dashboard anzeigen,
        # solange noch kein laufender Trip aktiv ist.
        if car_id is not None and self.trip_recorder.trip_id is None:
            self._refresh_last_trip_stats(car_id)

    def _refresh_last_trip_stats(self, car_id: int) -> None:
        """Letzten abgeschlossenen Trip aus DB laden und Dashboard aktualisieren."""
        try:
            stats = self.db.get_last_trip_stats(car_id)
        except Exception:
            log.exception("Could not load last trip stats for car id=%s", car_id)
            stats = None
        self.dashboard_canvas.update_last_trip_stats(stats)

    def _db_periodic_tick(self) -> bool:
        # WAL-Checkpoint + Idle-Erkennung
        try:
            self.db.checkpoint()
        except Exception:
            log.exception("Could not checkpoint database")
        try:
            self.trip_recorder.maybe_end_idle_trip(time.time())
        except Exception:
            log.exception("Could not end idle trip")
        return True

    def _shutdown_db(self) -> None:
        try:
            self.trip_recorder.end_trip()
        except Exception:
            log.exception("Could not end active trip during shutdown")
        try:
            self.db.close()
        except Exception:
            log.exception("Could not close database")
