"""Shared HTTP client with connection pooling and per-host rate limiting."""
from __future__ import annotations

import logging
import threading
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from drivepulse_app.diagnostics import get_logger, write_diagnostic_log

log = get_logger(__name__)

_UA = "DrivePulse/1.0"
_TIMEOUT = 12

# At most this many concurrent requests to the same hostname
_MAX_PER_HOST = 4

_sem_lock = threading.Lock()
_semaphores: dict[str, threading.Semaphore] = {}


def _host_sem(url: str) -> threading.Semaphore:
    host = urlparse(url).netloc
    with _sem_lock:
        if host not in _semaphores:
            _semaphores[host] = threading.Semaphore(_MAX_PER_HOST)
        return _semaphores[host]


def _make_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = _UA
    retry = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=4,
        pool_maxsize=_MAX_PER_HOST,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_tlocal = threading.local()


def _session() -> requests.Session:
    if not hasattr(_tlocal, "sess"):
        _tlocal.sess = _make_session()
    return _tlocal.sess


def http_get(url: str, timeout: int = _TIMEOUT) -> Any:
    """GET *url* and return parsed JSON, or None on any error."""
    sem = _host_sem(url)
    with sem:
        try:
            resp = _session().get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            log.warning("HTTP GET failed %s — %s", url, exc)
            return None


def http_post(url: str, data: str, timeout: int = 45) -> Any:
    """POST plain-text *data* to *url* and return parsed JSON, or None on error."""
    sem = _host_sem(url)
    with sem:
        try:
            resp = _session().post(
                url,
                data=data.encode(),
                headers={"Content-Type": "text/plain; charset=utf-8"},
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            log.warning("HTTP POST failed %s — %s", url, exc)
            return None


def http_post_json(url: str, payload: Any, timeout: int = 45) -> Any:
    """POST JSON *payload* to *url* and return parsed JSON, or None on error."""
    sem = _host_sem(url)
    with sem:
        try:
            resp = _session().post(url, json=payload, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            err_resp = getattr(exc, "response", None)
            status = getattr(err_resp, "status_code", None)
            body = (getattr(err_resp, "text", "") or "").strip()
            if body:
                write_diagnostic_log(
                    __name__,
                    logging.WARNING,
                    "HTTP POST JSON failed %s status=%s body=%r — %s",
                    url, status, body[:500], exc,
                )
            else:
                write_diagnostic_log(
                    __name__,
                    logging.WARNING,
                    "HTTP POST JSON failed %s — %s",
                    url, exc,
                )
            return None


def http_post_json_result(url: str, payload: Any, timeout: int = 45) -> tuple[Any | None, int | None]:
    """POST JSON and return ``(parsed_json, status_code)``.

    Unlike :func:`http_post_json`, this keeps JSON error bodies available to
    callers that want to branch on service-specific error codes.
    """
    sem = _host_sem(url)
    with sem:
        data = None
        try:
            resp = _session().post(url, json=payload, timeout=timeout)
            status = getattr(resp, "status_code", None)
            try:
                data = resp.json()
            except Exception:
                data = None
            resp.raise_for_status()
            return data, status
        except Exception as exc:
            err_resp = getattr(exc, "response", None)
            status = getattr(err_resp, "status_code", None)
            body = (getattr(err_resp, "text", "") or "").strip()
            if body:
                write_diagnostic_log(
                    __name__,
                    logging.WARNING,
                    "HTTP POST JSON failed %s status=%s body=%r — %s",
                    url, status, body[:500], exc,
                )
            else:
                write_diagnostic_log(
                    __name__,
                    logging.WARNING,
                    "HTTP POST JSON failed %s — %s",
                    url, exc,
                )
            if data is not None:
                return data, status
            return None, status
