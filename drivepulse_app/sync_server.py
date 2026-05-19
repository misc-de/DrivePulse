from __future__ import annotations

import json
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable

from .diagnostics import get_logger


log = get_logger(__name__)


PAIRING_TIMEOUT_S = 60


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
    ) -> None:
        self._cert_path = cert_path
        self._key_path = key_path
        self._pairing_token = pairing_token
        self._session_token = session_token
        self._on_paired_cb = on_paired_cb
        self._get_export_fn = get_export_fn
        self._on_import_fn = on_import_fn
        self._on_timeout_cb = on_timeout_cb
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._timeout_timer: threading.Timer | None = None
        self._paired = False
        self._cancelled = False
        self.actual_port: int = self.PORT

    def start(self) -> None:
        if self._cancelled:
            return
        log.info(
            "Sync server start requested in pid=%s thread=%s",
            os.getpid(),
            threading.current_thread().name,
            stack_info=True,
        )
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
        log.info("Sync server binding on 0.0.0.0:%s", port)

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(self._cert_path), str(self._key_path))
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        self._server = httpd

        def _run() -> None:
            try:
                httpd.serve_forever()
            except Exception:
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

    def _on_timeout(self) -> None:
        if not self._paired:
            log.info("Sync server: no pairing within %ds — stopping", PAIRING_TIMEOUT_S)
            self.stop()
            if self._on_timeout_cb:
                self._on_timeout_cb()

    def stop(self) -> None:
        self._cancelled = True
        self._cancel_timeout()
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                log.exception("Could not stop sync server")
            self._server = None


class _SyncHandler(BaseHTTPRequestHandler):
    def __init__(self, srv: SyncServer, *args: Any, **kwargs: Any) -> None:
        self._srv = srv
        super().__init__(*args, **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send_json(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_bearer(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {self._srv._session_token}"

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length > 0 else b""

    def do_POST(self) -> None:
        if self.path == "/pair":
            body = self._read_body()
            try:
                data = json.loads(body)
            except Exception:
                self._send_json(400, {"ok": False, "error": "bad json"})
                return
            token = data.get("token", "")
            if token != self._srv._pairing_token:
                self._send_json(403, {"ok": False, "error": "invalid token"})
                return
            self._srv.mark_paired()
            device_info = {
                "device_id": data.get("device_id", ""),
                "hostname": data.get("hostname", ""),
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
            try:
                data = json.loads(body)
            except Exception:
                self._send_json(400, {"ok": False, "error": "bad json"})
                return
            try:
                self._srv._on_import_fn(data)
            except Exception:
                log.exception("Sync import callback failed")
                self._send_json(500, {"ok": False, "error": "import failed"})
                return
            self._send_json(200, {"ok": True})
            return

        self._send_json(404, {"ok": False, "error": "not found"})

    def do_GET(self) -> None:
        if self.path == "/sync/export":
            if not self._check_bearer():
                self._send_json(403, {"ok": False, "error": "unauthorized"})
                return
            try:
                payload = self._srv._get_export_fn()
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
                return
            self._send_json(200, payload)
            return

        self._send_json(404, {"ok": False, "error": "not found"})
