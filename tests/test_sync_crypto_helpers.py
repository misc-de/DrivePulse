"""Tests for the small entropy/fingerprint helpers in sync_crypto.

The TLS keypair + SPKI flow already has comprehensive tests in
test_sync_robustness; here we cover the token generators and the
URL-safe encoding contract that pairing URLs depend on."""
from __future__ import annotations

import base64

import pytest

pytest.importorskip("cryptography")

from drivepulse_app.sync.crypto import (  # noqa: E402
    generate_device_id,
    generate_token,
)


def test_generate_token_default_length():
    # Default n=32 bytes → ceil(32*4/3) = 43 chars unpadded base64.
    tok = generate_token()
    assert len(tok) == 43


def test_generate_token_respects_n():
    # 16-byte token = 22 chars unpadded base64 url-safe.
    tok = generate_token(n=16)
    assert len(tok) == 22


def test_generate_token_is_url_safe():
    # No "+", no "/", no padding — pairing URLs embed this raw.
    tok = generate_token(n=64)
    assert "+" not in tok
    assert "/" not in tok
    assert "=" not in tok


def test_generate_token_decodes_to_requested_byte_count():
    # The string must round-trip back to the requested number of bytes.
    tok = generate_token(n=24)
    decoded = base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4))
    assert len(decoded) == 24


def test_generate_token_is_random():
    # Two consecutive calls should not collide (negligible probability).
    a = generate_token()
    b = generate_token()
    assert a != b


def test_generate_device_id_fixed_16_byte_payload():
    # device IDs are 16 random bytes → 22 chars unpadded.
    did = generate_device_id()
    assert len(did) == 22
    assert "=" not in did and "+" not in did and "/" not in did


def test_generate_device_id_is_random():
    assert generate_device_id() != generate_device_id()
