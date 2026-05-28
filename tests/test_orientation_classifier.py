"""Tests for the accelerometer → screen-orientation classifier.

``OrientationReader._on_accel`` is the heart of the auto-rotate feature:
it inspects the raw accelerometer reading (in milli-g) and emits one of
the four screen orientations the GTK side knows how to rotate to. Bugs
here flip the dashboard upside down or refuse to rotate at all — both
painful to reproduce in production.

We bypass the constructor's ``GLib.idle_add(self._start)`` path by
passing ``enabled=False``, and substitute a list-capturing replacement
for ``_emit`` so the test sees the raw classifier output without the
GLib debounce window.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def reader_with_capture():
    """Build an OrientationReader that records every classifier output."""
    from drivepulse_app.sensors.orientation import OrientationReader

    r = OrientationReader(lambda *_a: None, enabled=False)
    captured: list[str] = []
    r._emit = captured.append
    return r, captured


# ── Single-axis dominant readings → expected orientation ──────────────────────


def test_on_accel_y_positive_is_normal(reader_with_capture):
    r, captured = reader_with_capture
    # Phone upright in portrait → y points down through gravity → +y.
    r._on_accel(0, 1000, 0)
    assert captured == ["normal"]


def test_on_accel_y_negative_is_bottom_up(reader_with_capture):
    r, captured = reader_with_capture
    # Phone upside-down in portrait → -y dominant.
    r._on_accel(0, -1000, 0)
    assert captured == ["bottom-up"]


def test_on_accel_x_positive_is_left_up(reader_with_capture):
    r, captured = reader_with_capture
    # Phone rotated 90° clockwise → +x dominant.
    r._on_accel(1000, 0, 0)
    assert captured == ["left-up"]


def test_on_accel_x_negative_is_right_up(reader_with_capture):
    r, captured = reader_with_capture
    # Phone rotated 90° counter-clockwise → -x dominant.
    r._on_accel(-1000, 0, 0)
    assert captured == ["right-up"]


# ── Threshold + dominance rules ───────────────────────────────────────────────


def test_on_accel_below_threshold_keeps_current_orientation(reader_with_capture):
    r, captured = reader_with_capture
    # |ax| and |ay| both under 600 mg — device is lying flat. The classifier
    # must skip the emit so the last known orientation persists; otherwise
    # the dashboard would oscillate when the user puts the phone on a table.
    r._on_accel(500, 500, 800)
    assert captured == []


def test_on_accel_axis_tie_resolves_to_y(reader_with_capture):
    r, captured = reader_with_capture
    # Equal |ax| and |ay| with the y-test condition `ay >= ax` favours the
    # y-axis branch — verifies the tie-breaker stays on y.
    r._on_accel(700, 700, 0)
    assert captured == ["normal"]


def test_on_accel_y_dominates_with_small_x(reader_with_capture):
    r, captured = reader_with_capture
    # Both axes above threshold but |ay| > |ax| → y branch wins.
    r._on_accel(650, 950, 0)
    assert captured == ["normal"]


def test_on_accel_x_dominates_with_small_y(reader_with_capture):
    r, captured = reader_with_capture
    # Both above threshold but |ax| > |ay| → x branch wins.
    r._on_accel(950, 650, 0)
    assert captured == ["left-up"]


# ── Orientation → angle/landscape lookup table ────────────────────────────────


def test_map_table_covers_all_four_orientations():
    from drivepulse_app.sensors.orientation import OrientationReader

    # _MAP is consulted by _emit_stable to translate the string orientation
    # into (rotation_degrees, is_landscape).  Locking these four pairs down
    # so a refactor can't silently change what "right-up" rotates to.
    assert OrientationReader._MAP == {
        "normal":    (0,   False),
        "right-up":  (270, True),
        "bottom-up": (180, False),
        "left-up":   (90,  True),
    }
