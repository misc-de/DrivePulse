"""Generic OBD-II PID definitions (SAE J1979 Mode 01).

Loads the standard PID set shared by all OBD-II compliant vehicles. Stays
intentionally generic — manufacturer-specific UDS PIDs are out of scope here.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

_VEHICLES_DIR = Path(__file__).parent
_STANDARD_PIDS_FILE = _VEHICLES_DIR / "standard_pids.toml"
_CODING_FILES = {"audi_a6_4g": _VEHICLES_DIR / "audi_a6_4g_coding.toml"}


@dataclass(frozen=True)
class PidDefinition:
    """One pollable standard OBD-II PID."""

    key: str
    pid: str
    category: str
    name: str
    unit: str = ""
    formula: str = ""
    min: float | None = None
    max: float | None = None


def load_standard_pids() -> dict[str, PidDefinition]:
    """Return the SAE J1979 Mode 01 baseline PIDs keyed by short name."""
    if not _STANDARD_PIDS_FILE.exists():
        return {}
    with _STANDARD_PIDS_FILE.open("rb") as fh:
        data = tomllib.load(fh)
    out: dict[str, PidDefinition] = {}
    for entry in data.get("pid", []):
        key = entry["key"]
        out[key] = PidDefinition(
            key=key,
            pid=entry["pid"],
            category=entry.get("category", "other"),
            name=entry.get("name", key),
            unit=entry.get("unit", ""),
            formula=entry.get("formula", ""),
            min=entry.get("min"),
            max=entry.get("max"),
        )
    return out


def pids_by_category(pids: dict[str, PidDefinition]) -> dict[str, list[PidDefinition]]:
    """Group a PID set by category for UI display."""
    grouped: dict[str, list[PidDefinition]] = {}
    for pid in pids.values():
        grouped.setdefault(pid.category, []).append(pid)
    return grouped


@dataclass(frozen=True)
class CodingFunction:
    """One community-documented coding/adaptation function for a vehicle.

    Read-only reference only. ``location`` is the typical byte/bit or adaptation
    channel where documented; when ``verify`` is set it must be confirmed on the
    specific car (software versions move things) via the snapshot diff finder.
    """

    key: str
    module: str
    category: str
    name: str
    type: str
    note: str = ""
    location: str = ""
    verify: bool = True


def load_coding_functions(vehicle: str = "audi_a6_4g") -> dict[str, CodingFunction]:
    """Return the known coding functions for *vehicle*, keyed by short name."""
    path = _CODING_FILES.get(vehicle)
    if path is None or not path.exists():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    out: dict[str, CodingFunction] = {}
    for entry in data.get("coding", []):
        key = entry["key"]
        out[key] = CodingFunction(
            key=key,
            module=entry["module"],
            category=entry.get("category", "other"),
            name=entry.get("name", key),
            type=entry.get("type", "long_coding"),
            note=entry.get("note", ""),
            location=entry.get("location", ""),
            verify=entry.get("verify", True),
        )
    return out
