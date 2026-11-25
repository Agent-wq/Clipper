import sys
from clipper.sysutil import WIN
from clipper.setup import system_info, plan_for, DEPS
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import tkinter.font as tkfont
except ImportError:
    _n, _m = system_info()
    sys.exit("tkinter is missing, so the window can't open.\nInstall it with:  " + plan_for(DEPS[0], _m, False)["text"] + "\nOr install everything at once:  python3 clipper_gui.py --install")
# ───────────────────────────── UI ─────────────────────────────
BG, SIDE, PANEL, FIELD, BORDER = "#0b1220", "#101a2b", "#151f30", "#1c2a3d", "#2a3a50"
FG, MUTED, ACC, ACC_HI, ACC_FG, DANGER = "#edf3fb", "#9aaac0", "#35c7b2", "#50dfc9", "#071a18", "#f07878"
SUCCESS, WARNING, LOG_FG = "#55d6a4", "#e8b86d", "#c5d1e1"
FAM = "Segoe UI" if WIN else "Sans"
SUBTITLES = {
    "Source": "Where the video comes from and how it is downloaded.",
    "Transcribe": "Turns speech into text with Whisper. The GPU is used when available.",
    "AI picker": "A local Ollama model chooses the best moments.",
    "Growth": "Finds the moments most likely to hold attention and writes the title, caption, hashtags and cover.",
    "Clips": "How many clips to make and how long they are.",
    "Captions": "Subtitles burned into the video.",
    "Split screen": "Face cam enlarged on one half, main content on the other, like a streamer short.",
    "Watermark": "A small credit for the original creator, shown on every clip.",
    "Editing": "The AI plans zooms, animated captions, text and sound for each clip, then renders it finished.",
    "Video": "Shape, quality and encoder of the finished clips.",
    "Output": "Where files are saved and what gets saved.",
    "YouTube": "Upload finished clips to your YouTube channel. The title, description and hashtags are filled from the AI picker.",
}

def pick_font():
    fams = set(tkfont.families())
    for f in (("Segoe UI",) if WIN else ()) + ("Inter", "Noto Sans", "Cantarell", "Ubuntu", "DejaVu Sans"):
        if f in fams: return f
    return "Segoe UI" if WIN else "Sans"

def rrect(cv, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)

def mix_color(a, b, t):
    """Blend two #RRGGBB colors for lightweight Tk hover transitions."""
    t = max(0.0, min(1.0, float(t)))
    try:
        av = tuple(int(a[i:i + 2], 16) for i in (1, 3, 5))
        bv = tuple(int(b[i:i + 2], 16) for i in (1, 3, 5))
        return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(av, bv))
    except (TypeError, ValueError):
        return b

