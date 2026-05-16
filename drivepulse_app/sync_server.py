from __future__ import annotations

import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable


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
    ) -> None:
        self._cert_path = cert_path
        self._key_path = key_path
        self._pairing_token = pairing_token
        self._session_token = session_token
        self._on_paired_cb = on_paired_cb
        self._get_export_fn = get_export_fn
        self._on_import_fn = on_import_fn
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._paired = False

    def start(self) -> None:
        server = self

        def make_handler(*args: Any, **kwargs: Any) -> _SyncHandler:
            return _SyncHandler(server, *args, **kwargs)

        httpd = HTTPServer(("0.0.0.0", self.PORT), make_handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(self._cert_path), str(self._key_path))
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        self._server = httpd

        def _run() -> None:
            try:
                httpd.serve_forever()
            except Exception:
                pass

        self._thread = threading.Thread(target=_run, name="sync-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass
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
            self._srv._paired = True
            device_info = {"device_id": data.get("device_id", "")}
            try:
                self._srv._on_paired_cb(device_info)
            except Exception:
                pass
            self._send_json(200, {"session_token": self._srv._session_token, "ok": True})
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
                pass
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
