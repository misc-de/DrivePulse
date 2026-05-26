"""Tests for the keyring-backed credentials helper. libsecret isn't
available in the test environment (the gi.repository stub doesn't ship a
`Secret` namespace), so these tests focus on the fallback path: load()
returns None, store() returns False, migration is a no-op. The actual
libsecret path is exercised at runtime — see the manual smoke check in
the commit message."""
from __future__ import annotations

from drivepulse_app import credentials


def test_secret_fields_cover_expected_settings_keys():
    # If anyone adds a new secret field to settings, this guards that
    # they remember to register it for keyring routing.
    assert set(credentials.SECRET_FIELDS) == {
        "autodev_api_key",
        "vindecoder_api_key",
        "vindecoder_secret_key",
    }


def test_load_returns_none_when_keyring_unavailable():
    # Without libsecret the helper must signal "not available" by
    # returning None — callers fall back to settings.json values.
    assert credentials.load("autodev_api_key") is None


def test_store_returns_false_when_keyring_unavailable():
    # Same contract on the write side: callers keep storing in JSON.
    assert credentials.store("autodev_api_key", "anything") is False


def test_load_unknown_field_returns_none():
    # Defensive — random field names must not crash.
    assert credentials.load("not_a_secret_field") is None


def test_store_unknown_field_returns_false():
    assert credentials.store("not_a_secret_field", "x") is False


def test_migrate_noop_without_keyring():
    settings = {
        "autodev_api_key": "abc",
        "vindecoder_api_key": "def",
        "vindecoder_secret_key": "ghi",
        "some_other_field": "value",
    }
    migrated = credentials.migrate_from_settings(settings)
    assert migrated is False
    # Without keyring the values must stay put — migration not happening.
    assert settings["autodev_api_key"] == "abc"
    assert settings["vindecoder_api_key"] == "def"
    assert settings["vindecoder_secret_key"] == "ghi"
    assert settings["some_other_field"] == "value"
