"""Unit tests for the pure persisted-stopwatch-run parsers."""

from drivepulse_app.stopwatch._run_parsing import (
    parse_range_key,
    parse_range_results,
    parse_run_samples,
    parse_target_results,
)

_TARGETS = (30, 50, 100)
_RANGES = ((100, 200),)


# --- parse_range_key --------------------------------------------------------

def test_parse_range_key_tuple_repr():
    assert parse_range_key("(100, 200)") == (100, 200)


def test_parse_range_key_dash_form():
    assert parse_range_key("100-200") == (100, 200)


def test_parse_range_key_comma_form():
    assert parse_range_key("100,200") == (100, 200)


def test_parse_range_key_rejects_no_separator():
    assert parse_range_key("100") is None


def test_parse_range_key_rejects_non_numeric():
    assert parse_range_key("(100, abc)") is None
    assert parse_range_key("garbage") is None


# --- parse_target_results ---------------------------------------------------

def test_target_results_seed_all_targets_as_none():
    assert parse_target_results({}, _TARGETS) == {
        30: {"obd": None, "gps": None},
        50: {"obd": None, "gps": None},
        100: {"obd": None, "gps": None},
    }


def test_target_results_fills_known_targets():
    blob = {"targets": {"100": {"obd": 5.2, "gps": 5.4}}}
    assert parse_target_results(blob, _TARGETS)[100] == {"obd": 5.2, "gps": 5.4}


def test_target_results_ignores_unknown_target_and_bad_types():
    blob = {"targets": {
        "999": {"obd": 1.0},          # not a known target
        "50": {"obd": "bad", "gps": 9.0},  # obd non-numeric -> None
        "30": "not-a-dict",           # non-dict value -> skipped
    }}
    out = parse_target_results(blob, _TARGETS)
    assert 999 not in out
    assert out[50] == {"obd": None, "gps": 9.0}
    assert out[30] == {"obd": None, "gps": None}


# --- parse_range_results ----------------------------------------------------

def test_range_results_accepts_both_key_forms():
    assert parse_range_results({"ranges": {"100-200": {"obd": 7.1}}}, _RANGES)[(100, 200)] == {
        "obd": 7.1, "gps": None,
    }
    assert parse_range_results({"ranges": {"(100, 200)": {"gps": 6.5}}}, _RANGES)[(100, 200)] == {
        "obd": None, "gps": 6.5,
    }


def test_range_results_ignores_unknown_range():
    out = parse_range_results({"ranges": {"50-100": {"obd": 3.0}}}, _RANGES)
    assert out == {(100, 200): {"obd": None, "gps": None}}


# --- parse_run_samples ------------------------------------------------------

def test_run_samples_from_triplet_lists():
    assert parse_run_samples([[1.0, 0.5, 0.1], (2.0, None, 0.2)]) == [
        (1.0, 0.5, 0.1),
        (2.0, None, 0.2),
    ]


def test_run_samples_from_canonical_dict():
    assert parse_run_samples([{"ts": 1.5, "accel_g": 0.4, "lateral_g": 0.05}]) == [
        (1.5, 0.4, 0.05),
    ]


def test_run_samples_from_alternate_dict_keys():
    # elapsed/active_g are the canonical-triplet field names in dict form.
    assert parse_run_samples([{"elapsed": 2.0, "active_g": 0.3}]) == [(2.0, 0.3, 0.0)]


def test_run_samples_skips_rows_without_timestamp():
    assert parse_run_samples([{"accel_g": 0.4}, {"ts": 1.0, "accel_g": 0.2}]) == [(1.0, 0.2, 0.0)]


def test_run_samples_skips_unparseable_and_short_rows():
    assert parse_run_samples([{"ts": "bad"}, [1.0, 2.0], [1.0, "x", 3.0]]) == []
