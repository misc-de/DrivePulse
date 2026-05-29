"""Tests for the pure/DB-only helpers in the Car Lab UDS-exploration module."""

from types import SimpleNamespace

from drivepulse_app.cars.car_lab import (
    CarsCarLabMixin,
    _hex_snapshot,
    module_icon_name,
)
from drivepulse_app.obd.uds import VAG_CODING_DID

# --- module_icon_name -------------------------------------------------------

def test_module_icon_name_known_module():
    assert module_icon_name("engine") == "dp-ecu-engine-symbolic"


def test_module_icon_name_is_case_and_whitespace_insensitive():
    assert module_icon_name("  Engine ") == "dp-ecu-engine-symbolic"


def test_module_icon_name_unknown_falls_back():
    assert module_icon_name("ecu_7E2") == "dp-ecu-generic-symbolic"
    assert module_icon_name("") == "dp-ecu-generic-symbolic"


# --- _hex_snapshot ----------------------------------------------------------

def test_hex_snapshot_decodes_hex_to_bytes():
    assert _hex_snapshot({0xF190: "0102ff"}) == {0xF190: b"\x01\x02\xff"}


def test_hex_snapshot_skips_unparseable_hex():
    # The odd-length / non-hex entries are dropped, valid ones kept.
    out = _hex_snapshot({1: "ABCD", 2: "xyz", 3: "0"})
    assert out == {1: b"\xab\xcd"}


def test_hex_snapshot_empty():
    assert _hex_snapshot({}) == {}


# --- _carlab_watch_dids (DB-backed, exercised via a stub self) --------------

class _FakeDB:
    def __init__(self, discoveries, data_by_id):
        self._discoveries = discoveries
        self._data_by_id = data_by_id

    def list_discoveries_for_car(self, _car_id):
        return self._discoveries

    def get_discovery_data(self, disc_id):
        return self._data_by_id[disc_id]


def _watch_dids(db, module, car_id=1):
    stub = SimpleNamespace(db=db, _selected_car_id=car_id)
    return CarsCarLabMixin._carlab_watch_dids(stub, module)


def test_watch_dids_without_db_returns_only_coding_did():
    assert _watch_dids(None, "engine") == [VAG_CODING_DID]


def test_watch_dids_merges_discovery_dids_for_matching_module():
    db = _FakeDB(
        discoveries=[{"id": 7, "label": "engine"}],
        data_by_id={7: {"did_responses": {"F190": {"hex": "aa"}, "F1A2": {"hex": "bb"}}}},
    )
    # VAG coding DID plus the two discovered DIDs, sorted ascending.
    assert _watch_dids(db, "engine") == sorted({VAG_CODING_DID, 0xF190, 0xF1A2})


def test_watch_dids_ignores_other_modules_and_entries_without_hex():
    db = _FakeDB(
        discoveries=[
            {"id": 1, "label": "abs"},        # different module -> skipped
            {"id": 2, "label": "engine"},
        ],
        data_by_id={
            1: {"did_responses": {"F100": {"hex": "00"}}},
            2: {"did_responses": {"F190": {"hex": "aa"}, "F1B0": {}, "ZZZZ": {"hex": "cc"}}},
        },
    )
    # Only the engine discovery contributes; F1B0 has no "hex", ZZZZ is not hex.
    assert _watch_dids(db, "engine") == sorted({VAG_CODING_DID, 0xF190})
