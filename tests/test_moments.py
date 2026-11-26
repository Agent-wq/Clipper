import re

import pytest

from clipper.moments import (
    clean_tags, clean_title, compose_caption, creator_handle, exclude_cam, fallback_meta, hinglish_text,
    hook_score, is_hinglish, layout_sizes, parse_loudness, parse_region, post_text, rank_windows, refine_bounds,
    region_px, select_candidates, youtube_description, youtube_fields, describe_score,
)


def rects_overlap(a, b):
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


# ───────────── regions and layout ─────────────
def test_parse_region_valid():
    assert parse_region("0.1,0.2,0.5,0.5") == (0.1, 0.2, 0.5, 0.5)
    assert parse_region("0,0,1,1") == (0.0, 0.0, 1.0, 1.0)


@pytest.mark.parametrize("bad", ["", "a,b,c,d", "0,0,1", "0.5,0,0.6,1", "0,0,0.01,1", "nan,0,1,1"])
def test_parse_region_invalid(bad):
    assert parse_region(bad) is None


def test_region_px_full_frame_and_even_sizes():
    assert region_px((0, 0, 1, 1), 1920, 1080) == (0, 0, 1920, 1080)
    assert region_px((0, 0, 1, 1), 101, 51) == (0, 0, 100, 50)
    assert region_px((0.5, 0.5, 0.5, 0.5), 1920, 1080) == (960, 540, 960, 540)


def test_region_px_stays_inside_picture():
    x, y, w, h = region_px((0.9, 0.9, 0.5, 0.5), 1000, 800)
    assert x >= 0 and y >= 0 and x + w <= 1000 and y + h <= 800
    assert w % 2 == 0 and h % 2 == 0


def test_layout_sizes():
    c = {"aspect": "9:16", "out_width": 1080, "split_cam_pct": 40}
    assert layout_sizes(c) == (1080, 1920, 768)


def test_exclude_cam_no_overlap_and_keep_mode():
    main = (0, 0, 1000, 1000)
    assert exclude_cam(main, (1100, 0, 100, 100), "auto") == (main, None, "none")
    assert exclude_cam(main, (400, 400, 200, 200), "keep") == (main, None, "none")


def test_exclude_cam_crops_corner_cam():
    cam = (0, 0, 200, 200)
    main, patch, how = exclude_cam((0, 0, 1000, 1000), cam, "auto")
    assert how == "crop" and patch is None
    assert not rects_overlap(main, cam)
    assert main[2] * main[3] >= 500000


def test_exclude_cam_fills_when_cam_is_in_the_middle():
    main, patch, how = exclude_cam((0, 0, 1000, 1000), (400, 400, 200, 200), "auto")
    assert how == "fill" and main == (0, 0, 1000, 1000)
    assert patch is not None and patch[0] + patch[2] <= 1000 and patch[1] + patch[3] <= 1000


def test_exclude_cam_forced_crop():
    _, _, how = exclude_cam((0, 0, 1000, 1000), (400, 400, 200, 200), "crop")
    assert how == "crop"


# ───────────── post text ─────────────
def test_creator_handle():
    assert creator_handle({"uploader_id": "@ohnepixel"}) == "@ohnepixel"
    assert creator_handle({"channel": "Ohne Pixel"}) == "@OhnePixel"
    assert creator_handle({"uploader_id": "UC123", "channel": "A B"}) == "@AB"
    assert creator_handle({}) == ""


def test_clean_tags():
    assert clean_tags(["Viral", "#Gaming", "gaming", "ab", "fyp", "Minecraft"], ["MyBrand"], 4) == ["mybrand", "gaming", "minecraft"]
    assert clean_tags([], ["fyp"], 3) == ["fyp"]  # the user's own tags are never filtered
    assert len(clean_tags(["alpha", "beta", "gamma", "delta"], [], 2)) == 2


def test_clean_title():
    assert clean_title("  HELLO world #tag https://x.com  ") == "HELLO world"
    assert clean_title(None) == ""
    assert clean_title("HELLO WORLD") == "Hello world"
    t = clean_title("word " * 30)
    assert len(t) <= 60 and t == t.strip()


def test_compose_caption():
    assert compose_caption("Hook.", ["a", "b"], "Credit: @x") == "Hook.\n\nCredit: @x\n\n#a #b"
    assert compose_caption("Hook.", ["a", "b"]) == "Hook.\n\n#a #b"


def test_youtube_description():
    d = youtube_description("Caption\n\n#one #two", "https://youtu.be/x")
    assert d == "Caption\n\nOriginal video: https://youtu.be/x\n\n#Shorts #one #two"
    assert youtube_description("Caption", "/tmp/a.mp4") == "Caption\n\n#Shorts"


def test_youtube_fields_limits():
    f = youtube_fields("x" * 150, "Caption\n\n#one", ["kw one"], "")
    assert len(f["title"]) == 100
    assert "one" in f["tags"] and "kw one" in f["tags"]


def test_post_text():
    cl = {"title": "T", "description": "D", "hook": "H", "score": 7.5, "why": ["a"]}
    yt = {"title": "T", "tags": ["x"], "description": "D"}
    out = post_text(cl, yt)
    assert "ON-SCREEN HOOK\nH" in out and "Score 7.5/10: a" in out


def test_fallback_meta():
    m = fallback_meta("Nobody tells you the truth about investing. More text here about investing and money.")
    assert m["title"] == "Nobody tells you the truth about investing"
    assert m["hashtags"][0] == "investing"
    assert fallback_meta("")["title"] == "Highlight"


# ───────────── Hinglish ─────────────
def test_is_hinglish():
    assert is_hinglish("Hinglish") and is_hinglish("hi-latn")
    assert not is_hinglish("en") and not is_hinglish("")


def test_hinglish_text():
    assert hinglish_text("hello") == "hello"
    assert hinglish_text("है") == "hai"
    assert hinglish_text("में") == "mein"
    assert hinglish_text("है", sentence=True) == "Hai"
    assert hinglish_text("Hello है") == "Hello hai"
    assert not re.search(r"[\u0900-\u097F]", hinglish_text("कमला"))


# ───────────── scoring ─────────────
def test_hook_score_prefers_a_real_hook():
    assert hook_score("Why does nobody tell you this secret?") > hook_score("ok")


def test_parse_loudness():
    lines = ["frame:0 pts:0", "lavfi.astats.Overall.RMS_level=-23.5",
             "lavfi.astats.Overall.RMS_level=-inf", "lavfi.astats.Overall.RMS_level=-120"]
    assert parse_loudness(lines) == [-23.5, -80.0, -80.0]


def make_segs(n=40, punct=True):
    end = "." if punct else ""
    return [{"start": 5.0 * k, "end": 5.0 * k + 5.0, "text": f"This is sentence number {k} about topic{k}{end}"} for k in range(n)]


def test_rank_windows_basic_properties():
    segs = make_segs()
    wins = rank_windows(segs, 200.0, 10, 30)
    assert wins
    assert all(wins[i]["raw"] >= wins[i + 1]["raw"] for i in range(len(wins) - 1))
    for w in wins:
        assert 10 <= w["end"] - w["start"] <= 30
        assert 0 <= w["heur"] <= 10
        assert w["start"] == segs[w["i"]]["start"] and w["end"] == segs[w["j"]]["end"]


def test_rank_windows_empty_and_too_short():
    assert rank_windows([], 100.0, 10, 30) == []
    assert rank_windows(make_segs(2), 10.0, 35, 60) == []


def test_rank_windows_relaxed_handles_missing_punctuation():
    segs = make_segs(punct=False)
    assert rank_windows(segs, 200.0, 10, 30) == []
    assert rank_windows(segs, 200.0, 10, 30, relaxed=True)


def test_rank_windows_prefers_loud_region():
    segs = make_segs()
    loud = [-30.0] * 400
    for i in range(120, 150):  # 60 s to 75 s
        loud[i] = -10.0
    best = rank_windows(segs, 200.0, 10, 30, loud)[0]
    assert best["start"] < 75 and best["end"] > 60


def test_select_candidates_do_not_overlap():
    wins = rank_windows(make_segs(), 200.0, 10, 30)
    cands = select_candidates(wins, 3)
    assert 1 <= len(cands) <= 3
    for i, a in enumerate(cands):
        for b in cands[i + 1:]:
            overlap = min(a["end"], b["end"]) - max(a["start"], b["start"])
            assert overlap <= 0.25 * min(a["end"] - a["start"], b["end"] - b["start"])


def test_refine_bounds():
    segs = [{"start": 1.0, "end": 5.0, "text": "a."}, {"start": 5.0, "end": 9.0, "text": "b."}]
    a, b = refine_bounds({"i": 0, "j": 1}, segs, 5, 20.0)
    assert a == pytest.approx(0.85) and b == pytest.approx(9.3)


def test_refine_bounds_never_runs_into_next_segment():
    segs = [{"start": 1.0, "end": 5.0, "text": "a."}, {"start": 5.0, "end": 9.0, "text": "b."},
            {"start": 9.05, "end": 12.0, "text": "c."}]
    _, b = refine_bounds({"i": 0, "j": 1}, segs, 5, 20.0)
    assert b <= 9.05

def test_describe_score():
    assert describe_score({}) == ""
    assert describe_score({"score": 6.0}) == "Score 6.0/10"
    cl = {"score": 7.5, "why": ["strong opening", "asks a question"], "ratings": {"s_hook": 8.0, "s_share": 6.5}}
    assert describe_score(cl) == "Score 7.5/10: strong opening, asks a question\nAI ratings: hook 8 · shareability 6.5"
