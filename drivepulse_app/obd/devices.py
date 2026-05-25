"""OBD adapter discovery helpers for DrivePulse."""
from __future__ import annotations

import subprocess
from pathlib import Path

from drivepulse_app.common import OBD_BT_ADDR, OBD_PORT, OBD_SOCKET_URL
from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)


def candidate_bt_addresses() -> list[tuple[str, int]]:
    """Parse OBD_BT_ADDR into (mac_address, rfcomm_channel) pairs."""
    if not OBD_BT_ADDR or OBD_PORT:
        return []
    result = []
    for raw in OBD_BT_ADDR.split(","):
        entry = raw.strip()
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
            result = subprocess.run(args, capture_output=True, text=True, timeout=5, check=False)
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


def scan_bt_nearby_devices(
    scan_seconds: int = 6,
    known_addrs: set[str] | None = None,
) -> list[tuple[str, str]]:
    """Run a short BT discovery scan; return unknown devices sorted by RSSI, max 10.

    known_addrs: uppercase MAC addresses to exclude (already-paired devices).
    """
    import time as _time
    rssi_map: dict[str, int] = {}
    scan_names: dict[str, str] = {}
    try:
        # Use bluetoothctl interactively via stdin so discovery actually runs.
        proc = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            assert proc.stdin is not None
            proc.stdin.write("scan on\n")
            proc.stdin.flush()
            _time.sleep(scan_seconds)
            proc.stdin.write("scan off\nquit\n")
            proc.stdin.flush()
            out, _ = proc.communicate(timeout=5)
        except Exception:
            proc.kill()
            out = ""
        for line in out.splitlines():
            if "Device" not in line:
                continue
            parts = line.split()
            try:
                dev_idx = parts.index("Device")
                addr = parts[dev_idx + 1].upper()
                if "RSSI:" in line:
                    # "[CHG] Device AA:BB:CC:DD:EE:FF RSSI: -65"
                    rssi_idx = next(i for i, p in enumerate(parts) if p == "RSSI:")
                    rssi_map[addr] = int(parts[rssi_idx + 1])
                if "[NEW]" in line and len(parts) > dev_idx + 2:
                    # "[NEW] Device AA:BB:CC:DD:EE:FF DeviceName"
                    scan_names[addr] = " ".join(parts[dev_idx + 2:])
            except (ValueError, IndexError, StopIteration):
                pass
    except (OSError, subprocess.SubprocessError):
        log.debug("bluetoothctl scan probe failed", exc_info=True)
    try:
        result = subprocess.run(
            ["bluetoothctl", "devices"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        known_db: dict[str, str] = {}
        for line in result.stdout.strip().splitlines():
            parts = line.split(" ", 2)
            if len(parts) >= 2 and parts[0] == "Device":
                addr = parts[1].upper()
                name = parts[2].strip() if len(parts) >= 3 else addr
                known_db[addr] = name
        # Include devices discovered during scan but not yet in bluetoothctl's cache.
        for addr, name in scan_names.items():
            if addr not in known_db:
                known_db[addr] = name
        devices: list[tuple[str, str, int]] = []
        for addr, name in known_db.items():
            if known_addrs and addr in known_addrs:
                continue
            devices.append((f"{name}  ({addr})", f"bt:{addr}", rssi_map.get(addr, -999)))
        devices.sort(key=lambda x: x[2], reverse=True)
        return [(label, port) for label, port, _ in devices[:10]]
    except Exception:
        return []


def probe_bt_rfcomm_socket(addr: str, channel: int = 1, timeout: float = 10.0) -> tuple[bool, str]:
    """Try to open a raw RFCOMM socket to addr:channel. Returns (success, error_msg).

    Used as a lightweight fallback when rfcomm bind is unavailable.
    On success the caller should set the port to 'bt:ADDR' so ObdReader uses
    its BluetoothPtyBridge path (_try_bt_direct) for the actual OBD session.
    """
    import socket as _socket
    last_err = "Keine Verbindung möglich"
    for ch in (channel, 2, 6) if channel == 1 else (channel,):
        try:
            sock = _socket.socket(_socket.AF_BLUETOOTH, _socket.SOCK_STREAM, _socket.BTPROTO_RFCOMM)
            sock.settimeout(timeout)
            sock.connect((addr, ch))
            sock.close()
            return True, ""
        except Exception as exc:
            last_err = str(exc)
    return False, last_err


def bind_bt_to_rfcomm(addr: str, channel: int = 1) -> tuple[str, str] | tuple[None, str]:
    """Try to bind a BT address to /dev/rfcommN via rfcomm(1).

    Returns (device_path, "") on success or (None, error_message) on failure.
    Tries without elevated privileges first, then via pkexec.
    """
    # Find a free rfcomm slot.
    slot: int | None = None
    for i in range(10):
        if not Path(f"/dev/rfcomm{i}").exists():
            slot = i
            break
    if slot is None:
        return None, "Kein freier rfcomm-Slot verfügbar (0-9 belegt)"

    dev = f"/dev/rfcomm{slot}"
    bind_cmd = ["rfcomm", "bind", str(slot), addr, str(channel)]

    # Release any stale binding first (ignore errors).
    try:
        subprocess.run(["rfcomm", "release", str(slot)],
                       capture_output=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        log.debug("Pre-release of rfcomm slot %s failed (ok if first bind)", slot, exc_info=True)

    # Try without sudo, then escalate via pkexec.
    for cmd in (bind_cmd, ["pkexec", *bind_cmd]):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
            if result.returncode == 0:
                return dev, ""
        except FileNotFoundError as exc:
            return None, f"rfcomm not found: {exc}"
        except subprocess.TimeoutExpired:
            return None, "Timeout while binding"
        except Exception as exc:
            return None, str(exc)

    return None, "rfcomm bind failed (returncode != 0)"


OBD_CANDIDATE_PATHS = [
    "/dev/rfcomm0",
    "/dev/rfcomm1",
    "/dev/ttyUSB0",
    "/dev/ttyUSB1",
    "/dev/ttyUSB2",
    "/dev/ttyACM0",
    "/dev/ttyACM1",
]


def scan_obd_devices() -> list[tuple[str, str, bool]]:
    """Return (display_label, port_value, is_present) for serial/USB OBD device paths.

    BT devices (bt: prefix) are excluded — they are managed via OBD_BT_ADDR env var.
    is_present=True means the device node currently exists on the system.
    """
    devices: list[tuple[str, str, bool]] = []
    seen_paths: set[str] = set()

    # /dev/serial/by-id/* — descriptive USB-serial names (only existing)
    for path in sorted(Path("/dev/serial/by-id").glob("*")) if Path("/dev/serial/by-id").exists() else []:
        real = str(path.resolve())
        label = f"{path.name} ({real})"
        devices.append((label, real, True))
        seen_paths.add(real)

    # Directly present wired / already-bound serial devices
    for pattern in ("/dev/rfcomm*", "/dev/ttyUSB*", "/dev/ttyACM*"):
        for path in sorted(Path("/").glob(pattern.lstrip("/"))):
            p = str(path)
            if p not in seen_paths:
                devices.append((p, p, True))
                seen_paths.add(p)

    if OBD_SOCKET_URL:
        devices.append((OBD_SOCKET_URL, OBD_SOCKET_URL, True))

    # Common candidate paths not yet present — let users pre-configure
    for candidate in OBD_CANDIDATE_PATHS:
        if candidate not in seen_paths:
            devices.append((f"{candidate} (not found)", candidate, False))
            seen_paths.add(candidate)

    return devices
