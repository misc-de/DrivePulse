"""Self-repair the Bluetooth stack for OBD discovery.

On FuriOS / MediaTek-binder builds the HCI adapter comes up in ``PSCAN``-only
mode (accepts incoming connections, never actively looks) — both on a fresh
install and after every ``apt dist-upgrade``. Without ISCAN, DrivePulse's
Settings auto-scan sees nothing and no OBD dongle can be paired.

Running ``hciconfig hci0 piscan`` fixes it for the current session; a small
systemd oneshot makes the setting stick. Both changes need root, which we
obtain via ``pkexec`` — the OS pops the standard authentication prompt
exactly once. If the fix is already in place (unit installed *and* adapter
reports ISCAN), the call is a no-op and no banner appears.

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


def _iscan_active() -> bool:
    """True when ``hciconfig hci0`` reports ISCAN (inquiry scan) enabled.

    Runs unprivileged — ``hciconfig`` reads via netlink and needs no root
    for status queries on all setups we care about. On error we conservatively
    return True so a broken helper doesn't nag the user with an unnecessary
    ``pkexec`` prompt.
    """
    hciconfig = shutil.which("hciconfig")
    if hciconfig is None:
        return True
    try:
        result = subprocess.run(
            [hciconfig, "hci0"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        return True
    return "ISCAN" in (result.stdout or "").upper()


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
    """Both conditions must hold for the repair to be skipped.

    Runtime OK *and* persistence installed. If ISCAN is on but the unit isn't
    installed the next reboot / update reverts everything, so we still prompt.
    """
    return _iscan_active() and _persisted()


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
        if bt_inquiry_ready():
            log.debug("BT inquiry scan already enabled + persisted; no repair needed")
            return
    except Exception:
        log.debug("BT inquiry-ready check failed", exc_info=True)
        return
    log.info("BT inquiry scan disabled or non-persistent — offering repair via pkexec")

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
