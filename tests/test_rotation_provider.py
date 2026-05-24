"""Tests for RotationProvider — the source-of-truth for screen rotation.

Two independent inputs (sensor accelerometer + system compositor) feed
into one effective angle whose computation depends on the current mode
("follow_sensor" or "follow_system"). Subscribers can lock to a specific
source or follow the mode. A regression here either double-rotates the
gauges or leaves them upside-down."""
from __future__ import annotations

import pytest

from drivepulse_app.rotation import RotationProvider, VALID_MODES


def test_initial_effective_is_zero():
    rp = RotationProvider()
    assert rp.get() == 0


def test_set_sensor_updates_effective_in_follow_sensor_mode():
    rp = RotationProvider(mode="follow_sensor")
    rp.set_sensor(90)
    assert rp.get() == 90
    rp.set_sensor(270)
    assert rp.get() == 270


def test_set_sensor_normalises_modulo_360():
    rp = RotationProvider()
    rp.set_sensor(450)
    assert rp.get() == 90
    rp.set_sensor(-90)
    assert rp.get() == 270


def test_set_sensor_noop_when_unchanged_does_not_fire_callback():
    rp = RotationProvider()
    calls: list[int] = []
    rp.bind(calls.append)
    # bind fires once immediately.
    initial_count = len(calls)
    rp.set_sensor(0)  # already 0 → no notification
    assert len(calls) == initial_count


def test_follow_sensor_subtracts_system_rotation():
    # Compositor rotated 90° → widget must counter-rotate to stay upright.
    rp = RotationProvider(mode="follow_sensor")
    rp.set_sensor(90)
    rp.set_system(90)
    # (90 - 90) % 360 = 0
    assert rp.get() == 0


def test_follow_sensor_wraps_negative_difference():
    rp = RotationProvider(mode="follow_sensor")
    rp.set_sensor(0)
    rp.set_system(90)
    # (0 - 90) % 360 = 270
    assert rp.get() == 270


def test_follow_system_mode_always_zero():
    # In follow_system mode the compositor already did the work — the
    # widget renders at angle 0 regardless of sensor reading.
    rp = RotationProvider(mode="follow_system")
    rp.set_sensor(180)
    rp.set_system(90)
    assert rp.get() == 0


def test_set_mode_switches_effective_calculation():
    rp = RotationProvider(mode="follow_sensor")
    rp.set_sensor(90)
    assert rp.get() == 90
    rp.set_mode("follow_system")
    assert rp.get() == 0


def test_set_mode_ignores_invalid_value():
    rp = RotationProvider(mode="follow_sensor")
    rp.set_mode("not-a-mode")
    assert rp.mode == "follow_sensor"


def test_set_mode_noop_when_unchanged():
    rp = RotationProvider(mode="follow_sensor")
    calls: list[int] = []
    rp.bind(calls.append)
    initial = len(calls)
    rp.set_mode("follow_sensor")  # same mode → no callback
    assert len(calls) == initial


def test_init_with_invalid_mode_falls_back_to_sensor():
    rp = RotationProvider(mode="weird-mode")  # type: ignore[arg-type]
    assert rp.mode == "follow_sensor"


def test_bind_fires_immediately_with_current_value():
    rp = RotationProvider()
    rp.set_sensor(180)
    calls: list[int] = []
    rp.bind(calls.append)
    assert calls == [180]


def test_bind_with_locked_source_ignores_mode_changes():
    rp = RotationProvider(mode="follow_sensor")
    rp.set_sensor(90)
    sensor_calls: list[int] = []
    rp.bind(sensor_calls.append, source="follow_sensor")
    # Switching mode notifies subscribers but the locked-source subscriber
    # always reports the follow_sensor effective value.
    rp.set_mode("follow_system")
    assert sensor_calls[-1] == 90  # still reports sensor-effective


def test_bind_following_mode_re_fires_on_mode_switch():
    rp = RotationProvider(mode="follow_sensor")
    rp.set_sensor(90)
    calls: list[int] = []
    rp.bind(calls.append)  # source=None → follows active mode
    rp.set_mode("follow_system")
    # On mode switch the subscriber should see 0 (follow_system always 0).
    assert calls[-1] == 0


def test_set_system_noop_when_unchanged():
    rp = RotationProvider()
    calls: list[int] = []
    rp.bind(calls.append)
    initial = len(calls)
    rp.set_system(0)
    assert len(calls) == initial


def test_subscriber_exception_does_not_break_other_subscribers():
    rp = RotationProvider()
    good_calls: list[int] = []

    def bad(_angle):
        raise RuntimeError("subscriber boom")

    rp.bind(bad)
    rp.bind(good_calls.append)
    # Triggering a change should call BOTH; the bad one must not stop
    # the good one from getting notified.
    rp.set_sensor(180)
    assert 180 in good_calls


def test_valid_modes_constant_locks_set():
    assert set(VALID_MODES) == {"follow_sensor", "follow_system"}
