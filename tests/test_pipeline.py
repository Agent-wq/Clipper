import threading

from clipper.pipeline import Pipeline


def make_pipe(**over):
    cfg = dict(num_clips=5, pad_start=0.0, pad_end=0.0, min_len=20, max_len=40, snap=True)
    cfg.update(over)
    logs = []
    pipe = Pipeline(cfg, "src", lambda *a: logs.append(a), threading.Event())
    return pipe, logs


def make_segs():
    return [{"start": 10.0 * k, "end": 10.0 * k + 10.0,
             "text": f"line {k} alpha{k} beta{k} gamma{k} delta{k} epsilon{k}"} for k in range(10)]


CANDS = [
    {"start": 0, "score": 8, "title": "T"},
    {"start": 0, "score": 7, "title": "duplicate of the first"},  # same words as the first clip
    {"start": 50, "score": 9, "title": "U"},
    {"start": "bad"},                                              # unusable start
    "not a dict",
    {"start": 500, "score": 5},                                    # beyond the end of the video
    {"start": 20, "score": 99},                                    # score outside 1-10
]


def test_finish_keeps_good_clips_sorted_and_drops_bad_ones():
    pipe, _ = make_pipe()
    out = pipe.finish(CANDS, make_segs(), 100.0)
    assert [(c["start"], c["end"]) for c in out] == [(0, 20), (50, 70)]
    assert [c["title"] for c in out] == ["T", "U"]
    assert all(c["use"] for c in out)


def test_finish_respects_num_clips_and_prefers_higher_score():
    pipe, _ = make_pipe(num_clips=1)
    out = pipe.finish(CANDS, make_segs(), 100.0)
    assert len(out) == 1 and out[0]["title"] == "U"


def test_finish_survives_garbage():
    pipe, _ = make_pipe()
    assert pipe.finish([], [], 100.0) == []
    assert pipe.finish(CANDS, make_segs(), float("nan")) == []
    assert pipe.finish("not a list", make_segs(), 100.0) == []


def test_content_signature():
    segs = [{"start": 0, "end": 5, "text": "Hello, World"}, {"start": 5, "end": 10, "text": "Foo"}]
    assert Pipeline._content_signature(2, 6, segs) == ["hello", "world", "foo"]
    assert Pipeline._content_signature(5, 10, segs) == ["foo"]


def test_similar_content():
    sim = Pipeline._similar_content
    assert not sim([], ["a"])
    assert sim(["hello", "world"], ["hello", "world"])
    assert not sim(["a", "b"], ["a", "c"])
    one = "alpha beta gamma delta epsilon zeta eta theta iota kappa".split()
    two = "one two three four five six seven eight nine ten".split()
    assert sim(one, list(one))
    assert not sim(one, two)


def test_zoom_chain():
    plan = {"zooms": [{"t0": 1.0, "t1": 3.0, "end": 3.5, "r": 0.5, "amp": 0.1, "pan": 0}]}
    assert Pipeline.zoom_chain({"zooms": []}, (1920, 1080)) == ""
    assert Pipeline.zoom_chain(plan, None) == ""
    flat = Pipeline.zoom_chain(plan, (1920, 1080))
    assert "eval=frame" in flat and "crop=1920:1080" in flat and "between" not in flat
    panned = Pipeline.zoom_chain({"zooms": [dict(plan["zooms"][0], pan=1)]}, (1920, 1080))
    assert "between(t" in panned
