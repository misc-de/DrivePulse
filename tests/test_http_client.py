"""Tests for the shared HTTP client: per-host semaphore caching and
defensive failure handling in http_get. The module backs every external
API call (geocoding, traffic, updater, VIN), so a regression here causes
silent network silence rather than visible errors."""
from __future__ import annotations

import requests

from drivepulse_app import http_client


def test_host_sem_caches_per_host():
    """Two URLs on the same host share one semaphore; different hosts don't."""
    s_a1 = http_client._host_sem("https://api.example.com/foo")
    s_a2 = http_client._host_sem("https://api.example.com/bar")
    s_b = http_client._host_sem("https://other.example.org/baz")
    assert s_a1 is s_a2
    assert s_a1 is not s_b


def test_http_get_returns_none_on_request_exception(monkeypatch):
    """Network errors must surface as None — callers fan out to fallbacks
    (Nominatim → Photon, Valhalla → OSRM, etc.) and rely on this contract."""
    class _BrokenSession:
        def get(self, *_args, **_kwargs):
            raise requests.exceptions.ConnectionError("simulated")

    monkeypatch.setattr(http_client, "_session", lambda: _BrokenSession())
    assert http_client.http_get("https://api.example.com/anything") is None


def test_http_get_returns_none_on_http_error_status(monkeypatch):
    """4xx/5xx responses come back as None; raise_for_status fires inside."""
    class _Resp:
        def raise_for_status(self):
            raise requests.exceptions.HTTPError("500")
        def json(self):
            return {}

    class _Session:
        def get(self, *_args, **_kwargs):
            return _Resp()

    monkeypatch.setattr(http_client, "_session", lambda: _Session())
    assert http_client.http_get("https://api.example.com/x") is None


def test_http_get_returns_parsed_json_on_success(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass
        def json(self):
            return {"ok": True, "data": [1, 2, 3]}

    class _Session:
        def get(self, *_args, **_kwargs):
            return _Resp()

    monkeypatch.setattr(http_client, "_session", lambda: _Session())
    out = http_client.http_get("https://api.example.com/x")
    assert out == {"ok": True, "data": [1, 2, 3]}


def test_http_get_returns_none_when_json_parse_fails(monkeypatch):
    """Server returned 200 but body wasn't JSON — surface as None rather
    than letting a ValueError escape upward."""
    class _Resp:
        def raise_for_status(self):
            pass
        def json(self):
            raise ValueError("not JSON")

    class _Session:
        def get(self, *_args, **_kwargs):
            return _Resp()

    monkeypatch.setattr(http_client, "_session", lambda: _Session())
    assert http_client.http_get("https://api.example.com/x") is None


def test_http_get_passes_timeout_through_to_session(monkeypatch):
    seen: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass
        def json(self):
            return None

    class _Session:
        def get(self, url, **kwargs):
            seen["url"] = url
            seen["timeout"] = kwargs.get("timeout")
            return _Resp()

    monkeypatch.setattr(http_client, "_session", lambda: _Session())
    http_client.http_get("https://api.example.com/x", timeout=7)
    assert seen["timeout"] == 7
