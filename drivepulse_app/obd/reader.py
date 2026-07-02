"""Background OBD reader, connection management and mock fallback."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject

try:
    import obd
except Exception:
    obd = None

from drivepulse_app.common import (
    CONNECTION_LOG_FILE,
    LOG_DIR,
    LOG_FILE,
    OBD_BAUDRATE,
    OBD_FAST,
    OBD_PORT,
    OBD_SOCKET_URL,
    OBD_TIMEOUT_SECONDS,
    POLL_INTERVAL_SECONDS,
)
from drivepulse_app.diagnostics import append_jsonl, get_logger
from drivepulse_app.obd.adapter import AdapterInfo, _serial_port, probe_adapter, raw_send
from drivepulse_app.obd.devices import (
    _has_spp_uuid,
    _looks_like_obd,
    bt_is_reachable,
    candidate_bt_addresses,
    pair_bt_device,
    paired_obd_addresses,
    parse_bt_port,
    scan_bt_known_devices,
    scan_bt_nearby_devices,
    scan_bt_paired_devices,
    unpair_bt_device,
)
from drivepulse_app.obd.mock import MockObdSimulator, MockUdsSimulator
from drivepulse_app.obd.polling import (
    OBD_COMMAND_ATTRS,
    command_map,
    response_to_plain_value,
    should_query_key,
)
from drivepulse_app.obd.scanner import ObdScanner
from drivepulse_app.sensors.bluetooth import BluetoothPtyBridge

try:
    from drivepulse_app.obd import native as _native
except Exception:
    _native = None  # type: ignore[assignment]

# Backend selection: prefer python-OBD when installed (richer PID/protocol
# coverage), otherwise fall back to the GPL-free native ELM327 driver. python-OBD
# is an optional dependency that is deliberately never bundled, keeping the
# PolyForm Noncommercial license clear of GPL copyleft. See CREDITS.md.
obd_backend = obd if obd is not None else _native

log = get_logger(__name__)


def _extract_speed_kmh(value: Any) -> float | None:
    """Pull a km/h scalar out of the normalized OBD payload value.

    ``response_to_plain_value`` returns either ``{"value": float, "unit": str}``
    or a stringified value. Vehicle speed PIDs report km/h on every adapter we
    care about, so a unit conversion isn't needed — we just need the number.
    """
    if isinstance(value, dict):
        v = value.get("value")
        if isinstance(v, int | float):
            return float(v)
    return None


class ObdReader(GObject.Object):
    """Liest OBD-II-Werte in einem Hintergrund-Thread."""

    __gtype_name__ = "ObdReader"

    # Minimum OBD() timeout for direct BT connections (ELM327 init can be slow over BT)
    _BT_OBD_TIMEOUT = 15.0
    # Periodic re-scan keeps the scan history (DTCs, PIDs) fresh while connected.
    _RESCAN_INTERVAL_S = float(os.environ.get("OBD_RESCAN_INTERVAL", "90"))
    # How often to probe for a real dongle while in mock fallback. Lower = faster
    # pickup when the car is started, at the cost of more failed connect attempts.
    _MOCK_RECONNECT_INTERVAL_S = float(os.environ.get("OBD_MOCK_RECONNECT_INTERVAL", "3"))
    # Idle backoff: when the vehicle has been below _IDLE_MOTION_KMH for at
    # least _IDLE_HOLD_S, raise the minimum polling interval for all PIDs to
    # _IDLE_MIN_INTERVAL_S. Fast PIDs (rpm/speed/coolant) normally hit every
    # 500 ms tick — at a standstill that's 2 Hz of Bluetooth traffic for
    # values that aren't changing. The backoff drops it to ~0.5 Hz instead.
    _IDLE_MOTION_KMH = 3.0
    _IDLE_HOLD_S = 10.0
    _IDLE_MIN_INTERVAL_S = float(os.environ.get("OBD_IDLE_MIN_INTERVAL", "2.0"))

    def __init__(self, on_update: Callable[[dict[str, Any]], None], force_mock: bool = False) -> None:
        super().__init__()
        self.on_update = on_update
        self.force_mock = force_mock
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.connection: Any = None
        self.connected_port: str | None = None
        self.failed_read_count = 0
        self.next_mock_reconnect_attempt = 0.0
        self._bt_bridge: BluetoothPtyBridge | None = None
        self._configured_port: str | None = None
        self._force_reconnect = False
        # One shot per reader life: if we don't find any paired OBD dongle, do
        # an inquiry scan and auto-pair OBD-named devices in range. Keeps the
        # cost bounded (a ~6 s scan + a ~10–30 s pair) to first connect only —
        # subsequent connects use the already-bonded device immediately.
        self._auto_pair_attempted = False
        self._scanned_identities: set[str] = set()
        self._last_scan_monotonic: float = 0.0
        self._adapter_info: AdapterInfo | None = None
        # Set while a read-only UDS diagnostic session (Car Lab) owns the bus.
        # The live poll loop and vehicle scan pause so module-addressed UDS
        # traffic isn't interleaved with the 7DF functional broadcast.
        self._diagnostic_active = False
        # Serializes access to self.connection between the reader thread and the
        # asynchronous vehicle-scan thread so they can interleave queries safely.
        self._obd_lock = threading.Lock()
        self._scan_thread: threading.Thread | None = None
        self._obd_value_cache: dict[str, Any] = {}
        self._obd_last_query: dict[str, float] = {}
        # Tracks the last monotonic time the vehicle was observed in motion
        # (speed >= _IDLE_MOTION_KMH). When the vehicle has been parked for
        # longer than _IDLE_HOLD_S, fast-PID polling backs off from "every
        # tick" to _IDLE_MIN_INTERVAL_S — quietens the BT radio while parked.
        self._last_motion_monotonic: float = time.monotonic()
        self._obd_log_enabled: bool = True
        self._mock_simulator = MockObdSimulator()
        # Simulated UDS module for the Car Lab when no real adapter is present.
        self._mock_uds = MockUdsSimulator()
        if obd_backend is None:
            self.mock_reason = "kein OBD-Backend verfügbar"
        elif force_mock:
            self.mock_reason = "Manually enabled"
        else:
            self.mock_reason = ""
        self.mock = obd_backend is None or force_mock

    def set_obd_log_enabled(self, enabled: bool) -> None:
        self._obd_log_enabled = enabled

    def _connection_log(self, event: str, **fields: Any) -> None:
        """Schreibt jeden Verbindungsversuch sofort in ein separates Debug-Log."""
        if not self._obd_log_enabled or self.mock:
            return
        try:
            payload = {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": event,
                "obd_port": self._configured_port or OBD_PORT,
                "obd_baudrate": OBD_BAUDRATE,
                "obd_timeout": OBD_TIMEOUT_SECONDS,
                "obd_fast": OBD_FAST,
                "python_obd_available": obd is not None,
                **fields,
            }
            append_jsonl(CONNECTION_LOG_FILE, payload)
        except Exception:
            log.exception("Could not write OBD connection log event=%s", event)

    def _announce(self, text: str, *, speak: bool = True, voice_text: str | None = None) -> None:
        """Surface a connect/pair milestone to the UI and (optionally) TTS.

        Driving means the user can't watch JSON logs. This pushes a German
        human-readable status string into the dashboard's existing
        ``connection_status`` field (rendered on the OBD indicator) and, when
        TTS is enabled in settings, speaks it through Piper. *voice_text* lets
        the spoken sentence differ from the on-screen banner where the screen
        version benefits from being terser.

        Repeated identical announcements are suppressed to avoid spamming the
        TTS queue when the reader retries every few seconds.
        """
        if text == getattr(self, "_last_announce_text", None):
            speak = False
        else:
            self._last_announce_text = text
        try:
            GLib.idle_add(
                self.on_update,
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "source": "status",
                    "connection_status": text,
                    "obd_port": self.connected_port,
                },
            )
        except Exception:
            log.debug("UI status push failed for %r", text, exc_info=True)
        if not speak:
            return
        try:
            from drivepulse_app.app_settings import load_settings
            from drivepulse_app.tts import service as _tts
            s = load_settings()
            if not s.get("tts_enabled"):
                return
            lang = s.get("tts_language") or "auto"
            if lang == "auto":
                lang = s.get("language") or "de"
            voice = s.get("tts_voice") or "female"
            quality = s.get("tts_quality") or "medium"
            _tts.speak(voice_text or text, lang, gender=voice, quality=quality)
        except Exception:
            log.debug("TTS announce failed for %r", text, exc_info=True)

    def start(self) -> None:
        self._connection_log("reader_start")
        self._prerender_announce_cache()
        self.thread = threading.Thread(target=self._run, name="obd-reader", daemon=True)
        self.thread.start()

    def _prerender_announce_cache(self) -> None:
        """Warm the Piper cache with the one spoken connect phrase.

        Piper synthesizes ~1-2 s per phrase on first call — long enough that
        a freshly-started reader would speak "Verbunden" several seconds
        after the actual connect. Prerendering at start lets the eventual
        ``speak()`` skip piper and just play the cached PCM (~50 ms total).
        Background-threaded inside ``prerender`` itself; never blocks startup.
        All other OBD status messages (pairing, disconnect, no-dongle) are
        surfaced on the banner only (``speak=False``); "Verbunden" is the
        sole audible OBD announcement.
        """
        try:
            from drivepulse_app.app_settings import load_settings
            from drivepulse_app.tts import service as _tts
        except Exception:
            log.debug("TTS prerender: import failed", exc_info=True)
            return
        try:
            s = load_settings()
            if not s.get("tts_enabled"):
                return
            lang = s.get("tts_language") or "auto"
            if lang == "auto":
                lang = s.get("language") or "de"
            gender = s.get("tts_voice") or "female"
            quality = s.get("tts_quality") or "medium"
            for phrase in (
                "Verbunden.",
            ):
                _tts.prerender(phrase, lang, gender=gender, quality=quality)
        except Exception:
            log.debug("TTS prerender: failed to prime cache", exc_info=True)

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.5)
        if self.connection:
            self._close_connection()

    def _candidate_ports(self) -> list[str | None]:
        if OBD_PORT:
            return [OBD_PORT]

        candidates: list[str | None] = []
        for pattern in ("/dev/rfcomm*", "/dev/ttyUSB*", "/dev/ttyACM*", "/dev/serial/by-id/*"):
            candidates.extend(str(path) for path in sorted(Path("/").glob(pattern.lstrip("/"))))
        if OBD_SOCKET_URL:
            candidates.append(OBD_SOCKET_URL)
        return [*candidates, None]

    def _ensure_bt_powered(self) -> None:
        """Best-effort power-on of the local Bluetooth adapter before a bt: connect.

        On binder-based phones (FuriOS/bluebinder) the controller does not stay
        powered when nothing is connected — it silently drops to ``Powered: no``
        after idle, and a connect against an off adapter then fails for no
        obvious reason. ``bluetoothctl power on`` is a fast no-op when already on;
        any failure is logged but never blocks the connect attempt.
        """
        try:
            result = subprocess.run(
                ["bluetoothctl", "power", "on"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            ok = result.returncode == 0 and "fail" not in result.stdout.lower()
            self._connection_log("bt_power_on", ok=ok, out=result.stdout.strip()[-120:])
        except FileNotFoundError:
            self._connection_log("bt_power_on_not_found")
        except subprocess.TimeoutExpired:
            self._connection_log("bt_power_on_timeout")
        except Exception as exc:
            self._connection_log("bt_power_on_error", error=str(exc))

    def _try_bt_direct(self, addr: str, channel: int) -> bool:
        """Try direct Bluetooth RFCOMM socket without rfcomm bind. Returns True on success."""
        self._connection_log("bt_direct_attempt", bt_addr=addr, channel=channel)
        bridge: BluetoothPtyBridge | None = None
        try:
            bridge = BluetoothPtyBridge(addr, channel)
            connect_kwargs: dict[str, Any] = {
                "fast": False,
                "timeout": max(OBD_TIMEOUT_SECONDS, self._BT_OBD_TIMEOUT),
                "baudrate": OBD_BAUDRATE if OBD_BAUDRATE is not None else 38400,
            }
            self._connection_log("connect_attempt", port=bridge.pty_path, bt_addr=addr, **connect_kwargs)
            self.connection = obd_backend.OBD(bridge.pty_path, **connect_kwargs)
            connected = bool(self.connection and self.connection.is_connected())
            # Live-check: python-obd reports "connected" the moment its PTY init
            # finishes — even when the remote RFCOMM channel is half-dead and
            # the dongle never actually answered ATZ. Forcing a real round-trip
            # (an empty AT command) catches that case: a dead BT link returns
            # an empty response and we treat the attempt as failed, no false
            # ``connect_success`` event in the log.
            if connected:
                try:
                    iface = getattr(self.connection, "interface", None)
                    port_obj = _serial_port(self.connection)
                    probe = raw_send(port_obj, "ATI", timeout=2.0) if port_obj is not None else ""
                    if not probe.strip():
                        connected = False
                        self._connection_log("connect_live_check_failed", port=bridge.pty_path, bt_addr=addr)
                except Exception as exc:
                    connected = False
                    self._connection_log("connect_live_check_error", bt_addr=addr, error=str(exc))
            self._connection_log("connect_result", port=bridge.pty_path, bt_addr=addr, connected=connected)
            if connected:
                self._bt_bridge = bridge
                self.mock = False
                self.mock_reason = ""
                self.connected_port = f"bt:{addr}"
                self.failed_read_count = 0
                supported = sorted(str(c) for c in getattr(self.connection, "supported_commands", set()))
                self._connection_log("connect_success", port=self.connected_port, supported_commands=supported)
                self._announce("Verbunden.")
                self._probe_adapter()
                return True
            self._close_connection()
            bridge.close()
            return False
        except Exception as exc:
            self._connection_log("bt_direct_exception", bt_addr=addr, channel=channel, error=repr(exc), error_type=type(exc).__name__)
            self._close_connection()
            if bridge is not None:
                bridge.close()
            return False

    def _close_connection(self) -> None:
        try:
            if self.connection:
                self.connection.close()
        except Exception as exc:
            self._connection_log("connect_close_error", port=self.connected_port, error=str(exc))
        finally:
            self.connection = None
            self.connected_port = None
            self._adapter_info = None
            self._obd_value_cache.clear()
            self._obd_last_query.clear()
        if self._bt_bridge is not None:
            self._bt_bridge.close()
            self._bt_bridge = None

    def _send_raw_locked(self, cmd: str, timeout: float = 1.5) -> str:
        """Send a raw AT/ST command while holding the shared OBD serial lock.

        *timeout* widens the read window for slow multi-frame answers (e.g. the
        7-frame Mode 09 IUMPR response over the Bluetooth pty bridge).
        """
        port = _serial_port(self.connection)
        if port is None:
            return ""
        with self._obd_lock:
            return raw_send(port, cmd, timeout=timeout)

    def _resync_obd_locked(self) -> None:
        """Drain the serial line and re-assert python-obd's connect-time format.

        After a raw STPX batch a slow multi-frame response can keep arriving
        over the Bluetooth pty bridge past raw_send's read window; python-obd
        then reads those stale frames as answers to its own queries and returns
        NO DATA for everything (observed on scan 208: full scan, 0 live values,
        even PID 0100 null). Drain until the line is quiet, then restore the
        exact init python-obd applies on connect — ATE0/ATH1/ATL0, see
        python-obd elm327.py — so subsequent queries parse again.
        """
        port = _serial_port(self.connection)
        if port is None:
            return
        with self._obd_lock:
            try:
                prev_timeout = getattr(port, "timeout", None)
                try:
                    port.timeout = 0.1
                except Exception:
                    pass
                quiet = 0
                deadline = time.monotonic() + 2.0
                # Three consecutive empty reads (~0.3 s) = line idle; 2 s cap so
                # a chatty adapter can never wedge the scan here.
                while time.monotonic() < deadline and quiet < 3:
                    chunk = port.read(getattr(port, "in_waiting", 0) or 1)
                    quiet = 0 if chunk else quiet + 1
                try:
                    port.timeout = prev_timeout
                except Exception:
                    pass
            except Exception:
                log.debug("post-STPX drain failed", exc_info=True)
            for cmd in ("ATE0", "ATH1", "ATL0"):
                raw_send(port, cmd)

    def _probe_adapter(self) -> None:
        """Detect adapter type after a successful connection and cache the result."""
        if self.connection is None or self.mock:
            return
        self._adapter_info = probe_adapter(self.connection, locked_raw=self._send_raw_locked)

    def set_force_mock(self, force_mock: bool) -> None:
        self.force_mock = force_mock
        if force_mock:
            self.mock = True
            self.mock_reason = "Manually enabled"
        else:
            self._force_reconnect = True
            if obd_backend is None:
                self.mock_reason = "kein OBD-Backend verfügbar"
            else:
                self.mock_reason = ""

    def set_configured_port(self, port: str | None) -> None:
        self._configured_port = port
        self._force_reconnect = True

    def _rfcomm_bind(self, addr: str, channel: int) -> str | None:
        """Bind a Bluetooth address to an rfcomm device node. Returns device path or None."""
        # Find a free rfcomm slot (0-9)
        slot = 0
        for i in range(10):
            if not Path(f"/dev/rfcomm{i}").exists():
                slot = i
                break
        dev = f"/dev/rfcomm{slot}"
        release_cmd = ["rfcomm", "release", str(slot)]
        bind_cmd = ["rfcomm", "bind", str(slot), addr, str(channel)]
        self._connection_log("rfcomm_bind_attempt", addr=addr, channel=channel, dev=dev)
        # Release any stale binding first (ignore errors)
        for prefix in ([], ["pkexec"]):
            try:
                subprocess.run([*prefix, *release_cmd], capture_output=True, timeout=5, check=False)
            except Exception as exc:
                self._connection_log("rfcomm_release_error", addr=addr, error=str(exc))
            break
        # Try bind without sudo, then with pkexec (GUI password dialog)
        for cmd in (bind_cmd, ["pkexec", *bind_cmd]):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
                if result.returncode == 0:
                    self._connection_log("rfcomm_bind_ok", addr=addr, dev=dev, cmd=cmd[0])
                    return dev
                self._connection_log(
                    "rfcomm_bind_failed",
                    addr=addr, dev=dev, cmd=cmd[0],
                    returncode=result.returncode,
                    stderr=result.stderr.strip()[-200:],
                )
            except FileNotFoundError:
                self._connection_log("rfcomm_bind_not_found", addr=addr)
                return None
            except subprocess.TimeoutExpired:
                self._connection_log("rfcomm_bind_timeout", addr=addr)
                return None
            except Exception as exc:
                self._connection_log("rfcomm_bind_error", addr=addr, error=str(exc))
        return None

    def _bt_candidates(self) -> list[tuple[str, int, str]]:
        """Bluetooth dongles to try, in priority order: (addr, channel, source).

        The user may keep several OBD adapters paired (one per car). We try the
        configured port first (user's preferred dongle), then OBD_BT_ADDR env
        entries, then every paired BT device whose name matches a known OBD
        brand — so switching cars no longer requires re-configuring the port.
        Duplicates are dropped.
        """
        # Diagnostic: dump every paired device the system reports, before SPP
        # filtering. Makes it visible in the log whether a "just-paired" dongle
        # is actually known to BlueZ (paired-but-not-trusted is a common limbo).
        try:
            paired_dump = [
                {"addr": port_url[3:].upper(),
                 "name": label[4:].rsplit(" (", 1)[0] if label.startswith("BT: ") else label}
                for label, port_url in scan_bt_paired_devices()
            ]
            self._connection_log("bt_paired_seen", count=len(paired_dump), devices=paired_dump)
        except Exception as exc:
            self._connection_log("bt_paired_seen_error", error=str(exc))

        seen: set[str] = set()
        result: list[tuple[str, int, str]] = []
        if self._configured_port and self._configured_port.startswith("bt:"):
            addr, ch = parse_bt_port(self._configured_port)
            seen.add(addr)
            result.append((addr, ch, "configured"))
        for addr, ch in candidate_bt_addresses():
            if addr not in seen:
                seen.add(addr)
                result.append((addr, ch, "env"))
        for addr, ch, name in paired_obd_addresses():
            if addr not in seen:
                seen.add(addr)
                result.append((addr, ch, f"paired:{name}"))
        return result

    def _try_bt(self, addr: str, channel: int) -> bool:
        """Try one BT address: direct RFCOMM socket first, rfcomm bind as fallback."""
        if self._try_bt_direct(addr, channel):
            return True
        self._connection_log("bt_direct_failed_trying_rfcomm", bt_addr=addr)
        dev = self._rfcomm_bind(addr, channel)
        if not dev:
            return False
        try:
            connect_kwargs: dict[str, Any] = {
                "fast": False,
                "timeout": max(OBD_TIMEOUT_SECONDS, self._BT_OBD_TIMEOUT),
                "baudrate": OBD_BAUDRATE if OBD_BAUDRATE is not None else 38400,
            }
            self._connection_log("connect_attempt", port=dev, bt_addr=addr, **connect_kwargs)
            self.connection = obd_backend.OBD(dev, **connect_kwargs)
            connected = bool(self.connection and self.connection.is_connected())
            self._connection_log("connect_result", port=dev, bt_addr=addr, connected=connected,
                                 status=str(getattr(self.connection, "status", lambda: "unknown")()))
            if connected:
                self.mock = False
                self.mock_reason = ""
                self.connected_port = dev
                self.failed_read_count = 0
                supported = sorted(str(c) for c in getattr(self.connection, "supported_commands", set()))
                self._connection_log("connect_success", port=dev, supported_commands=supported)
                self._announce("Verbunden.")
                self._probe_adapter()
                return True
            self._close_connection()
            return False
        except Exception as exc:
            self._close_connection()
            self._connection_log("connect_exception", port=dev, error=repr(exc))
            return False

    def _auto_pair_nearby_obd(self) -> int:
        """Inquiry-scan and auto-pair any OBD-named device in range.

        Used as a fallback when no paired OBD dongle answered: a brand-new ELM
        clone or freshly powered MX+ shows up in nearby discovery but isn't
        bonded yet, so RFCOMM connects bounce with "Host is down". pair_bt_device
        bonds it (legacy PIN cascade for HC-05/06 clones, Just-Works otherwise).

        Brand-agnostic: a named OBD dongle is tried directly; an unnamed/unknown
        in-range device is *probed* — paired, SPP-verified, and unpaired again if
        it isn't a serial adapter — bounded by a probe budget so we don't bond
        the whole neighbourhood. Returns the number of confirmed OBD dongles.
        """
        self._connection_log("auto_pair_scan_start")
        self._announce("Suche Dongle über Bluetooth …", speak=False)
        try:
            known_addrs = {pu[3:].upper() for _l, pu in scan_bt_paired_devices()}
            # 20 s window: cheap ELM clones advertise on a slow duty cycle so a
            # single HCI inquiry round (~10 s) often misses them. Two full rounds
            # is the sweet spot — longer barely improves yield.
            nearby = scan_bt_nearby_devices(scan_seconds=20, known_addrs=known_addrs)
        except Exception as exc:
            self._connection_log("auto_pair_scan_error", error=str(exc))
            return 0
        self._connection_log(
            "auto_pair_scan_done",
            count=len(nearby),
            devices=[{"label": label, "port": port} for label, port in nearby],
        )

        # Fallback: if the active inquiry returned nothing, read BlueZ's device
        # cache. The Phosh BT panel (or any concurrent discovery) populates that
        # cache even when our scan window finds zero — common on binder-stack
        # phones where only one discovery runs at a time. We pick OBD-named
        # entries that aren't already paired and feed them into the same pair
        # loop below.
        if not nearby:
            try:
                cached = scan_bt_known_devices()
            except Exception as exc:
                cached = []
                self._connection_log("auto_pair_cache_error", error=str(exc))
            cache_candidates = [
                (f"{name}  ({addr})", f"bt:{addr}")
                for name, addr in cached
                if addr not in known_addrs and _looks_like_obd(name, addr)
            ]
            self._connection_log(
                "auto_pair_cache_done",
                cache_total=len(cached),
                obd_candidates=len(cache_candidates),
                devices=[{"label": l, "port": p} for l, p in cache_candidates],
            )
            nearby = cache_candidates

        if not nearby:
            self._announce("Kein Dongle in Reichweite gefunden.", speak=False)
        else:
            self._announce(f"{len(nearby)} Dongle gefunden.", speak=False)

        paired_n = 0
        probes = 0
        _MAX_PROBES = 5  # bound pair-probe cost; the wanted dongle is usually closest
        for label, port_url in nearby:
            if self.stop_event.is_set():
                return paired_n
            addr = port_url[3:].upper()
            # Label format is "<name>  (<addr>)" (two spaces, see devices.py).
            name = label.rsplit("(", 1)[0].strip() if "(" in label else label
            named_obd = _looks_like_obd(name, addr)
            # Named OBD dongles are high-confidence → always tried. Unnamed/unknown
            # devices are probed within a budget so we don't bond every phone/beacon
            # in a busy car park.
            if not named_obd:
                if probes >= _MAX_PROBES:
                    self._connection_log("auto_pair_probe_budget", addr=addr, skipped=True)
                    continue
                probes += 1
            # Pre-flight reachability check: a stale BlueZ cache entry (an old
            # ELM in another car, a no-longer-powered dongle) can sit in the
            # candidate list forever. l2ping settles in ~1 s when reachable, or
            # times out in ~3 s — far cheaper than burning 25 s on the full
            # pair handshake. Skip silently when l2ping isn't installed (the
            # helper returns True there) so we never lose the pair path.
            reachable = bt_is_reachable(addr, timeout=3.0)
            self._connection_log("auto_pair_reachable", addr=addr, reachable=reachable)
            if not reachable:
                continue
            self._connection_log("auto_pair_attempt", addr=addr, name=name, named_obd=named_obd)
            shortname = name if named_obd else "Dongle"
            self._announce(f"Koppele {shortname} …", speak=False)
            try:
                ok, msg = pair_bt_device(addr)
            except Exception as exc:
                self._connection_log("auto_pair_exception", addr=addr, error=str(exc))
                self._announce(f"Pairing-Fehler: {exc}", speak=False)
                continue
            if not ok:
                self._connection_log("auto_pair_result", addr=addr, ok=False, msg=msg)
                self._announce(
                    f"Pairing fehlgeschlagen: {msg}",
                    speak=False,
                )
                continue
            # Paired — verify it's actually a serial/OBD adapter via SPP (brand-
            # agnostic, no name list). Discard probed non-OBD bonds so nothing
            # random is left paired; a named OBD device that momentarily fails the
            # SPP readout stays paired to avoid thrashing.
            is_obd = _has_spp_uuid(addr)
            self._connection_log("auto_pair_result", addr=addr, ok=True, msg=msg, spp=is_obd)
            if is_obd:
                paired_n += 1
                self._announce(
                    f"{shortname} gekoppelt — verbinde …",
                    speak=False,
                )
            elif not named_obd:
                try:
                    unpair_bt_device(addr)
                    self._connection_log("auto_pair_discarded_non_obd", addr=addr)
                except Exception as exc:
                    self._connection_log("auto_pair_discard_error", addr=addr, error=str(exc))
        return paired_n

    def _try_serial(self, port: str) -> bool:
        """Try one /dev/* serial port (USB ELM or rfcomm node)."""
        is_rfcomm = port.startswith("/dev/rfcomm")
        try:
            connect_kwargs: dict[str, Any] = {
                "fast": False if is_rfcomm else OBD_FAST,
                "timeout": max(OBD_TIMEOUT_SECONDS, self._BT_OBD_TIMEOUT) if is_rfcomm else OBD_TIMEOUT_SECONDS,
            }
            if OBD_BAUDRATE is not None:
                connect_kwargs["baudrate"] = OBD_BAUDRATE
            elif is_rfcomm:
                connect_kwargs["baudrate"] = 38400
            self._connection_log("connect_attempt", port=port, **connect_kwargs)
            self.connection = obd_backend.OBD(port, **connect_kwargs)
            connected = bool(self.connection and self.connection.is_connected())
            self._connection_log("connect_result", port=port, connected=connected,
                                 status=str(getattr(self.connection, "status", lambda: "unknown")()))
            if connected:
                self.mock = False
                self.mock_reason = ""
                self.connected_port = port
                self.failed_read_count = 0
                supported = sorted(str(c) for c in getattr(self.connection, "supported_commands", set()))
                self._connection_log("connect_success", port=port, supported_commands=supported)
                self._announce("Verbunden.")
                self._probe_adapter()
                return True
            self._close_connection()
            return False
        except Exception as exc:
            self._close_connection()
            self._connection_log("connect_exception", port=port, error=repr(exc), error_type=type(exc).__name__)
            return False

    def _connect(self) -> None:
        if self.force_mock:
            self.mock = True
            self.mock_reason = "Manuell aktiviert"
            self._connection_log("connect_skipped", reason="force_mock")
            return
        self._connection_log("connect_begin")
        GLib.idle_add(
            self.on_update,
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "source": "status",
                "obd_connecting": True,
                "connection_status": "Connecting to OBD...",
                "obd_port": self.connected_port,
            },
        )

        if obd_backend is None:
            self.mock = True
            self.mock_reason = "kein OBD-Backend verfügbar"
            self._connection_log("connect_failed", reason=self.mock_reason, fallback="mock")
            return

        self._close_connection()

        # 1. Settings-configured non-BT port (USB ELM, ELM-WiFi URL, …). The BT
        #    case is handled by the unified BT scan below so a missing/asleep
        #    preferred dongle no longer skips the other paired OBD adapters.
        if self._configured_port and not self._configured_port.startswith("bt:"):
            if self.stop_event.is_set():
                self._connection_log("connect_aborted", reason="stop_event")
                return
            if self._try_serial(self._configured_port):
                return

        # 2. All known BT OBD adapters: configured (preferred) → ENV → every
        #    paired device with an OBD-looking name. First one that handshakes
        #    wins. Lets the same install drive any car that has a paired dongle.
        bt_candidates = self._bt_candidates()
        if bt_candidates:
            self._ensure_bt_powered()
        for addr, channel, source in bt_candidates:
            if self.stop_event.is_set():
                self._connection_log("connect_aborted", reason="stop_event")
                return
            self._connection_log("bt_candidate", bt_addr=addr, source=source)
            if self._try_bt(addr, channel):
                return

        # 2b. No BT dongle responded. Silent auto-pair is intentionally OFF —
        #     bonding a stranger's dongle without user consent surprised people
        #     in the field (the reader would happily pair anything OBD-named in
        #     range). Unknown dongles now surface in Settings → OBD-Dongle for
        #     explicit selection; pair_bt_device runs only on that click. The
        #     ``_auto_pair_nearby_obd`` helper stays in the file so the Settings
        #     side can call it directly when the user picks an unpaired entry.
        self._connection_log(
            "auto_pair_skipped",
            reason="needs user selection in Settings → OBD-Dongle",
        )

        # 3. No BT match — auto-scan local serial/USB ports as a last resort.
        if not self._configured_port:
            for port in self._candidate_ports():
                if self.stop_event.is_set():
                    self._connection_log("connect_aborted", reason="stop_event")
                    return
                if port and port.startswith("/dev/rfcomm") and not OBD_PORT:
                    # Skip AUTO-DISCOVERED rfcomm nodes — the BT loop's rfcomm-bind
                    # fallback already covers those, and racing a stale node here
                    # would just churn. An explicit OBD_PORT=/dev/rfcommN is the
                    # user's choice (see startup hint) and is still honoured below.
                    continue
                if self._try_serial(port) if port else False:
                    return
                if port is None:
                    # python-obd's "no port" auto-scan fallback (returns Not Connected
                    # when nothing usable is found). Still worth one shot.
                    if self._try_serial_none():
                        return

        self.mock = True
        self.mock_reason = "kein nutzbarer Dongle gefunden"
        self._connection_log("connect_failed", reason=self.mock_reason, fallback="mock")
        self._announce(
            "Kein Dongle gefunden — bitte in den Einstellungen einen Dongle auswählen.",
            speak=False,
        )

    def _try_serial_none(self) -> bool:
        """Last-resort: let python-obd autodetect (port=None)."""
        try:
            connect_kwargs: dict[str, Any] = {"fast": OBD_FAST, "timeout": OBD_TIMEOUT_SECONDS}
            if OBD_BAUDRATE is not None:
                connect_kwargs["baudrate"] = OBD_BAUDRATE
            self._connection_log("connect_attempt", port=None, **connect_kwargs)
            self.connection = obd_backend.OBD(None, **connect_kwargs)
            connected = bool(self.connection and self.connection.is_connected())
            self._connection_log("connect_result", port=None, connected=connected,
                                 status=str(getattr(self.connection, "status", lambda: "unknown")()))
            if connected:
                self.mock = False
                self.mock_reason = ""
                self.connected_port = None
                self.failed_read_count = 0
                supported = sorted(str(c) for c in getattr(self.connection, "supported_commands", set()))
                self._connection_log("connect_success", port=None, supported_commands=supported)
                self._probe_adapter()
                return True
            self._close_connection()
            return False
        except Exception as exc:
            self._close_connection()
            self._connection_log("connect_exception", port=None, error=repr(exc), error_type=type(exc).__name__)
            return False

    def _query_locked(self, command: Any, force: bool = False) -> Any:
        """Run an OBD query through the shared lock so the reader and scanner
        threads cannot interleave bytes on the serial line.

        *force* lets the scanner query custom commands (e.g. Mode 0A permanent
        DTCs) that python-obd never lists in ``supported_commands``."""
        with self._obd_lock:
            return self.connection.query(command, force=force)

    def clear_dtcs(self) -> bool:
        """Send OBD Mode 04 (CLEAR_DTC). Returns True if the ECU acknowledged.

        Note for callers: this clears stored *and* pending DTCs, the freeze
        frame, and resets the emissions readiness monitors. The caller is
        responsible for confirming the action with the user before
        invoking this method.
        """
        if obd_backend is None or self.connection is None or self.mock:
            return False
        cmd = getattr(obd_backend.commands, "CLEAR_DTC", None)
        if cmd is None:
            return False
        try:
            response = self._query_locked(cmd)
            return not response.is_null()
        except Exception:
            log.exception("CLEAR_DTC query failed")
            return False

    def run_uds_session(
        self,
        tx: str,
        rx: str,
        fn: Callable[[Any], Any],
        protocol: str = "6",
    ) -> Any:
        """Run *fn(client)* against one control module over a read-only UDS session.

        Pauses live polling (``_diagnostic_active``) and holds the OBD lock for the
        whole session so module-addressed traffic never interleaves with the live
        loop. The adapter's CAN header is restored to the functional broadcast on
        exit. Returns ``fn``'s result, or ``None`` when no real connection exists.
        """
        from drivepulse_app.obd.uds import UdsClient

        if obd_backend is None or self.connection is None or self.mock:
            return None
        port = _serial_port(self.connection)
        if port is None:
            return None

        self._diagnostic_active = True
        t0 = time.monotonic()
        log.info(
            "UDS session begin: port=%s proto=%s tx=%s rx=%s", port, protocol, tx.upper(), rx.upper()
        )
        try:
            with self._obd_lock:
                client = UdsClient(lambda cmd: raw_send(port, cmd))
                client.open(tx, rx, protocol=protocol)
                try:
                    return fn(client)
                finally:
                    client.close()
        except Exception:
            log.exception("UDS session failed (tx=%s rx=%s)", tx, rx)
            return None
        finally:
            self._diagnostic_active = False
            log.info("UDS session end (%.1fs)", time.monotonic() - t0)

    def discover_module(self, tx: str, rx: str, protocol: str = "6") -> dict[str, Any]:
        """Read-only inventory of one module: identification DIDs + VAG coding DID.

        Returns a JSON-friendly dict suitable for ``DriveDB.add_discovery``.
        """
        from drivepulse_app.obd.uds import (
            IDENTIFICATION_DIDS,
            VAG_CODING_DID,
            as_ascii,
            did_payload,
        )

        if self.mock:
            # Simulated data only in explicit mock mode; a no-dongle fallback
            # has nothing real to read (see scan_modules).
            return self._mock_uds.discover(tx, rx) if self.force_mock else {}

        def work(client: Any) -> dict[str, Any]:
            out: dict[str, Any] = {
                "created_at": datetime.now(UTC).isoformat(),
                "tx": tx.upper(), "rx": rx.upper(),
                "identification": {}, "coding": {}, "did_responses": {},
            }
            for did, resp in client.scan_dids([*IDENTIFICATION_DIDS, VAG_CODING_DID]):
                key = f"{did:04X}"
                payload = did_payload(resp, did)
                if payload is not None:
                    entry: dict[str, Any] = {"hex": payload.hex().upper()}
                    ascii_val = as_ascii(payload)
                    if ascii_val is not None:
                        entry["ascii"] = ascii_val
                    out["did_responses"][key] = entry
                    if did in IDENTIFICATION_DIDS:
                        out["identification"][IDENTIFICATION_DIDS[did]] = entry
                    if did == VAG_CODING_DID:
                        out["coding"][key] = entry
                elif resp.negative is not None:
                    out["did_responses"][key] = {
                        "nrc": f"{resp.negative.nrc:02X}",
                        "nrc_name": resp.negative.name,
                    }
            return out

        return self.run_uds_session(tx, rx, work, protocol) or {}

    def sweep_module(
        self,
        tx: str,
        rx: str,
        ranges: list[tuple[int, int]] | None = None,
        protocol: str = "6",
    ) -> dict[str, Any]:
        """Deep read-only sweep of a module's DID space (discovery-shaped dict).

        Reads every DID in *ranges* (default :data:`DISCOVERY_SWEEP_RANGES`) and
        records: every positive value, plus negatives that are NOT
        ``requestOutOfRange`` (0x31). A 0x31 means "no such DID" — pure noise
        over a big sweep — while any other NRC (securityAccessDenied,
        conditionsNotCorrect, session…) means the DID *exists* but isn't
        readable right now, which is exactly what's worth knowing.
        """
        from drivepulse_app.obd.uds import (
            DISCOVERY_SWEEP_RANGES,
            IDENTIFICATION_DIDS,
            VAG_CODING_DID,
            as_ascii,
            did_payload,
            expand_ranges,
        )

        dids = expand_ranges(ranges or list(DISCOVERY_SWEEP_RANGES))

        if self.mock:
            return self._mock_uds.sweep(tx, rx, dids) if self.force_mock else {}

        def work(client: Any) -> dict[str, Any]:
            log.info(
                "deep DID sweep start: tx=%s rx=%s dids=%d", tx.upper(), rx.upper(), len(dids)
            )
            t0 = time.monotonic()
            out: dict[str, Any] = {
                "created_at": datetime.now(UTC).isoformat(),
                "tx": tx.upper(), "rx": rx.upper(), "sweep": True,
                "did_count": len(dids),
                "identification": {}, "coding": {}, "did_responses": {},
            }
            positive = gated = 0
            for did, resp in client.scan_dids(dids, log_each=True):
                key = f"{did:04X}"
                payload = did_payload(resp, did)
                if payload is not None:
                    entry: dict[str, Any] = {"hex": payload.hex().upper()}
                    ascii_val = as_ascii(payload)
                    if ascii_val is not None:
                        entry["ascii"] = ascii_val
                    out["did_responses"][key] = entry
                    positive += 1
                    if did in IDENTIFICATION_DIDS:
                        out["identification"][IDENTIFICATION_DIDS[did]] = entry
                    if did == VAG_CODING_DID:
                        out["coding"][key] = entry
                elif resp.negative is not None and resp.negative.nrc != 0x31:
                    out["did_responses"][key] = {
                        "nrc": f"{resp.negative.nrc:02X}",
                        "nrc_name": resp.negative.name,
                        "gated": True,
                    }
                    gated += 1
            log.info(
                "deep DID sweep done: %d positive, %d gated of %d DIDs in %.1fs",
                positive, gated, len(dids), time.monotonic() - t0,
            )
            return out

        return self.run_uds_session(tx, rx, work, protocol) or {}

    def uds_snapshot(
        self, tx: str, rx: str, dids: list[int], protocol: str = "6"
    ) -> dict[int, str]:
        """Read *dids* from one module once; return ``{did: hex_string}`` positives."""
        from drivepulse_app.obd.uds import did_payload

        if self.mock:
            # Simulated data only in explicit mock mode; a no-dongle fallback
            # has nothing real to read (see scan_modules).
            return self._mock_uds.snapshot(dids) if self.force_mock else {}

        def work(client: Any) -> dict[int, str]:
            out: dict[int, str] = {}
            for did, resp in client.scan_dids(dids):
                payload = did_payload(resp, did)
                if payload is not None:
                    out[did] = payload.hex().upper()
            return out

        return self.run_uds_session(tx, rx, work, protocol) or {}

    def scan_modules(self, protocol: str = "6") -> list[dict[str, str]]:
        """Probe known module addresses; return those that answer (read-only).

        Brand-independent: the legislated 0x7E0–0x7E7 ECUs answer on every
        OBD-II/UDS vehicle, plus the known VAG body modules. Each entry is
        ``{"name", "tx", "rx"}``.
        """
        from drivepulse_app.obd.uds import UdsClient, candidate_modules

        candidates = candidate_modules()
        if self.mock:
            # Only an explicitly chosen mock mode serves simulated modules. An
            # automatic no-dongle fallback (mock without force_mock) has no real
            # bus, so it must report nothing rather than fabricate control units.
            return self._mock_uds.scan_modules(candidates) if self.force_mock else []
        if obd_backend is None or self.connection is None:
            return []
        port = _serial_port(self.connection)
        if port is None:
            return []

        found: list[dict[str, str]] = []
        self._diagnostic_active = True
        try:
            with self._obd_lock:
                client = UdsClient(lambda cmd: raw_send(port, cmd))
                client.init_adapter(protocol)
                for mod in candidates:
                    if self.stop_event.is_set():
                        break
                    client.set_target(mod.tx, mod.rx)
                    if client.is_present():
                        found.append({"name": mod.name, "tx": mod.tx, "rx": mod.rx})
                client.close()
        except Exception:
            log.exception("Module scan failed")
        finally:
            self._diagnostic_active = False
        return found

    def mock_uds_toggle(self) -> None:
        """Flip the simulated coding bit (Car Lab mock) so the next capture diffs."""
        self._mock_uds.toggle_function()

    def _run_vehicle_scan(self, force_rescan: bool = False) -> None:
        """Start the vehicle scan in a background thread so the live read loop
        is not blocked. The scan can take 30+ seconds over Bluetooth; running it
        asynchronously lets gauges update within the first poll cycle after
        connect instead of after the full scan."""
        if obd_backend is None or self.connection is None or self.mock:
            return
        if self._scan_thread is not None and self._scan_thread.is_alive():
            return  # a scan is already in progress
        connection = self.connection
        port = self.connected_port
        # Mark scan time at start so periodic re-scans don't pile up.
        self._last_scan_monotonic = time.monotonic()

        adapter_info = self._adapter_info

        def _worker() -> None:
            try:
                ObdScanner(
                    connection, port, self.on_update, self._scanned_identities,
                    force_rescan=force_rescan,
                    query_locked=self._query_locked,
                    yield_between_queries=0.04,
                    stop_event=self.stop_event,
                    obd_module=obd_backend,
                    raw_send_locked=self._send_raw_locked,
                    resync_locked=self._resync_obd_locked,
                    adapter_info=adapter_info,
                ).run()
            except Exception as exc:
                self._connection_log("scan_thread_error", error=repr(exc), error_type=type(exc).__name__)

        self._scan_thread = threading.Thread(target=_worker, name="obd-scan", daemon=True)
        self._scan_thread.start()

    def _maybe_periodic_rescan(self) -> None:
        if self.mock or self.connection is None or self._RESCAN_INTERVAL_S <= 0:
            return
        if self._last_scan_monotonic <= 0:
            return
        if time.monotonic() - self._last_scan_monotonic < self._RESCAN_INTERVAL_S:
            return
        if self._scan_thread is not None and self._scan_thread.is_alive():
            return
        self._run_vehicle_scan(force_rescan=True)

    def _run(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not self.force_mock:
            self._connect()
            self._run_vehicle_scan()

        while not self.stop_event.is_set():
            if self._diagnostic_active:
                # A UDS diagnostic session owns the bus — don't poll or rescan.
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            if self._force_reconnect:
                self._force_reconnect = False
                if not self.force_mock:
                    self.mock = False
                    self.mock_reason = ""
                    self._connect()
                    self._run_vehicle_scan()
            self._maybe_reconnect_from_mock()
            self._maybe_periodic_rescan()
            payload = self._read_mock() if self.mock else self._read_obd()
            payload["timestamp"] = datetime.now(UTC).isoformat()
            payload["source"] = ("mock" if self.force_mock else "mock_fallback") if self.mock else "obd"
            payload["obd_connecting"] = False
            payload["connection_status"] = self._connection_status()
            payload["obd_port"] = self.connected_port
            if self.mock_reason:
                payload["mock_reason"] = self.mock_reason
            self._write_log(payload)
            GLib.idle_add(self.on_update, payload)
            self._maybe_reconnect_after_read(payload)
            time.sleep(POLL_INTERVAL_SECONDS)

    def _maybe_reconnect_from_mock(self) -> None:
        if self.force_mock or not self.mock or obd_backend is None:
            return

        now = time.monotonic()
        if now < self.next_mock_reconnect_attempt:
            return

        self.next_mock_reconnect_attempt = now + self._MOCK_RECONNECT_INTERVAL_S
        self._connection_log("mock_reconnect_probe")
        self._connect()
        self._run_vehicle_scan()

    def _connection_status(self) -> str:
        if self.mock:
            return f"Mock: {self.mock_reason or 'active'}"
        return f"OBD connected: {self.connected_port or 'auto'}"

    def _maybe_reconnect_after_read(self, payload: dict[str, Any]) -> None:
        if self.mock:
            return

        command_count = int(payload.get("_command_count", 0))
        read_error_count = int(payload.get("_read_error_count", 0))
        disconnected = bool(self.connection and not self.connection.is_connected())
        bt_dead = self._bt_bridge is not None and not self._bt_bridge.is_alive
        # Active liveness probe when the idle-backoff has suppressed real
        # queries for a while. Without this the reader would happily sit on
        # a dead Bluetooth bridge forever: nothing in the regular failed-read
        # path can flip ``disconnected`` (python-obd's flag stays True until
        # an actual error response arrives) and ``bt_dead`` only flips after
        # the pump thread sees a socket EOF — which a half-broken RFCOMM
        # link does not always deliver. A ~1 s ATI round-trip via the shared
        # serial lock is cheap; an empty response means the channel is gone.
        liveness_dead = False
        if not disconnected and not bt_dead and command_count == 0 and self.connection is not None:
            try:
                port = _serial_port(self.connection)
                if port is not None:
                    if not raw_send(port, "ATI", timeout=1.0).strip():
                        liveness_dead = True
            except Exception:
                liveness_dead = True
        failed_read = disconnected or bt_dead or liveness_dead or (command_count > 0 and read_error_count >= command_count)
        self.failed_read_count = self.failed_read_count + 1 if failed_read else 0
        if self.failed_read_count < 3:
            return

        self._connection_log("reconnect_begin", reason="wiederholte Lesefehler", failed_reads=self.failed_read_count)
        # Surface the loss to the user the moment we decide to reconnect — by
        # this point the dongle has been silent for ~3 read cycles. Without
        # this the only feedback was the indicator icon turning grey; the
        # bottom-right status banner stayed on stale text and no TTS fired
        # so a driver had no audible cue the link dropped.
        self._announce(
            "Dongle getrennt — versuche neu zu verbinden.",
            speak=False,
        )
        self.mock = False
        self.mock_reason = ""
        self._connect()
        self._run_vehicle_scan()

    def _read_obd(self) -> dict[str, Any]:
        assert obd_backend is not None
        assert self.connection is not None
        if self._diagnostic_active:
            # Bus is owned by a UDS session; serve the last known values.
            return {**self._obd_value_cache, "_command_count": 0, "_read_error_count": 0}
        if self._adapter_info is not None and self._adapter_info.supports_stpx:
            return self._read_obd_batch()
        return self._read_obd_single()

    def _read_obd_batch(self) -> dict[str, Any]:
        """Read live PIDs via a single STPX batch round-trip (STN/OBDLink).

        One CAN request returns several PIDs at once, replacing the per-PID
        Bluetooth round-trips of the single-query path. PIDs not due this tick
        are served from cache; PIDs without an STPX decoder — or that the
        adapter fails to answer in the batch — fall back to an individual
        python-obd query so a gauge never goes blank.
        """
        from drivepulse_app.obd.adapter import _MODE1_DECODE, batch_query_stpx

        commands = command_map(obd_backend)
        name_to_key = {attr: key for key, attr in OBD_COMMAND_ATTRS}

        data: dict[str, Any] = {}
        now = time.monotonic()
        idle_min = self._idle_min_interval(now)

        due_batch: dict[int, str] = {}        # pid -> key (decodable, due this tick)
        due_single: list[tuple[str, Any]] = []  # (key, command) without STPX decoder
        for key, command in commands.items():
            if command is None:
                continue
            if not self._should_query_obd_key(key, now, idle_min):
                if key in self._obd_value_cache:
                    data[key] = self._obd_value_cache[key]
                continue
            pid = getattr(command, "pid", None)
            if pid is not None and pid in _MODE1_DECODE:
                due_batch[pid] = key
            else:
                due_single.append((key, command))

        command_count = 0
        read_error_count = 0

        if due_batch:
            command_count += 1
            try:
                batched = batch_query_stpx(self._send_raw_locked, list(due_batch))
            except Exception as exc:
                batched = {}
                log.debug("STPX live batch failed: %s", exc)
            if not batched:
                read_error_count += 1
            answered: set[str] = set()
            for name, value in batched.items():
                mapped_key = name_to_key.get(name)
                if mapped_key is None:
                    continue
                data[mapped_key] = value
                self._obd_value_cache[mapped_key] = value
                self._obd_last_query[mapped_key] = now
                answered.add(mapped_key)
                if mapped_key == "speed":
                    self._note_speed_for_idle(value, now)
            # Demote any due PID the adapter didn't answer to a single query.
            for key in due_batch.values():
                if key not in answered:
                    due_single.append((key, commands[key]))

        for key, command in due_single:
            command_count += 1
            try:
                with self._obd_lock:
                    response = self.connection.query(command)
                value = self._response_to_plain_value(response)
                data[key] = value
                self._obd_value_cache[key] = value
                self._obd_last_query[key] = now
                if key == "speed":
                    self._note_speed_for_idle(value, now)
            except Exception as exc:
                read_error_count += 1
                data[f"{key}_error"] = str(exc)

        data["_command_count"] = command_count
        data["_read_error_count"] = read_error_count
        return data

    def _read_obd_single(self) -> dict[str, Any]:
        assert obd_backend is not None
        assert self.connection is not None

        commands = command_map(obd_backend)

        data: dict[str, Any] = {}
        command_count = 0
        read_error_count = 0
        now = time.monotonic()
        idle_min = self._idle_min_interval(now)
        for key, command in commands.items():
            if command is None:
                continue
            if not self._should_query_obd_key(key, now, idle_min):
                if key in self._obd_value_cache:
                    data[key] = self._obd_value_cache[key]
                continue
            command_count += 1
            try:
                with self._obd_lock:
                    response = self.connection.query(command)
                value = self._response_to_plain_value(response)
                data[key] = value
                self._obd_value_cache[key] = value
                self._obd_last_query[key] = now
                if key == "speed":
                    self._note_speed_for_idle(value, now)
            except Exception as exc:
                read_error_count += 1
                data[f"{key}_error"] = str(exc)
        data["_command_count"] = command_count
        data["_read_error_count"] = read_error_count
        return data

    def _should_query_obd_key(self, key: str, now: float, min_interval: float = 0.0) -> bool:
        return should_query_key(key, now, self._obd_last_query, min_interval)

    def _idle_min_interval(self, now: float) -> float:
        """Return the minimum polling interval to apply right now.

        Zero while the vehicle is moving (or recently moved) — fast PIDs run
        every tick as before. Raised to ``_IDLE_MIN_INTERVAL_S`` once the car
        has been below ``_IDLE_MOTION_KMH`` for ``_IDLE_HOLD_S``.
        """
        if now - self._last_motion_monotonic < self._IDLE_HOLD_S:
            return 0.0
        return self._IDLE_MIN_INTERVAL_S

    def _note_speed_for_idle(self, value: Any, now: float) -> None:
        """Update the motion timestamp from a freshly-read speed value."""
        speed_kmh = _extract_speed_kmh(value)
        if speed_kmh is None:
            # Unknown reading — treat conservatively as motion so we don't
            # back off based on a single noisy sample.
            self._last_motion_monotonic = now
        elif speed_kmh >= self._IDLE_MOTION_KMH:
            self._last_motion_monotonic = now

    def _response_to_plain_value(self, response: Any) -> Any:
        return response_to_plain_value(response)

    def trigger_mock_stopwatch(self) -> None:
        """Start a mock 0-230 km/h stopwatch run (called when Start is pressed in mock mode)."""
        self._mock_simulator.trigger_acceleration()

    def _read_mock(self) -> dict[str, Any]:
        return self._mock_simulator.read()

    def _write_log(self, payload: dict[str, Any]) -> None:
        if not self._obd_log_enabled or self.mock:
            return
        try:
            append_jsonl(LOG_FILE, payload)
        except Exception:
            log.exception("Could not write OBD payload log")
