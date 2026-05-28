"""Snapshot diffing for the read-only coding "function finder".

The workflow: read a control module's data identifiers repeatedly to learn which
bytes drift on their own (live values, counters — the "noise"), then have the
user change one thing in the car and diff a fresh read against the stable
baseline. The bytes that changed — minus the noisy ones — point at the DID / byte
/ bit that backs the toggled function.

A *snapshot* is ``{did: value_bytes}`` (the value part of a ReadDataByIdentifier
response, i.e. what :func:`drivepulse_app.obd.uds.did_payload` returns). Pure
functions only — no I/O — so the whole reverse-engineering core is unit-testable
without a vehicle.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

Snapshot = dict[int, bytes]


def volatile_bytes(samples: Sequence[Snapshot]) -> set[tuple[int, int]]:
    """Return ``(did, byte_index)`` pairs that varied across baseline *samples*.

    These are the self-changing bytes (sensor values, counters, timestamps) that
    must be ignored when looking for a user-triggered change. A DID whose length
    differs between samples has all its observed byte positions marked volatile,
    since the layout itself is unstable.
    """
    if len(samples) < 2:
        return set()

    volatile: set[tuple[int, int]] = set()
    dids = set().union(*(s.keys() for s in samples))
    for did in dids:
        values = [s.get(did) for s in samples]
        if any(v is None for v in values):
            # DID not present in every sample → treat every seen byte as unstable.
            for v in values:
                if v is not None:
                    volatile.update((did, i) for i in range(len(v)))
            continue
        present = [v for v in values if v is not None]
        lengths = {len(v) for v in present}
        if len(lengths) > 1:
            for v in present:
                volatile.update((did, i) for i in range(len(v)))
            continue
        for i in range(next(iter(lengths))):
            if len({v[i] for v in present}) > 1:
                volatile.add((did, i))
    return volatile


def bit_changes(before: int, after: int) -> list[int]:
    """Return the bit masks (single-bit ints) that flipped between two bytes."""
    delta = before ^ after
    return [1 << bit for bit in range(8) if delta & (1 << bit)]


@dataclass(frozen=True)
class ByteChange:
    """One changed byte between two snapshots of the same module."""

    did: int
    byte_index: int
    before: int
    after: int
    bits: list[int] = field(default_factory=list)

    @property
    def bit_mask(self) -> int:
        """Combined mask of all flipped bits in this byte."""
        return self.before ^ self.after

    def describe(self) -> str:
        bits = ", ".join(f"bit{(m).bit_length() - 1}" for m in self.bits)
        return (
            f"DID {self.did:04X} byte {self.byte_index}: "
            f"{self.before:02X} → {self.after:02X} ({bits})"
        )


def diff_snapshots(
    before: Snapshot,
    after: Snapshot,
    volatile: set[tuple[int, int]] | None = None,
) -> list[ByteChange]:
    """Return the byte-level changes from *before* to *after*, skipping noise.

    Only DIDs present in both snapshots are compared, byte by byte up to the
    shorter length. ``(did, byte_index)`` pairs in *volatile* are ignored.
    Results are ordered by DID then byte index for stable display.
    """
    ignore = volatile or set()
    changes: list[ByteChange] = []
    for did in sorted(before.keys() & after.keys()):
        b, a = before[did], after[did]
        for i in range(min(len(b), len(a))):
            if (did, i) in ignore or b[i] == a[i]:
                continue
            changes.append(
                ByteChange(did=did, byte_index=i, before=b[i], after=a[i], bits=bit_changes(b[i], a[i]))
            )
    return changes
