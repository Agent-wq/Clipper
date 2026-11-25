# ───────────────────────────── auto-edit helpers (pure functions, no UI) ─────────────────────────────
import glob, hashlib, io, itertools, json, math, os, platform, queue, random, re, shlex, shutil, subprocess, sys, tempfile, threading, time, traceback, unicodedata, zipfile
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
import urllib.error, urllib.request
from clipper.sysutil import DEFAULT_FONT
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
ZOOM_AMP = {"punchy": (0.07, 0.11, 0.16), "subtle": (0.04, 0.06, 0.09)}  # zoom strength 1, 2, 3

def words_ok(seg):
    """True when a cached segment carries usable Whisper word timestamps."""
    ws = seg.get("words")
    return isinstance(ws, list) and all(
        isinstance(w, dict) and isinstance(w.get("word"), str)
        and math.isfinite(float(w["start"])) and math.isfinite(float(w["end"])) for w in ws)

def hexcol(s, default):
    h = str(s).lstrip("#").upper()
    return h if re.fullmatch(r"[0-9A-F]{6}", h) else default

def ass_col(h):  # RRGGBB -> ASS &H00BBGGRR
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}"

def ass_t(t):
    cs = max(0, int(round(float(t) * 100)))
    h, rem = divmod(cs, 360000); m, rem = divmod(rem, 6000)
    return f"{h}:{m:02}:{rem // 100:02}.{rem % 100:02}"

def ass_text(s):
    return re.sub(r"\s+", " ", re.sub(r"[{}\\]", "", str(s))).strip()

def clip_words(segs, a, b):
    """Words of the clip as (start, end, text) in clip time. Uses Whisper word timestamps when present,
    otherwise spreads each segment's words evenly over the segment."""
    out = []
    for s in segs:
        if s["end"] <= a or s["start"] >= b: continue
        if s.get("words"):
            items = [(float(w["start"]), float(w["end"]), str(w["word"]).strip()) for w in s["words"] if str(w.get("word", "")).strip()]
        else:
            toks = s["text"].split()
            step = max(s["end"] - s["start"], 0.01) / max(len(toks), 1)
            items = [(s["start"] + i * step, s["start"] + (i + 1) * step, t) for i, t in enumerate(toks)]
        for st, en, tx in items:
            if en <= a or st >= b: continue
            s0 = max(0.0, st - a)
            out.append((s0, max(s0, min(b, en) - a), tx))
    return sorted(out)

def caption_chunks(words, max_words, max_chars):
    """Group words into short caption lines: break on length, pauses and punctuation."""
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or sum(len(x[2]) + 1 for x in cur) + len(w[2]) > max_chars
                    or w[0] - cur[-1][1] > 0.7 or re.search(r"[.!?,;:…]$", cur[-1][2])):
            chunks.append(cur); cur = []
        cur.append(w)
    if cur: chunks.append(cur)
    return chunks

def _num(v, lo, hi):
    try: x = float(v)
    except (TypeError, ValueError): return None
    return x if math.isfinite(x) and lo <= x <= hi else None

def overlay_text(s, max_words=4, max_chars=24):
    out = ""
    for w in re.sub(r"[{}\\\r\n\"]+", " ", str(s)).split()[:max_words]:
        if len(out) + len(w) + (1 if out else 0) > max_chars: break
        out = (out + " " + w).strip()
    return out.upper()

def make_plan(raw, words, length, c):
    """Turn the AI's suggestions (or None) into a safe edit plan: clamps times, spaces effects out,
    caps their number, and falls back to simple rules when the AI gave nothing usable."""
    raw = raw if isinstance(raw, dict) else {}
    punchy = c["edit_style"] != "subtle"
    amps, r = ZOOM_AMP["punchy" if punchy else "subtle"], (0.45 if punchy else 0.65)
    cands = []
    if isinstance(raw.get("zooms"), list):
        for z in raw["zooms"]:
            if not isinstance(z, dict): continue
            t, s = _num(z.get("t"), 0, length), _num(z.get("strength", 2), 1, 3)
            if t is not None: cands.append((t, int(round(s)) if s is not None else 2))
    source = "ai" if cands else "auto"
    if not cands:  # no AI plan: zoom at the start of phrases, at least ~5 s apart
        last, k = -99.0, 0
        for i, w in enumerate(words):
            if (i == 0 or re.search(r"[.!?]$", words[i - 1][2]) or w[0] - words[i - 1][1] > 0.5) and w[0] - last >= 4.8:
                cands.append((w[0], (2, 1, 2, 3)[k % 4])); k += 1; last = w[0]
    zooms = []
    if c["edit_zoom"]:
        limit = min(8, max(1, int(length // 4.5)))
        for t, s in sorted(cands):
            t0 = max(0.0, t - 0.25); t1 = t0 + r + 1.2 + 0.4 * s
            if zooms and t0 < zooms[-1]["end"] + 1.0: continue
            if t1 + r > length - 0.1: continue
            zooms.append({"t0": round(t0, 2), "t1": round(t1, 2), "end": round(t1 + r, 2), "r": r, "amp": amps[s - 1],
                          "pan": ((1 if len(zooms) % 2 == 0 else -1) if c["edit_pan"] else 0)})
            if len(zooms) >= limit: break
    hook, callouts = None, []
    if c["edit_text"]:
        h = overlay_text(raw.get("hook", ""), 6, 28)
        hook = h if h and length > 6 else None
        items = []
        if isinstance(raw.get("callouts"), list):
            for it in raw["callouts"]:
                if not isinstance(it, dict): continue
                t, txt = _num(it.get("t"), 0, length), overlay_text(it.get("text", ""))
                if t is not None and txt: items.append((t, txt))
        for t, txt in sorted(items):
            if t < (2.9 if hook else 0.3) or t + 1.9 > length: continue
            if callouts and t < callouts[-1]["t"] + 3.0: continue
            callouts.append({"t": round(t, 2), "len": 1.8, "text": txt})
            if len(callouts) >= min(6, int(length // 8)): break
    return {"hook": hook, "zooms": zooms, "callouts": callouts, "source": source}

def plan_prompt(words, length, focus=""):
    lines, cur, cur_t = [], [], 0.0
    for w in words:
        if cur and w[0] - cur_t >= 3.0:
            lines.append("[%.1f] %s" % (cur_t, " ".join(cur))); cur = []
        if not cur: cur_t = w[0]
        cur.append(w[2])
    if cur: lines.append("[%.1f] %s" % (cur_t, " ".join(cur)))
    nz, nc = max(1, min(8, int(length // 5))), max(0, min(6, int(length // 9)))
    return ("You are a short-form video editor planning the edits for a %.0f second vertical clip. "
            "The transcript below has times in seconds from the start of the clip.%s\n"
            "Return valid JSON only, in this shape: "
            '{"hook":"...","zooms":[{"t":4.2,"strength":2}],"callouts":[{"t":9.5,"text":"..."}]}\n'
            "Rules:\n"
            "- hook: a punchy on-screen title of 3 to 6 words that makes a viewer keep watching. Do not copy a whole sentence.\n"
            "- zooms: up to %d moments of emphasis (a punchline, bold claim, surprising fact or emotional beat). t is the second the words are spoken. "
            "strength is 1 (subtle), 2 (medium) or 3 (strong). Keep at least 5 seconds between zooms.\n"
            "- callouts: up to %d very short on-screen texts (1 to 4 words) naming a key term, number or idea at the moment it is said. t is the second it is spoken. "
            "Leave the list empty if nothing deserves it.\n"
            "- Only use times that fall inside the transcript. Do not invent facts.\n\nTranscript:\n%s"
            % (length, (" Focus: " + focus + ".") if focus else "", nz, nc, "\n".join(lines)))

def build_ass(c, W, H, length, words, plan, ys=None, lay=None):
    """Build the ASS subtitle file with animated captions, hook/keyword boxes and the progress bar.
    Everything is drawn by libass in the same ffmpeg pass. Returns None when there is nothing to draw."""
    k = H / 288.0  # caption sizes are expressed on a 288 px tall canvas, like the plain captions
    fs, outline = max(8, round(c["cap_size"] * k)), round(c["cap_outline"] * k)
    base, hl = hexcol(c["cap_color"], "FFFFFF"), hexcol(c["edit_hl_color"], "FFD400")
    align, mvu = cap_place(c, H, lay); mv = round(mvu * k)
    font = re.sub(r"[,\r\n]", " ", str(c["edit_font"])).strip() or DEFAULT_FONT
    side, ev = round(W * 0.07), []
    if c["captions"] and words:
        upper, animate = c["edit_upper"], c["edit_words"]
        ws = [(a, b, t.upper() if upper else t) for a, b, t in words]
        maxc = max(8, min(30, int(W * 0.84 / (fs * (0.68 if upper else 0.6)))))
        chunks, cur = caption_chunks(ws, 3 if c["edit_style"] != "subtle" else 4, maxc), 0.0
        for ci, ch in enumerate(chunks):
            nxt = chunks[ci + 1][0][0] if ci + 1 < len(chunks) else length
            t_end, start = min(ch[-1][1] + 0.25, nxt, length), max(ch[0][0], cur)
            if t_end - start < 0.03: continue
            for n, i in enumerate(range(len(ch)) if animate else (0,)):
                s = start if n == 0 else max(ch[i][0], cur)
                e = t_end if (not animate or i + 1 == len(ch)) else min(max(ch[i + 1][0], s), t_end)
                if e - s < 0.02: continue
                if animate:
                    txt = " ".join("{\\1c" + ass_col(hl) + "&}" + ass_text(w[2]) + "{\\1c" + ass_col(base) + "&}" if j == i else ass_text(w[2]) for j, w in enumerate(ch))
                else:
                    txt = " ".join(ass_text(w[2]) for w in ch)
                pop = "{\\fscx88\\fscy88\\t(0,100,\\fscx100\\fscy100)}" if n == 0 else ""
                ev.append("Dialogue: 0,%s,%s,Cap,,0,0,0,,%s%s" % (ass_t(s), ass_t(e), pop, txt))
                cur = e
    y_hook, y_call = ys or ((0.16, 0.27) if c["cap_position"] != "top" else (0.84, 0.74))  # keep clear of the captions
    def box(t, dur, text, y):
        fx = ("\\an5\\pos(%d,%d)\\frz-3\\fad(90,160)\\fscx70\\fscy70\\t(0,130,\\fscx108\\fscy108)\\t(130,230,\\fscx100\\fscy100)" % (W // 2, round(H * y)))
        ev.append("Dialogue: 2,%s,%s,Box,,0,0,0,,{%s}%s" % (ass_t(t), ass_t(min(t + dur, length)), fx, ass_text(text)))
    if plan.get("hook"): box(0.2, 2.4, plan["hook"], y_hook)
    for q in plan.get("callouts") or []: box(q["t"], q["len"], q["text"], y_call)
    if c["edit_bar"] and length > 1:
        bh = max(4, round(H * 0.007))
        ev.append("Dialogue: 3,%s,%s,Cap,,0,0,0,,{\\an7\\pos(0,0)\\p1\\bord0\\shad0\\1c%s&\\clip(0,0,1,%d)\\t(0,%d,\\clip(0,0,%d,%d))}m 0 0 l %d 0 %d %d 0 %d"
                  % (ass_t(0), ass_t(length), ass_col(hl), bh, int(length * 1000), W, bh, W, W, bh, bh))
    if not ev: return None
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Cap,%s,%d,%s,&H000000FF,&H00000000,&H64000000,%d,0,0,0,100,100,0,0,1,%d,0,%d,%d,%d,%d,1\n"
            "Style: Box,%s,%d,&H00181818,&H000000FF,%s,&H00000000,-1,0,0,0,100,100,0,0,3,%d,0,5,%d,%d,0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            % (W, H, font, fs, ass_col(base), -1 if c["cap_bold"] else 0, outline, align, side, side, mv,
               font, round(fs * 1.05), ass_col(hl), round(fs * 0.3), side, side))
    return head + "\n".join(ev) + "\n"

def fit_filter(c, W, H):
    b = c["blur"]
    return {
        "blur": f"split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur={b}:5[bg];"
                f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1",
        "crop": f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1",
        "bars": f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1",
        "squish": f"scale={W}:{H},setsar=1",
    }[c["fit"]]

def split_filter(c, W, H, pre, lay):
    """Face cam enlarged in its own panel, main content (with the usual fit mode and zoom) in the other."""
    cx, cy, cw, ch = lay["cam"]; mx, my, mw, mh = lay["main"]; cam_h = lay["cam_h"]
    # Preserve the entire user-selected face-cam rectangle even when it is not the
    # same aspect ratio as the output panel; pad rather than silently cropping it.
    cam = f"crop={cw}:{ch}:{cx}:{cy},scale={W}:{cam_h}:force_original_aspect_ratio=decrease,pad={W}:{cam_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    px, py, pw, ph = lay.get("patch") or (0, 0, 0, 0)  # area of the content that still shows the cam: painted over
    fix = f"delogo=x={px}:y={py}:w={pw}:h={ph}," if lay.get("patch") else ""
    main = f"crop={mw}:{mh}:{mx}:{my}," + fix + (pre + "," if pre else "") + fit_filter(c, W, H - cam_h)
    stack = "[cam][main]vstack" if lay["top"] else "[main][cam]vstack"
    return f"split[sc][sm];[sc]{cam}[cam];[sm]{main}[main];{stack},setsar=1"

def cap_place(c, H, lay=None):
    """(ASS alignment, margin in 288 px units) for captions. In split mode 'bottom' stays on the content panel
    (never over the face cam) and 'middle' sits on the seam between the two panels."""
    pos = c["cap_position"]; align, mv = {"bottom": 2, "middle": 5, "top": 8}[pos], c["cap_margin"]
    if lay:
        k = 288.0 / H; seam = lay["cam_h"] if lay["top"] else H - lay["cam_h"]
        if pos == "bottom" and not lay["top"]: mv += lay["cam_h"] * k
        elif pos == "middle": align, mv = 2, max(0.0, (H - seam) * k - 0.6 * c["cap_size"])
    return align, mv

