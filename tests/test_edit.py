import pytest

from clipper.edit import (
    ass_col, ass_t, ass_text, build_ass, cap_place, caption_chunks, clip_words, fit_filter, hexcol, make_plan,
    overlay_text, split_filter, apply_caption_edit, clip_words_edited,
)

PLAN_CFG = {"edit_style": "punchy", "edit_zoom": True, "edit_pan": True, "edit_text": True}


def make_words(n=60):
    # one word every 0.5 s, a sentence end every 10 words
    return [(i * 0.5, i * 0.5 + 0.4, "word." if i % 10 == 9 else "word") for i in range(n)]


# ───────────── small helpers ─────────────
def test_hexcol_and_ass_col():
    assert hexcol("#ff00aa", "FFFFFF") == "FF00AA"
    assert hexcol("zzz", "FFFFFF") == "FFFFFF"
    assert ass_col("112233") == "&H00332211"


def test_ass_t():
    assert ass_t(0) == "0:00:00.00"
    assert ass_t(61.5) == "0:01:01.50"
    assert ass_t(3661.25) == "1:01:01.25"
    assert ass_t(-5) == "0:00:00.00"


def test_ass_text_strips_control_characters():
    assert ass_text("a {b} \\c  d") == "a b c d"


def test_overlay_text():
    assert overlay_text("hello brave new world today now extra", 4, 24) == "HELLO BRAVE NEW WORLD"
    assert overlay_text("a" * 30) == ""
    assert overlay_text('say "hi"') == "SAY HI"


def test_clip_words_clips_to_range_and_shifts_time():
    segs = [{"start": 0, "end": 4, "text": "one two three four"}]
    assert clip_words(segs, 1, 3) == [(0.0, 1.0, "two"), (1.0, 2.0, "three")]


def test_caption_chunks():
    words = [(i, i + 0.5, w) for i, w in enumerate("a b c d e".split())]
    assert [len(c) for c in caption_chunks(words, 3, 40)] == [3, 2]
    words = [(0, 0.4, "Hi,"), (0.5, 0.9, "there"), (1.0, 1.4, "friend")]
    assert [len(c) for c in caption_chunks(words, 3, 40)] == [1, 2]  # break after the comma
    words = [(0, 0.4, "a"), (2.0, 2.4, "b")]
    assert [len(c) for c in caption_chunks(words, 3, 40)] == [1, 1]  # break on a long pause


# ───────────── make_plan ─────────────
def test_make_plan_fallback_rules_are_safe():
    plan = make_plan(None, make_words(), 30.0, PLAN_CFG)
    assert plan["source"] == "auto"
    zooms = plan["zooms"]
    assert 1 <= len(zooms) <= 6
    for z in zooms:
        assert 0 <= z["t0"] < z["t1"] < z["end"] <= 30.0
    for prev, nxt in zip(zooms, zooms[1:]):
        assert nxt["t0"] >= prev["end"] + 1.0


def test_make_plan_zoom_off():
    cfg = dict(PLAN_CFG, edit_zoom=False)
    assert make_plan(None, make_words(), 30.0, cfg)["zooms"] == []


def test_make_plan_uses_ai_zooms():
    plan = make_plan({"zooms": [{"t": 5, "strength": 3}]}, make_words(), 30.0, PLAN_CFG)
    assert plan["source"] == "ai"
    assert len(plan["zooms"]) == 1
    assert plan["zooms"][0]["amp"] == pytest.approx(0.16)
    assert plan["zooms"][0]["pan"] == 1


@pytest.mark.parametrize("raw", ["garbage", 42, {"zooms": "no"}, {"zooms": [{"t": 999}]}, {"zooms": [None, "x"]}])
def test_make_plan_ignores_bad_ai_output(raw):
    plan = make_plan(raw, make_words(), 30.0, PLAN_CFG)
    assert plan["source"] == "auto"


def test_make_plan_hook_and_callouts():
    raw = {"hook": "Wait for it now",
           "callouts": [{"t": 10, "text": "big idea"}, {"t": 11, "text": "too close"}, {"t": 0.5, "text": "early"}]}
    plan = make_plan(raw, make_words(), 30.0, PLAN_CFG)
    assert plan["hook"] == "WAIT FOR IT NOW"
    assert len(plan["callouts"]) == 1  # "early" collides with the hook, "too close" is under 3 s after the first
    assert plan["callouts"][0]["text"] == "BIG IDEA"


def test_make_plan_text_off():
    cfg = dict(PLAN_CFG, edit_text=False)
    plan = make_plan({"hook": "Wait for it", "callouts": [{"t": 10, "text": "x y"}]}, make_words(), 30.0, cfg)
    assert plan["hook"] is None and plan["callouts"] == []


# ───────────── ffmpeg filters ─────────────
@pytest.mark.parametrize("mode", ["blur", "crop", "bars", "squish"])
def test_fit_filter_modes(mode):
    f = fit_filter({"blur": 20, "fit": mode}, 1080, 1920)
    assert isinstance(f, str) and "1080" in f and "1920" in f


def test_fit_filter_unknown_mode_raises():
    with pytest.raises(KeyError):
        fit_filter({"blur": 20, "fit": "nope"}, 1080, 1920)


def test_split_filter_order_and_patch():
    c = {"fit": "bars", "blur": 20}
    lay = {"cam": (0, 0, 400, 300), "main": (0, 300, 1920, 780), "cam_h": 768, "top": True, "patch": None}
    top = split_filter(c, 1080, 1920, "", lay)
    assert "[cam][main]vstack" in top and "delogo" not in top
    bottom = split_filter(c, 1080, 1920, "", dict(lay, top=False))
    assert "[main][cam]vstack" in bottom
    assert "delogo=x=10:y=20:w=30:h=40" in split_filter(c, 1080, 1920, "", dict(lay, patch=(10, 20, 30, 40)))


def test_cap_place():
    c = {"cap_position": "bottom", "cap_margin": 20, "cap_size": 14}
    assert cap_place(c, 1920) == (2, 20)
    assert cap_place(dict(c, cap_position="top"), 1920) == (8, 20)
    assert cap_place(dict(c, cap_position="middle"), 1920) == (5, 20)
    lay = {"cam_h": 768, "top": False}
    align, mv = cap_place(c, 1920, lay)
    assert align == 2 and mv == pytest.approx(20 + 768 * 0.15)
    align, mv = cap_place(dict(c, cap_position="middle"), 1920, {"cam_h": 768, "top": True})
    assert align == 2 and mv == pytest.approx(1152 * 0.15 - 0.6 * 14)


# ───────────── ASS file ─────────────
ASS_CFG = {"captions": True, "cap_size": 14, "cap_outline": 2, "cap_color": "FFFFFF", "edit_hl_color": "FFD400",
           "cap_position": "bottom", "cap_margin": 20, "cap_bold": True, "edit_upper": True, "edit_words": True,
           "edit_style": "punchy", "edit_font": "Arial", "edit_bar": True, "edit_text": True}


def test_build_ass_has_captions_hook_and_bar():
    words = [(0.0, 0.5, "hello"), (0.5, 1.0, "world")]
    text = build_ass(ASS_CFG, 1080, 1920, 10.0, words, {"hook": "WAIT", "zooms": [], "callouts": []})
    assert text.startswith("[Script Info]")
    assert "Dialogue:" in text and "HELLO" in text and "WORLD" in text and "WAIT" in text


def test_build_ass_returns_none_when_nothing_to_draw():
    cfg = dict(ASS_CFG, edit_bar=False)
    assert build_ass(cfg, 1080, 1920, 10.0, [], {"hook": None, "zooms": [], "callouts": []}) is None

def test_apply_caption_edit():
    words = [(0.0, 1.0, "helo"), (1.0, 2.0, "world"), (2.0, 3.0, "again")]
    assert apply_caption_edit(words, "hello world again") == [(0.0, 1.0, "hello"), (1.0, 2.0, "world"), (2.0, 3.0, "again")]
    assert [w[2] for w in apply_caption_edit(words, "helo again")] == ["helo", "again"]        # deleted word
    out = apply_caption_edit(words, "helo big world again")                                      # inserted word
    assert [w[2] for w in out] == ["helo", "big", "world", "again"]
    assert all(w[0] < w[1] for w in out)
    assert apply_caption_edit(words, "") == []


def test_clip_words_edited():
    segs = [{"start": 0, "end": 4, "text": "one two three four"}]
    cl = {"start": 0, "end": 4}
    assert clip_words_edited(segs, cl) == clip_words(segs, 0, 4)
    cl["cap_edit"] = {"text": "one 2 three four", "start": 0, "end": 4}
    assert [w[2] for w in clip_words_edited(segs, cl)] == ["one", "2", "three", "four"]
