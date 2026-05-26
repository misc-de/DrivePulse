"""Tests for the pure helpers in vin_api: value cleaning, source merging,
and internal-key stripping. The network-bound _fetch_* functions are not
exercised here — they're integration points already covered indirectly by
test_vin_api and would require live API access otherwise."""
from __future__ import annotations

from drivepulse_app.vin.api import (
    _clean,
    _parse_autodev_usage,
    merge_sources,
    strip_source_keys,
)

# ─── _clean: normalise empty-ish strings ─────────────────────────────────────

def test_clean_strips_whitespace():
    assert _clean("  Volkswagen  ") == "Volkswagen"


def test_clean_returns_empty_for_skip_values():
    # The NHTSA / decoder APIs return "Not Applicable", "N/A", "—" etc.
    # for unknown fields. We want to drop those rather than show them.
    assert _clean("Not Applicable") == ""
    assert _clean("n/a") == ""
    assert _clean("N/A") == ""
    assert _clean("—") == ""
    assert _clean("none") == ""
    assert _clean("Null") == ""
    assert _clean("") == ""


def test_clean_keeps_meaningful_text():
    assert _clean("Diesel") == "Diesel"
    assert _clean("2.0") == "2.0"


def test_clean_coerces_non_string():
    # Real-world JSON sometimes returns numbers — _clean must still cope.
    assert _clean(2024) == "2024"
    assert _clean(2.0) == "2.0"


# ─── merge_sources: last-write-wins priority ─────────────────────────────────

def test_merge_sources_combines_all_keys():
    sources = {
        "NHTSA":    {"make": "VOLKSWAGEN", "year": "1999"},
        "auto.dev": {"make": "Volkswagen", "model": "Golf"},
    }
    out = merge_sources(sources)
    assert out["year"] == "1999"     # only NHTSA had it
    assert out["model"] == "Golf"    # only auto.dev had it


def test_merge_sources_last_write_wins_within_dict_order():
    # Iteration over the sources dict is insertion-ordered (3.7+), so
    # later sources overwrite earlier ones. Validate that the contract
    # described in the docstring actually holds.
    sources = {
        "NHTSA":         {"make": "VOLKSWAGEN"},
        "vindecoder.eu": {"make": "Volkswagen"},
    }
    out = merge_sources(sources)
    assert out["make"] == "Volkswagen"


def test_merge_sources_skips_falsy_values():
    # Empty values from one source must not overwrite the meaningful value
    # from an earlier one.
    sources = {
        "NHTSA":         {"make": "Volkswagen"},
        "vindecoder.eu": {"make": ""},
    }
    out = merge_sources(sources)
    assert out["make"] == "Volkswagen"


def test_merge_sources_empty_input_returns_empty_dict():
    assert merge_sources({}) == {}


# ─── strip_source_keys: drop internal scratch keys ───────────────────────────

def test_strip_source_keys_removes_underscore_src_keys():
    data = {
        "make": "VW",
        "_src_nhtsa": "anything",
        "_src_autodev_raw": {"a": 1},
        "year": "2024",
    }
    out = strip_source_keys(data)
    assert out == {"make": "VW", "year": "2024"}


def test_strip_source_keys_passes_through_unrelated_keys():
    data = {"a": 1, "b_src_": 2, "src_x": 3}  # only "_src_*" prefix is stripped
    assert strip_source_keys(data) == data


def test_strip_source_keys_empty_dict():
    assert strip_source_keys({}) == {}


# ─── _parse_autodev_usage: live quota headers + plan ─────────────────────────


class _FakeHeaders:
    """Minimal stand-in for http.client.HTTPMessage — only .get() is used."""

    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    def get(self, key: str) -> str | None:
        return self._data.get(key)


def test_parse_autodev_usage_free_tier_starter():
    # Real-world response captured from auto.dev — X-Usage-* headers plus
    # user.plan in the JSON body.
    headers = _FakeHeaders({
        "X-Usage-Limit": "1000",
        "X-Usage-Remaining": "991",
        "X-Usage-Used": "9",
    })
    body = {"user": {"plan": "Starter"}}
    out = _parse_autodev_usage(headers, body)
    assert out["limit"] == 1000
    assert out["used"] == 9
    assert out["remaining"] == 991
    assert out["paid"] == 0      # within the 1000-cap → nothing billed yet
    assert out["plan"] == "Starter"


def test_parse_autodev_usage_paid_overage():
    # 5500 used against a 1000 limit → 4500 paid requests.
    headers = _FakeHeaders({
        "X-Usage-Limit": "1000",
        "X-Usage-Used": "5500",
        "X-Usage-Remaining": "0",
    })
    out = _parse_autodev_usage(headers, {"user": {"plan": "Starter"}})
    assert out["paid"] == 4500


def test_parse_autodev_usage_missing_headers_returns_empty():
    # When the response has no usage headers we don't fabricate numbers.
    out = _parse_autodev_usage(_FakeHeaders({}), {})
    assert out == {}


def test_parse_autodev_usage_none_headers_is_safe():
    # Failed call before headers were available — used for the local
    # fallback path; must not crash.
    assert _parse_autodev_usage(None, None) == {}
