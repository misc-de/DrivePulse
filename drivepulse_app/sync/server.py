from __future__ import annotations

import hmac
import ipaddress
import json
import os
import ssl
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)


PAIRING_TIMEOUT_S = 60
SYNC_SESSION_TIMEOUT_S = 30  # seconds after last ping before session expires
MAX_SYNC_BODY_BYTES = int(os.environ.get("DRIVEPULSE_SYNC_MAX_BODY_BYTES", str(100 * 1024 * 1024)))
# Abort pairing after this many failed token attempts. A legitimate client
# submits exactly one /pair request; anything more is a bruteforce attempt.
MAX_FAILED_PAIR_ATTEMPTS = 5

SYNC_ACCESS_OFF = "off"
SYNC_ACCESS_LAN = "lan_only"
SYNC_ACCESS_ANY = "any"


def is_lan_address(ip: str) -> bool:
    """True for private, loopback or link-local addresses (RFC1918 + IPv6 equivalents)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


class SyncServer:
    PORT = 8765

    def __init__(
        self,
        cert_path: Path,
        key_path: Path,
        pairing_token: str,
        session_token: str,
        on_paired_cb: Callable[[dict], None],
        get_export_fn: Callable[[], dict],
        on_import_fn: Callable[[dict], None],
        on_timeout_cb: Callable[[], None] | None = None,
        on_vehicle_check_fn: Callable[[str], bool] | None = None,
        on_share_import_fn: Callable[[dict], dict] | None = None,
        access_mode: str = SYNC_ACCESS_LAN,
    ) -> None:
        self._cert_path = cert_path
        self._key_path = key_path
        self._pairing_token = pairing_token
        self._session_token = session_token
        self._on_paired_cb = on_paired_cb
        self._get_export_fn = get_export_fn
        self._on_import_fn = on_import_fn
        self._on_timeout_cb = on_timeout_cb
        self._on_vehicle_check_fn = on_vehicle_check_fn
        self._on_share_import_fn = on_share_import_fn
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._timeout_timer: threading.Timer | None = None
        self._sync_timer: threading.Timer | None = None
        self._paired = False
        self._cancelled = False
        self._session_expiry: float = 0.0
        self.actual_port: int = self.PORT
        self.last_activity: float = 0.0
        self.last_ping: float = 0.0
        self.pending_sync_mode: str | None = None
        self._pending_share_payload: dict | None = None
        self._pending_share_lock = threading.Lock()
        self._failed_pair_attempts = 0
        self._pair_attempt_lock = threading.Lock()
        if access_mode not in (SYNC_ACCESS_LAN, SYNC_ACCESS_ANY):
            # Caller should refuse to start the server when access is "off";
            # if a bad value slips through, fall back to the safe default.
            access_mode = SYNC_ACCESS_LAN
        self._access_mode = access_mode

    def set_pending_share(self, payload: dict) -> None:
        with self._pending_share_lock:
            self._pending_share_payload = payload

    def take_pending_share(self) -> dict | None:
        with self._pending_share_lock:
            payload = self._pending_share_payload
            self._pending_share_payload = None
            return payload

    def has_pending_share(self) -> bool:
        with self._pending_share_lock:
            return self._pending_share_payload is not None

    def start(self) -> None:
        if self._cancelled:
            return
        log.info("Sync server starting in pid=%s thread=%s", os.getpid(), threading.current_thread().name)
        server = self

        def make_handler(*args: Any, **kwargs: Any) -> _SyncHandler:
            return _SyncHandler(server, *args, **kwargs)

        port = self.PORT
        httpd: HTTPServer | None = None
        for _ in range(10):
            try:
                httpd = HTTPServer(("0.0.0.0", port), make_handler)
                break
            except OSError:
                port += 1
        if httpd is None:
            raise OSError(f"No free port found in range {self.PORT}–{self.PORT + 9}")
        self.actual_port = port
        log.info("Sync server binding on 0.0.0.0:%s (access=%s)", port, self._access_mode)

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_cert_chain(str(self._cert_path), str(self._key_path))
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        self._server = httpd

        def _run() -> None:
            try:
                httpd.serve_forever()
            except OSError:
                log.exception("Sync server loop stopped unexpectedly")

        self._thread = threading.Thread(target=_run, name="sync-server", daemon=True)
        self._thread.start()

        self._start_pairing_timeout()

    def _start_pairing_timeout(self) -> None:
        self._cancel_timeout()
        self._timeout_timer = threading.Timer(PAIRING_TIMEOUT_S, self._on_timeout)
        self._timeout_timer.daemon = True
        self._timeout_timer.start()

    def _cancel_timeout(self) -> None:
        if self._timeout_timer is not None:
            self._timeout_timer.cancel()
            self._timeout_timer = None

    def mark_paired(self) -> None:
        self._paired = True
        self._cancel_timeout()
        self.last_activity = time.time()
        self._session_expiry = time.time() + SYNC_SESSION_TIMEOUT_S
        self._sync_timer = threading.Timer(SYNC_SESSION_TIMEOUT_S, self._on_session_timeout)
        self._sync_timer.daemon = True
        self._sync_timer.start()

    def _cancel_sync_timer(self) -> None:
        if self._sync_timer is not None:
            self._sync_timer.cancel()
            self._sync_timer = None

    def reset_session_timer(self) -> None:
        """Verlängert die Session, solange der Client noch pingt."""
        if not self._paired:
            return
        self._cancel_sync_timer()
        self.last_ping = time.time()
        self.last_activity = self.last_ping
        self._session_expiry = self.last_ping + SYNC_SESSION_TIMEOUT_S
        self._sync_timer = threading.Timer(SYNC_SESSION_TIMEOUT_S, self._on_session_timeout)
        self._sync_timer.daemon = True
        self._sync_timer.start()

    def _on_session_timeout(self) -> None:
        log.info("Sync session expired after %ds — stopping", SYNC_SESSION_TIMEOUT_S)
        self.stop()
        if self._on_timeout_cb:
            self._on_timeout_cb()

    def _on_timeout(self) -> None:
        if not self._paired:
            log.info("Sync server: no pairing within %ds — stopping", PAIRING_TIMEOUT_S)
            self.stop()
            if self._on_timeout_cb:
                self._on_timeout_cb()

    def stop(self) -> None:
        self._cancelled = True
        self._cancel_timeout()
        self._cancel_sync_timer()
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except OSError:
                log.exception("Could not stop sync server")
            self._server = None


class _SyncHandler(BaseHTTPRequestHandler):
    def __init__(self, srv: SyncServer, *args: Any, **kwargs: Any) -> None:
        self._srv = srv
        super().__init__(*args, **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _client_allowed(self) -> bool:
        if self._srv._access_mode == SYNC_ACCESS_ANY:
            return True
        ip = self.client_address[0] if self.client_address else ""
        if is_lan_address(ip):
            return True
        log.warning("Sync request from %s rejected (access=%s)", ip or "?", self._srv._access_mode)
        return False

    def _send_json(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_bearer(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {self._srv._session_token}"):
            return False
        if self._srv._session_expiry and time.time() > self._srv._session_expiry:
            log.warning("Sync request rejected — session expired")
            return False
        return True

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            self._send_json(400, {"ok": False, "error": "bad content length"})
            return None
        if length < 0 or length > MAX_SYNC_BODY_BYTES:
            self._send_json(413, {"ok": False, "error": "payload too large"})
            return None
        return self.rfile.read(length) if length > 0 else b""

    def do_POST(self) -> None:
        if not self._client_allowed():
            self._send_json(403, {"ok": False, "error": "not allowed"})
            return
        if self.path == "/pair":
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"ok": False, "error": "bad json"})
                return
            if not isinstance(data, dict):
                self._send_json(400, {"ok": False, "error": "bad json"})
                return
            if self._srv._paired:
                self._send_json(409, {"ok": False, "error": "already paired"})
                return
            token = data.get("token", "")
            if not hmac.compare_digest(str(token), self._srv._pairing_token):
                with self._srv._pair_attempt_lock:
                    self._srv._failed_pair_attempts += 1
                    attempts = self._srv._failed_pair_attempts
                if attempts >= MAX_FAILED_PAIR_ATTEMPTS:
                    log.warning(
                        "Sync server: aborting after %d failed pairing attempts from %s",
                        attempts,
                        self.client_address[0] if self.client_address else "?",
                    )
                    self._send_json(429, {"ok": False, "error": "too many attempts"})
                    threading.Thread(target=self._srv._on_timeout, daemon=True).start()
                    return
                self._send_json(403, {"ok": False, "error": "invalid token"})
                return
            self._srv.mark_paired()
            device_info = {
                "device_id": data.get("device_id", ""),
                "hostname": data.get("hostname", ""),
                "client_ip": self.client_address[0] if self.client_address else "",
            }
            try:
                self._srv._on_paired_cb(device_info)
            except Exception:
                log.exception("Sync paired callback failed")
            import socket as _socket
            self._send_json(200, {
                "session_token": self._srv._session_token,
                "hostname": _socket.gethostname(),
                "ok": True,
            })
            return

        if self.path == "/sync/import":
            if not self._check_bearer():
                self._send_json(403, {"ok": False, "error": "unauthorized"})
                return
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"ok": False, "error": "bad json"})
                return
            if not isinstance(data, dict):
                self._send_json(400, {"ok": False, "error": "bad json"})
                return
            self._srv.last_activity = time.time()
            self._srv.pending_sync_mode = None
            try:
                self._srv._on_import_fn(data)
            except Exception:
                log.exception("Sync import callback failed")
                self._send_json(500, {"ok": False, "error": "import failed"})
                return
            self._send_json(200, {"ok": True})
            return

        if self.path == "/share/import":
            if not self._check_bearer():
                self._send_json(403, {"ok": False, "error": "unauthorized"})
                return
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"ok": False, "error": "bad json"})
                return
            if not isinstance(data, dict):
                self._send_json(400, {"ok": False, "error": "bad json"})
                return
            self._srv.last_activity = time.time()
            if self._srv._on_share_import_fn is None:
                self._send_json(501, {"ok": False, "error": "not supported"})
                return
            try:
                result = self._srv._on_share_import_fn(data)
            except Exception:
                log.exception("Share import callback failed")
                self._send_json(500, {"ok": False, "error": "import failed"})
                return
            self._send_json(200, result)
            return

        if self.path == "/disconnect":
            if not self._check_bearer():
                self._send_json(403, {"ok": False, "error": "unauthorized"})
                return
            self._send_json(200, {"ok": True})
            threading.Thread(target=self._srv._on_session_timeout, daemon=True).start()
            return

        self._send_json(404, {"ok": False, "error": "not found"})

    def do_GET(self) -> None:
        if not self._client_allowed():
            self._send_json(403, {"ok": False, "error": "not allowed"})
            return
        if self.path == "/ping":
            if not self._check_bearer():
                self._send_json(403, {"ok": False, "error": "unauthorized"})
                return
            self._srv.reset_session_timer()
            self._send_json(200, {
                "ok": True,
                "sync": self._srv.pending_sync_mode,
                "share": self._srv.has_pending_share(),
            })
            return

        if self.path == "/share/pending":
            if not self._check_bearer():
                self._send_json(403, {"ok": False, "error": "unauthorized"})
                return
            payload = self._srv.take_pending_share()
            if payload is None:
                self._send_json(200, {"ok": True, "empty": True})
                return
            self._srv.last_activity = time.time()
            self._send_json(200, {"ok": True, "payload": payload})
            return

        if self.path == "/sync/export":
            if not self._check_bearer():
                self._send_json(403, {"ok": False, "error": "unauthorized"})
                return
            self._srv.last_activity = time.time()
            try:
                payload = self._srv._get_export_fn()
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
                return
            self._send_json(200, payload)
            return

        if self.path.startswith("/share/vehicle_check"):
            if not self._check_bearer():
                self._send_json(403, {"ok": False, "error": "unauthorized"})
                return
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            vin_hash = (qs.get("h") or [""])[0]
            if not vin_hash:
                self._send_json(400, {"ok": False, "error": "missing h"})
                return
            known = False
            if self._srv._on_vehicle_check_fn is not None:
                try:
                    known = bool(self._srv._on_vehicle_check_fn(vin_hash))
                except Exception:
                    log.exception("vehicle_check callback failed")
            self._send_json(200, {"known": known})
            return

        self._send_json(404, {"ok": False, "error": "not found"})
