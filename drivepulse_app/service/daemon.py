"""Hintergrund-Aufzeichnung: Fahrten erfassen, ohne dass die App läuft.

Zwei Zustände:

* **Bereit** — kein Reader, kein GPS, keine offene DB. Alle
  ``BASE_INTERVAL_S`` klopft der Dienst kurz beim Dongle an (wenige Sekunden
  RFCOMM, ``ATRV``, trennen). Ist der Dongle länger nicht erreichbar (Auto
  weit weg), wird das Raster auf ``AWAY_INTERVAL_S`` gestreckt. Ereignisse
  lösen sofort einen Check aus und setzen das Raster zurück:
  Phone beginnt zu laden / ein Bluetooth-Gerät (Auto-Radio) verbindet sich
  (starke Hinweise → zusätzlich Drehzahl abfragen) oder das Phone wird
  entsperrt (schwacher Hinweis → nur Spannung).
* **Aufzeichnung** — Motor läuft: ObdReader + GpsReader + TripRecorder wie in
  der App. Endet, wenn 120 s keine OBD-Daten mehr kommen (Motor aus), oder
  sofort, wenn die App startet — die übernimmt dann den Dongle selbst.

Läuft die App, tut der Dienst nichts.
"""
from __future__ import annotations

import fcntl
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

from drivepulse_app.app_settings import load_settings
from drivepulse_app.diagnostics import get_logger
from drivepulse_app.service.probe import ProbeResult, engine_running, probe_dongle

log = get_logger("drivepulse_app.service")

BASE_INTERVAL_S = 120
AWAY_INTERVAL_S = 300
AWAY_AFTER_FAILS = 30          # ≈ 1 h ohne Dongle → gestrecktes Raster
EVENT_DELAY_S = 4              # Ereignis → Check (Auto-Radio/Laden kurz setzen lassen)
MIN_PROBE_GAP_S = 20           # Ereignis-Flut entprellen
CANDIDATE_CACHE_S = 1800       # gekoppelte Dongles neu ermitteln
SESSION_IDLE_S = 120           # so lange ohne OBD-Daten → Motor aus
SESSION_START_GRACE_S = 150    # Verbindungsaufbau + VIN-Scan
EMPTY_SESSION_COOLDOWN_S = 600 # Motor „an", aber keine Daten → nicht im Kreis verbinden
TICK_S = 10
APP_CHECK_S = 3


def setting_enabled() -> bool:
    try:
        return bool(load_settings().get("background_recording", False))
    except Exception:
        log.exception("Could not read settings")
        return False


def app_running() -> bool:
    """Läuft die DrivePulse-App (GUI)? Über /proc statt über deren Lock, damit
    der Dienst die App nie am Start hindert."""
    me = os.getpid()
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            raw = Path(entry.path, "cmdline").read_bytes()
        except OSError:
            continue
        if is_app_cmdline(raw):
            return True
    return False


def is_app_cmdline(raw: bytes) -> bool:
    """Nur echte Python-Prozesse der App zählen — nicht etwa eine Shell, deren
    Befehlszeile den Modulnamen bloß enthält (SSH-Einzeiler, pkill …)."""
    args = [a for a in raw.split(b"\0") if a]
    if not args or b"python" not in Path(args[0].decode(errors="replace")).name.encode():
        return False
    return any(a == b"drivepulse_app.app" or a.endswith(b"drivepulse_app/app.py") for a in args[1:])


def bt_candidates() -> list[tuple[str, int]]:
    """Dongle-Adressen: konfigurierter bt:-Port, sonst alle gekoppelten SPP-Geräte."""
    from drivepulse_app.common import OBD_BT_ADDR
    from drivepulse_app.obd.devices import paired_obd_addresses, parse_bt_port

    port = str(load_settings().get("obd_port") or "")
    if port.startswith("bt:"):
        try:
            return [parse_bt_port(port)]
        except ValueError:
            log.warning("Invalid obd_port %r", port)
    if OBD_BT_ADDR:
        try:
            return [parse_bt_port(f"bt:{OBD_BT_ADDR}")]
        except ValueError:
            pass
    return [(addr, ch) for addr, ch, _name in paired_obd_addresses()]


class RecorderService:
    def __init__(self, loop: GLib.MainLoop) -> None:
        self.loop = loop
        self._probe_source: int | None = None
        self._probing = False
        self._last_probe = 0.0
        self._fails = 0
        self._rest_voltage: float | None = None
        self._cooldown_until = 0.0
        self._candidates: list[tuple[str, int]] = []
        self._candidates_at = 0.0
        self._subs: list[tuple[Gio.DBusConnection, int]] = []
        # Aufzeichnungs-Sitzung
        self._session: dict[str, Any] | None = None

    # ── Start / Stop ──────────────────────────────────────────────────────

    def start(self) -> None:
        self._subscribe_events()
        self._schedule_probe(10)
        log.info("Background recorder ready (interval %ss)", BASE_INTERVAL_S)

    def shutdown(self) -> None:
        self._end_session("shutdown")
        for bus, sid in self._subs:
            bus.signal_unsubscribe(sid)
        self._subs.clear()
        self.loop.quit()

    # ── Ereignisse ────────────────────────────────────────────────────────

    def _subscribe_events(self) -> None:
        try:
            system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self._subs.append((system, system.signal_subscribe(
                "org.bluez", "org.freedesktop.DBus.Properties", "PropertiesChanged",
                None, "org.bluez.Device1", Gio.DBusSignalFlags.NONE,
                self._on_bluez_changed,
            )))
            self._subs.append((system, system.signal_subscribe(
                "org.freedesktop.UPower", "org.freedesktop.DBus.Properties",
                "PropertiesChanged", "/org/freedesktop/UPower", None,
                Gio.DBusSignalFlags.NONE, self._on_upower_changed,
            )))
        except GLib.Error:
            log.exception("System bus unavailable — only interval checks")
        try:
            session = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._subs.append((session, session.signal_subscribe(
                None, "org.gnome.ScreenSaver", "ActiveChanged", None, None,
                Gio.DBusSignalFlags.NONE, self._on_screensaver,
            )))
        except GLib.Error:
            log.exception("Session bus unavailable — no unlock trigger")

    def _on_bluez_changed(self, _c, _s, path: str, _i, _sig, params: GLib.Variant) -> None:
        _iface, changed, _inv = params.unpack()
        if changed.get("Connected") is not True:
            return
        addr = path.rsplit("dev_", 1)[-1].replace("_", ":").upper()
        if any(addr == a for a, _ in self._candidates):
            return  # unser eigener Probe-/Reader-Connect
        self._event("bt-device", strong=True)

    def _on_upower_changed(self, _c, _s, _p, _i, _sig, params: GLib.Variant) -> None:
        _iface, changed, _inv = params.unpack()
        if changed.get("OnBattery") is False:
            self._event("charging", strong=True)

    def _on_screensaver(self, _c, _s, _p, _i, _sig, params: GLib.Variant) -> None:
        (active,) = params.unpack()
        if not active:
            self._event("unlock", strong=False)

    def _event(self, reason: str, *, strong: bool) -> None:
        if self._session is not None:
            return
        self._fails = 0
        if time.monotonic() - self._last_probe < MIN_PROBE_GAP_S and not strong:
            return
        log.info("Trigger %s → check dongle", reason)
        self._schedule_probe(EVENT_DELAY_S, strong=strong)

    # ── Anklopfen ─────────────────────────────────────────────────────────

    def _schedule_probe(self, delay_s: float, *, strong: bool = False) -> None:
        if self._probe_source is not None:
            GLib.source_remove(self._probe_source)
        self._probe_source = GLib.timeout_add_seconds(
            max(1, int(delay_s)), self._probe_due, strong,
        )

    def _next_interval(self) -> int:
        return AWAY_INTERVAL_S if self._fails >= AWAY_AFTER_FAILS else BASE_INTERVAL_S

    def _probe_due(self, strong: bool) -> bool:
        self._probe_source = None
        if not setting_enabled():
            log.info("Background recording disabled in settings — exiting")
            self.shutdown()
            return False
        if self._session is not None or self._probing:
            return False
        if app_running():
            self._schedule_probe(self._next_interval())
            return False
        if time.monotonic() < self._cooldown_until and not strong:
            self._schedule_probe(self._next_interval())
            return False
        self._probing = True
        self._last_probe = time.monotonic()
        threading.Thread(
            target=self._probe_worker, args=(strong,), name="recorder-probe", daemon=True,
        ).start()
        return False

    def _probe_worker(self, strong: bool) -> None:
        found: tuple[str, int, ProbeResult] | None = None
        errors: list[str] = []
        try:
            now = time.monotonic()
            if not self._candidates or now - self._candidates_at > CANDIDATE_CACHE_S:
                self._candidates = bt_candidates()
                self._candidates_at = now
            if self._candidates:
                _ensure_adapter_powered()
            for addr, ch in self._candidates:
                res = probe_dongle(addr, ch, check_rpm=strong)
                if res.reachable:
                    found = (addr, ch, res)
                    break
                errors.append(f"{addr}: {res.error or 'unreachable'}")
        except Exception:
            log.exception("Dongle probe failed")
        if found is None:
            log.info("Dongle not reachable (%s)", "; ".join(errors) or "no candidates")
        GLib.idle_add(self._probe_done, found, strong)

    def _probe_done(self, found: tuple[str, int, ProbeResult] | None, strong: bool) -> bool:
        self._probing = False
        if found is None:
            self._fails += 1
            self._schedule_probe(self._next_interval())
            return False
        addr, ch, res = found
        self._fails = 0
        running = engine_running(res, self._rest_voltage)
        log.info(
            "Dongle %s reachable: %.2f V rpm=%s rest=%s → %s",
            addr, res.voltage or 0.0, res.rpm, self._rest_voltage,
            "engine on" if running else "parked",
        )
        if running and not app_running():
            self._start_session(addr, ch)
            return False
        if res.voltage is not None and not running:
            self._rest_voltage = res.voltage
        self._schedule_probe(self._next_interval())
        return False

    # ── Aufzeichnung ──────────────────────────────────────────────────────

    def _start_session(self, addr: str, ch: int) -> None:
        from drivepulse_app.common import DB_FILE
        from drivepulse_app.db import DriveDB
        from drivepulse_app.sensors.gps import GpsReader
        from drivepulse_app.trip_recorder import TripRecorder

        log.info("Engine on — background recording via %s", addr)
        db = DriveDB(DB_FILE)
        trip = TripRecorder(db)
        reader = _new_service_reader(self._on_payload)
        reader._configured_port = f"bt:{addr}:{ch}"
        reader.set_obd_log_enabled(bool(load_settings().get("log_obd_enabled", False)))
        gps = GpsReader(self._on_payload)
        now = time.monotonic()
        self._session = {
            "db": db, "trip": trip, "reader": reader, "gps": gps,
            "started": now, "last_sample": None, "samples": 0,
        }
        reader.start()
        gps.start()
        self._session["tick"] = GLib.timeout_add_seconds(TICK_S, self._session_tick)
        self._session["app_check"] = GLib.timeout_add_seconds(APP_CHECK_S, self._session_app_check)

    def _on_payload(self, payload: dict[str, Any]) -> None:
        # Läuft per GLib.idle_add — Rückgabe None entfernt die Quelle wieder.
        s = self._session
        if s is None:
            return
        try:
            handle_payload(s["db"], s["trip"], payload, s)
        except Exception:
            log.exception("Could not handle payload in background recorder")

    def _session_tick(self) -> bool:
        s = self._session
        if s is None:
            return False
        now_mono = time.monotonic()
        try:
            s["db"].checkpoint()
            s["trip"].maybe_end_idle_trip(time.time())
        except Exception:
            log.exception("Background recorder tick failed")
        last = s["last_sample"]
        if last is None:
            if now_mono - s["started"] > SESSION_START_GRACE_S:
                self._cooldown_until = now_mono + EMPTY_SESSION_COOLDOWN_S
                self._end_session("no OBD data")
                return False
        elif now_mono - last > SESSION_IDLE_S:
            self._end_session("engine off")
            return False
        return True

    def _session_app_check(self) -> bool:
        if self._session is None:
            return False
        if app_running():
            self._end_session("app started")
            return False
        return True

    def _end_session(self, reason: str) -> None:
        s = self._session
        if s is None:
            return
        self._session = None
        for key in ("tick", "app_check"):
            if s.get(key):
                GLib.source_remove(s[key])
        for key in ("reader", "gps"):
            try:
                s[key].stop()
            except Exception:
                log.exception("Could not stop %s", key)
        try:
            s["trip"].end_trip()
        except Exception:
            log.exception("Could not end trip")
        try:
            s["db"].close()
        except Exception:
            log.exception("Could not close database")
        log.info("Background recording ended (%s, %d samples)", reason, s["samples"])
        if reason != "shutdown":
            self._schedule_probe(BASE_INTERVAL_S)


def handle_payload(db: Any, trip: Any, payload: dict[str, Any], state: dict[str, Any]) -> None:
    """Payload von ObdReader/GpsReader in den TripRecorder übernehmen —
    dieselben Regeln wie in der App (dashboard/telemetry.py)."""
    from drivepulse_app.dashboard.data import obd_sample_fields, scan_identity_from_payload
    from drivepulse_app.telemetry_utils import has_obd_data, plain_number

    source = payload.get("source", "")
    if source == "obd_scan_identity":
        ident = scan_identity_from_payload(payload)
        vin = ident.get("vin")
        if not vin:
            return
        known = known_car_id_for_vin(db, vin)
        trip.set_car(
            vin=vin, brand=ident["brand"], cal_id=ident["cal_id"], cvn=ident["cvn"],
            protocol=ident["protocol"], profile_path=ident["profile_path"],
            # Unbekannt → temporäres Live-Auto; die App promotet es beim
            # nächsten Start, weil Fahrten daran hängen.
            is_live=None if known is not None else True,
        )
        return
    if source == "gps":
        trip.update_gps(
            lat=plain_number(payload, "gps_lat"),
            lon=plain_number(payload, "gps_lon"),
            altitude_m=plain_number(payload, "gps_altitude"),
            heading_deg=plain_number(payload, "gps_heading"),
            gps_speed_kmh=plain_number(payload, "gps_speed"),
        )
        return
    if source == "obd" and has_obd_data(payload):
        if trip.car_id is None:
            return  # VIN noch unbekannt — wie in der App verwerfen
        trip.record_obd(time.time(), **obd_sample_fields(payload, plain_number))
        state["last_sample"] = time.monotonic()
        state["samples"] = state.get("samples", 0) + 1


def known_car_id_for_vin(db: Any, vin: str) -> int | None:
    from drivepulse_app.telemetry_utils import vins_same_vehicle

    cars = list(db.list_cars(include_live=True))
    for row in cars:
        if (row["vin"] or "") == vin:
            return int(row["id"])
    for row in cars:
        if vins_same_vehicle(row["vin"], vin):
            return int(row["id"])
    return None


def _ensure_adapter_powered() -> None:
    """bluebinder schaltet den Controller ohne Verbindung ab — vor dem Anklopfen
    per D-Bus wieder einschalten (billiger als ``bluetoothctl``)."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        res = bus.call_sync(
            "org.bluez", "/org/bluez/hci0", "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", ("org.bluez.Adapter1", "Powered")),
            GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 3000, None,
        )
        if res.unpack()[0]:
            return
        bus.call_sync(
            "org.bluez", "/org/bluez/hci0", "org.freedesktop.DBus.Properties", "Set",
            GLib.Variant("(ssv)", ("org.bluez.Adapter1", "Powered", GLib.Variant("b", True))),
            None, Gio.DBusCallFlags.NONE, 5000, None,
        )
        log.info("Bluetooth adapter powered on")
        time.sleep(1.0)
    except GLib.Error as exc:
        log.debug("Could not ensure adapter power: %s", exc.message)


def _make_reader_class() -> type:
    from drivepulse_app.obd.reader import ObdReader

    class ServiceObdReader(ObdReader):
        """ObdReader ohne Sprachausgabe und ohne pkexec-Fallback (kein Dialog
        aus dem Hintergrund)."""

        __gtype_name__ = "DrivePulseServiceObdReader"

        def __init__(self, on_update) -> None:
            super().__init__(on_update)
            self._auto_pair_attempted = True

        def _announce(self, text: str, *, speak: bool = True, voice_text: str | None = None) -> None:
            log.debug("reader: %s", text)

        def _rfcomm_bind(self, addr: str, channel: int) -> str | None:
            return None

    return ServiceObdReader


_reader_cls: type | None = None


def _new_service_reader(on_update) -> Any:
    global _reader_cls
    if _reader_cls is None:
        _reader_cls = _make_reader_class()
    return _reader_cls(on_update)


def _acquire_service_lock() -> object | None:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    fh = open(Path(runtime_dir) / "drivepulse-recorder.lock", "a")  # noqa: SIM115 — Lock lebt mit dem Prozess
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def main() -> int:
    if not setting_enabled():
        log.info("Background recording disabled in settings — not starting")
        return 0
    lock = _acquire_service_lock()
    if lock is None:
        log.info("Background recorder already running")
        return 0
    loop = GLib.MainLoop()
    service = RecorderService(loop)
    def _on_signal() -> bool:
        service.shutdown()
        return False

    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, _on_signal)
    service.start()
    loop.run()
    return 0
