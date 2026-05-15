"""OBD adapter discovery helpers for DrivePulse."""
from __future__ import annotations

import subprocess
from pathlib import Path

from common import OBD_BT_ADDR, OBD_PORT, OBD_SOCKET_URL


def candidate_bt_addresses() -> list[tuple[str, int]]:
    """Parse OBD_BT_ADDR into (mac_address, rfcomm_channel) pairs."""
    if not OBD_BT_ADDR or OBD_PORT:
        return []
    result = []
    for entry in OBD_BT_ADDR.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) == 7 and parts[6].isdigit():
            result.append((":".join(parts[:6]).upper(), int(parts[6])))
        else:
            result.append((entry.upper(), 1))
    return result


def parse_bt_port(port: str) -> tuple[str, int]:
    """Parse 'bt:AA:BB:CC:DD:EE:FF' or 'bt:AA:BB:CC:DD:EE:FF:channel' into (addr, channel)."""
    raw = port[3:]  # strip 'bt:'
    parts = raw.split(":")
    if len(parts) == 7 and parts[6].isdigit():
        return ":".join(parts[:6]).upper(), int(parts[6])
    return raw.upper(), 1


def scan_bt_paired_devices() -> list[tuple[str, str]]:
    """Return (label, bt:ADDR) for all paired Bluetooth devices via bluetoothctl."""
    try:
        # Try modern syntax first, fall back to legacy
        for args in (["bluetoothctl", "devices", "Paired"], ["bluetoothctl", "paired-devices"]):
            result = subprocess.run(args, capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                break
        devices: list[tuple[str, str]] = []
        for line in result.stdout.strip().splitlines():
            parts = line.split(" ", 2)
            if len(parts) >= 2 and parts[0] == "Device":
                addr = parts[1].upper()
                name = parts[2].strip() if len(parts) >= 3 else addr
                devices.append((f"BT: {name} ({addr})", f"bt:{addr}"))
        return devices
    except Exception:
        return []


OBD_CANDIDATE_PATHS = [
    "/dev/rfcomm0",
    "/dev/rfcomm1",
    "/dev/ttyUSB0",
    "/dev/ttyUSB1",
    "/dev/ttyUSB2",
    "/dev/ttyACM0",
    "/dev/ttyACM1",
]


def scan_obd_devices() -> list[tuple[str, str]]:
    """Return (display_label, port_value) pairs of detectable and common OBD device paths.

    Existing devices come first (with their real path or descriptive by-id name).
    Common candidate paths that are not currently present are appended with a
    '(not found)' suffix so users can pre-configure a port before connecting.
    """
    devices: list[tuple[str, str]] = []
    seen_paths: set[str] = set()

    # /dev/serial/by-id/* — descriptive USB-serial names (only existing)
    for path in sorted(Path("/dev/serial/by-id").glob("*")) if Path("/dev/serial/by-id").exists() else []:
        real = str(path.resolve())
        label = f"{path.name} ({real})"
        devices.append((label, real))
        seen_paths.add(real)

    # Directly present wired / already-bound serial devices
    for pattern in ("/dev/rfcomm*", "/dev/ttyUSB*", "/dev/ttyACM*"):
        for path in sorted(Path("/").glob(pattern.lstrip("/"))):
            p = str(path)
            if p not in seen_paths:
                devices.append((p, p))
                seen_paths.add(p)

    # Paired Bluetooth devices (direct RFCOMM socket, no rfcomm bind needed)
    seen_bt: set[str] = set()
    for label, val in scan_bt_paired_devices():
        devices.append((label, val))
        seen_bt.add(val)

    # Manually configured BT addresses from env (if not already listed)
    for addr, channel in candidate_bt_addresses():
        val = f"bt:{addr}" if channel == 1 else f"bt:{addr}:{channel}"
        if val not in seen_bt:
            devices.append((f"BT: {addr}", val))

    if OBD_SOCKET_URL:
        devices.append((OBD_SOCKET_URL, OBD_SOCKET_URL))

    # Common candidate paths not yet present — let users pre-configure
    for candidate in OBD_CANDIDATE_PATHS:
        if candidate not in seen_paths:
            devices.append((f"{candidate} (not found)", candidate))
            seen_paths.add(candidate)

    return devices
