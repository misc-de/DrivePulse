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


def test_sync_dialog_blocks_server_start_without_user_action(drivepulse_module):
    from drivepulse_app.sync_dialog import SyncDialog

    dialog = SyncDialog.__new__(SyncDialog)
    dialog._server_start_requested = False
    called = []
    dialog._stop_server = lambda: called.append("stop")

    SyncDialog._start_server_mode(dialog)

    assert called == []
