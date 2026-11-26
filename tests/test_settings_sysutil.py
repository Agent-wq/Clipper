import pytest

from clipper.settings import (
    PRESETS, SCHEMA, cast, fmt_clip_t, fmt_t, output_stem, parse_t, srt_t, transcript_cache_name, validate_clip_times,
)
from clipper.sysutil import safe_dirname, split_args


# ───────────── settings schema ─────────────
def all_options():
    return [opt for items in SCHEMA.values() for opt in items]


def test_schema_keys_are_unique():
    keys = [o[0] for o in all_options()]
    assert len(keys) == len(set(keys))


def test_schema_defaults_are_consistent():
    for key, label, kind, default, opts, hint, adv in all_options():
        if kind in ("int", "float"):
            assert opts[0] <= default <= opts[1], f"{key}: default {default} is outside {opts[:2]}"
        if kind == "choice":
            assert str(default) in [str(o) for o in opts], f"{key}: default not in options"
        if kind != "bool":
            assert cast(kind, default, default) == default, f"{key}: default does not survive cast()"


def test_default_clip_lengths_make_sense():
    d = {o[0]: o[3] for o in all_options()}
    assert d["min_len"] < d["max_len"]


def test_presets_only_use_known_keys():
    keys = {o[0] for o in all_options()}
    for name, values in PRESETS.items():
        assert set(values) <= keys, f"preset {name} has unknown keys"


def test_cast():
    assert cast("int", 5, "7.0") == 7
    assert cast("float", 0.4, "0.5") == 0.5
    assert cast("choice", 1080, "720") == 720
    assert cast("choice", "x", " y ") == "y"
    assert cast("bool", True, False) is False


# ───────────── time helpers ─────────────
def test_parse_t_and_fmt_t():
    assert parse_t("90") == 90 and parse_t("1:30") == 90 and parse_t("1:02:03") == 3723
    with pytest.raises(ValueError):
        parse_t("x")
    assert fmt_t(90) == "1:30" and fmt_t(3599) == "59:59"


def test_fmt_clip_t():
    assert fmt_clip_t(65.5) == "1:05.50"
    assert fmt_clip_t(59.999) == "1:00.00"
    assert fmt_clip_t(-3) == "0:00.00"


def test_srt_t():
    assert srt_t(3661.5) == "01:01:01,500"
    assert srt_t(-1) == "00:00:00,000"


def test_validate_clip_times():
    validate_clip_times(10, 50, 100, 35, 60)
    for args in [(-1, 40, 100, 35, 60), (50, 10, 100, 35, 60), (10, 120, 100, 35, 200),
                 (10, 20, 100, 35, 60), (10, 90, 100, 35, 60), (10, float("nan"), 100, 35, 60)]:
        with pytest.raises(ValueError):
            validate_clip_times(*args)


# ───────────── file names and caches ─────────────
def test_transcript_cache_name():
    cfg = {"whisper_model": "small", "language": "", "beam_size": 5, "vad_filter": False, "edit_enabled": True}
    assert transcript_cache_name(cfg) == "segments_small_auto_beam5_vad0_words.json"
    cfg.update(edit_enabled=False, language="en", vad_filter=True)
    assert transcript_cache_name(cfg) == "segments_small_en_beam5_vad1.json"


def test_output_stem_avoids_collisions(tmp_path):
    assert output_stem(tmp_path, "clip", 1) == "clip_1"
    (tmp_path / "clip_1.mp4").write_bytes(b"x")
    assert output_stem(tmp_path, "clip", 1) == "clip_1_2"
    assert output_stem(tmp_path, "my clip!", 3) == "my_clip_3"
    assert output_stem(tmp_path, "!!!", 4) == "clip_4"


def test_safe_dirname():
    assert safe_dirname("CON") == "CON_" and safe_dirname("con") == "con_"
    assert safe_dirname("hello: world?") == "hello world"
    assert safe_dirname("abc...") == "abc"
    assert safe_dirname("../..") == ""
    assert len(safe_dirname("a" * 100)) == 60
    assert safe_dirname("नमस्ते वीडियो") == "नमस्ते वीडियो"  # letters and combining marks are kept


def test_split_args():
    assert split_args("") == []
    assert split_args('--proxy "http://h:1" -x') == ["--proxy", "http://h:1", "-x"]
