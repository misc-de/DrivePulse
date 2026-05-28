from __future__ import annotations

import pytest


def test_import_data_ignores_unsupported_payload(tmp_path):
    from drivepulse_app.db import DriveDB
    from drivepulse_app.sync.data import import_data

    db = DriveDB(tmp_path / "drivepulse.sqlite3")
    try:
        assert import_data(db, {"version": 999, "cars": []}) == {
            "cars_added": 0,
            "cars_updated": 0,
            "trips_added": 0,
            "samples_added": 0,
        }
    finally:
        db.close()


def test_import_data_skips_malformed_entries(tmp_path):
    from drivepulse_app.db import DriveDB
    from drivepulse_app.sync.data import import_data

    db = DriveDB(tmp_path / "drivepulse.sqlite3")
    try:
        result = import_data(
            db,
            {
                "version": 1,
                "cars": [
                    "bad",
                    {
                        "vin": "ROBUSTVIN",
                        "trips": [
                            "bad-trip",
                            {
                                "started_at": "2026-01-01T00:00:00+00:00",
                                "samples": ["bad-sample", {"ts": 1.0, "speed_kmh": 42}],
                            },
                        ],
                    },
                ],
            },
            mode="unknown",
        )

        assert result["cars_added"] == 1
        assert result["trips_added"] == 1
        assert result["samples_added"] == 1
    finally:
        db.close()


def test_import_data_skips_car_without_identity(tmp_path):
    from drivepulse_app.db import DriveDB
    from drivepulse_app.sync.data import import_data

    db = DriveDB(tmp_path / "drivepulse.sqlite3")
    try:
        result = import_data(
            db,
            {
                "version": 1,
                "cars": [
                    {"label": "No identity", "trips": [{"started_at": "2026-01-01T00:00:00+00:00"}]},
                    {"vin": 123, "profile_path": None},
                ],
            },
        )

        assert result["cars_added"] == 0
        assert db.list_cars() == []
    finally:
        db.close()


def test_import_data_ignores_non_list_collections(tmp_path):
    from drivepulse_app.db import DriveDB
    from drivepulse_app.sync.data import import_data

    db = DriveDB(tmp_path / "drivepulse.sqlite3")
    try:
        result = import_data(db, {"version": 1, "cars": {"vin": "NOT-A-LIST"}})

        assert result == {
            "cars_added": 0,
            "cars_updated": 0,
            "trips_added": 0,
            "samples_added": 0,
            "vin_data_review": [],
        }
    finally:
        db.close()


def test_sync_preserves_profile_path_and_deduplicates_vinless_cars(tmp_path):
    from drivepulse_app.db import DriveDB
    from drivepulse_app.sync.data import export_all, import_data

    source = DriveDB(tmp_path / "source.sqlite3")
    target = DriveDB(tmp_path / "target.sqlite3")
    try:
        source.upsert_car(profile_path="/tmp/profile.json", label="Profile car")
        payload = export_all(source)

        assert payload["cars"][0]["profile_path"] == "/tmp/profile.json"

        first = import_data(target, payload, mode="merge")
        second = import_data(target, payload, mode="merge")

        assert first["cars_added"] == 1
        assert second["cars_added"] == 0
        assert second["cars_updated"] == 1
        assert len(target.list_cars()) == 1
        assert target.list_cars()[0]["profile_path"] == "/tmp/profile.json"
    finally:
        source.close()
        target.close()


def test_import_data_reports_actual_inserted_samples_for_duplicate_timestamps(tmp_path):
    from drivepulse_app.db import DriveDB
    from drivepulse_app.sync.data import import_data

    db = DriveDB(tmp_path / "drivepulse.sqlite3")
    try:
        result = import_data(
            db,
            {
                "version": 1,
                "cars": [
                    {
                        "vin": "DUPETS",
                        "trips": [
                            {
                                "started_at": "2026-01-01T00:00:00+00:00",
                                "samples_count": 99,
                                "samples": [
                                    {"ts": 1.0, "speed_kmh": 10},
                                    {"ts": 1.0, "speed_kmh": 20},
                                    {"ts": 2.0, "speed_kmh": 30},
                                ],
                            },
                        ],
                    },
                ],
            },
            mode="merge",
        )

        car = db.list_cars()[0]
        trip = db.list_trips_for_car(car["id"])[0]

        assert result["samples_added"] == 2
        assert trip["samples_count"] == 2
        assert len(db.samples_for_trip(trip["id"])) == 2
    finally:
        db.close()


def test_parse_pairing_url_validates_expiry():
    from drivepulse_app.sync.flow import parse_pairing_url

    info = parse_pairing_url(
        "drivepulse://pair?v=1&h=192.0.2.10&p=8765&fp=fingerprint&t=token&exp=999",
        default_port=1234,
        now=100,
    )

    assert info.host == "192.0.2.10"
    assert info.port == 8765
    assert info.spki_fingerprint == "fingerprint"
    assert info.pairing_token == "token"

    try:
        parse_pairing_url(
            "drivepulse://pair?v=1&h=192.0.2.10&fp=fingerprint&t=token&exp=99",
            default_port=8765,
            now=100,
        )
    except TimeoutError:
        pass
    else:
        raise AssertionError("expired pairing URL should raise TimeoutError")


def test_parse_pairing_url_rejects_out_of_range_port():
    from drivepulse_app.sync.flow import parse_pairing_url

    for port in ("0", "65536", "-1"):
        try:
            parse_pairing_url(
                f"drivepulse://pair?v=1&h=192.0.2.10&p={port}&fp=fingerprint&t=token&exp=999",
                default_port=8765,
                now=100,
            )
        except ValueError as exc:
            assert "port" in str(exc).lower()
        else:
            raise AssertionError(f"port {port} should be rejected")


def test_sync_client_refuses_pairing_before_fingerprint_verification():
    pytest.importorskip("cryptography")
    from drivepulse_app.sync.client import SyncClient

    client = SyncClient("127.0.0.1", 8765, "fingerprint", "device")

    assert client.pair("token") is False
    assert client.export_from_server() is None
    assert client.import_to_server({"version": 1, "cars": []}) is False


def test_sync_client_rejects_non_object_export_response(monkeypatch):
    import urllib.request

    pytest.importorskip("cryptography")
    from drivepulse_app.sync.client import SyncClient

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"[]"

    client = SyncClient("127.0.0.1", 8765, "fingerprint", "device")
    client._fingerprint_verified = True
    client._session_token = "session"
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())

    assert client.export_from_server() is None


def test_sync_client_handles_invalid_json_response(monkeypatch):
    import urllib.request

    pytest.importorskip("cryptography")
    from drivepulse_app.sync.client import SyncClient

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"{not json"

    client = SyncClient("127.0.0.1", 8765, "fingerprint", "device")
    client._fingerprint_verified = True
    client._session_token = "session"
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())

    assert client.import_to_server({"version": 1, "cars": []}) is False


def test_get_local_ip_uses_timeout_and_fallback(monkeypatch):
    import socket

    pytest.importorskip("cryptography")
    from drivepulse_app.sync import crypto as sync_crypto

    calls = []

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            calls.append(("closed",))

        def settimeout(self, timeout):
            calls.append(("timeout", timeout))

        def connect(self, address):
            calls.append(("connect", address))
            raise OSError("offline")

    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: FakeSocket())

    assert sync_crypto.get_local_ip() == "127.0.0.1"
    assert ("timeout", 1.0) in calls
    assert ("closed",) in calls


def test_sync_identity_replaces_empty_persisted_device_id(monkeypatch, tmp_path):
    pytest.importorskip("cryptography")
    from drivepulse_app.sync import identity as sync_identity

    sync_dir = tmp_path / "sync"
    device_file = sync_dir / "device_id.txt"
    sync_dir.mkdir()
    device_file.write_text("\n", encoding="utf-8")

    monkeypatch.setattr(sync_identity, "SYNC_DIR", sync_dir)
    monkeypatch.setattr(sync_identity, "DEVICE_ID_FILE", device_file)
    monkeypatch.setattr(sync_identity, "generate_device_id", lambda: "new-device")

    assert sync_identity.get_or_create_device_id() == "new-device"
    assert device_file.read_text(encoding="utf-8") == "new-device"


def test_sync_identity_falls_back_when_device_id_cannot_be_persisted(monkeypatch, tmp_path):
    pytest.importorskip("cryptography")
    from drivepulse_app.sync import identity as sync_identity

    sync_dir = tmp_path / "sync"
    monkeypatch.setattr(sync_identity, "SYNC_DIR", sync_dir)
    monkeypatch.setattr(sync_identity, "DEVICE_ID_FILE", sync_dir / "device_id.txt")
    monkeypatch.setattr(sync_identity, "generate_device_id", lambda: "fallback-device")

    def fail_write(*_args, **_kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(sync_identity, "atomic_write_text", fail_write)

    assert sync_identity.get_or_create_device_id() == "fallback-device"


def test_perform_sync_reports_server_import_failure(tmp_path):
    from drivepulse_app.db import DriveDB
    from drivepulse_app.sync.flow import perform_sync

    class Client:
        def export_from_server(self):
            return {"version": 1, "cars": []}

        def import_to_server(self, data):
            return False

    db = DriveDB(tmp_path / "drivepulse.sqlite3")
    try:
        try:
            perform_sync(db, Client(), "merge")
        except RuntimeError as exc:
            assert "Server import failed" in str(exc)
        else:
            raise AssertionError("failed server import should raise RuntimeError")
    finally:
        db.close()


def test_sync_server_stops_after_pairing_timeout(monkeypatch, tmp_path):
    import threading

    import drivepulse_app.sync.server as sync_server
    from drivepulse_app.sync.server import SyncServer

    monkeypatch.setattr(sync_server, "PAIRING_TIMEOUT_S", 0.05)
    timed_out = threading.Event()

    class FakeServer:
        def __init__(self):
            self.shutdown_called = False
            self.close_called = False

        def shutdown(self):
            self.shutdown_called = True

        def server_close(self):
            self.close_called = True

    server = SyncServer(
        tmp_path / "cert.pem",
        tmp_path / "key.pem",
        pairing_token="pair",
        session_token="session",
        on_paired_cb=lambda _info: None,
        get_export_fn=lambda: {"version": 1, "cars": []},
        on_import_fn=lambda _data: None,
        on_timeout_cb=timed_out.set,
    )
    fake = FakeServer()
    server._server = fake

    server._start_pairing_timeout()

    assert timed_out.wait(1.0)
    assert server._server is None
    assert fake.shutdown_called is True
    assert fake.close_called is True


def test_sync_server_pairing_cancels_timeout(monkeypatch, tmp_path):
    import threading

    import drivepulse_app.sync.server as sync_server
    from drivepulse_app.sync.server import SyncServer

    monkeypatch.setattr(sync_server, "PAIRING_TIMEOUT_S", 0.05)
    timed_out = threading.Event()

    server = SyncServer(
        tmp_path / "cert.pem",
        tmp_path / "key.pem",
        pairing_token="pair",
        session_token="session",
        on_paired_cb=lambda _info: None,
        get_export_fn=lambda: {"version": 1, "cars": []},
        on_import_fn=lambda _data: None,
        on_timeout_cb=timed_out.set,
    )

    server._start_pairing_timeout()
    server.mark_paired()

    assert not timed_out.wait(0.15)
    server.stop()


def test_sync_handler_rejects_oversized_body(monkeypatch):
    from io import BytesIO
    from types import SimpleNamespace

    from drivepulse_app.sync import server as sync_server
    from drivepulse_app.sync.server import _SyncHandler

    monkeypatch.setattr(sync_server, "MAX_SYNC_BODY_BYTES", 4)
    handler = _SyncHandler.__new__(_SyncHandler)
    handler.headers = {"Content-Length": "5"}
    handler.rfile = BytesIO(b"12345")
    responses = []
    handler._send_json = lambda code, data: responses.append((code, data))

    assert _SyncHandler._read_body(handler) is None
    assert responses == [(413, {"ok": False, "error": "payload too large"})]

    handler._srv = SimpleNamespace(_session_token="secret", _session_expiry=0)
    handler.headers = {"Authorization": "Bearer secret"}
    assert _SyncHandler._check_bearer(handler) is True


def test_sync_handler_rejects_non_object_pairing_json():
    from io import BytesIO
    from types import SimpleNamespace

    from drivepulse_app.sync.server import _SyncHandler

    handler = _SyncHandler.__new__(_SyncHandler)
    handler.path = "/pair"
    handler.headers = {"Content-Length": "2"}
    handler.rfile = BytesIO(b"[]")
    handler.client_address = ("127.0.0.1", 12345)
    handler._srv = SimpleNamespace(_pairing_token="pair", _access_mode="any")
    responses = []
    handler._send_json = lambda code, data: responses.append((code, data))

    _SyncHandler.do_POST(handler)

    assert responses == [(400, {"ok": False, "error": "bad json"})]


def test_parse_pairing_url_requires_expiry():
    from drivepulse_app.sync.flow import parse_pairing_url

    for url in (
        "drivepulse://pair?v=1&h=192.0.2.10&p=8765&fp=fp&t=tok",
        "drivepulse://pair?v=1&h=192.0.2.10&p=8765&fp=fp&t=tok&exp=",
        "drivepulse://pair?v=1&h=192.0.2.10&p=8765&fp=fp&t=tok&exp=0",
    ):
        try:
            parse_pairing_url(url, default_port=8765, now=100)
        except ValueError:
            pass
        else:
            raise AssertionError(f"URL without valid expiry should be rejected: {url}")


def test_pair_handler_aborts_after_too_many_failed_attempts():
    import json
    import threading
    import time
    from io import BytesIO
    from types import SimpleNamespace

    from drivepulse_app.sync.server import MAX_FAILED_PAIR_ATTEMPTS, _SyncHandler

    timeout_calls = []
    srv = SimpleNamespace(
        _pairing_token="correct",
        _paired=False,
        _failed_pair_attempts=0,
        _pair_attempt_lock=threading.Lock(),
        _on_timeout=lambda: timeout_calls.append(True),
        _access_mode="any",
    )

    responses = []

    def make_handler(body: bytes) -> _SyncHandler:
        h = _SyncHandler.__new__(_SyncHandler)
        h.path = "/pair"
        h.headers = {"Content-Length": str(len(body))}
        h.rfile = BytesIO(body)
        h.client_address = ("127.0.0.1", 12345)
        h._srv = srv
        h._send_json = lambda code, data: responses.append((code, data))
        return h

    bad_body = json.dumps({"token": "wrong"}).encode()
    for _ in range(MAX_FAILED_PAIR_ATTEMPTS - 1):
        _SyncHandler.do_POST(make_handler(bad_body))
    assert all(code == 403 for code, _ in responses)
    assert timeout_calls == []

    _SyncHandler.do_POST(make_handler(bad_body))
    assert responses[-1][0] == 429
    for _ in range(20):
        if timeout_calls:
            break
        time.sleep(0.01)
    assert timeout_calls == [True]


def test_is_lan_address_recognises_private_loopback_and_link_local():
    from drivepulse_app.sync.server import is_lan_address

    for lan in ("127.0.0.1", "10.0.0.5", "172.16.1.1", "172.31.255.254",
                "192.168.1.42", "169.254.10.10", "::1", "fe80::1"):
        assert is_lan_address(lan), lan
    for wan in ("8.8.8.8", "1.1.1.1", "172.32.0.1", "2001:4860:4860::8888", "not-an-ip"):
        assert not is_lan_address(wan), wan


def test_handler_rejects_non_lan_client_in_lan_only_mode():
    import json
    import threading
    from io import BytesIO
    from types import SimpleNamespace

    from drivepulse_app.sync.server import SYNC_ACCESS_LAN, _SyncHandler

    body = json.dumps({"token": "wrong"}).encode()

    def _make(client_ip: str, mode: str = SYNC_ACCESS_LAN) -> tuple[_SyncHandler, list]:
        # _pairing_token differs from the body's token → handler stops at the
        # 403 "invalid token" branch, so we don't need mark_paired etc.
        srv = SimpleNamespace(
            _access_mode=mode,
            _pairing_token="correct",
            _paired=False,
            _failed_pair_attempts=0,
            _pair_attempt_lock=threading.Lock(),
        )
        h = _SyncHandler.__new__(_SyncHandler)
        h.path = "/pair"
        h.headers = {"Content-Length": str(len(body))}
        h.rfile = BytesIO(body)
        h.client_address = (client_ip, 12345)
        h._srv = srv
        responses: list = []
        h._send_json = lambda code, data: responses.append((code, data))
        return h, responses

    h, responses = _make("8.8.8.8")
    _SyncHandler.do_POST(h)
    assert responses == [(403, {"ok": False, "error": "not allowed"})]

    h, responses = _make("192.168.1.10")
    _SyncHandler.do_POST(h)
    assert (403, {"ok": False, "error": "not allowed"}) not in responses

    h, responses = _make("8.8.8.8", mode="any")
    _SyncHandler.do_POST(h)
    assert (403, {"ok": False, "error": "not allowed"}) not in responses


def test_is_valid_device_id_accepts_generated_format_and_rejects_garbage():
    from drivepulse_app.sync.crypto import generate_device_id, is_valid_device_id

    # Anything we generate must round-trip the validator — otherwise our own
    # clients couldn't pair with our own servers.
    for _ in range(10):
        assert is_valid_device_id(generate_device_id())

    # Reject obvious bad inputs.
    assert not is_valid_device_id("")
    assert not is_valid_device_id("a" * 15)        # too short (min 16)
    assert not is_valid_device_id("a" * 65)        # too long (max 64)
    assert not is_valid_device_id("contains spaces in id")
    assert not is_valid_device_id("has/slash/like/path")
    assert not is_valid_device_id("control\x00null")
    assert not is_valid_device_id("control\nlf")
    assert not is_valid_device_id(b"valid_bytes_long_enough_but_bytes")  # type: ignore[arg-type]
    assert not is_valid_device_id(None)            # type: ignore[arg-type]
    assert not is_valid_device_id(12345)           # type: ignore[arg-type]


def test_is_valid_hostname_accepts_reasonable_strings_and_rejects_garbage():
    from drivepulse_app.sync.crypto import is_valid_hostname

    assert is_valid_hostname("")                    # empty allowed
    assert is_valid_hostname("phone-pixel-8")
    assert is_valid_hostname("Höllental.local")     # printable unicode ok
    assert is_valid_hostname("a" * 100)             # at the length ceiling

    assert not is_valid_hostname("a" * 101)         # > 100 chars
    assert not is_valid_hostname("has\nnewline")
    assert not is_valid_hostname("has\ttab")
    assert not is_valid_hostname("has\x00null")
    assert not is_valid_hostname(None)              # type: ignore[arg-type]
    assert not is_valid_hostname(42)                # type: ignore[arg-type]


def test_pair_handler_rejects_malformed_device_id_with_400():
    import json
    import threading
    from io import BytesIO
    from types import SimpleNamespace

    from drivepulse_app.sync.server import _SyncHandler

    paired_called = []
    srv = SimpleNamespace(
        _pairing_token="correct",
        _paired=False,
        _failed_pair_attempts=0,
        _pair_attempt_lock=threading.Lock(),
        _access_mode="any",
        mark_paired=lambda: paired_called.append(True),
    )

    body = json.dumps({
        "token": "correct",
        # Path-traversal-shaped garbage that previously sailed through.
        "device_id": "../../../etc/passwd",
        "hostname": "ok",
    }).encode()
    handler = _SyncHandler.__new__(_SyncHandler)
    handler.path = "/pair"
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    handler.client_address = ("127.0.0.1", 12345)
    handler._srv = srv
    responses = []
    handler._send_json = lambda code, data: responses.append((code, data))

    _SyncHandler.do_POST(handler)

    assert responses == [(400, {"ok": False, "error": "invalid client identity"})]
    # mark_paired must not have fired — otherwise the malformed client would
    # have claimed the session.
    assert paired_called == []


def test_pair_handler_rejects_malformed_hostname_with_400():
    import json
    import threading
    from io import BytesIO
    from types import SimpleNamespace

    from drivepulse_app.sync.crypto import generate_device_id
    from drivepulse_app.sync.server import _SyncHandler

    paired_called = []
    srv = SimpleNamespace(
        _pairing_token="correct",
        _paired=False,
        _failed_pair_attempts=0,
        _pair_attempt_lock=threading.Lock(),
        _access_mode="any",
        mark_paired=lambda: paired_called.append(True),
    )

    body = json.dumps({
        "token": "correct",
        "device_id": generate_device_id(),
        "hostname": "hostname\nwith\nnewlines",
    }).encode()
    handler = _SyncHandler.__new__(_SyncHandler)
    handler.path = "/pair"
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    handler.client_address = ("127.0.0.1", 12345)
    handler._srv = srv
    responses = []
    handler._send_json = lambda code, data: responses.append((code, data))

    _SyncHandler.do_POST(handler)

    assert responses == [(400, {"ok": False, "error": "invalid client identity"})]
    assert paired_called == []


def test_pair_handler_rejects_second_pairing_when_already_paired():
    import json
    import threading
    from io import BytesIO
    from types import SimpleNamespace

    from drivepulse_app.sync.server import _SyncHandler

    srv = SimpleNamespace(
        _pairing_token="correct",
        _paired=True,
        _failed_pair_attempts=0,
        _pair_attempt_lock=threading.Lock(),
        _access_mode="any",
    )

    body = json.dumps({"token": "correct", "device_id": "anything"}).encode()
    handler = _SyncHandler.__new__(_SyncHandler)
    handler.path = "/pair"
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    handler.client_address = ("127.0.0.1", 12345)
    handler._srv = srv
    responses = []
    handler._send_json = lambda code, data: responses.append((code, data))

    _SyncHandler.do_POST(handler)

    assert responses == [(409, {"ok": False, "error": "already paired"})]


def test_qr_scanner_element_available_handles_factory_errors():
    from drivepulse_app.sync import qr_scanner

    class Factory:
        @staticmethod
        def find(_name):
            raise RuntimeError("registry unavailable")

    class Gst:
        ElementFactory = Factory

    assert qr_scanner._element_available(Gst, "zxing") is False


def test_qr_scanner_scan_supported_requires_camera_and_zxing(monkeypatch):
    from drivepulse_app.sync import qr_scanner

    class Factory:
        @staticmethod
        def find(name):
            return object() if name == "autovideosrc" else None

    class Gst:
        ElementFactory = Factory

    monkeypatch.setattr(qr_scanner, "_import_gstreamer", lambda: Gst)

    assert qr_scanner.scan_supported() is False


def test_qr_scanner_stop_pipeline_tolerates_runtime_cleanup_errors():
    from types import SimpleNamespace

    from drivepulse_app.sync.qr_scanner import WebcamQRScanner

    class Bus:
        def remove_signal_watch(self):
            raise RuntimeError("already removed")

    class Pipeline:
        def set_state(self, _state):
            raise RuntimeError("already stopped")

    scanner = WebcamQRScanner.__new__(WebcamQRScanner)
    scanner._timeout_id = None
    scanner._bus = Bus()
    scanner._pipeline = Pipeline()
    scanner._Gst = SimpleNamespace(State=SimpleNamespace(NULL="null"))

    WebcamQRScanner._stop_pipeline(scanner)

    assert scanner._bus is None
    assert scanner._pipeline is None


def test_sync_poller_treats_403_as_reachable(monkeypatch):
    """The reachability poller does not have the session bearer, so /ping
    answers 403. A 403 still proves the peer is up — the poller must report
    "reachable" in that case, not "offline"."""
    import urllib.error
    import urllib.request

    from drivepulse_app.sync.poller import SyncPoller

    def fake_urlopen(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            url="https://x/ping", code=403, msg="forbidden", hdrs=None, fp=None
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    poller = SyncPoller(lambda _: None)
    assert poller._ping("127.0.0.1", 8765) is True


def test_sync_poller_stop_exits_wait_quickly(monkeypatch):
    """Power-saving regression: the poller used to spin in a 0.5 s sleep
    loop, waking the thread 60 times per 30 s window. The new Event.wait
    implementation must (a) actually wait for POLL_INTERVAL_S between polls
    instead of busy-looping, and (b) exit immediately when stop() fires
    mid-wait — no second poll after stop, no multi-second join delay."""
    import threading
    import time

    from drivepulse_app.sync import poller as poller_mod

    monkeypatch.setattr(poller_mod, "POLL_INTERVAL_S", 30)
    poll_calls = []
    started = threading.Event()

    def fake_poll(self):
        poll_calls.append(time.monotonic())
        started.set()

    monkeypatch.setattr(poller_mod.SyncPoller, "_poll", fake_poll)

    poller = poller_mod.SyncPoller(lambda _: None)
    poller.start()
    assert started.wait(1.0), "first poll did not run"

    t0 = time.monotonic()
    poller.stop()
    poller._thread.join(timeout=2.0)
    elapsed = time.monotonic() - t0

    assert not poller._thread.is_alive(), "thread did not exit after stop()"
    assert elapsed < 1.0, f"stop() took {elapsed:.2f}s — Event.wait not honoured"
    # Exactly one poll (the initial one) — the 30 s wait was interrupted by stop.
    assert len(poll_calls) == 1


def test_sync_poller_treats_other_http_errors_as_offline(monkeypatch):
    import urllib.error
    import urllib.request

    from drivepulse_app.sync.poller import SyncPoller

    def fake_urlopen(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            url="https://x/ping", code=500, msg="boom", hdrs=None, fp=None
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    poller = SyncPoller(lambda _: None)
    assert poller._ping("127.0.0.1", 8765) is False


def test_sync_client_pins_cert_for_subsequent_requests(monkeypatch, tmp_path):
    """Regression: verify_fingerprint() used to do a one-time SPKI match and
    leave every subsequent request at ssl.CERT_NONE. A LAN MitM could then
    substitute its own cert after pairing. Now the peer cert is pinned and
    _make_ssl_context() requires CERT_REQUIRED with that cert as the only
    trusted CA."""
    import socket
    import ssl

    pytest.importorskip("cryptography")
    from drivepulse_app.sync.client import SyncClient
    from drivepulse_app.sync.crypto import generate_tls_keypair, get_spki_fingerprint

    # Build a real self-signed cert/key pair so the SSL plumbing has
    # something legitimate to chew on.
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    generate_tls_keypair(cert_path, key_path)
    spki = get_spki_fingerprint(cert_path)
    cert_der_bytes = ssl.PEM_cert_to_DER_cert(cert_path.read_text())

    client = SyncClient("127.0.0.1", 9999, spki, "device-x")

    # Before verify_fingerprint, _make_ssl_context falls back to CERT_NONE
    # — that path is only legitimate for the initial probe.
    ctx = client._make_ssl_context()
    assert ctx.verify_mode == ssl.CERT_NONE
    assert client._pinned_cert_pem is None

    # Now stub verify_fingerprint's network call so it sees our real cert.
    class FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def getpeercert(self, binary_form=False):
            return cert_der_bytes if binary_form else {}

    class FakeContext:
        def wrap_socket(self, sock, server_hostname=None):
            return FakeSock()

    monkeypatch.setattr(ssl, "SSLContext", lambda *_a, **_k: FakeContext())
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: object())

    assert client.verify_fingerprint() is True
    assert client._pinned_cert_pem is not None
    assert "BEGIN CERTIFICATE" in client._pinned_cert_pem


def test_sync_client_make_context_requires_cert_after_pinning(tmp_path):
    """After the cert is pinned, every fresh SSLContext must require it."""
    import ssl

    pytest.importorskip("cryptography")
    from drivepulse_app.sync.client import SyncClient
    from drivepulse_app.sync.crypto import generate_tls_keypair

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    generate_tls_keypair(cert_path, key_path)

    client = SyncClient("127.0.0.1", 9999, "fingerprint", "device-x")
    client._pinned_cert_pem = cert_path.read_text()

    ctx = client._make_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is False
    # The pinned cert should be the only thing trusted: any unrelated cert
    # loaded into a vanilla context would not match.
    stats = ctx.cert_store_stats()
    assert stats["x509_ca"] >= 1


def test_sync_client_failed_verify_clears_pinned_cert(monkeypatch, tmp_path):
    """If a re-verification ever fails (wrong SPKI), the previously pinned
    cert must be dropped so we don't keep talking to an attacker's session
    state."""
    import socket
    import ssl

    pytest.importorskip("cryptography")
    from drivepulse_app.sync.client import SyncClient
    from drivepulse_app.sync.crypto import generate_tls_keypair

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    generate_tls_keypair(cert_path, key_path)

    client = SyncClient("127.0.0.1", 9999, "wrong-fingerprint", "device-x")
    client._pinned_cert_pem = "stale"

    class FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def getpeercert(self, binary_form=False):
            return ssl.PEM_cert_to_DER_cert(cert_path.read_text())

    class FakeContext:
        def wrap_socket(self, sock, server_hostname=None):
            return FakeSock()

    monkeypatch.setattr(ssl, "SSLContext", lambda *_a, **_k: FakeContext())
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: object())

    assert client.verify_fingerprint() is False
    assert client._pinned_cert_pem is None


def test_generate_tls_keypair_writes_key_with_0600_mode(tmp_path):
    """The ephemeral TLS private key holds the server's identity. It must
    never be world-readable, even momentarily — other users on the system
    could MitM the sync session by impersonating the server."""
    import os
    import stat

    pytest.importorskip("cryptography")
    from drivepulse_app.sync.crypto import generate_tls_keypair

    cert_path = tmp_path / "sync" / "cert.pem"
    key_path = tmp_path / "sync" / "key.pem"

    generate_tls_keypair(cert_path, key_path)

    key_mode = stat.S_IMODE(os.stat(key_path).st_mode)
    assert key_mode == 0o600, f"expected 0o600 on key.pem, got 0o{key_mode:o}"
    # Cert is public; we don't pin its mode.

    dir_mode = stat.S_IMODE(os.stat(key_path.parent).st_mode)
    assert dir_mode == 0o700, f"expected 0o700 on sync/, got 0o{dir_mode:o}"


def test_generate_tls_keypair_overwrites_loose_permissions(tmp_path):
    """If the file already existed from a pre-fix install with 0644,
    the regeneration path must tighten it down to 0600."""
    import os
    import stat

    pytest.importorskip("cryptography")
    from drivepulse_app.sync.crypto import generate_tls_keypair

    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    key_path = sync_dir / "key.pem"
    cert_path = sync_dir / "cert.pem"
    # Simulate a pre-fix file left at 0644.
    key_path.write_bytes(b"stale")
    os.chmod(key_path, 0o644)

    generate_tls_keypair(cert_path, key_path)

    key_mode = stat.S_IMODE(os.stat(key_path).st_mode)
    assert key_mode == 0o600


def test_sync_handler_ping_rejects_unauthenticated_caller():
    """An unauthenticated /ping must not reset the session timer or leak state.

    Regression: prior to this fix, /ping was reachable without the bearer token,
    so anyone on the LAN could keep the post-pairing session alive indefinitely
    by spamming pings.
    """
    from types import SimpleNamespace

    from drivepulse_app.sync.server import _SyncHandler

    reset_calls = []
    srv = SimpleNamespace(
        _session_token="secret",
        _session_expiry=0,
        reset_session_timer=lambda: reset_calls.append(1),
        pending_sync_mode=None,
        has_pending_share=lambda: False,
        _access_mode="any",
    )
    handler = _SyncHandler.__new__(_SyncHandler)
    handler._srv = srv
    handler.path = "/ping"
    handler.client_address = ("127.0.0.1", 12345)
    handler.headers = {}  # no Authorization
    responses = []
    handler._send_json = lambda code, data: responses.append((code, data))

    _SyncHandler.do_GET(handler)

    assert responses == [(403, {"ok": False, "error": "unauthorized"})]
    assert reset_calls == []

    # With the correct bearer, /ping succeeds and resets the session timer.
    handler.headers = {"Authorization": "Bearer secret"}
    responses.clear()
    _SyncHandler.do_GET(handler)

    assert reset_calls == [1]
    assert responses and responses[0][0] == 200
    assert responses[0][1]["ok"] is True


def test_sync_dialog_blocks_server_start_without_user_action(drivepulse_module):
    import threading

    pytest.importorskip("cryptography")
    from drivepulse_app.sync.dialog import SyncDialog

    dialog = SyncDialog.__new__(SyncDialog)
    dialog._server_lock = threading.RLock()
    dialog._closed = False
    dialog._server_start_generation = 0
    dialog._server_start_requested = False
    called = []
    dialog._stop_server = lambda: called.append("stop")

    SyncDialog._start_server_mode(dialog)

    assert called == []


def test_sync_dialog_stop_invalidates_pending_server_start(drivepulse_module):
    import threading

    pytest.importorskip("cryptography")
    from drivepulse_app.sync.dialog import SyncDialog

    dialog = SyncDialog.__new__(SyncDialog)
    dialog._server_lock = threading.RLock()
    dialog._closed = False
    dialog._server_start_generation = 10
    dialog._server_start_requested = True
    dialog._server = None

    SyncDialog._stop_server(dialog)

    assert dialog._server_start_requested is False
    assert dialog._server_start_generation == 11
    assert SyncDialog._server_start_is_current(dialog, 10) is False


def test_sync_dialog_stop_handles_runtime_stop_error(drivepulse_module):
    import threading

    pytest.importorskip("cryptography")
    from drivepulse_app.sync.dialog import SyncDialog

    class BrokenServer:
        def stop(self):
            raise RuntimeError("already stopped")

    dialog = SyncDialog.__new__(SyncDialog)
    dialog._server_lock = threading.RLock()
    dialog._server_start_generation = 2
    dialog._server_start_requested = True
    dialog._server = BrokenServer()

    SyncDialog._stop_server(dialog)

    assert dialog._server is None
    assert dialog._server_start_requested is False
    assert dialog._server_start_generation == 3


def test_sync_dialog_cancel_scanner_handles_runtime_cancel_error(drivepulse_module):
    pytest.importorskip("cryptography")
    from drivepulse_app.sync.dialog import SyncDialog

    class BrokenScanner:
        def cancel(self):
            raise RuntimeError("camera already closed")

    dialog = SyncDialog.__new__(SyncDialog)
    dialog._scanner = BrokenScanner()

    SyncDialog._cancel_scanner(dialog)

    assert dialog._scanner is None


def test_sync_dialog_close_stops_server_and_invalidates_starts(drivepulse_module):
    import threading

    pytest.importorskip("cryptography")
    from drivepulse_app.sync.dialog import SyncDialog

    class FakeServer:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    server = FakeServer()
    dialog = SyncDialog.__new__(SyncDialog)
    dialog._server_lock = threading.RLock()
    dialog._closed = False
    dialog._server_start_generation = 20
    dialog._server_start_requested = True
    dialog._server = server
    dialog._scanner = None
    dialog._pushing_subpage = False
    dialog._server_survived_dialog = False

    SyncDialog._on_hiding(dialog)

    assert dialog._closed is True
    assert dialog._server is None
    assert dialog._server_start_requested is False
    assert dialog._server_start_generation == 21
    assert server.stopped is True
