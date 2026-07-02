"""OBD adapter discovery helpers for DrivePulse."""
from __future__ import annotations

import subprocess
import sys
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


def bt_is_reachable(addr: str, timeout: float = 3.0, *, strict: bool = False) -> bool:
    """Cheap pre-flight check: is the device at *addr* actually answering?

    Sends a single L2CAP echo (`l2ping`) which works without pairing — round
    trip is under 1 s for an in-range, powered dongle.

    Two modes controlled by *strict*:

    * ``strict=False`` (default, used by ``pair_bt_device``): conservative —
      only ``Host is down`` counts as a confirmed negative. Permission errors,
      missing binary, unknown BlueZ warnings all return ``True`` so a pair
      attempt still runs. Wasting 25 s of handshake is cheaper than never
      trying when the probe itself is broken.
    * ``strict=True`` (used by the nearby-scan UI): only a *positive* echo
      reply (``bytes from``) counts as reachable. Anything else — timeout,
      permission error, missing tool, ``Host is down`` — excludes the device
      from the list. Prevents ghost entries from BlueZ's known-device cache
      cluttering the Settings "OBD-Geräte suchen" list with dongles that
      were used once and are physically elsewhere now.
    """
    try:
        result = subprocess.run(
            ["l2ping", "-c", "1", "-t", str(max(1, int(timeout))), addr],
            capture_output=True, text=True, timeout=timeout + 1.0, check=False,
        )
    except FileNotFoundError:
        return not strict  # tool missing → conservative: True in loose mode, False in strict
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        return not strict
    out = (result.stdout + " " + result.stderr).lower()
    if "bytes from" in out:
        return True
    if strict:
        return False
    if "host is down" in out or "host unreachable" in out or "no route" in out:
        return False
    # Anything else (permission denied, "can't create socket", unfamiliar BlueZ
    # warning) — don't assume offline.
    return True


def scan_bt_known_devices() -> list[tuple[str, str]]:
    """Return (name, ADDR) for every device BlueZ currently knows about.

    Unlike ``scan_bt_paired_devices`` this also lists cached/discovered but
    not-yet-bonded devices — useful when the active inquiry on a binder/Phosh
    stack returns nothing because the system shell is already running its own
    discovery. The OS Bluetooth panel still populates the BlueZ device cache,
    and we can pick OBD adapters straight out of it.
    """
    try:
        result = subprocess.run(
            ["bluetoothctl", "devices"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    devices: list[tuple[str, str]] = []
    for line in result.stdout.strip().splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 2 and parts[0] == "Device":
            addr = parts[1].upper()
            name = parts[2].strip() if len(parts) >= 3 else addr
            devices.append((name, addr))
    return devices


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
            # Force BR/EDR (Bluetooth-Classic) discovery transport. On
            # binder/bluebinder phone stacks SetDiscoveryFilter.Transport
            # defaults to "le" — classic OBD dongles (HC-05/06 ELM clones,
            # OBDLink MX+ in legacy mode) then never surface in the inquiry
            # and the auto-pair pass walks away with count=0 even when the
            # dongle is plugged in and advertising. `bredr` covers OBD without
            # missing anything we care about; LE-only beacons aren't OBD.
            # `menu scan` → `transport bredr` → `back` is the BlueZ ≥5.50
            # syntax; older bluetoothctl ignores the unknown commands and
            # the scan then falls back to its compile-time default.
            proc.stdin.write("menu scan\ntransport bredr\nback\n")
            proc.stdin.flush()
            _time.sleep(0.3)
            # Belt-and-braces: top-level alias used by some BlueZ packagings.
            proc.stdin.write("set-scan-filter-transport bredr\n")
            proc.stdin.flush()
            _time.sleep(0.2)
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
        # Primary source: devices ACTUALLY DETECTED in this scan — RSSI or
        # freshly announced. A paired-but-absent dongle (e.g. an MX+ left in
        # another car) lives in bluetoothctl's cache with no RSSI; it'd be a
        # ghost in the "in range" list.
        # Secondary source: OBD-named devices in the BlueZ cache that *answer
        # an L2-ping*. The MX+/ELM only advertise for ~30 s after power-on,
        # but once they're in the cache and the host can still reach them
        # over L2CAP, they're functionally "in range" from the user's point
        # of view — exclude them and Settings shows an empty list even when
        # the dongle is plugged in and working.
        seen_now = set(rssi_map) | set(scan_names)
        # Probe-budget for the cache rescue: l2ping is ~1-3 s per address; an
        # untargeted sweep over every cached device (BLE beacons, headsets,
        # printers, …) would lock the UI. We only probe entries whose name
        # already looks like an OBD adapter.
        _CACHE_PROBE_BUDGET = 4
        cache_probed = 0
        for addr, name in known_db.items():
            if addr in seen_now or (known_addrs and addr in known_addrs):
                continue
            if cache_probed >= _CACHE_PROBE_BUDGET:
                break
            if not _looks_like_obd(name, addr):
                continue
            cache_probed += 1
            try:
                # ``strict=True`` — only *positive* l2ping echoes qualify. A
                # timeout / permission error does NOT rescue the entry. That
                # keeps ghost dongles (used once, physically elsewhere now)
                # out of the "OBD-Geräte suchen" list; the user only sees
                # what is truly in range.
                if bt_is_reachable(addr, timeout=2.0, strict=True):
                    seen_now.add(addr)
                    # Synthesise a faint RSSI so the entry sorts last among
                    # the actively-discovered devices but ahead of nothing.
                    rssi_map.setdefault(addr, -99)
            except Exception:
                log.debug("cache reachability probe failed for %s", addr, exc_info=True)
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


def _bluetoothctl_session(
    commands: list[str],
    timeout: float = 30.0,
    delays: list[float] | None = None,
) -> str:
    """Run *commands* through one interactive bluetoothctl session.

    *delays*, if given, sets a per-command sleep (seconds) — same length as
    *commands*. Commands that trigger a real BlueZ operation (``pair``,
    ``connect``, ``discoverable``) need a much longer pause than passive ones
    (``power on``, ``agent`` config) so BlueZ has time to talk to the remote
    radio and emit its response into stdout before we send the next line.
    Without per-step pacing the session quits before pairing actually starts.

    Returns combined stdout.
    """
    import time as _time
    if delays is not None and len(delays) != len(commands):
        raise ValueError("delays must match commands length")
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
        for i, cmd in enumerate(commands):
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()
            _time.sleep(delays[i] if delays is not None else 0.6)
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


def _pair_outcome_ok(out: str) -> bool:
    """True if a bluetoothctl pair session ended with a confirmed bond."""
    low = out.lower()
    return (
        "pairing successful" in low
        or "alreadyexists" in low
        or "paired: yes" in low
        or "connection successful" in low
    )


def _pair_outcome_error(out: str) -> str:
    """Pull the most informative error line out of a failed pair session.

    Falls back to a trimmed tail of the raw bluetoothctl output when no
    canonical error string appears — that tail almost always contains the
    actual reason (PinCodeRequest cancelled, AuthenticationRejected, "Device
    not available", BlueZ "Operation already in progress", …) and we want it
    in the log so the next iteration can target the real failure mode.
    """
    canonical = ("failed to pair", "authenticationfailed", "authentication failed",
                 "authenticationrejected", "connectionrejected",
                 "operation already in progress", "not available",
                 "no agent", "no agent available", "request cancelled",
                 "passkey", "passkey request", "confirmation request",
                 "request canceled", "pincoderequest")
    for line in out.splitlines():
        low = line.lower()
        if any(c in low for c in canonical):
            return line.strip()[:300]
    # Last resort: hand back the trailing 200 chars of the raw output, scrubbed
    # of empty lines so it stays readable in the JSON log.
    tail = "\n".join(l for l in out.splitlines() if l.strip())[-200:]
    return f"unbestätigt: …{tail}" if tail else "pairing not confirmed (no output)"


def _pair_attempt(
    addr: str,
    agent: str,
    replies: list[str],
    timeout: float,
    pair_wait: float = 8.0,
    reply_wait: float = 2.5,
) -> tuple[bool, str]:
    """One bluetoothctl pair session with the given agent and prompt replies."""
    cmds: list[str] = ["power on", f"agent {agent}", "default-agent", f"pair {addr}"]
    delays: list[float] = [0.5, 0.5, 0.5, pair_wait]
    for r in replies:
        cmds.append(r)
        delays.append(reply_wait)
    cmds.extend([f"trust {addr}", f"connect {addr}"])
    delays.extend([1.0, 5.0])
    out = _bluetoothctl_session(cmds, timeout=timeout, delays=delays)
    if _pair_outcome_ok(out):
        return True, out
    return False, out


def pair_bt_device(
    addr: str,
    pin: str = "1234",
    timeout: float = 45.0,
    pin_candidates: tuple[str, ...] | None = None,
) -> tuple[bool, str]:
    """Pair a Bluetooth device via an isolated BlueZ-agent subprocess.

    Spawns ``python3 -m drivepulse_app.obd._pair_agent <ADDR> [PIN ...]`` so
    the agent's GLib main loop runs in its own process — never tangles with
    the GTK main context. The helper registers a private ``Agent1`` object on
    the system bus, claims ``RequestDefaultAgent``, calls ``Device1.Pair()``
    and replies to every PIN/passkey/confirmation callback non-interactively.

    This sidesteps the system Bluetooth agent (Phosh's gnome-bluetooth, GNOME
    Shell, …) entirely. Those force Secure-Simple-Pairing Numeric Comparison
    and show a 6-digit "Confirm" dialog the cheap HC-05/06 ELM clones cannot
    satisfy — every OS-driven pair ends with ``AuthenticationFailed``. With
    our own agent at ``NoInputNoOutput`` BlueZ falls through to legacy PIN
    entry; we feed the cascade (1234 / 0000 / 6789 / 0123 / 1111 / 8888)
    silently. Returns ``(success, message)``. Already-paired devices count as
    success (the agent's Pair() returns AlreadyExists, mapped here to True).
    """
    addr = addr.upper()
    pins = pin_candidates or (pin, "0000", "6789", "0123", "1111", "8888")
    # No --capability: let the agent cascade through NoInputNoOutput →
    # DisplayYesNo → KeyboardOnly. The first that produces a persisted
    # ``Paired: yes`` bond wins. Covers the entire OBD adapter zoo without
    # any system config edits.
    cmd = [sys.executable, "-m", "drivepulse_app.obd._pair_agent", addr, *pins]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=timeout + 5.0, check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"pair agent subprocess timeout after {timeout}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"pair agent subprocess failed: {exc}"

    stdout = (result.stdout or "").strip()
    try:
        last_line = stdout.splitlines()[-1] if stdout else ""
        import json as _json
        payload = _json.loads(last_line)
        ok = bool(payload.get("ok"))
        msg = str(payload.get("msg") or ("paired" if ok else "no message"))
    except (ValueError, IndexError):
        # Helper crashed before printing the JSON tail — surface stderr for diag.
        err_tail = (result.stderr or "").strip().splitlines()[-1:]
        msg = err_tail[0] if err_tail else "no result from pair agent"
        ok = False

    # AlreadyExists from BlueZ means the device is already bonded — treat as ok.
    if not ok and "AlreadyExists" in msg:
        ok = True
        msg = "already paired"
    return ok, msg


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
    """Return (display_label, port_value, is_present) for every OBD-usable port.

    Lists three classes in priority order:

    1. **Paired Bluetooth dongles that advertise SPP** — brand-agnostic match
       via ``paired_obd_addresses`` (used by the reader's auto-connect path).
       These appear *first* so the settings dropdown surfaces what DrivePulse
       can already drive without any user action.
    2. **Wired serial/USB devices** under ``/dev/serial/by-id`` and the
       canonical ``rfcomm*/ttyUSB*/ttyACM*`` patterns.
    3. **OBD socket URL** from the environment and the common
       pre-configurable candidate paths (shown as "(not found)" until they
       actually appear).

    is_present=True means the port is reachable right now (BT bonded, serial
    node present). False is reserved for the pre-configurable candidate
    paths so the dropdown can still surface them as choices.
    """
    devices: list[tuple[str, str, bool]] = []
    seen_paths: set[str] = set()

    # 1. Paired BT OBD dongles — visible immediately after auto-pair so the
    # settings UI mirrors the reader's known device set.
    # MAC-only label by user preference: dongle marketing names ("OBDLink MX+
    # 02393", "OBDII", "iCar Pro") are noisy and inconsistent across TTS
    # engines anyway — the MAC alone identifies the device unambiguously and
    # keeps the dropdown narrow on a phone screen.
    try:
        for addr, _ch, _name in paired_obd_addresses():
            port = f"bt:{addr}"
            label = f"Bluetooth · {addr}"
            devices.append((label, port, True))
            seen_paths.add(port)
    except Exception:
        log.debug("paired_obd_addresses() failed in scan_obd_devices", exc_info=True)

    # 2a. /dev/serial/by-id/* — descriptive USB-serial names (only existing)
    for path in sorted(Path("/dev/serial/by-id").glob("*")) if Path("/dev/serial/by-id").exists() else []:
        real = str(path.resolve())
        label = f"{path.name} ({real})"
        devices.append((label, real, True))
        seen_paths.add(real)

    # 2b. Directly present wired / already-bound serial devices
    for pattern in ("/dev/rfcomm*", "/dev/ttyUSB*", "/dev/ttyACM*"):
        for path in sorted(Path("/").glob(pattern.lstrip("/"))):
            p = str(path)
            if p not in seen_paths:
                devices.append((p, p, True))
                seen_paths.add(p)

    # 3a. Socket bridge URL (e.g. socat to remote OBD-WiFi adapter)
    if OBD_SOCKET_URL:
        devices.append((OBD_SOCKET_URL, OBD_SOCKET_URL, True))

    # 3b. Common candidate paths not yet present — let users pre-configure
    for candidate in OBD_CANDIDATE_PATHS:
        if candidate not in seen_paths:
            devices.append((f"{candidate} (not found)", candidate, False))
            seen_paths.add(candidate)

    return devices
