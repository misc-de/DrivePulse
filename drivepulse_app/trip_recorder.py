"""Trip recording state machine for DrivePulse."""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from drivepulse_app.db import DriveDB

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)

# GPS-Ausreißer-Filter: Punkte, die eine höhere implizite Geschwindigkeit als
# diesen Schwellwert ergeben, werden verworfen. 250 km/h liegt deutlich über
# jeder realistischen Fahrgeschwindigkeit und toleriert trotzdem GPS-Rauschen.
_MAX_GPS_SPEED_MS = 250.0 / 3.6
# Nach einer GPS-Pause länger als diese Zeit ist der letzte Standort veraltet —
# erster neuer Punkt wird bedingungslos akzeptiert (z. B. nach Tunnel).
_GPS_GAP_RESET_S = 30.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 6_371_000.0 * 2 * math.asin(math.sqrt(min(a, 1.0)))


class TripRecorder:
    """Hält den aktuellen Fahrt-Kontext und merged OBD-/GPS-Payloads in DB-Rows."""

    IDLE_TIMEOUT_S = 120.0

    def __init__(self, db: DriveDB) -> None:
        self.db = db
        self.car_id: int | None = None
        self.trip_id: int | None = None
        self._last_gps: dict[str, float] = {}
        self._last_obd_ts: float = 0.0
        self._last_gps_ts: float = 0.0

    # Identitäts-Update — typischerweise nach erfolgreichem Scan
    def set_car(
        self,
        vin: str | None = None,
        brand: str | None = None,
        cal_id: str | None = None,
        cvn: str | None = None,
        label: str | None = None,
        protocol: str | None = None,
        profile_path: str | None = None,
    ) -> int:
        # Wechsel des Autos beendet eine laufende Fahrt
        new_id = self.db.upsert_car(
            vin=vin, brand=brand, cal_id=cal_id, cvn=cvn,
            label=label, protocol=protocol, profile_path=profile_path,
        )
        if self.car_id is not None and self.car_id != new_id and self.trip_id is not None:
            self.end_trip()
        self.car_id = new_id
        return new_id

    # GPS-Cache, damit OBD-Samples die letzten Koordinaten mitführen
    def update_gps(self, *, lat: float | None = None, lon: float | None = None,
                   altitude_m: float | None = None, heading_deg: float | None = None,
                   gps_speed_kmh: float | None = None) -> None:
        now = time.monotonic()
        if lat is not None and lon is not None:
            prev_lat = self._last_gps.get("lat")
            prev_lon = self._last_gps.get("lon")
            if prev_lat is not None and prev_lon is not None:
                dt = now - self._last_gps_ts
                if 0 < dt < _GPS_GAP_RESET_S:
                    dist_m = _haversine_m(prev_lat, prev_lon, lat, lon)
                    if dist_m / dt > _MAX_GPS_SPEED_MS:
                        log.debug(
                            "GPS outlier rejected: %.0f m in %.1f s (%.0f km/h implied)",
                            dist_m, dt, dist_m / dt * 3.6,
                        )
                        return
            self._last_gps_ts = now
        if lat is not None:
            self._last_gps["lat"] = lat
        if lon is not None:
            self._last_gps["lon"] = lon
        if altitude_m is not None:
            self._last_gps["altitude_m"] = altitude_m
        if heading_deg is not None:
            self._last_gps["heading_deg"] = heading_deg
        if gps_speed_kmh is not None:
            self._last_gps["gps_speed_kmh"] = gps_speed_kmh

    def record_obd(self, ts: float, **fields: Any) -> None:
        """Schreibt ein OBD-Sample (inklusive zuletzt gesehener GPS-Daten)."""
        if self.car_id is None:
            # Fahrzeugidentität noch nicht bekannt — Sample verwerfen
            return
        if self.trip_id is None:
            self.trip_id = self.db.start_trip(self.car_id)
        merged = dict(self._last_gps)
        merged.update({k: v for k, v in fields.items() if v is not None})
        self.db.add_sample(self.trip_id, ts, **merged)
        self._last_obd_ts = ts

    def maybe_end_idle_trip(self, now: float) -> bool:
        if self.trip_id is None or not self._last_obd_ts:
            return False
        if now - self._last_obd_ts > self.IDLE_TIMEOUT_S:
            self.end_trip()
            return True
        return False

    def end_trip(self) -> None:
        if self.trip_id is None:
            return
        try:
            self.db.end_trip(self.trip_id)
        finally:
            self.trip_id = None
            self._last_obd_ts = 0.0
