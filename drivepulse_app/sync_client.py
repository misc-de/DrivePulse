from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.request
from typing import Any

from .diagnostics import get_logger
from .sync_crypto import verify_spki_fingerprint


log = get_logger(__name__)


class SyncClient:
    def __init__(self, host: str, port: int, spki_fingerprint: str, device_id: str) -> None:
        self._host = host
        self._port = port
        self._spki_fingerprint = spki_fingerprint
        self._device_id = device_id
        self._session_token: str | None = None
        self._fingerprint_verified = False
        self.server_hostname: str = ""
        self.last_contact: float = 0.0

    def _make_ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _base_url(self) -> str:
        return f"https://{self._host}:{self._port}"

    def verify_fingerprint(self) -> bool:
        try:
            ctx = self._make_ssl_context()
            sock = socket.create_connection((self._host, self._port), timeout=10)
            with ctx.wrap_socket(sock, server_hostname=self._host) as ssock:
                cert_der = ssock.getpeercert(binary_form=True)
            if cert_der is None:
                return False
            self._fingerprint_verified = verify_spki_fingerprint(
                cert_der,
                self._spki_fingerprint,
            )
            return self._fingerprint_verified
        except Exception:
            log.exception("Could not verify sync peer fingerprint for %s:%s", self._host, self._port)
            self._fingerprint_verified = False
            return False

    def pair(self, pairing_token: str) -> bool:
        if not self._fingerprint_verified:
            log.warning("Refusing sync pairing before peer fingerprint was verified")
            return False
        try:
            body = json.dumps({
                "token": pairing_token,
                "device_id": self._device_id,
                "hostname": socket.gethostname(),
            }).encode()
            req = urllib.request.Request(
                f"{self._base_url()}/pair",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            ctx = self._make_ssl_context()
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                data: dict[str, Any] = json.loads(resp.read())
            if data.get("ok"):
                self._session_token = data.get("session_token")
                self.server_hostname = data.get("hostname", "")
                self.last_contact = time.time()
                return True
            return False
        except Exception:
            log.exception("Could not pair with sync peer %s:%s", self._host, self._port)
            return False

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._session_token}"}

    def export_from_server(self) -> dict | None:
        if not self._fingerprint_verified or not self._session_token:
            return None
        try:
            req = urllib.request.Request(
                f"{self._base_url()}/sync/export",
                headers=self._auth_headers(),
                method="GET",
            )
            ctx = self._make_ssl_context()
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                result = json.loads(resp.read())
            self.last_contact = time.time()
            return result
        except Exception:
            log.exception("Could not export data from sync peer %s:%s", self._host, self._port)
            return None

    def import_to_server(self, data: dict) -> bool:
        if not self._fingerprint_verified or not self._session_token:
            return False
        try:
            body = json.dumps(data).encode()
            headers = dict(self._auth_headers())
            headers["Content-Type"] = "application/json"
            req = urllib.request.Request(
                f"{self._base_url()}/sync/import",
                data=body,
                headers=headers,
                method="POST",
            )
            ctx = self._make_ssl_context()
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                result: dict[str, Any] = json.loads(resp.read())
            if result.get("ok"):
                self.last_contact = time.time()
            return bool(result.get("ok"))
        except Exception:
            log.exception("Could not import data to sync peer %s:%s", self._host, self._port)
            return False

    def vehicle_check(self, vin_hash: str) -> bool | None:
        if not self._fingerprint_verified or not self._session_token:
            return None
        try:
            from urllib.parse import quote
            req = urllib.request.Request(
                f"{self._base_url()}/share/vehicle_check?h={quote(vin_hash)}",
                headers=self._auth_headers(),
                method="GET",
            )
            ctx = self._make_ssl_context()
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                data: dict[str, Any] = json.loads(resp.read())
            self.last_contact = time.time()
            return bool(data.get("known"))
        except Exception:
            log.exception("Could not check vehicle with sync peer %s:%s", self._host, self._port)
            return None

    def share_import(self, payload: dict) -> dict | None:
        if not self._fingerprint_verified or not self._session_token:
            return None
        try:
            body = json.dumps(payload).encode()
            headers = dict(self._auth_headers())
            headers["Content-Type"] = "application/json"
            req = urllib.request.Request(
                f"{self._base_url()}/share/import",
                data=body,
                headers=headers,
                method="POST",
            )
            ctx = self._make_ssl_context()
            with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
                result: dict[str, Any] = json.loads(resp.read())
            self.last_contact = time.time()
            return result
        except Exception:
            log.exception("Could not share import to sync peer %s:%s", self._host, self._port)
            return None

    def disconnect(self) -> None:
        """Benachrichtigt den Server dass dieser Client sich trennt."""
        if not self._fingerprint_verified or not self._session_token:
            return
        try:
            req = urllib.request.Request(
                f"{self._base_url()}/disconnect",
                data=b"{}",
                headers={**self._auth_headers(), "Content-Type": "application/json"},
                method="POST",
            )
            ctx = self._make_ssl_context()
            with urllib.request.urlopen(req, context=ctx, timeout=5) as _resp:
                pass
        except Exception:
            log.debug("Could not notify server of disconnect (may already be gone)")
