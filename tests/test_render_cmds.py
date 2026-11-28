import threading

from clipper.pipeline import Pipeline

LEAD = "crop=608:1080:x='clip(656.0,0,1312)':y=0"
PLAN = {"hook": None, "zooms": [], "callouts": []}
ZOOM_PLAN = {"hook": None, "callouts": [], "zooms": [{"t0": 1.0, "t1": 3.0, "end": 3.5, "r": 0.5, "amp": 0.1, "pan": 0}]}
CODEC = ["-c:v", "libx264"]


def make_pipe(**over):
    cfg = dict(fit="blur", blur=20, audio_bitrate="192k", loudnorm=False, extra_ffmpeg="",
               cap_color="FFFFFF", cap_position="bottom", cap_margin=20, cap_size=14, cap_outline=2, cap_bold=True, edit_font="Arial")
    cfg.update(over)
    return Pipeline(cfg, "src", lambda *a: None, threading.Event())


def graph_of(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


def test_edit_cmd_uses_the_face_tracking_crop():
    cmd = make_pipe().edit_cmd(0.0, 10.0, "v.mp4", 1080, 1920, None, PLAN, None, None, CODEC, "out.mp4", None, None, LEAD)
    assert graph_of(cmd) == "[0:v]" + LEAD + ",scale=1080:1920,setsar=1[v]"


def test_edit_cmd_without_tracking_uses_the_fit_mode():
    cmd = make_pipe().edit_cmd(0.0, 10.0, "v.mp4", 1080, 1920, None, PLAN, None, None, CODEC, "out.mp4")
    g = graph_of(cmd)
    assert "split[a][b]" in g and "608" not in g


def test_edit_cmd_zoom_runs_after_the_tracking_crop():
    cmd = make_pipe().edit_cmd(0.0, 10.0, "v.mp4", 1080, 1920, None, ZOOM_PLAN, (608, 1080), None, CODEC, "out.mp4", None, None, LEAD)
    g = graph_of(cmd)
    assert g.index(LEAD) < g.index("eval=frame") < g.index("scale=1080:1920")


def test_plain_render_filter_follows_the_face_too():
    assert make_pipe().vf(1080, 1920, None, pre=LEAD, track=True) == LEAD + ",scale=1080:1920,setsar=1"


def test_plain_captions_use_the_chosen_font():
    f = make_pipe(edit_font="Burbank Big Condensed").vf(1080, 1920, "sub_1.srt")
    assert "FontName=Burbank Big Condensed," in f
