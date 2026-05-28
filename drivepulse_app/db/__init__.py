"""SQLite store for vehicles, trips and telemetry samples.

Schema (see ``_schema.py``):
- ``cars``     : one row per vehicle (unique by VIN, fallback by profile path)
- ``trips``    : a single drive, attached to a car
- ``samples``  : ~1–2 Hz telemetry point (OBD + GPS merged)

Optimized for:
- Very fast sample appends (WAL journal, INSERT OR IGNORE, PK ``(trip_id, ts)``).
- Listing all trips of a car (index ``car_id, started_at DESC``).
- Replaying a trip with GPS track (sample order implicit via PK).

The full ``DriveDB`` API is split into one mixin per concern (cars, trips,
samples, scans, stopwatch, photos, sync, tours) plus a base that owns the
connection. They are composed below into a single ``DriveDB`` class.
"""
from __future__ import annotations

from drivepulse_app.db._base import _DriveDBBase
from drivepulse_app.db._cars import CarsMixin
from drivepulse_app.db._discoveries import DiscoveriesMixin
from drivepulse_app.db._photos import PhotosMixin
from drivepulse_app.db._samples import SamplesMixin
from drivepulse_app.db._scans import ScansMixin
from drivepulse_app.db._schema import (
    _SCHEMA_VERSION,
    _is_duplicate_column_error,
)
from drivepulse_app.db._stopwatch import StopwatchMixin
from drivepulse_app.db._sync import SyncMixin
from drivepulse_app.db._tours import ToursMixin
from drivepulse_app.db._trips import TripsMixin


class DriveDB(
    CarsMixin,
    TripsMixin,
    SamplesMixin,
    ScansMixin,
    StopwatchMixin,
    PhotosMixin,
    SyncMixin,
    ToursMixin,
    DiscoveriesMixin,
    _DriveDBBase,
):
    """Slim wrapper around ``sqlite3``."""


__all__ = ["_SCHEMA_VERSION", "DriveDB", "_is_duplicate_column_error"]
