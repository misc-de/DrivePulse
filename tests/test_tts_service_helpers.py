"""Unit tests for the pure-logic helpers in tts.service.

These cover the parts of the TTS subsystem that *don't* touch a subprocess:
mapping/normalisation helpers, cache-key hashing, setter validation/clamping,
and the latency EMA. The subprocess-driven render/play paths are intentionally
not exercised here — they need real piper/espeak binaries and audio devices."""
from __future__ import annotations

import json

import pytest

from drivepulse_app.tts import service


@pytest.fixture(autouse=True)
def _isolate_module_state(monkeypatch):
    """Snapshot/restore the module-level state every helper mutates so
    individual tests don't bleed into each other."""
    monkeypatch.setattr(service, "_volume_pct", 100, raising=False)
    monkeypatch.setattr(service, "_duck_pct", 50, raising=False)
    monkeypatch.setattr(service, "_duck_pre_ms", 200, raising=False)
    monkeypatch.setattr(service, "_backend", "espeak", raising=False)
    monkeypatch.setattr(service, "_tts_latency_s", 1.0, raising=False)


# ── Language / voice mapping ──────────────────────────────────────────────────


def test_effective_lang_passes_through_known_codes():
    assert service._effective_lang("en") == "en"
    assert service._effective_lang("de") == "de"


def test_effective_lang_falls_back_to_english_for_unknown():
    # Anything not in the supported set must resolve to "en" — that's what the
    # rest of the pipeline assumes when picking espeak voices and piper models.
    assert service._effective_lang("auto") == "en"
    assert service._effective_lang("fr") == "en"
    assert service._effective_lang("") == "en"


def test_espeak_voice_appends_female_suffix_only_for_female():
    # The "+f3" suffix swaps espeak to its third female variant; male gets the
    # bare language code. Getting this wrong silently flips the voice.
    assert service._espeak_voice("de", "female") == "de+f3"
    assert service._espeak_voice("de", "male") == "de"
    assert service._espeak_voice("en", "female") == "en+f3"
    assert service._espeak_voice("fr", "male") == "en"  # falls back via _effective_lang


# ── HuggingFace URL construction ──────────────────────────────────────────────


def test_model_hf_url_constructs_path_from_dashed_name():
    url = service._model_hf_url("de_DE-kerstin-low")
    assert url.endswith("/de/de_DE/kerstin/low/de_DE-kerstin-low.onnx")
    assert url.startswith("https://huggingface.co/")


def test_model_hf_url_with_json_suffix():
    url = service._model_hf_url("en_US-lessac-high", suffix=".json")
    assert url.endswith("/en/en_US/lessac/high/en_US-lessac-high.onnx.json")


# ── Cache-key hashing ─────────────────────────────────────────────────────────


def test_cache_key_is_deterministic_and_unique_per_input():
    base = service._cache_key("Turn left in 200 metres", "en", "female", "high")

    # Same inputs → same key.
    assert service._cache_key("Turn left in 200 metres", "en", "female", "high") == base

    # Any axis change → different key, otherwise we'd play the wrong file from
    # cache (e.g. German audio for an English request).
    assert service._cache_key("Turn right in 200 metres", "en", "female", "high") != base
    assert service._cache_key("Turn left in 200 metres", "de", "female", "high") != base
    assert service._cache_key("Turn left in 200 metres", "en", "male", "high") != base
    assert service._cache_key("Turn left in 200 metres", "en", "female", "low") != base


def test_cache_key_returns_md5_hex_digest():
    key = service._cache_key("x", "en", "female", "high")
    assert len(key) == 32
    assert all(c in "0123456789abcdef" for c in key)


# ── paplay volume scaling ─────────────────────────────────────────────────────


def test_paplay_volume_arg_maps_percent_to_linear_scale(monkeypatch):
    # paplay's --volume runs 0…65536 (100 % = 65536). The mapping is linear,
    # so 50 % → 32768 and 200 % → 131072. Off-by-one drifts here change actual
    # output volume.
    monkeypatch.setattr(service, "_volume_pct", 100)
    assert service._paplay_volume_arg() == "65536"

    monkeypatch.setattr(service, "_volume_pct", 50)
    assert service._paplay_volume_arg() == "32768"

    monkeypatch.setattr(service, "_volume_pct", 200)
    assert service._paplay_volume_arg() == "131072"


def test_paplay_volume_arg_clamps_out_of_range_values(monkeypatch):
    # set_volume_pct already clamps, but _paplay_volume_arg re-clamps as
    # belt-and-braces — verify the floor (1) and ceiling (200) are honoured
    # even if _volume_pct gets a rogue value somehow.
    monkeypatch.setattr(service, "_volume_pct", 0)
    assert service._paplay_volume_arg() == str(int(65536 * 1 / 100))

    monkeypatch.setattr(service, "_volume_pct", 500)
    assert service._paplay_volume_arg() == str(int(65536 * 200 / 100))


# ── Setters: validation / clamping ────────────────────────────────────────────


def test_set_backend_accepts_known_values_and_falls_back_otherwise():
    service.set_backend("piper")
    assert service._backend == "piper"

    service.set_backend("espeak")
    assert service._backend == "espeak"

    # Garbage input must not leak through — silently snap back to espeak so the
    # rest of the pipeline still has a working subprocess to talk to.
    service.set_backend("festival")
    assert service._backend == "espeak"

    service.set_backend("")
    assert service._backend == "espeak"


def test_set_volume_pct_clamps_to_valid_range():
    service.set_volume_pct(150)
    assert service._volume_pct == 150

    service.set_volume_pct(0)
    assert service._volume_pct == 1  # floor

    service.set_volume_pct(9999)
    assert service._volume_pct == 200  # ceiling


def test_set_volume_pct_falls_back_to_100_for_garbage():
    service.set_volume_pct("not-a-number")  # type: ignore[arg-type]
    assert service._volume_pct == 100

    service.set_volume_pct(None)  # type: ignore[arg-type]
    assert service._volume_pct == 100


def test_set_duck_clamps_both_parameters():
    service.set_duck(percent=80, pre_ms=500)
    assert service._duck_pct == 80
    assert service._duck_pre_ms == 500

    # percent ceiling is 90 (90 % is the max sane ducking — fully muting other
    # streams would defeat ducking's purpose vs a hard pause)
    service.set_duck(percent=200, pre_ms=10000)
    assert service._duck_pct == 90
    assert service._duck_pre_ms == 2000

    # percent floor is 0 (disables ducking) and pre_ms floor is 0
    service.set_duck(percent=-50, pre_ms=-100)
    assert service._duck_pct == 0
    assert service._duck_pre_ms == 0


def test_set_duck_handles_garbage_per_parameter_independently():
    # If one arg parses fine but the other doesn't, only the bad one should
    # reset to default — current code resets to 0 for both ducking values.
    service.set_duck(percent="x", pre_ms=300)  # type: ignore[arg-type]
    assert service._duck_pct == 0
    assert service._duck_pre_ms == 300


# ── Latency EMA ───────────────────────────────────────────────────────────────


def test_record_launch_latency_updates_via_30_70_ema(monkeypatch):
    # The EMA formula is _tts_latency_s = 0.3 * new + 0.7 * prior.
    # Starting from 1.0, a new measurement of 2.0 should land at 0.3*2 + 0.7*1 = 1.3.
    monkeypatch.setattr(service, "_tts_latency_s", 1.0)
    service._record_launch_latency(2.0)
    assert service.get_latency_s() == pytest.approx(1.3, abs=1e-9)

    # A second measurement of 2.0 from 1.3 → 0.3*2 + 0.7*1.3 = 1.51.
    service._record_launch_latency(2.0)
    assert service.get_latency_s() == pytest.approx(1.51, abs=1e-9)


def test_record_launch_latency_converges_on_constant_input(monkeypatch):
    # With repeated equal measurements the EMA must converge to that value;
    # 100 iterations is more than enough to push the residual below 1e-6.
    monkeypatch.setattr(service, "_tts_latency_s", 5.0)
    for _ in range(100):
        service._record_launch_latency(0.5)
    assert service.get_latency_s() == pytest.approx(0.5, abs=1e-6)


# ── Piper model path resolution ───────────────────────────────────────────────


def test_piper_model_path_returns_none_for_unmapped_combo(tmp_path, monkeypatch):
    # "auto" is not a real model language — _PIPER_MODELS has no entry for it
    # after _effective_lang resolves to "en", but only when the gender/quality
    # are also valid. Garbage gender shouldn't crash, just return None.
    monkeypatch.setattr(service, "_PIPER_DIRS", [tmp_path])
    assert service._piper_model_path("en", "robot", "high") is None
    assert service._piper_model_path("xx", "female", "ultra") is None


def test_piper_model_path_returns_none_when_file_is_missing(tmp_path, monkeypatch):
    # Mapping resolves to a model name but no .onnx file exists on disk →
    # auto-download will need to fire; this branch must report missing.
    monkeypatch.setattr(service, "_PIPER_DIRS", [tmp_path])
    assert service._piper_model_path("de", "female", "high") is None


def test_piper_model_path_returns_first_dir_that_has_the_file(tmp_path, monkeypatch):
    # Two search dirs; only the second contains the file. The helper must
    # iterate and return the hit instead of stopping at the first miss.
    dir1 = tmp_path / "empty"
    dir1.mkdir()
    dir2 = tmp_path / "filled"
    dir2.mkdir()
    model_file = dir2 / "de_DE-kerstin-low.onnx"
    model_file.write_bytes(b"\x00")

    monkeypatch.setattr(service, "_PIPER_DIRS", [dir1, dir2])
    assert service._piper_model_path("de", "female", "high") == model_file


# ── Piper sample-rate config parsing ──────────────────────────────────────────


def test_piper_sample_rate_reads_from_companion_json(tmp_path):
    onnx = tmp_path / "voice.onnx"
    onnx.write_bytes(b"\x00")
    (tmp_path / "voice.onnx.json").write_text(
        json.dumps({"audio": {"sample_rate": 16000}}), encoding="utf-8"
    )
    assert service._piper_sample_rate(onnx) == 16000


def test_piper_sample_rate_falls_back_to_22050_on_any_error(tmp_path):
    # Missing config file, malformed JSON, missing "audio" key — all three
    # have to land on the default 22050 Hz, never raise.
    missing = tmp_path / "missing.onnx"
    assert service._piper_sample_rate(missing) == 22050

    broken = tmp_path / "broken.onnx"
    broken.write_bytes(b"\x00")
    (tmp_path / "broken.onnx.json").write_text("not json", encoding="utf-8")
    assert service._piper_sample_rate(broken) == 22050

    keyless = tmp_path / "keyless.onnx"
    keyless.write_bytes(b"\x00")
    (tmp_path / "keyless.onnx.json").write_text("{}", encoding="utf-8")
    assert service._piper_sample_rate(keyless) == 22050
