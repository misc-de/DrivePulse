"""Unit tests for MapGpsFilterMixin._gps_filter.

The kinematic filter is the only part of the map pipeline that can silently
drop or rewrite GPS samples — broken behaviour here would show up as missing
points or stuck position during a tour. The filter only reads its own
``_gps_filt_*`` / ``_obd_*`` state and the class-level thresholds, so we can
test it with a minimal stand-in instead of a full MapPage."""
from __future__ import annotations

from drivepulse_app.map.gps_filter import MapGpsFilterMixin


class _Filter(MapGpsFilterMixin):
    """Minimal owner that satisfies the attributes _gps_filter touches."""

    def __init__(self) -> None:
        self._gps_filt_lat = None
        self._gps_filt_lon = None
        self._gps_filt_heading: float = 0.0
        self._gps_filt_speed_kmh: float = 0.0
        self._gps_filt_time: float = 0.0
        self._gps_filt_suspect = None
        self._obd_speed_kmh: float | None = None
        self._obd_speed_time: float = 0.0


# Roughly 11.1 m at the equator — handy step size for crafting plausible jumps.
_DEG_STEP_11M = 0.0001


def test_first_fix_is_accepted_unconditionally_and_initializes_state():
    f = _Filter()

    out = f._gps_filter(50.0, 7.0, heading=90.0, speed_kmh=42.0, now=100.0)

    assert out == (50.0, 7.0, 90.0, 42.0)
    assert f._gps_filt_lat == 50.0
    assert f._gps_filt_lon == 7.0
    assert f._gps_filt_heading == 90.0
    assert f._gps_filt_speed_kmh == 42.0
    assert f._gps_filt_time == 100.0


def test_plausible_step_is_accepted():
    # Moving 11.1 m in 1 s ≈ 40 km/h — well below the kinematic ceiling at rest
    # ((0 + 36) * 1.2 = 43.2 km/h), so the second fix must be accepted.
    f = _Filter()
    f._gps_filter(50.0, 7.0, heading=0.0, speed_kmh=0.0, now=100.0)

    lat2 = 50.0 + _DEG_STEP_11M
    out = f._gps_filter(lat2, 7.0, heading=0.0, speed_kmh=40.0, now=101.0)

    assert out[0] == lat2
    assert f._gps_filt_lat == lat2
    assert f._gps_filt_suspect is None


def test_stale_gap_disables_filter_and_accepts():
    # After _GPS_MAX_STALE_S (10s) with no fix, the filter must give up and
    # accept the next sample regardless of how implausible the jump looks —
    # this is the tunnel-exit / GPS-recovery escape hatch.
    f = _Filter()
    f._gps_filter(50.0, 7.0, heading=0.0, speed_kmh=0.0, now=100.0)

    huge_jump_lat = 50.5  # ~55 km north — physically impossible in 11 s
    out = f._gps_filter(huge_jump_lat, 7.0, heading=0.0, speed_kmh=0.0, now=111.0)

    assert out[0] == huge_jump_lat
    assert f._gps_filt_lat == huge_jump_lat


def test_implausible_jump_with_consistent_direction_is_held_as_suspect():
    # 10× the step (~111 m in 1 s = ~400 km/h) is implausible, but the move
    # bearing is north (≈ 0°) and the current heading is also 0°, so the
    # filter must hold the new point as a suspect and return the last valid.
    f = _Filter()
    f._gps_filter(50.0, 7.0, heading=0.0, speed_kmh=0.0, now=100.0)

    jump_lat = 50.0 + 10 * _DEG_STEP_11M
    out = f._gps_filter(jump_lat, 7.0, heading=0.0, speed_kmh=400.0, now=101.0)

    # Returned position is the previous valid fix, not the jump.
    assert out[0] == 50.0
    assert out[1] == 7.0
    # A suspect was stored for retroactive validation on the next cycle.
    assert f._gps_filt_suspect is not None
    s_lat, s_lon, _, _, _ = f._gps_filt_suspect
    assert s_lat == jump_lat
    assert s_lon == 7.0


def test_implausible_jump_with_wrong_direction_is_discarded_silently():
    # Same magnitude jump as above but the move bearing (north, ~0°) is 90°
    # away from the current heading (east, 90°) — that's outside the 45°
    # tolerance, so the jump must be dropped without becoming a suspect.
    f = _Filter()
    f._gps_filter(50.0, 7.0, heading=90.0, speed_kmh=0.0, now=100.0)

    jump_lat = 50.0 + 10 * _DEG_STEP_11M  # moves north, heading says east
    out = f._gps_filter(jump_lat, 7.0, heading=90.0, speed_kmh=400.0, now=101.0)

    assert out[0] == 50.0
    assert f._gps_filt_suspect is None


def test_suspect_corroborated_by_next_fix_is_accepted_retroactively():
    # Sequence: rest at A → big jump to B (held as suspect) → consistent
    # continuation to C. The suspect must be promoted to "accepted" and C
    # becomes the new valid position.
    f = _Filter()
    f._gps_filter(50.0, 7.0, heading=0.0, speed_kmh=0.0, now=100.0)

    b_lat = 50.0 + 10 * _DEG_STEP_11M  # ~111 m north — suspect
    f._gps_filter(b_lat, 7.0, heading=0.0, speed_kmh=400.0, now=101.0)
    assert f._gps_filt_suspect is not None

    # C: another ~11 m north over 1 s from the *suspect* — that implies
    # ~40 km/h from B, which is plausible given the high suspect speed.
    c_lat = b_lat + _DEG_STEP_11M
    out = f._gps_filter(c_lat, 7.0, heading=0.0, speed_kmh=400.0, now=102.0)

    assert out[0] == c_lat
    assert f._gps_filt_lat == c_lat
    assert f._gps_filt_suspect is None


def test_suspect_not_corroborated_drops_both_points():
    # Sequence: rest at A → big jump to B (suspect) → another wild jump to D
    # that's not a plausible continuation of B. Both must be discarded, and
    # the last accepted position (A) is returned.
    f = _Filter()
    f._gps_filter(50.0, 7.0, heading=0.0, speed_kmh=0.0, now=100.0)

    b_lat = 50.0 + 10 * _DEG_STEP_11M
    f._gps_filter(b_lat, 7.0, heading=0.0, speed_kmh=400.0, now=101.0)
    assert f._gps_filt_suspect is not None

    # D: another huge jump in 1 s — even from B this is way too fast.
    d_lat = b_lat + 20 * _DEG_STEP_11M
    out = f._gps_filter(d_lat, 7.0, heading=0.0, speed_kmh=900.0, now=102.0)

    assert out[0] == 50.0
    assert out[1] == 7.0
    assert f._gps_filt_lat == 50.0
    assert f._gps_filt_suspect is None


def test_obd_speed_contradiction_holds_position():
    # Fresh OBD says we are stopped, but GPS reports 80 km/h — the position
    # fix must be held at the last valid value, regardless of kinematic
    # plausibility (this catches phones where the GPS chip overshoots).
    f = _Filter()
    f._gps_filter(50.0, 7.0, heading=0.0, speed_kmh=0.0, now=100.0)

    f.update_obd_speed(0.0)  # OBD says vehicle is stationary
    # Subtle jump (kinematically plausible) but with a GPS speed that
    # contradicts OBD by more than _OBD_GPS_SPEED_DIFF_KMH (30 km/h).
    lat2 = 50.0 + _DEG_STEP_11M
    out = f._gps_filter(lat2, 7.0, heading=0.0, speed_kmh=80.0, now=100.5)

    assert out[0] == 50.0
    assert out[1] == 7.0
    # Last valid fix is *not* advanced.
    assert f._gps_filt_lat == 50.0


def test_obd_speed_stale_is_ignored():
    # Same scenario but OBD reading is older than _OBD_SPEED_STALE_S (5 s),
    # so it must not influence the decision; the plausible jump is accepted.
    f = _Filter()
    f._gps_filter(50.0, 7.0, heading=0.0, speed_kmh=0.0, now=100.0)

    f.update_obd_speed(0.0)
    f._obd_speed_time = 50.0  # ancient — > 5 s before "now"

    lat2 = 50.0 + _DEG_STEP_11M
    out = f._gps_filter(lat2, 7.0, heading=0.0, speed_kmh=80.0, now=101.0)

    assert out[0] == lat2
    assert f._gps_filt_lat == lat2


def test_zero_dt_skips_filter_and_accepts():
    # When two fixes share a timestamp dt becomes 0, which would divide by
    # zero in the implied-speed calculation. The guard at the top of the
    # filter must short-circuit to "accept" instead of raising.
    f = _Filter()
    f._gps_filter(50.0, 7.0, heading=0.0, speed_kmh=0.0, now=100.0)

    lat2 = 50.0 + 5 * _DEG_STEP_11M
    out = f._gps_filter(lat2, 7.0, heading=0.0, speed_kmh=0.0, now=100.0)

    assert out[0] == lat2
    assert f._gps_filt_lat == lat2
