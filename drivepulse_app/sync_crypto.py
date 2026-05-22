from __future__ import annotations

import base64
import hashlib
import os
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import datetime

from .diagnostics import get_logger


log = get_logger(__name__)


def generate_tls_keypair(cert_path: Path, key_path: Path) -> None:
    # Always regenerate — each server session gets a fresh ephemeral keypair.
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    # Restrict the sync directory itself so the key never lives in a
    # world-readable directory even briefly. mode=0o700 only applies on POSIX.
    try:
        os.chmod(cert_path.parent, 0o700)
    except OSError:
        pass
    key = ec.generate_private_key(ec.SECP256R1())
    key_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    # Create the key file with restrictive permissions from the start —
    # avoid the window where write_bytes would produce a 0644 file.
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key_bytes)
    finally:
        os.close(fd)
    # Ensure the mode is correct even if the file already existed with
    # a wider mode (e.g. left over from a pre-fix install).
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "drivepulse")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(hours=2))
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def get_spki_fingerprint(cert_path: Path) -> str:
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    spki_der = cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.sha256(spki_der).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def verify_spki_fingerprint(cert_der: bytes, expected_fp: str) -> bool:
    try:
        cert = x509.load_der_x509_certificate(cert_der)
        spki_der = cert.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        digest = hashlib.sha256(spki_der).digest()
        actual = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return actual == expected_fp
    except Exception:
        log.exception("Could not verify sync certificate fingerprint")
        return False


def generate_token(n: int = 32) -> str:
    return base64.urlsafe_b64encode(os.urandom(n)).rstrip(b"=").decode()


def generate_device_id() -> str:
    return base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1.0)
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:
        log.exception("Could not determine local IP address")
        return "127.0.0.1"
