"""OBD adapter discovery helpers for DrivePulse."""
from __future__ import annotations

import subprocess
from pathlib import Path

from drivepulse_app.common import OBD_BT_ADDR, OBD_PORT, OBD_SOCKET_URL
from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)

# Substrings (case-insensitive) that mark a Bluetooth device name as a likely
# OBD-II adapter. The nearby scan otherwise surfaces every BLE advertisement in
# range (headphones, keyboards, unnamed beacons) — none of which can be a
# dongle. Extend this list if a differently-branded adapter is missed.
_OBD_NAME_HINTS = (
    "obd",        # OBDII, OBD2, OBD-II, OBDLink, …
    "elm327",
    "vgate",      # Vgate iCar
    "viecar",
    "vlinker",    # vLinker FD/MC/MS
    "vlink",      # V-LINK / vLink
    "scantool",
    "konnwei",
    "carista",
    "panlong",
)


def _looks_like_obd(name: str, addr: str) -> bool:
    """True if *name* looks like an OBD-II adapter rather than BLE noise.

    Unnamed devices (where bluetoothctl echoes the MAC as the name) and names
    without any known OBD token are rejected — they cannot be dongles.
    """
    n = name.strip().lower()
    if not n or n in (addr.lower(), addr.replace(":", "-").lower()):
        return False
    return any(hint in n for hint in _OBD_NAME_HINTS)


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


# Standard 16-bit UUIDs in their 128-bit form. Pre-expanded so the substring
# match against bluetoothctl's output is locale-independent.
_SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"

# UUIDs that rule a device OUT as an OBD dongle, even if it also exposes SPP.
# Modern TWS earbuds (e.g. Anker Soundcore) advertise SPP for firmware-update
# channels — without this exclusion they would be tried as OBD candidates and
# burn ~8 s per connect cycle on the Host-is-down timeout.
_NON_OBD_UUIDS = (
    "0000110a",  # A2DP Source
    "0000110b",  # A2DP Sink
    "0000110c",  # AVRCP Target
    "0000110e",  # AVRCP Controller
    "0000111e",  # Handsfree
    "00001108",  # Headset
    "00001112",  # Headset Audio Gateway
    "00001124",  # HID
    "00001812",  # HID over GATT
    "0000110d",  # Advanced Audio Distribution
)


def _has_spp_uuid(addr: str) -> bool:
    """True if the paired device at *addr* looks like an OBD/serial adapter.

    Requires the Serial Port Profile and rejects anything that also advertises
    audio (A2DP/HFP/HSP) or input (HID) profiles. OBD dongles only ever expose
    SPP (sometimes plus OBEX), so the exclusion list is unambiguous.
    """
    try:
        result = subprocess.run(
            ["bluetoothctl", "info", addr],
            capture_output=True, text=True, timeout=4, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    out = result.stdout.lower()
    if _SPP_UUID not in out:
        return False
    return not any(u in out for u in _NON_OBD_UUIDS)


def paired_obd_addresses() -> list[tuple[str, int, str]]:
    """Return (addr, channel=1, name) for paired BT devices that advertise SPP.

    Lets the reader auto-try every plausible OBD dongle the system already knows
    about — so a user who pairs an MX+ in one car and an ELM clone in another
    doesn't need to re-configure the OBD port every time they switch vehicles.
    Using SDP/SPP (not the device name) makes detection brand-agnostic.
    """
    result: list[tuple[str, int, str]] = []
    for label, port_url in scan_bt_paired_devices():
        addr = port_url[3:].upper()
        name = label[4:].rsplit(" (", 1)[0] if label.startswith("BT: ") else addr
        if _has_spp_uuid(addr):
            result.append((addr, 1, name))
    return result


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
            # Power the adapter on first. A nearby scan against a powered-off
            # controller silently surfaces only cached/paired devices that then
            # fail to connect ("Bluetooth off") — exactly the confusing state a
            # disabled radio produces. Mirrors pair_bt_device; harmless if on.
            proc.stdin.write("power on\n")
            proc.stdin.flush()
            _time.sleep(0.5)
            # Force a dual-mode (BR/EDR + LE) discovery filter. Several
            # binder/bluebinder stacks default to LE-only discovery, so
            # Bluetooth-Classic OBD dongles (HC-05/06 ELM clones, OBDLink MX+)
            # never surface in the inquiry. `menu scan` → `transport auto` → `back`
            # sets SetDiscoveryFilter.Transport; an older bluetoothctl simply
            # ignores the unknown commands, so this stays safe.
            proc.stdin.write("menu scan\ntransport auto\nback\n")
            proc.stdin.flush()
            _time.sleep(0.3)
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
                if "Name:" in parts:
                    # Classic adapters often resolve their friendly name only
                    # later via "[CHG] Device AA:BB:… Name: OBDLink MX+ 02393" —
                    # capture it so the OBD name filter can match.
                    name_idx = parts.index("Name:")
                    scan_names[addr] = " ".join(parts[name_idx + 1:])
            except (ValueError, IndexError, StopIteration):
                pass
    except (OSError, subprocess.SubprocessError):
        log.debug("bluetoothctl scan probe failed", exc_info=True)
    # Raw inquiry result for live debugging: every address the scan surfaced,
    # with whatever name/RSSI we resolved — including ones the OBD filter later
    # drops. Invaluable when a dongle "doesn't show up" (Classic vs LE, etc.).
    if rssi_map or scan_names:
        log.info(
            "bt scan raw (%d): %s",
            len(set(rssi_map) | set(scan_names)),
            [{"addr": a, "name": scan_names.get(a, ""), "rssi": rssi_map.get(a)}
             for a in sorted(set(rssi_map) | set(scan_names))],
        )
    else:
        log.info("bt scan raw: nothing discovered")
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
        # Only surface devices ACTUALLY DETECTED in this scan — i.e. they sent an
        # RSSI or were freshly announced. A paired-but-absent dongle (e.g. an MX+
        # left in another car) lives in bluetoothctl's devices cache with no RSSI;
        # it is "known", not "nearby", and must not be listed as in range.
        seen_now = set(rssi_map) | set(scan_names)
        matched: list[tuple[str, str, int]] = []
        in_range_other: list[tuple[str, str, int]] = []
        for addr, name in known_db.items():
            if addr not in seen_now:
                continue
            if known_addrs and addr in known_addrs:
                continue
            rssi = rssi_map.get(addr, -999)
            if _looks_like_obd(name, addr):
                matched.append((f"{name}  ({addr})", f"bt:{addr}", rssi))
            elif (not name) or name.lower() in (addr.lower(), addr.replace(":", "-").lower()):
                # Unnamed in-range device: a just-plugged ELM clone often advertises
                # only its MAC until paired, so keep these as candidates. Named
                # non-OBD devices (headphones, keyboards, beacons) are skipped — they
                # cannot be the dongle and would just be noise in this list.
                in_range_other.append((f"BT {addr}  ({addr})", f"bt:{addr}", rssi))
        # In-range OBD dongles first, then other in-range devices, by signal.
        matched.sort(key=lambda x: x[2], reverse=True)
        in_range_other.sort(key=lambda x: x[2], reverse=True)
        combined = matched + in_range_other
        return [(label, port) for label, port, _ in combined[:10]]
    except Exception:
        return []


def _bluetoothctl_session(commands: list[str], timeout: float = 30.0) -> str:
    """Run *commands* through one interactive bluetoothctl session. Returns combined output."""
    import time as _time
    try:
        proc = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    assert proc.stdin is not None
    try:
        for cmd in commands:
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()
            _time.sleep(0.6)
        proc.stdin.write("quit\n")
        proc.stdin.flush()
        out, _ = proc.communicate(timeout=timeout)
        return out
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, _ = proc.communicate()
            return out
        except (OSError, subprocess.SubprocessError, ValueError):
            return ""
    except Exception:
        proc.kill()
        return ""


def pair_bt_device(
    addr: str,
    pin: str = "1234",
    timeout: float = 35.0,
    pin_candidates: tuple[str, ...] | None = None,
) -> tuple[bool, str]:
    """Pair, trust and connect a Bluetooth device via bluetoothctl.

    ELM327 BT clones (HC-05/HC-06 modules) speak Bluetooth 2.0 legacy pairing
    only — they reject Just-Works SSP and need a numeric PIN. So:

    1. ``remove`` first to wipe any stale half-bonded state (a common cause of
       BlueZ ``AuthenticationFailed`` on the next attempt — see bluez#605).
    2. ``agent KeyboardOnly`` so this Python side supplies the PIN; BlueZ won't
       hand the prompt over to the OS agent (Phosh/GNOME) and won't try SSP.
    3. ``pair`` and feed PIN candidates in succession (1234 is the factory
       default for HC-05/06; 0000 / 6789 are common alternates).
    4. ``trust`` + explicit ``connect`` — without ``trust`` BlueZ re-prompts on
       every reconnect, and some clones won't open the RFCOMM channel until a
       ``connect`` lands.

    Returns ``(success, message)``. Already-paired devices count as success.
    """
    addr = addr.upper()
    pins = pin_candidates or (pin, "0000", "6789", "0123")

    # Step 1: clear stale state. Idempotent — ignored if device isn't known.
    _bluetoothctl_session([f"remove {addr}"], timeout=8.0)

    # Step 2: pair + PIN-cascade in a fresh session.
    cmds: list[str] = [
        "power on",
        "agent KeyboardOnly",
        "default-agent",
        f"pair {addr}",
    ]
    # Feed all candidate PINs; bluetoothctl ignores extras once paired.
    for p in pins:
        cmds.append(p)
    # Trust + explicit connect for legacy adapters that need the bond verified.
    cmds.extend([f"trust {addr}", f"connect {addr}"])
    out = _bluetoothctl_session(cmds, timeout=timeout)

    low = out.lower()
    if (
        "pairing successful" in low
        or "alreadyexists" in low
        or "paired: yes" in low
        or "connection successful" in low
    ):
        # Best-effort: leave it disconnected so the OBD reader can open the
        # RFCOMM socket itself (BlueZ won't lend the SPP channel while it
        # holds an active LE/HFP link). Failure here is harmless.
        _bluetoothctl_session([f"disconnect {addr}"], timeout=6.0)
        return True, "paired"
    for line in out.splitlines():
        if "failed to pair" in line.lower() or "authenticationfailed" in line.lower():
            return False, line.strip()
    return False, "pairing not confirmed"


def unpair_bt_device(addr: str) -> None:
    """Remove (unpair) a Bluetooth device. Best-effort.

    Used to undo a *pair-probe*: the auto-pair fallback bonds an unknown in-range
    device just long enough to read its SDP profiles, then calls this to discard
    it again when it turns out not to be a serial/OBD adapter — so no random
    earbuds or phones are left bonded behind the user's back.
    """
    _bluetoothctl_session([f"remove {addr.upper()}"], timeout=8.0)


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
