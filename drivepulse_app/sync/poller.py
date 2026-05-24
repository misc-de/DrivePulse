"""Background poller: prüft alle 30s ob bekannte Sync-Geräte erreichbar sind."""
from __future__ import annotations

import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from gi.repository import GLib

from drivepulse_app.diagnostics import get_logger
from drivepulse_app.sync.data import load_paired_devices
from drivepulse_app.sync.server import SyncServer

log = get_logger(__name__)

POLL_INTERVAL_S = 30
PING_TIMEOUT_S = 5


class SyncPoller:
    """Pingt bekannte Geräte alle POLL_INTERVAL_S Sekunden via HTTPS /ping."""

    def __init__(self, on_status: Callable[[bool], None]) -> None:
        self._on_status = on_status
        self._stopped = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stopped = False
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="sync-poller"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopped = True

    def _run(self) -> None:
        # Erster Poll direkt beim Start, danach alle 30s
        self._poll()
        elapsed = 0.0
        while not self._stopped:
            time.sleep(0.5)
            elapsed += 0.5
            if elapsed >= POLL_INTERVAL_S:
                elapsed = 0.0
                self._poll()

    def _poll(self) -> None:
        devices = load_paired_devices()
        reachable = False
        for device in devices:
            host = device.get("host", "")
            port = device.get("port", SyncServer.PORT)
            if host and self._ping(str(host), int(port)):
                reachable = True
                break
        on_status = self._on_status
        GLib.idle_add(lambda: on_status(reachable) or False)

    def _ping(self, host: str, port: int) -> bool:
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_3
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(
                f"https://{host}:{port}/ping",
                method="GET",
            )
            with urllib.request.urlopen(req, context=ctx, timeout=PING_TIMEOUT_S) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as exc:
            # /ping requires the bearer token; the poller doesn't have one.
            # A 403 still proves the server is reachable and responding.
            return exc.code == 403
        except Exception:
            return False
