"""Incoming GPS / OBD speed handling and the kinematic sanity filter.

The kinematic filter rejects GPS jumps that cannot be explained by physical
acceleration. A jump that is direction-consistent with the current heading is
held as a "suspect" for one cycle — if the next fix corroborates it, the
suspect is accepted retroactively; otherwise it is discarded as noise. OBD
speed (when fresh) is used as a second sanity check against position-implied
speed."""
from __future__ import annotations

import time

from drivepulse_app.diagnostics import get_logger
from drivepulse_app.map.services import bearing, haversine, snap_to_route

log = get_logger(__name__)


class MapGpsFilterMixin:
    # Owning class (MapPage) initializes these in __init__. Annotated here so
    # mypy doesn't infer them as non-Optional from a later assignment inside
    # this mixin's methods.
    _gps_lat: float | None
    _gps_lon: float | None
    _gps_filt_lat: float | None
    _gps_filt_lon: float | None
    _snapped_lat: float | None
    _snapped_lon: float | None
    _gps_filt_suspect: tuple | None
    _obd_speed_kmh: float | None
    _last_map_js_lat: float | None
    _last_map_js_lon: float | None

    # ── GPS kinematic sanity filter ───────────────────────────────────────────

    # Maximum plausible acceleration: ~10 m/s² ≈ 36 km/h per second (sports car).
    # GPS noise typically implies thousands of km/h, so this threshold is generous.
    _GPS_MAX_ACCEL_KMH_S: float = 36.0
    # Extra headroom applied on top of the kinematic maximum (20 %).
    _GPS_SPEED_TOL: float = 1.2
    # Bearing-vs-heading tolerance for classifying an implausible jump as a
    # "possible rapid acceleration" rather than an outright GPS error.
    _GPS_DIR_TOL_DEG: float = 45.0
    # After this many seconds without an accepted fix, stop filtering and accept
    # whatever arrives (GPS receiver recovered / tunnel exit / etc.).
    _GPS_MAX_STALE_S: float = 10.0
    # OBD speed cross-validation: how long OBD data stays valid, and how large a
    # discrepancy between GPS speed and OBD speed triggers position rejection.
    _OBD_SPEED_STALE_S: float = 5.0
    _OBD_GPS_SPEED_DIFF_KMH: float = 30.0

    def update_gps(
        self,
        lat: float | None,
        lon: float | None,
        heading: float | None,
        speed_kmh: float | None = None,
    ) -> None:
        if lat is None or lon is None:
            return
        now = time.monotonic()
        lat, lon, heading, speed_kmh = self._gps_filter(lat, lon, heading, speed_kmh, now)

        self._gps_lat = lat
        self._gps_lon = lon
        self._gps_heading = heading or 0.0
        # Heading is only reliable when the vehicle is actually moving; below
        # ~5 km/h GPS heading readings are too noisy to disambiguate direction.
        self._gps_heading_valid = heading is not None and (speed_kmh or 0.0) >= 5.0
        self._gps_speed_mps = (speed_kmh / 3.6) if speed_kmh is not None else self._gps_speed_mps

        # Snap GPS onto the nearest route segment during active/paused navigation.
        if (self._tour_active or self._tour_paused) and len(self._tour_coords) >= 2 and self._route_cum_m:
            heading_snap = self._gps_heading if self._gps_heading_valid else None
            slat, slon, seg_idx, scum = snap_to_route(
                lat, lon, self._tour_coords, self._route_cum_m, self._gps_route_idx,
                heading=heading_snap,
            )
            self._snapped_lat = slat
            self._snapped_lon = slon
            self._gps_route_idx = seg_idx
            self._snapped_cum_m = scum
            if self._tour_active:
                off_dist_m = haversine(lat, lon, slat, slon)
                self._check_off_route(off_dist_m, now)
        else:
            self._snapped_lat = None
            self._snapped_lon = None

        display_lat = self._snapped_lat if self._snapped_lat is not None else lat
        display_lon = self._snapped_lon if self._snapped_lon is not None else lon

        # During an active tour always re-engage follow so the map tracks the driver.
        if self._tour_active and not self._follow_gps:
            self._set_follow(True)

        if self._backend == "webkit":
            now = time.monotonic()
            if now - self._last_map_js >= self._MAP_JS_INTERVAL:
                heading_delta = abs(self._gps_heading - self._last_map_js_heading)
                if heading_delta > 180.0:
                    heading_delta = 360.0 - heading_delta
                moved = (
                    self._last_map_js_lat is None
                    or self._last_map_js_lon is None
                    or abs(display_lat - self._last_map_js_lat) >= self._MAP_JS_MIN_DEG
                    or abs(display_lon - self._last_map_js_lon) >= self._MAP_JS_MIN_DEG
                    or heading_delta >= self._MAP_JS_MIN_HEADING
                )
                stale = now - self._last_map_js >= self._MAP_JS_HEARTBEAT_S
                if moved or stale:
                    self._last_map_js = now
                    self._last_map_js_lat = display_lat
                    self._last_map_js_lon = display_lon
                    self._last_map_js_heading = self._gps_heading
                    self._js(f"mapSetCar({display_lat}, {display_lon}, {self._gps_heading})")
        elif self._backend == "shumate" and self._shumate_map is not None:
            self._update_shumate_gps(display_lat, display_lon)
            if self._follow_gps:
                self._goto(display_lat, display_lon)

        if self._coord_lbl is not None:
            ns = "N" if lat >= 0 else "S"
            ew = "E" if lon >= 0 else "W"
            self._coord_lbl.set_label(
                f"{abs(lat):.5f}° {ns}  {abs(lon):.5f}° {ew}"
            )
            if self._coord_overlay is not None:
                self._coord_overlay.set_visible(True)

        if self._tour_active or self._tour_paused:
            self._update_maneuver_overlay()
            self._check_waypoint_proximity()

    def update_obd_speed(self, speed_kmh: float) -> None:
        """Store the latest OBD vehicle speed for GPS cross-validation."""
        self._obd_speed_kmh = speed_kmh
        self._obd_speed_time = time.monotonic()

    def _gps_filter(
        self,
        lat: float,
        lon: float,
        heading: float | None,
        speed_kmh: float | None,
        now: float,
    ) -> tuple[float, float, float | None, float | None]:
        """Kinematic GPS sanity filter.

        Rejects position jumps that cannot be explained by physical acceleration.
        A jump that is direction-consistent with the current heading is held as a
        "suspect" for one GPS cycle: if the following point validates it (movement
        from the suspect is also plausible) it is accepted retroactively; otherwise
        it is discarded as noise and the last valid position is kept.

        Returns (filtered_lat, filtered_lon, heading, speed_kmh).
        """
        # ── First fix: accept unconditionally ─────────────────────────────────
        if self._gps_filt_lat is None or self._gps_filt_lon is None:
            self._gps_filter_accept(lat, lon, heading, speed_kmh, now)
            return lat, lon, heading, speed_kmh

        # Past the first-fix guard both lat and lon are concrete floats;
        # bind locally so mypy can narrow across the haversine/bearing calls.
        filt_lat: float = self._gps_filt_lat
        filt_lon: float = self._gps_filt_lon

        dt = now - self._gps_filt_time
        # Too long since last fix — stop filtering (tunnel exit, GPS recovery).
        if dt >= self._GPS_MAX_STALE_S or dt <= 0:
            self._gps_filter_accept(lat, lon, heading, speed_kmh, now)
            return lat, lon, heading, speed_kmh

        dist_m = haversine(filt_lat, filt_lon, lat, lon)
        implied_kmh = (dist_m / dt) * 3.6
        max_ok_kmh = (
            self._gps_filt_speed_kmh + self._GPS_MAX_ACCEL_KMH_S * dt
        ) * self._GPS_SPEED_TOL

        # ── OBD speed cross-validation ────────────────────────────────────────
        # If a fresh OBD speed reading is available and the GPS-reported speed
        # (or position-implied speed as fallback) differs from it by more than
        # _OBD_GPS_SPEED_DIFF_KMH, the GPS position fix is unreliable — hold
        # the last valid position regardless of kinematic plausibility.
        obd_kmh = self._obd_speed_kmh
        if (
            obd_kmh is not None
            and (now - self._obd_speed_time) < self._OBD_SPEED_STALE_S
        ):
            gps_speed = speed_kmh if speed_kmh is not None else implied_kmh
            if abs(gps_speed - obd_kmh) > self._OBD_GPS_SPEED_DIFF_KMH:
                log.debug(
                    "GPS speed %.0f km/h contradicts OBD %.0f km/h — position held",
                    gps_speed, obd_kmh,
                )
                return (
                    filt_lat,
                    filt_lon,
                    self._gps_filt_heading,
                    self._gps_filt_speed_kmh,
                )

        if implied_kmh <= max_ok_kmh:
            # ── Speed is kinematically plausible ──────────────────────────────
            if self._gps_filt_suspect is not None:
                # The new point is consistent with the *last valid* fix (not the
                # suspect), so the suspect was GPS noise — discard it silently.
                slat, slon, _shdg, sspd, _st = self._gps_filt_suspect
                log.debug(
                    "GPS filter: suspect (%.0f km/h jump from valid) discarded — "
                    "next point consistent with last valid",
                    haversine(filt_lat, filt_lon, slat, slon) / dt * 3.6,
                )
                self._gps_filt_suspect = None
            self._gps_filter_accept(lat, lon, heading, speed_kmh, now)
            return lat, lon, heading, speed_kmh

        # ── Speed is implausible ──────────────────────────────────────────────
        if self._gps_filt_suspect is not None:
            # We already hold one suspect.  Check if the *new* point is plausible
            # as a continuation of the suspect (i.e., the jump was a real
            # rapid acceleration and both points are consistent).
            slat, slon, shdg, sspd, st = self._gps_filt_suspect
            dt_susp = now - st
            if dt_susp > 0:
                dist_from_susp = haversine(slat, slon, lat, lon)
                impl_from_susp = (dist_from_susp / dt_susp) * 3.6
                max_from_susp = (
                    (sspd or self._gps_filt_speed_kmh) + self._GPS_MAX_ACCEL_KMH_S * dt_susp
                ) * self._GPS_SPEED_TOL
                if impl_from_susp <= max_from_susp:
                    # Confirmed: the original jump was a real rapid acceleration.
                    log.debug(
                        "GPS filter: suspect confirmed as real acceleration "
                        "(%.0f → %.0f km/h) — accepted retroactively",
                        self._gps_filt_speed_kmh, sspd or 0,
                    )
                    self._gps_filter_accept(slat, slon, shdg, sspd, st)
                    self._gps_filt_suspect = None
                    self._gps_filter_accept(lat, lon, heading, speed_kmh, now)
                    return lat, lon, heading, speed_kmh
            # New point is also inconsistent with the suspect → discard both.
            log.debug(
                "GPS filter: suspect and new point both implausible — discarding both"
            )
            self._gps_filt_suspect = None
            # Fall through: return last valid position.

        else:
            # No pending suspect yet.  Check if movement direction is consistent
            # with the current heading — if so, the jump might be a rapid
            # acceleration; hold it for one cycle.
            move_bearing = (
                bearing(filt_lat, filt_lon, lat, lon)
                if dist_m > 5.0 else self._gps_filt_heading
            )
            diff = abs(self._gps_filt_heading - move_bearing) % 360.0
            if diff > 180.0:
                diff = 360.0 - diff

            if diff <= self._GPS_DIR_TOL_DEG:
                log.debug(
                    "GPS filter: suspect — %.0f km/h implied (max %.0f), "
                    "dir OK (%.0f° off) — holding one cycle",
                    implied_kmh, max_ok_kmh, diff,
                )
                self._gps_filt_suspect = (lat, lon, heading, speed_kmh, now)
            else:
                log.debug(
                    "GPS filter: discarding implausible jump — %.0f km/h implied "
                    "(max %.0f), dir mismatch (%.0f°)",
                    implied_kmh, max_ok_kmh, diff,
                )

        # Return last accepted position for this cycle.
        return (
            filt_lat,
            filt_lon,
            self._gps_filt_heading,
            self._gps_filt_speed_kmh,
        )

    def _gps_filter_accept(
        self,
        lat: float,
        lon: float,
        heading: float | None,
        speed_kmh: float | None,
        t: float,
    ) -> None:
        """Commit a GPS fix as the new last-valid reference."""
        self._gps_filt_lat = lat
        self._gps_filt_lon = lon
        self._gps_filt_heading = heading or 0.0
        self._gps_filt_speed_kmh = speed_kmh if speed_kmh is not None else self._gps_filt_speed_kmh
        self._gps_filt_time = t
