"""Tests for the coding "function finder" diff core (drivepulse_app.obd.coding_diff).

These verify the reverse-engineering heart without hardware: detecting the noisy
(self-changing) bytes from a baseline, and isolating a user-triggered byte/bit
change against that baseline."""
from __future__ import annotations

from drivepulse_app.obd.coding_diff import (
    ByteChange,
    bit_changes,
    diff_snapshots,
    volatile_bytes,
)


# --- volatile_bytes ---------------------------------------------------------

def test_volatile_bytes_flags_self_changing_positions():
    samples = [
        {0x0600: bytes([0x01, 0x10, 0xAA])},
        {0x0600: bytes([0x01, 0x11, 0xAA])},  # byte 1 drifts
        {0x0600: bytes([0x01, 0x12, 0xAA])},
    ]
    assert volatile_bytes(samples) == {(0x0600, 1)}


def test_volatile_bytes_empty_for_fully_stable_baseline():
    samples = [{0x0600: bytes([1, 2, 3])}] * 3
    assert volatile_bytes(samples) == set()


def test_volatile_bytes_needs_at_least_two_samples():
    assert volatile_bytes([{0x0600: b"\x01\x02"}]) == set()


def test_volatile_bytes_marks_all_positions_when_length_differs():
    samples = [
        {0x0600: bytes([1, 2])},
        {0x0600: bytes([1, 2, 3])},  # layout unstable
    ]
    assert volatile_bytes(samples) == {(0x0600, 0), (0x0600, 1), (0x0600, 2)}


def test_volatile_bytes_marks_did_missing_in_some_samples():
    samples = [
        {0x0600: bytes([1, 2])},
        {},  # DID vanished this read
    ]
    assert volatile_bytes(samples) == {(0x0600, 0), (0x0600, 1)}


# --- bit_changes ------------------------------------------------------------

def test_bit_changes_lists_flipped_bit_masks():
    # 0x00 → 0x09 flips bit0 (0x01) and bit3 (0x08)
    assert bit_changes(0x00, 0x09) == [0x01, 0x08]


def test_bit_changes_empty_when_equal():
    assert bit_changes(0xAB, 0xAB) == []


# --- diff_snapshots ---------------------------------------------------------

def test_diff_isolates_user_change_ignoring_volatile():
    volatile = {(0x0600, 1)}  # byte 1 known noisy
    before = {0x0600: bytes([0x01, 0x55, 0x00])}
    after = {0x0600: bytes([0x01, 0x99, 0x08])}  # byte1 noise + byte2 real change
    changes = diff_snapshots(before, after, volatile)
    assert len(changes) == 1
    c = changes[0]
    assert (c.did, c.byte_index, c.before, c.after) == (0x0600, 2, 0x00, 0x08)
    assert c.bits == [0x08]
    assert c.bit_mask == 0x08


def test_diff_empty_when_only_volatile_bytes_changed():
    before = {0x0600: bytes([0x01, 0x10])}
    after = {0x0600: bytes([0x01, 0x20])}
    assert diff_snapshots(before, after, {(0x0600, 1)}) == []


def test_diff_compares_only_common_dids():
    before = {0x0600: b"\x01", 0xF190: b"\xAA"}
    after = {0x0600: b"\x02", 0x1234: b"\xBB"}  # F190 gone, 1234 new
    changes = diff_snapshots(before, after)
    assert [c.did for c in changes] == [0x0600]


def test_diff_handles_differing_lengths_up_to_shorter():
    before = {0x0600: bytes([0x01, 0x02, 0x03])}
    after = {0x0600: bytes([0x01, 0x09])}  # shorter; compare first 2 bytes
    changes = diff_snapshots(before, after)
    assert [(c.byte_index, c.after) for c in changes] == [(1, 0x09)]


def test_byte_change_describe_is_human_readable():
    c = ByteChange(did=0x0600, byte_index=2, before=0x00, after=0x08, bits=[0x08])
    text = c.describe()
    assert "0600" in text and "byte 2" in text and "bit3" in text
