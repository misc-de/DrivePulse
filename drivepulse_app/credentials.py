"""System-keyring backed storage for the project's API keys / secrets.

Uses libsecret via the gi `Secret` namespace — the standard Linux Secret
Service API that GNOME Keyring and KWallet both implement. Falls back
gracefully to plain settings.json storage when:

  - the `Secret` GIR is not installed (older / minimal systems), or
  - no Secret Service daemon is running (headless / locked-down setups).

In that fallback case the secret stays in settings.json (chmod 0600)
exactly like before — the API simply reports ``available = False``.

Callers should never branch on this themselves: just use ``load`` /
``store`` / ``clear`` and ``settings.json`` will be used iff keyring is
unavailable. The migration helper ``migrate_from_settings`` moves any
plain-text keys it finds into the keyring on first run and zeros them
in the JSON.
"""
from __future__ import annotations

from typing import Any

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)

_SCHEMA_NAME = "de.drivepulse.ApiKeys"
_LABEL_PREFIX = "DrivePulse"

# Maps the settings.json field name → keyring service identifier. We keep
# the dict because not every settings field is a secret, and the
# settings key is what the rest of the app already speaks.
SECRET_FIELDS: dict[str, str] = {
    "autodev_api_key":         "autodev",
    "vindecoder_api_key":      "vindecoder_api",
    "vindecoder_secret_key":   "vindecoder_secret",
}

_secret_mod: Any = None
_schema: Any = None
_probed: bool = False
_available: bool = False


def _probe() -> bool:
    """Lazy-import libsecret and verify the daemon answers a trivial
    lookup. Result is cached for the process lifetime."""
    global _secret_mod, _schema, _probed, _available
    if _probed:
        return _available
    _probed = True
    try:
        import gi
        gi.require_version("Secret", "1")
        from gi.repository import Secret
        _secret_mod = Secret
        _schema = Secret.Schema.new(
            _SCHEMA_NAME,
            Secret.SchemaFlags.NONE,
            {"service": Secret.SchemaAttributeType.STRING},
        )
        # A no-op lookup verifies the daemon is reachable; if no daemon
        # is running this raises GLib.Error and we degrade to plain
        # settings.json storage.
        Secret.password_lookup_sync(_schema, {"service": "__probe__"}, None)
        _available = True
        log.info("libsecret keyring available — secrets will be stored there")
    except Exception as exc:
        log.info("libsecret keyring unavailable (%s) — secrets stay in settings.json", exc)
        _available = False
    return _available


def is_available() -> bool:
    return _probe()


def load(field: str) -> str | None:
    """Read a secret by its settings-field name. Returns None when the
    keyring is unavailable so the caller can fall back to settings.json;
    returns "" when the keyring is reachable but has no entry."""
    if not _probe() or field not in SECRET_FIELDS:
        return None
    service = SECRET_FIELDS[field]
    try:
        value = _secret_mod.password_lookup_sync(_schema, {"service": service}, None)
        return value or ""
    except Exception:
        log.warning("Could not load secret %s from keyring", field, exc_info=True)
        return None


def store(field: str, value: str) -> bool:
    """Write or clear a secret. Empty value triggers a clear so we don't
    keep stale entries around. Returns True on success, False otherwise
    (caller should keep using settings.json in that case)."""
    if not _probe() or field not in SECRET_FIELDS:
        return False
    service = SECRET_FIELDS[field]
    try:
        if not value:
            _secret_mod.password_clear_sync(_schema, {"service": service}, None)
            return True
        return bool(_secret_mod.password_store_sync(
            _schema,
            {"service": service},
            _secret_mod.COLLECTION_DEFAULT,
            f"{_LABEL_PREFIX} {service}",
            value,
            None,
        ))
    except Exception:
        log.warning("Could not store secret %s in keyring", field, exc_info=True)
        return False


def clear(field: str) -> bool:
    return store(field, "")


def migrate_from_settings(settings: dict[str, Any]) -> bool:
    """Move plain-text secrets from settings.json into the keyring.

    Called once at startup right after loading settings.json. When the
    keyring is reachable AND a secret field is non-empty in the dict, we
    write it to the keyring and zero out the in-memory dict entry so the
    next save_settings persists an empty string in the JSON file.

    Returns True if any field was migrated, so the caller knows it
    should flush the updated settings back to disk.
    """
    if not _probe():
        return False
    migrated = False
    for field in SECRET_FIELDS:
        plain = (settings.get(field) or "").strip()
        if not plain:
            continue
        # Don't clobber an existing keyring entry — if both are present,
        # the keyring value wins (it's newer or equal, and the JSON one
        # is a leftover that should be cleaned up anyway).
        existing = load(field)
        if not existing and store(field, plain):
            log.info("Migrated %s from settings.json into the keyring", field)
            migrated = True
        # In both cases (already-in-keyring OR fresh-migration), the
        # JSON copy is removed.
        settings[field] = ""
    return migrated
