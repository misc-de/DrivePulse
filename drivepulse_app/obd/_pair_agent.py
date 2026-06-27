"""Stand-alone BlueZ agent + Device1.Pair() driver for OBD dongles.

This module exists because the system BlueZ agent (Phosh's gnome-bluetooth,
GNOME Shell, …) registers itself as the *default* agent and forces every pair
to flow through Secure Simple Pairing — fine for headphones and watches, but
the cheap HC-05/HC-06 ELM327 clones can only do legacy 4-digit PIN. The OS
agent then either pops a "Confirm 6-digit code" dialog the user can only
``Confirm`` or ``Cancel`` (and the dongle has no display to compare against),
or silently rejects with ``AuthenticationFailed``.

We side-step the whole mess by registering our *own* Agent1 object on D-Bus
with capability ``NoInputNoOutput`` and calling ``RequestDefaultAgent``. BlueZ
then routes every authentication callback (RequestPinCode, RequestPasskey,
RequestConfirmation, …) to us, and we simply return the next PIN in a fixed
cascade (1234 → 0000 → 6789 → …) or auto-accept Just-Works / Numeric
Comparison without user interaction. The OS agent never sees the request.

Invoked synchronously from ``pair_bt_device`` in ``devices.py`` — either
in-process (when the caller already runs a GLib main loop) or via
``python3 -m drivepulse_app.obd._pair_agent <ADDR> [PIN ...]`` as a fresh
subprocess. Stand-alone use lets us keep the agent completely isolated from
the rest of the reader.
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

_AGENT_PATH = "/org/drivepulse/btagent"
_BLUEZ_SERVICE = "org.bluez"
_AGENT_IFACE = "org.bluez.Agent1"
_AGENT_MGR_IFACE = "org.bluez.AgentManager1"
_DEVICE_IFACE = "org.bluez.Device1"
_PROPS_IFACE = "org.freedesktop.DBus.Properties"
_DEFAULT_PINS: tuple[str, ...] = ("1234", "0000", "6789", "0123", "1111", "8888")


def _device_path(addr: str) -> str:
    return f"/org/bluez/hci0/dev_{addr.upper().replace(':', '_')}"


class DrivePulseAgent(dbus.service.Object):
    """BlueZ ``Agent1`` that answers every auth callback non-interactively.

    Owns a cyclic list of PIN candidates: each ``RequestPinCode`` / ``RequestPasskey``
    pops the next one. ``DisplayPin/Passkey`` and the Confirmation/Authorization
    callbacks are no-ops (auto-accept) so Just-Works SSP and Numeric Comparison
    bond silently.
    """

    def __init__(self, bus: dbus.Bus, pins: list[str]) -> None:
        super().__init__(bus, _AGENT_PATH)
        self._pins: list[str] = list(pins) or list(_DEFAULT_PINS)
        self._pin_idx: int = 0
        self.last_event: str = ""

    def next_pin(self) -> str:
        """Pop the next PIN in the cascade, looping at the end."""
        pin = self._pins[self._pin_idx % len(self._pins)]
        self._pin_idx += 1
        return pin

    @dbus.service.method(_AGENT_IFACE, in_signature="", out_signature="")
    def Release(self) -> None:  # noqa: N802 — D-Bus name
        self.last_event = "Release"

    @dbus.service.method(_AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device: str) -> str:  # noqa: N802
        pin = self.next_pin()
        self.last_event = f"PinCode -> {pin}"
        return pin

    @dbus.service.method(_AGENT_IFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device: str, pincode: str) -> None:  # noqa: N802
        self.last_event = f"DisplayPinCode {pincode}"

    @dbus.service.method(_AGENT_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device: str) -> int:  # noqa: N802
        pin = self.next_pin()
        try:
            v = int(pin)
        except ValueError:
            v = 0
        self.last_event = f"Passkey -> {v}"
        return dbus.UInt32(v)

    @dbus.service.method(_AGENT_IFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device: str, passkey: int, entered: int) -> None:  # noqa: N802
        self.last_event = f"DisplayPasskey {passkey} ({entered})"

    @dbus.service.method(_AGENT_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device: str, passkey: int) -> None:  # noqa: N802
        # Returning no error = confirm. Just-Works SSP numeric comparison passes.
        self.last_event = f"Confirm {passkey} -> ok"

    @dbus.service.method(_AGENT_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device: str) -> None:  # noqa: N802
        self.last_event = f"Authorize {device}"

    @dbus.service.method(_AGENT_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device: str, uuid: str) -> None:  # noqa: N802
        # Always allow — auto-accept SPP service profile.
        self.last_event = f"AuthorizeService {uuid}"

    @dbus.service.method(_AGENT_IFACE, in_signature="", out_signature="")
    def Cancel(self) -> None:  # noqa: N802
        self.last_event = "Cancel"


_DEFAULT_CAPABILITY_CASCADE: tuple[str, ...] = (
    # In order of decreasing reach. Each entry maps to a BlueZ pairing flow:
    # NoInputNoOutput    → Just-Works SSP    (most modern adapters: MX+, vLinker, …)
    # DisplayYesNo       → Numeric Comparison (our RequestConfirmation auto-yes)
    # KeyboardOnly       → Legacy PIN entry  (HC-05/06 ELM clones; PIN cascade)
    "NoInputNoOutput",
    "DisplayYesNo",
    "KeyboardOnly",
)


def _attempt_pair(
    bus: dbus.Bus,
    mgr: dbus.Interface,
    agent: DrivePulseAgent,
    addr: str,
    capability: str,
    timeout: float,
) -> tuple[bool, str]:
    """One pair pass with the given agent capability. Returns ``(ok, msg)``.

    The DBus object exporting our ``Agent1`` interface is created once by the
    caller (``DrivePulseAgent``); we just re-bind it to BlueZ at the requested
    capability. Trying to recreate the dbus.service.Object on each pass would
    raise "object already exported at path" on the second iteration and kill
    the cascade after the first failure.
    """
    # Re-bind to BlueZ with the new capability. UnregisterAgent is idempotent.
    try:
        mgr.UnregisterAgent(_AGENT_PATH)
    except dbus.exceptions.DBusException:
        pass
    try:
        mgr.RegisterAgent(_AGENT_PATH, capability)
        mgr.RequestDefaultAgent(_AGENT_PATH)
    except dbus.exceptions.DBusException as exc:
        return False, f"agent register failed ({capability}): {exc}"
    # Reset the per-pass PIN cursor so each capability starts fresh from 1234.
    agent._pin_idx = 0

    device = bus.get_object(_BLUEZ_SERVICE, _device_path(addr))
    pair_iface = dbus.Interface(device, _DEVICE_IFACE)
    props_iface = dbus.Interface(device, _PROPS_IFACE)

    # Clear any stale half-bond from a previous pass: a failed pair can leave
    # the device in ``Connected: yes / Paired: no`` and BlueZ cannot start
    # fresh authentication on top of the dead ACL link, so every retry
    # collapses into ``AuthenticationFailed``.
    try:
        if bool(props_iface.Get(_DEVICE_IFACE, "Connected")):
            try:
                pair_iface.Disconnect()
            except dbus.exceptions.DBusException:
                pass
            time.sleep(0.5)
    except dbus.exceptions.DBusException:
        pass

    # Mark Trusted *before* pairing. With Just-Works SSP some controllers
    # (notably the MediaTek/binder stack on FuriOS) flag the new link key as
    # ``store_hint=0`` — "ephemeral" — and BlueZ drops it when the ACL link
    # goes down. Trusted overrides that hint: BlueZ keeps the key and the
    # bond persists across reconnects.
    try:
        props_iface.Set(_DEVICE_IFACE, "Trusted", True)
    except dbus.exceptions.DBusException:
        pass

    state: dict[str, Any] = {"ok": False, "msg": "", "done": False}
    loop = GLib.MainLoop()

    def on_pair_done() -> None:
        state.update(ok=True, msg="paired", done=True)
        loop.quit()

    def on_pair_error(exc: Exception) -> None:
        state.update(ok=False, msg=str(exc)[:300], done=True)
        loop.quit()

    def on_timeout() -> bool:
        if not state["done"]:
            state.update(msg=f"timeout after {timeout}s", done=True)
            try:
                pair_iface.CancelPairing()
            except dbus.exceptions.DBusException:
                pass
            loop.quit()
        return False

    GLib.timeout_add(int(timeout * 1000), on_timeout)
    # Match the D-Bus reply timeout to our GLib watchdog — the default 25 s
    # in dbus-python raises NoReply when BlueZ legitimately needs longer.
    pair_iface.Pair(
        reply_handler=on_pair_done,
        error_handler=on_pair_error,
        timeout=timeout + 5.0,
    )
    try:
        loop.run()
    except KeyboardInterrupt:
        state["msg"] = "interrupted"

    if not state["ok"]:
        return False, state["msg"]

    # Pair() returned success. Now keep the ACL alive long enough for BlueZ
    # to commit the bond to disk: an immediate disconnect (or even just our
    # subprocess exiting before BlueZ flushes) makes a ``store_hint=0`` key
    # vanish. Connect() establishes the data path, the ~1.5 s settle window
    # lets bluetoothd write the key, and we don't disconnect ourselves.
    try:
        pair_iface.Connect()
    except dbus.exceptions.DBusException:
        pass
    time.sleep(1.5)
    try:
        props_iface.Set(_DEVICE_IFACE, "Trusted", True)
    except dbus.exceptions.DBusException:
        pass
    # Confirm the bond actually persisted before we report success — Pair()
    # may return ``ok`` while the link key drops moments later. ``Paired:yes``
    # is the post-commit state we hand back to the Reader.
    try:
        if not bool(props_iface.Get(_DEVICE_IFACE, "Paired")):
            return False, "bond dropped after Pair() (key not persisted)"
    except dbus.exceptions.DBusException:
        pass
    return True, f"paired ({capability})"


def pair_via_agent(
    addr: str,
    pin_candidates: tuple[str, ...] | None = None,
    capability: str | None = None,
    timeout: float = 30.0,
) -> tuple[bool, str]:
    """Pair *addr* via an in-process BlueZ agent, cascading capabilities.

    The cascade trades a small amount of time on failure for a fully generic
    pairing path: every OBD dongle we've tested bonds at one of the three
    levels, and no system config edits are needed (no main.conf tweaks, no
    polkit dialogs, no Phosh agent fighting). When *capability* is given the
    cascade is restricted to that single pass — used by tests and callers
    that already know what the adapter accepts.

    Returns ``(success, message)``. ``message`` includes the winning
    capability label on success, or a "|"-joined per-pass error tail on
    failure.
    """
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    pins = list(pin_candidates) if pin_candidates else list(_DEFAULT_PINS)
    mgr = dbus.Interface(bus.get_object(_BLUEZ_SERVICE, "/org/bluez"), _AGENT_MGR_IFACE)
    capabilities = (capability,) if capability else _DEFAULT_CAPABILITY_CASCADE

    # Export the Agent1 object exactly once — the DBus path is a singleton,
    # so re-creating it in every cascade pass throws "object already exported".
    agent = DrivePulseAgent(bus, pins)
    per_pass: list[str] = []
    for cap in capabilities:
        ok, msg = _attempt_pair(bus, mgr, agent, addr, cap, timeout)
        if ok:
            try:
                mgr.UnregisterAgent(_AGENT_PATH)
            except dbus.exceptions.DBusException:
                pass
            return True, msg
        per_pass.append(f"{cap}: {msg}")
    try:
        mgr.UnregisterAgent(_AGENT_PATH)
    except dbus.exceptions.DBusException:
        pass
    return False, " | ".join(per_pass) or "all capabilities failed"


def main(argv: list[str]) -> int:
    """``python3 -m drivepulse_app.obd._pair_agent [--capability X] ADDR [PIN ...]``.

    Stand-alone mode for tooling — prints a JSON ``{ok, msg}`` result on
    stdout, exits 0 on success, 1 on failure.
    """
    args = list(argv[1:])
    capability = "NoInputNoOutput"
    while args and args[0] == "--capability" and len(args) >= 2:
        capability = args[1]
        args = args[2:]
    if not args:
        print(json.dumps({"ok": False, "msg": "usage: [--capability X] ADDR [PIN ...]"}))
        return 1
    addr = args[0]
    pins = tuple(args[1:]) if len(args) > 1 else None
    ok, msg = pair_via_agent(addr, pin_candidates=pins, capability=capability)
    print(json.dumps({"ok": ok, "msg": msg}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
