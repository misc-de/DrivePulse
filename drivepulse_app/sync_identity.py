"""Persistent identity paths for local sync pairing."""
from __future__ import annotations

from pathlib import Path

from .common import LOG_DIR
from .diagnostics import atomic_write_text, get_logger
from .sync_crypto import generate_device_id


log = get_logger(__name__)
SYNC_DIR = LOG_DIR / "sync"
CERT_PATH = SYNC_DIR / "cert.pem"
KEY_PATH = SYNC_DIR / "key.pem"
QR_TMP = Path("/tmp/drivepulse_pair.png")
DEVICE_ID_FILE = SYNC_DIR / "device_id.txt"


def get_or_create_device_id() -> str:
    try:
        SYNC_DIR.mkdir(parents=True, exist_ok=True)
        if DEVICE_ID_FILE.exists():
            device_id = DEVICE_ID_FILE.read_text().strip()
            if device_id:
                return device_id
        device_id = generate_device_id()
        atomic_write_text(DEVICE_ID_FILE, device_id, mode=0o600)
        return device_id
    except Exception:
        log.exception("Could not load or persist sync device id")
        return generate_device_id()
