# ───────────────────────────── settings schema ─────────────────────────────
# One line per setting. The UI, saving, presets and the pipeline all read from this.
# (key, label, kind, default, options, hint, advanced)
import math, os, re
from pathlib import Path
from clipper.sysutil import WIN, DEFAULT_FONT
def Opt(key, label, kind, default, opts=None, hint="", adv=False): return (key, label, kind, default, opts, hint, adv)

SCHEMA = {
    "Source": [
        Opt("max_height", "Max download quality (p)", "choice", 1080, [360, 480, 720, 1080, 1440, 2160], "Lower is a faster download"),
        Opt("use_cache", "Reuse earlier download and transcript", "bool", True, hint="Skips work already done for the same video"),
        Opt("cache_days", "Delete cached videos unused for (days)", "int", 14, (0, 365), "Old downloads and transcripts are removed when a job starts. 0 keeps everything", True),
        Opt("cookies_browser", "Browser cookies for yt-dlp", "choice", "none", ["none", "firefox", "chrome", "chromium", "brave", "edge"], "Try this if YouTube blocks the download" + (". On Windows Firefox is the most reliable: Chrome and Edge often can't share their cookies" if WIN else ""), True),
        Opt("ytdlp_extra", "Extra yt-dlp arguments", "str", "", hint="For example: --proxy http://host:port", adv=True),
    ],
    "Transcribe": [
        Opt("device", "Run Whisper on", "choice", "auto", ["auto", "cuda", "cpu"], "auto uses the GPU when available and falls back to the CPU"),
        Opt("whisper_model", "Whisper model", "choice", "small", ["tiny", "base", "small", "medium", "large-v3-turbo", "large-v3"], "Bigger is more accurate and slower"),
        Opt("language", "Spoken language", "str", "", hint="Leave empty to detect it. Or type a code like en, es, hi. Type hinglish for Hindi written in English letters (use the small model or bigger)"),
        Opt("vad_filter", "Skip silence (VAD)", "bool", False, hint="Can reduce made-up text in quiet parts", adv=True),
        Opt("beam_size", "Beam size", "int", 5, (1, 10), "Higher is slightly more accurate and slower", True),
    ],
    "AI picker": [
        Opt("use_ai", "Use local AI to pick moments", "bool", True, hint="Off spreads the clips evenly across the video"),
        Opt("ollama_model", "Ollama model", "ollama", "llama3.1:8b", hint="Press the refresh button to list installed models"),
        Opt("focus", "What should it look for?", "text", "", hint="Optional. For example: funny moments, or advice for beginners"),
        Opt("ollama_host", "Ollama address", "str", "http://127.0.0.1:11434", adv=True),
        Opt("num_ctx", "Context size (tokens)", "int", 8192, (2048, 65536), "Higher uses more VRAM. Long videos are split into parts automatically", True),
        Opt("temperature", "Creativity", "float", 0.4, (0, 1.5, 0.1), adv=True),
        Opt("ai_retries", "Retries on bad AI output", "int", 3, (1, 10), adv=True),
    ],
    "Growth": [
        Opt("smart_pick", "Smart moment picker", "bool", True, hint="Scores every possible clip on hook strength, audio energy, pacing and a clean start and end, then the AI rates the best of them. Off lets the AI choose alone"),
        Opt("pick_min_score", "Minimum AI score (1-10)", "float", 5.0, (1, 9, 0.5), "Moments the AI rates lower are skipped, even if that means fewer clips"),
        Opt("audience", "Your niche or audience", "text", "", hint="For example: GTA RP highlights, or personal finance tips. Used for titles, captions and hashtags"),
        Opt("hashtag_count", "Hashtags per clip", "int", 4, (1, 10), "3 to 5 relevant tags work better than a long list of generic ones"),
        Opt("extra_hashtags", "Always add these hashtags", "str", "", hint="Your brand or community tag, for example #ohnepixelclips"),
        Opt("credit_line", "Credit the original creator in the caption", "bool", True, hint="Adds 'Credit: @handle' from the video's channel, or from the watermark text"),
        Opt("cover_export", "Save a cover image for each clip", "bool", True, hint="A JPG taken just after the hook appears, ready to use as the cover"),
    ],
    "Clips": [
        Opt("num_clips", "Number of clips", "int", 5, (1, 30), "Ignored while auto-edit is on (see the Editing page)"),
        Opt("min_len", "Shortest clip (seconds)", "int", 35, (5, 180)),
        Opt("max_len", "Longest clip (seconds)", "int", 60, (10, 300)),
        Opt("review", "Review and edit clips before rendering", "bool", True),
        Opt("snap", "Start and end on sentence boundaries", "bool", True, adv=True),
        Opt("pad_start", "Extra time before (seconds)", "float", 0.0, (0, 5, 0.1), adv=True),
        Opt("pad_end", "Extra time after (seconds)", "float", 0.0, (0, 5, 0.1), adv=True),
    ],
    "Captions": [
        Opt("captions", "Burn captions into the video", "bool", True),
        Opt("cap_position", "Position", "choice", "bottom", ["bottom", "middle", "top"]),
        Opt("cap_size", "Font size", "int", 14, (6, 40)),
        Opt("cap_color", "Text color (hex)", "str", "FFFFFF", adv=True),
        Opt("cap_outline", "Outline thickness", "int", 2, (0, 8), adv=True),
        Opt("cap_bold", "Bold", "bool", True, adv=True),
        Opt("cap_margin", "Distance from edge", "int", 20, (0, 200), adv=True),
    ],
    "Editing": [
        Opt("edit_enabled", "Auto-edit clips with AI", "bool", True, hint="The AI plans zooms, pans, animated captions, text and sound effects for each clip"),
        Opt("edit_clips", "Edited clips per video", "int", 2, (1, 10), "Replaces 'Number of clips' while auto-edit is on"),
        Opt("edit_style", "Style", "choice", "punchy", ["subtle", "punchy"], "subtle: gentle zooms, slower captions. punchy: stronger and faster"),
        Opt("edit_zoom", "Zoom punch-ins", "bool", True),
        Opt("edit_pan", "Slow pan while zoomed", "bool", True),
        Opt("edit_words", "Highlight each word as it is spoken", "bool", True, hint="Needs 'Burn captions into the video' on the Captions page"),
        Opt("edit_text", "Hook title and keyword pop-ups", "bool", True),
        Opt("edit_bar", "Progress bar", "bool", True),
        Opt("edit_sfx", "Sound effects", "bool", True, hint="A quiet whoosh on zooms and a pop on text"),
        Opt("edit_hl_color", "Highlight color (hex)", "str", "FFD400", adv=True),
        Opt("edit_upper", "ALL CAPS captions", "bool", True, adv=True),
        Opt("edit_font", "Caption font", "font", DEFAULT_FONT, hint="Any font installed on this computer", adv=True),
    ],
    "Watermark": [
        Opt("wm_enabled", "Credit the original creator", "bool", True, hint="Shows their name on every clip. Local files have no channel, so type the name below"),
        Opt("wm_text", "Text", "str", "", hint="For example @ohnepixel. Leave empty to use the video's channel handle automatically"),
        Opt("wm_position", "Position", "choice", "top right", ["top right", "top left", "bottom right", "bottom left"]),
        Opt("wm_size", "Size", "int", 8, (4, 20)),
        Opt("wm_opacity", "Opacity (%)", "int", 80, (10, 100)),
    ],
    "Split screen": [
        Opt("split_enabled", "Split face cam and content", "bool", False, hint="For streams: an enlarged face cam on one half and the main content on the other. After download, mark the face-cam area and (optionally) the content area"),
        Opt("split_cam_pos", "Face cam position", "choice", "top", ["top", "bottom"]),
        Opt("split_cam_pct", "Face cam height (% of the screen)", "int", 40, (20, 70)),
        Opt("split_ask", "Ask where they are for every video", "bool", True, hint="Off reuses the face-cam and content areas you marked last time, which is handy when your layout never changes"),
        Opt("split_overlap", "If the face cam is on top of the content", "choice", "auto", ["auto", "crop", "fill", "keep"],
            "Keeps the face out of the content panel. crop: cut away the part of the content that holds the cam. fill: paint over the cam. auto: crop if that keeps at least half of the content, otherwise fill. keep: do nothing"),
        Opt("split_cam", "Face cam area (saved)", "str", "", hint="x, y, width, height as fractions of the video. Filled in by the picker window", adv=True),
        Opt("split_main", "Main content area (saved)", "str", "0,0,1,1", hint="x, y, width, height as fractions of the video. 0,0,1,1 is the full frame. Filled in by the picker window", adv=True),
    ],
    "Video": [
        Opt("fit", "Fit mode", "choice", "blur", ["blur", "crop", "bars", "squish"], "blur: whole video over a blurred copy. crop: fill the frame and cut the sides. bars: black bars. squish: stretch (distorts)"),
        Opt("aspect", "Aspect ratio", "choice", "9:16", ["9:16", "4:5", "1:1", "16:9"]),
        Opt("face_track", "Follow the speaker's face", "bool", False, hint="Crops a wide video to the vertical shape and keeps the face in view, instead of the Fit mode. Needs OpenCV (Setup page). Not used with split screen"),
        Opt("face_debug", "Save a face-tracking debug video", "bool", False, hint="Saves face_debug_1.mp4 etc. next to the clips: the original picture with a green box on the face that was found and a blue box for the part that is kept. Use it to check the tracking", adv=True),
        Opt("out_width", "Output width (px)", "int", 1080, (240, 3840), adv=True),
        Opt("blur", "Blur strength", "int", 20, (1, 60), adv=True),
        Opt("encoder", "Video encoder", "choice", "auto", ["auto", "nvenc", "x264"], "auto uses the NVIDIA GPU encoder when available", True),
        Opt("quality", "Quality (lower is better)", "int", 23, (14, 35), adv=True),
        Opt("audio_bitrate", "Audio bitrate", "str", "192k", adv=True),
        Opt("loudnorm", "Even out loudness", "bool", False, adv=True),
        Opt("extra_ffmpeg", "Extra ffmpeg arguments", "str", "", hint="Added before the output file", adv=True),
    ],
    "Output": [
        Opt("output_dir", "Save clips to", "path", str(Path.home() / "clipper_output")),
        Opt("prefix", "File name prefix", "str", "clip"),
        Opt("subfolder", "Give each video its own folder", "bool", True),
        Opt("write_txt", "Save Instagram descriptions (.txt)", "bool", True),
        Opt("open_done", "Open the folder when finished", "bool", True),
        Opt("write_json", "Save a manifest (.json)", "bool", False, adv=True),
        Opt("write_srt", "Also save subtitle files (.srt)", "bool", False, adv=True),
    ],
    "YouTube": [
        Opt("yt_enabled", "Upload clips to YouTube", "bool", False, hint="Requires a Google Cloud client_secret.json. Click 'Get credentials guide' below for steps."),
        Opt("yt_privacy", "Privacy status", "choice", "private", ["private", "unlisted", "public"], "private: only you. unlisted: anyone with the link. public: everyone."),
        Opt("yt_category", "Category", "choice", "People & Blogs", ["People & Blogs", "Gaming", "Entertainment", "Comedy", "Education", "Howto & Style", "Science & Technology", "Sports", "News & Politics", "Film & Animation", "Music", "Pets & Animals", "Travel & Events", "Autos & Vehicles"], "Helps YouTube decide who to show the clip to"),
        Opt("yt_hashtags_in_title", "Move hashtags to title", "bool", True, hint="Appends hashtags from the description to the end of the title"),
        Opt("yt_auto_upload", "Auto-upload after render", "bool", False, hint="Uploads each clip automatically when rendering finishes"),
        Opt("yt_default_tags", "Default tags (comma-separated)", "str", "shorts", hint="Generic tags like viral or fyp do not help on YouTube; the AI keywords are added automatically", adv=True),
        Opt("yt_client_secret", "client_secret.json path", "path", str(Path.home() / "client_secret.json"), adv=True),
    ],
}
PRESETS = {
    "Fast": {"whisper_model": "base", "review": False, "quality": 26},
    "Balanced": {"whisper_model": "small", "review": True, "quality": 23},
    "Best quality": {"whisper_model": "large-v3-turbo", "review": True, "quality": 20, "beam_size": 5},
}
STEPS = ["Get video", "Read audio", "Transcribe", "Pick moments", "Plan edits", "Render clips"]
WEIGHTS = [0.10, 0.03, 0.35, 0.12, 0.10, 0.30]
# Per-user settings: %APPDATA%\Clipper on Windows, ~/.config/clipper on Linux.
APP_DIR = (Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Clipper") if WIN else Path.home() / ".config" / "clipper"
SETTINGS_FILE = APP_DIR / "settings.json"

def cast(kind, default, v):
    if kind == "bool": return bool(v)
    if kind == "int": return int(float(v))
    if kind == "float": return float(v)
    if kind == "choice" and isinstance(default, int): return int(v)
    return str(v).strip()

def parse_t(s):  # "90", "1:30" or "1:02:03" -> seconds
    try: p = [float(x) for x in s.strip().split(":")]
    except ValueError: raise ValueError("Use seconds (90) or m:ss (1:30).") from None
    return sum(v * 60 ** i for i, v in enumerate(reversed(p)))

def fmt_t(sec):
    sec = int(sec); return f"{sec // 60}:{sec % 60:02d}"

def fmt_clip_t(sec):
    """Format editable clip times without discarding sub-second precision."""
    sec = max(0.0, float(sec))
    minutes = int(sec // 60)
    rem = sec - minutes * 60
    if round(rem, 2) >= 60: minutes, rem = minutes + 1, 0.0
    return f"{minutes}:{rem:05.2f}"

def output_stem(output_dir, prefix, index):
    """Create a safe, collision-free output basename inside output_dir."""
    safe_prefix = re.sub(r"[^A-Za-z0-9._-]+", "_", str(prefix)).strip("._-") or "clip"
    base = f"{safe_prefix}_{int(index)}"
    candidate, suffix = base, 2
    directory = Path(output_dir)
    while any((directory / f"{candidate}.{ext}").exists() for ext in ("mp4", "srt", "txt")):
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate

def transcript_cache_name(cfg):
    """Include every transcription-affecting option in a safe cache filename."""
    model = re.sub(r"[^A-Za-z0-9_-]+", "_", str(cfg.get("whisper_model", "small"))) or "small"
    language = re.sub(r"[^A-Za-z0-9_-]+", "_", str(cfg.get("language") or "auto")).strip("_") or "auto"
    words = "_words" if cfg.get("edit_enabled") else ""  # edited clips need word timestamps, so they get their own cache
    return f"segments_{model}_{language}_beam{int(cfg['beam_size'])}_vad{int(bool(cfg['vad_filter']))}{words}.json"

def validate_clip_times(start, end, total, min_len, max_len):
    """Raise ValueError when a reviewed clip cannot safely be rendered."""
    values = (start, end, total, min_len, max_len)
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError("Times must be finite numbers.")
    if start < 0:
        raise ValueError("The start cannot be before the beginning of the video.")
    if end <= start:
        raise ValueError("The end must come after the start.")
    if end > total + 0.05:
        raise ValueError(f"The end cannot exceed the video duration ({fmt_clip_t(total)}).")
    length = end - start
    if length < min_len - 0.05 or length > max_len + 0.05:
        raise ValueError(f"Clip length must be between {min_len:g} and {max_len:g} seconds; this clip is {length:.2f} seconds.")

def srt_t(t):
    ms = max(0, int(round(float(t) * 1000)))
    h, rem = divmod(ms, 3600000); m, sec = divmod(rem, 60000)
    return f"{h:02}:{m:02}:{sec // 1000:02},{sec % 1000:03}"

