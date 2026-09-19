"""Self-repair the Bluetooth stack for OBD discovery.

On FuriOS / MediaTek-binder builds the HCI adapter comes up in ``PSCAN``-only
mode (accepts incoming connections, never actively looks) — both on a fresh
install and after every ``apt dist-upgrade``. Without ISCAN, DrivePulse's
Settings auto-scan sees nothing and no OBD dongle can be paired.

Running ``hciconfig hci0 piscan`` fixes it for the current session; a small
systemd oneshot makes the setting stick. Installing that unit needs root,
which we obtain via ``pkexec`` — the OS pops the standard authentication
prompt exactly once, on first run.

Afterwards we must be careful *not* to prompt again. Two traps, both hit in
the field:

* The controller does not stay powered when nothing is connected (see
  ``ObdReader._ensure_bt_powered``). ``hciconfig hci0`` then prints just
  ``DOWN`` — no ISCAN flag — even though the persistence unit is installed
  and runs fine on every boot. Reading that as "scan disabled" asked for the
  password at every single launch. A sleeping adapter says nothing about the
  configuration, and root cannot fix it either: ``hciconfig hci0 piscan``
  fails on a down adapter too.
* A D-Bus power cycle (``bluetoothctl power off/on``) clears ISCAN without
  emitting the udev ``add`` event the rule keys on, so the flag stays off
  until something sets it again. ``bluetoothctl discoverable on`` does that
  through BlueZ — no root needed.

So we only fall back to ``pkexec`` when the persistence is genuinely missing,
or when a *running* adapter refuses the unprivileged repair.

Invoked from the app's startup hook so users never need to know a shell exists.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)

_SYSTEMD_UNIT = Path("/etc/systemd/system/bluetooth-piscan.service")
_FIX_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fix-bt-inquiry.sh"


ADAPTER_ISCAN = "iscan"        # up, inquiry scan on — nothing to do
ADAPTER_NO_ISCAN = "no_iscan"  # up, but inquiry scan off — the one repairable case
ADAPTER_DOWN = "down"          # powered off / asleep — flags say nothing
ADAPTER_UNKNOWN = "unknown"    # no hciconfig, or it failed — assume nothing


def _adapter_state() -> str:
    """Classify ``hciconfig hci0`` into one of the ``ADAPTER_*`` constants.

    Runs unprivileged — ``hciconfig`` reads via netlink and needs no root for
    status queries on all setups we care about. The distinction that matters
    is *down* versus *up-without-ISCAN*: only the latter is a misconfiguration
    we can act on. Anything we cannot read is ``ADAPTER_UNKNOWN`` and is
    treated as healthy, so a broken helper never nags for a password.
    """
    hciconfig = shutil.which("hciconfig")
    if hciconfig is None:
        return ADAPTER_UNKNOWN
    try:
        result = subprocess.run(
            [hciconfig, "hci0"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        return ADAPTER_UNKNOWN
    out = (result.stdout or "").upper()
    if not out.strip():
        return ADAPTER_UNKNOWN
    if "ISCAN" in out:
        return ADAPTER_ISCAN
    # The flag line reads either "UP RUNNING PSCAN ISCAN" or plain "DOWN".
    if "DOWN" in out or "UP" not in out:
        return ADAPTER_DOWN
    return ADAPTER_NO_ISCAN


def _enable_iscan_unprivileged() -> bool:
    """Switch inquiry scan back on without root; True when it took effect.

    ``bluetoothctl discoverable on`` sets ISCAN through BlueZ's D-Bus API,
    which polkit grants to the active local session — no password prompt.

    The timeout has to go to 0 first. BlueZ defaults ``DiscoverableTimeout``
    to 180 s and drops ISCAN again when it expires, so without this the repair
    silently came undone three minutes later — the systemd unit's
    ``hciconfig piscan`` has no such expiry, and matching it is the whole
    point. Verified against the adapter afterwards rather than trusting the
    exit code, because bluetoothctl reports success for a queued command too.
    """
    bluetoothctl = shutil.which("bluetoothctl")
    if bluetoothctl is None:
        return False
    try:
        for args in (["discoverable-timeout", "0"], ["discoverable", "on"]):
            subprocess.run(
                [bluetoothctl, *args],
                capture_output=True, text=True, timeout=5, check=False,
            )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        return False
    ok = _adapter_state() == ADAPTER_ISCAN
    log.info("unprivileged ISCAN repair via bluetoothctl: %s", "ok" if ok else "no effect")
    return ok


def _persisted() -> bool:
    """True when the piscan-repair systemd unit is installed and enabled."""
    if not _SYSTEMD_UNIT.exists():
        return False
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return True  # can't tell; unit file exists, assume good
    try:
        result = subprocess.run(
            [systemctl, "is-enabled", _SYSTEMD_UNIT.stem],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        return True
    return "enabled" in (result.stdout or "").strip().lower()


def bt_inquiry_ready() -> bool:
    """True when nothing needs a password prompt right now.

    Persistence must be installed — without the unit the next reboot or update
    reverts everything, so we still prompt even if ISCAN happens to be on.
    With it installed, only a *running* adapter that lacks ISCAN is a real
    finding; a sleeping or unreadable adapter is not something root could fix
    (see module docstring).
    """
    if not _persisted():
        return False
    return _adapter_state() != ADAPTER_NO_ISCAN


def _run_pkexec_repair() -> None:
    """Background worker: launch pkexec + the fix script."""
    if not _FIX_SCRIPT.exists():
        log.warning("BT repair script missing at %s — cannot self-repair", _FIX_SCRIPT)
        return
    pkexec = shutil.which("pkexec")
    if pkexec is None:
        log.info("pkexec not available — user must run %s manually", _FIX_SCRIPT)
        return
    log.info("BT stack repair: launching pkexec %s", _FIX_SCRIPT)
    try:
        result = subprocess.run(
            [pkexec, "sh", str(_FIX_SCRIPT)],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
        log.info("BT stack repair aborted: %s", exc)
        return
    if result.returncode == 0:
        log.info("BT stack repair OK: piscan enabled + persistence installed")
    else:
        # Non-zero exit = user cancelled the polkit dialog, or a real error.
        # Either way, DrivePulse keeps running; the user can invoke the script
        # manually or retry on next start.
        log.info(
            "BT stack repair exit %s — stdout=%s stderr=%s",
            result.returncode,
            (result.stdout or "").strip()[:200],
            (result.stderr or "").strip()[:200],
        )


_BANNER_TITLE = (
    "Damit DrivePulse den OBD-Dongle findet, muss die Bluetooth-Suche "
    "(Inquiry-Scan) am Adapter aktiv sein. Sie ist gerade aus. "
    "„Reparieren“ schaltet sie ein und sorgt dafür, dass sie es bleibt — "
    "einmalige Passwort-Eingabe, alles bleibt auf diesem Gerät."
)


def _show_explanation_banner(parent_window, on_confirm) -> None:
    """Attach an in-window ``Adw.Banner`` explaining why root is needed.

    Deliberately not a modal ``MessageDialog``: on phone form factors the
    dialog opens as a separate top-level window, which Phosh puts on the
    task switcher — users found it confusing to leave the app "to answer a
    question about the app". The banner slides into the main window's
    toolbar area instead, is dismissible with the standard × icon, and only
    launches ``pkexec`` when the user taps its action button.
    """
    from gi.repository import Adw

    banner = Adw.Banner.new(_BANNER_TITLE)
    banner.set_button_label("Reparieren")
    banner.set_use_markup(False)
    banner.set_revealed(True)

    def _on_click(_banner) -> None:
        banner.set_revealed(False)
        try:
            toolbar = getattr(parent_window, "toolbar_view", None)
            if toolbar is not None:
                toolbar.remove(banner)
        except Exception:
            log.debug("could not detach BT-repair banner", exc_info=True)
        on_confirm()

    banner.connect("button-clicked", _on_click)

    toolbar = getattr(parent_window, "toolbar_view", None)
    if toolbar is not None:
        toolbar.add_top_bar(banner)
    else:
        # Fallback: the parent isn't the dashboard window (e.g. missing-deps
        # window). Just fire the repair straight away — better than losing
        # the notification silently.
        log.info("no toolbar_view on parent; skipping banner and repairing directly")
        on_confirm()


def ensure_bt_inquiry_enabled(parent=None, async_: bool = True) -> None:
    """Public entry point — no-op when already fixed, else show explanation + pkexec.

    Skipped entirely on healthy systems. When repair *is* needed, an
    in-app dialog first explains why to the user; only after they confirm
    do we launch pkexec (system password prompt). The pkexec process itself
    runs off the UI thread so the app stays responsive.

    ``parent`` is the top-level GTK window used to anchor the message
    dialog; when omitted the dialog is application-modal.
    """
    try:
        persisted = _persisted()
        state = _adapter_state()
        if persisted and state != ADAPTER_NO_ISCAN:
            log.debug("BT inquiry persisted, adapter state %s — no repair needed", state)
            return
        # Persistence is in place and the adapter is awake but lost ISCAN —
        # typically after a D-Bus power cycle. BlueZ can put it back without
        # root, so try that before bothering the user for a password.
        if persisted and _enable_iscan_unprivileged():
            return
    except Exception:
        log.debug("BT inquiry-ready check failed", exc_info=True)
        return
    log.info(
        "BT inquiry repair needed (persisted=%s, adapter=%s) — offering repair via pkexec",
        persisted, state,
    )

    def _launch_repair() -> None:
        if async_:
            threading.Thread(target=_run_pkexec_repair, daemon=True, name="bt-repair").start()
        else:
            _run_pkexec_repair()

    try:
        _show_explanation_banner(parent, _launch_repair)
    except Exception:
        log.debug("BT repair banner failed, falling back to direct pkexec", exc_info=True)
        _launch_repair()
