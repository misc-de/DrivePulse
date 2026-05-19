"""Pure sync-flow helpers shared by the sync dialog and tests."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from .sync_data import export_all, import_data


@dataclass(frozen=True)
class PairingInfo:
    host: str
    port: int
    spki_fingerprint: str
    pairing_token: str
    expiry: int


def parse_pairing_url(url_text: str, default_port: int, now: float | None = None) -> PairingInfo:
    parsed = urlparse(url_text)
    if parsed.scheme != "drivepulse":
        raise ValueError("Invalid URL scheme")
    params = parse_qs(parsed.query)
    host = (params.get("h") or [""])[0]
    try:
        port = int((params.get("p") or [str(default_port)])[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid port") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Invalid port")
    spki_fp = (params.get("fp") or [""])[0]
    pairing_token = (params.get("t") or [""])[0]
    try:
        expiry = int((params.get("exp") or ["0"])[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid expiry") from exc

    if not host or not spki_fp or not pairing_token:
        raise ValueError("Invalid QR data")
    if expiry and (now if now is not None else time.time()) > expiry:
        raise TimeoutError("QR code expired")
    return PairingInfo(host, port, spki_fp, pairing_token, expiry)


def perform_sync(db: Any, client: Any, mode: str) -> dict[str, int]:
    cars_imported = trips_imported = samples_imported = 0

    def _import_to_server(data: dict) -> None:
        if not client.import_to_server(data):
            raise RuntimeError("Server import failed")

    def _export_from_server(required: bool = False) -> dict | None:
        data = client.export_from_server()
        if required and data is None:
            raise RuntimeError("Server export failed")
        return data

    if mode == "merge":
        server_data = _export_from_server(required=False)
        if server_data:
            result = import_data(db, server_data, mode="merge")
            cars_imported = result["cars_added"] + result["cars_updated"]
            trips_imported = result["trips_added"]
            samples_imported = result["samples_added"]
        local_data = export_all(db)
        local_data["import_mode"] = "merge"
        _import_to_server(local_data)

    elif mode == "remote_wins":
        server_data = _export_from_server(required=True)
        result = import_data(db, server_data or {}, mode="replace")
        cars_imported = result["cars_added"] + result["cars_updated"]
        trips_imported = result["trips_added"]
        samples_imported = result["samples_added"]

    elif mode == "local_wins":
        local_data = export_all(db)
        local_data["import_mode"] = "replace"
        _import_to_server(local_data)

    elif mode == "remote_wins_all":
        server_data = _export_from_server(required=True)
        result = import_data(db, server_data or {}, mode="replace_all")
        cars_imported = result["cars_added"] + result["cars_updated"]
        trips_imported = result["trips_added"]
        samples_imported = result["samples_added"]

    elif mode == "local_wins_all":
        local_data = export_all(db)
        local_data["import_mode"] = "replace_all"
        _import_to_server(local_data)

    else:
        raise RuntimeError(f"Unknown sync mode: {mode}")

    return {
        "cars": cars_imported,
        "trips": trips_imported,
        "samples": samples_imported,
    }
