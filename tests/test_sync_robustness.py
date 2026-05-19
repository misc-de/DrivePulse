from __future__ import annotations


def test_import_data_ignores_unsupported_payload(tmp_path):
    from drivepulse_app.db import DriveDB
    from drivepulse_app.sync_data import import_data

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
    from drivepulse_app.sync_data import import_data

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


def test_sync_preserves_profile_path_and_deduplicates_vinless_cars(tmp_path):
    from drivepulse_app.db import DriveDB
    from drivepulse_app.sync_data import export_all, import_data

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
    from drivepulse_app.sync_data import import_data

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
    from drivepulse_app.sync_flow import parse_pairing_url

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
    from drivepulse_app.sync_flow import parse_pairing_url

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
    from drivepulse_app.sync_client import SyncClient

    client = SyncClient("127.0.0.1", 8765, "fingerprint", "device")

    assert client.pair("token") is False
    assert client.export_from_server() is None
    assert client.import_to_server({"version": 1, "cars": []}) is False


def test_get_local_ip_uses_timeout_and_fallback(monkeypatch):
    import socket

    from drivepulse_app import sync_crypto

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
    from drivepulse_app import sync_identity

    sync_dir = tmp_path / "sync"
    device_file = sync_dir / "device_id.txt"
    sync_dir.mkdir()
    device_file.write_text("\n", encoding="utf-8")

    monkeypatch.setattr(sync_identity, "SYNC_DIR", sync_dir)
    monkeypatch.setattr(sync_identity, "DEVICE_ID_FILE", device_file)
    monkeypatch.setattr(sync_identity, "generate_device_id", lambda: "new-device")

    assert sync_identity.get_or_create_device_id() == "new-device"
    assert device_file.read_text(encoding="utf-8") == "new-device"


def test_perform_sync_reports_server_import_failure(tmp_path):
    from drivepulse_app.db import DriveDB
    from drivepulse_app.sync_flow import perform_sync

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

    from drivepulse_app.sync_server import SyncServer
    import drivepulse_app.sync_server as sync_server

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

    from drivepulse_app.sync_server import SyncServer
    import drivepulse_app.sync_server as sync_server

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

    from drivepulse_app import sync_server
    from drivepulse_app.sync_server import _SyncHandler

    monkeypatch.setattr(sync_server, "MAX_SYNC_BODY_BYTES", 4)
    handler = _SyncHandler.__new__(_SyncHandler)
    handler.headers = {"Content-Length": "5"}
    handler.rfile = BytesIO(b"12345")
    responses = []
    handler._send_json = lambda code, data: responses.append((code, data))

    assert _SyncHandler._read_body(handler) is None
    assert responses == [(413, {"ok": False, "error": "payload too large"})]

    handler._srv = SimpleNamespace(_session_token="secret")
    handler.headers = {"Authorization": "Bearer secret"}
    assert _SyncHandler._check_bearer(handler) is True


def test_sync_dialog_blocks_server_start_without_user_action(drivepulse_module):
    import threading

    from drivepulse_app.sync_dialog import SyncDialog

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

    from drivepulse_app.sync_dialog import SyncDialog

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


def test_sync_dialog_close_stops_server_and_invalidates_starts(drivepulse_module):
    import threading

    from drivepulse_app.sync_dialog import SyncDialog

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

    SyncDialog._on_closed(dialog)

    assert dialog._closed is True
    assert dialog._server is None
    assert dialog._server_start_requested is False
    assert dialog._server_start_generation == 21
    assert server.stopped is True
