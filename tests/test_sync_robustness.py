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
