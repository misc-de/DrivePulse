"""Tests for the pure/DB-only helpers in the Car Lab UDS-exploration module."""

from types import SimpleNamespace

from drivepulse_app.cars.car_lab import (
    CarsCarLabMixin,
    _hex_snapshot,
    module_icon_name,
)
from drivepulse_app.obd.uds import VAG_CODING_DID

# --- headless widget-tree helpers -------------------------------------------


def _texts(widget):
    """Recursively collect every label/text string in a stub widget tree."""
    out = []
    text = getattr(widget, "text", None)
    if isinstance(text, str) and text:
        out.append(text)
    nodes = list(getattr(widget, "children", []) or [])
    # Buttons/list rows attach their content via set_child -> props["child"].
    child_prop = getattr(widget, "props", {}).get("child")
    if child_prop is not None:
        nodes.append(child_prop)
    for child in nodes:
        # _Stack stores (child, name, title) tuples; _Widget stores widgets.
        node = child[0] if isinstance(child, tuple) else child
        if hasattr(node, "children") or hasattr(node, "text") or hasattr(node, "props"):
            out.extend(_texts(node))
    return out

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


# --- headless builder smoke tests (Discover + Saved) ------------------------
#
# Build the Discover and Saved widget trees against the conftest GTK stubs with
# DB methods stubbed, to validate the tree assembles and shows the expected
# content. CarsPage state is minimal (only the attributes the two builders
# touch), so these exercise the new grouping/scan-list layout without a window.


class _ScanDB:
    """Stub DB exposing only the methods the Discover/Saved builders read."""

    def __init__(self, modules=(), discoveries=(), data_by_id=None):
        self._modules = list(modules)
        self._discoveries = list(discoveries)
        self._data_by_id = data_by_id or {}

    def list_scanned_modules_for_car(self, _car_id):
        return self._modules

    def list_discoveries_for_car(self, _car_id):
        return self._discoveries

    def get_discovery_data(self, disc_id):
        return self._data_by_id.get(disc_id, {})


def _make_lab(db):
    """A concrete CarsCarLabMixin instance with the minimal page state."""

    class _Lab(CarsCarLabMixin):
        pass

    lab = _Lab()
    lab.language = "en"
    lab.db = db
    lab._selected_car_id = 1
    lab.mock_mode = False
    lab.on_carlab_scan = lambda on_done: None
    lab.on_carlab_discover = lambda tx, rx, done: None
    # The stub boxes don't model get_first_child()/remove(); a fresh build has
    # nothing to clear, so make the clear helper a no-op for these tests.
    lab._cl_clear = lambda _box: None  # type: ignore[method-assign]
    return lab


def test_build_discover_without_scan_shows_full_scan_and_hint():
    lab = _make_lab(_ScanDB(modules=[]))
    texts = _texts(lab._build_discover())
    # The "Start complete scan" button is always present …
    assert any("complete scan" in t.lower() for t in texts)
    # … and with no scanned modules the needs-scan hint is shown.
    assert any("appear here" in t.lower() for t in texts)


def test_build_discover_lists_known_modules():
    db = _ScanDB(modules=[
        {"name": "engine", "tx": "7E0", "rx": "7E8"},
        {"name": "abs", "tx": "713", "rx": "77D"},
    ])
    lab = _make_lab(db)
    texts = _texts(lab._build_discover())
    assert any("complete scan" in t.lower() for t in texts)
    # Both confirmed-present modules appear as clickable rows.
    assert "engine" in texts
    assert "abs" in texts
    # tx/rx caption is rendered for at least one module.
    assert any("tx=7E0" in t for t in texts)


def test_build_saved_groups_by_module_newest_first():
    # Newest-first input (as list_discoveries_for_car returns).
    discoveries = [
        {"id": 3, "label": "engine", "created_at": "2026-05-03T00:00:00Z"},
        {"id": 2, "label": "abs", "created_at": "2026-05-02T00:00:00Z"},
        {"id": 1, "label": "engine", "created_at": "2026-05-01T00:00:00Z"},
    ]
    lab = _make_lab(_ScanDB(discoveries=discoveries))
    box = lab._build_discoveries_list()
    texts = _texts(box)
    # Each module gets a section header (icon before the name); the engine
    # group comes first (its newest discovery is more recent than abs's).
    assert texts.index("engine") < texts.index("abs")
    # Every discovery's timestamp is listed, and engine's two are grouped
    # before the abs section (newest-first within the group).
    assert "2026-05-03T00:00:00Z" in texts
    assert "2026-05-01T00:00:00Z" in texts
    assert "2026-05-02T00:00:00Z" in texts
    assert texts.index("2026-05-03T00:00:00Z") < texts.index("abs")
    assert texts.index("2026-05-01T00:00:00Z") < texts.index("abs")


def test_build_saved_empty_shows_placeholder():
    lab = _make_lab(_ScanDB(discoveries=[]))
    texts = _texts(lab._build_discoveries_list())
    assert any("no discoveries" in t.lower() for t in texts)


def test_discovery_content_wraps_long_values():
    long_hex = "DE" * 60
    data = {
        "module": "engine", "tx": "7E0", "rx": "7E8",
        "identification": {"VIN": {"ascii": "WAUZZZ" * 10}},
        "did_responses": {"F190": {"hex": long_hex}},
    }
    lab = _make_lab(_ScanDB())
    box = lab._build_discovery_content(data)
    texts = _texts(box)
    assert any(long_hex in t for t in texts)
    assert any("WAUZZZ" in t for t in texts)
