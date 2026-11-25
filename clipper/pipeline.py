import glob, hashlib, io, itertools, json, math, os, platform, queue, random, re, shlex, shutil, subprocess, sys, tempfile, threading, time, traceback, unicodedata, zipfile
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
import urllib.error, urllib.request
from clipper.sysutil import WIN, kill_tree, refresh_paths, which, proc_kw, safe_dirname, split_args
from clipper.setup import ytdlp_cmd, ensure_av_compat, is_av_mismatch, av_mismatch_help, ollama_up, ollama_post, sh
from clipper.settings import STEPS, transcript_cache_name, fmt_t, output_stem, srt_t
from clipper.edit import words_ok, fit_filter, split_filter, cap_place, clip_words, plan_prompt, make_plan, LOUDNORM, build_ass
from clipper.moments import (creator_handle, parse_region, is_hinglish, HINGLISH_PROMPT, hinglish_text,
    hinglish_note, parse_loudness, clip_text, rerank_prompt, parse_rating, rank_windows, select_candidates,
    keyword_idf, refine_bounds, fallback_meta, clean_title, clean_tags, compose_caption, build_wm_ass,
    region_px, exclude_cam, layout_sizes, youtube_fields, post_text)
class Cancelled(Exception): pass
class OllamaError(RuntimeError): pass  # a real model error (out of memory, bad request) is not worth retrying

# ───────────────────────────── pipeline (no UI code) ─────────────────────────────
class Pipeline:
    def __init__(self, cfg, src, emit, cancel):
        self.c, self.src, self.emit, self.cancel = cfg, src, emit, cancel
        self.proc, self.i = None, 1
        self.wm_text, self.regions = "", None
        self.video, self.work, self.creator, self.src_url = None, None, "", ""

    # helpers
    def log(self, m): self.emit("log", m)
    def step(self, i, name): self.i = i; self.emit("step", i, name)
    def prog(self, frac, text=""): self.emit("prog", self.i, frac, text)
    def check(self):
        if self.cancel.is_set(): raise Cancelled()
    def kill(self): kill_tree(self.proc)

    def run(self, cmd, cwd=None, on_line=None):
        self.check()
        # Resolve executables after refreshing the user PATH.
        refresh_paths()
        if cmd:
            resolved = which(cmd[0])
            if not resolved:
                raise RuntimeError(
                    f"'{cmd[0]}' is not installed or not on PATH. "
                    "Open the Setup page in Clipper to install it, then try again."
                )
            cmd = [resolved] + list(cmd[1:])
        try:
            self.proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1, **proc_kw())
        except FileNotFoundError as e:
            raise RuntimeError(
                f"'{cmd[0]}' is not installed or not on PATH. "
                "Open the Setup page in Clipper to install it, then try again."
            ) from e
        out = []
        for line in self.proc.stdout:
            self.check()
            line = line.rstrip(); out.append(line)
            if on_line: on_line(line)
        self.proc.wait(); rc = self.proc.returncode; self.proc = None
        self.check()
        return rc, out

    def duration(self, path):
        ffprobe = which("ffprobe")
        if not ffprobe:
            raise RuntimeError("ffprobe (part of ffmpeg) is not installed or not on PATH. Open the Setup page to install it, then try again.")
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, timeout=20, **proc_kw())
        except subprocess.TimeoutExpired:
            raise RuntimeError("ffprobe timed out while reading the video duration.") from None
        if result.returncode != 0:
            raise RuntimeError("ffprobe could not read the video duration: " + (result.stderr or "unknown error")[:300])
        try:
            duration = float(result.stdout.strip())
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError
            return duration
        except ValueError:
            raise RuntimeError("Couldn't read a valid video duration. Check that the file is valid and ffmpeg is working.") from None

    @staticmethod
    def find_video(work):
        v = [p for p in work.glob("video.*") if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")]
        return v[0] if v else None

    # main flow
    def go(self):
        c, src = self.c, self.src
        if c["edit_enabled"]:
            c["num_clips"] = c["edit_clips"]
            self.log(f"Auto-edit is on: making {c['num_clips']} edited clip(s).")
        root = Path(c["output_dir"]).expanduser(); root.mkdir(parents=True, exist_ok=True)
        is_url = bool(re.match(r"https?://", src))
        if not is_url and not Path(src).is_file(): raise RuntimeError("That file doesn't exist.")
        # Fail fast with a clear message when required tools are missing.
        refresh_paths()
        missing = [t for t in ("ffmpeg", "ffprobe") if not which(t)]
        if is_url and not ytdlp_cmd(): missing.append("yt-dlp")
        if missing:
            names = ", ".join(missing)
            raise RuntimeError(
                f"{names} {'is' if len(missing) == 1 else 'are'} not installed or not on PATH. "
                "Open the Setup page in Clipper to install what's missing, then try again."
            )
        source_identity = src
        if not is_url:
            source_stat = Path(src).stat()
            source_identity = f"{src}:{source_stat.st_size}:{source_stat.st_mtime_ns}"
        # Quality is part of the key so changing it re-downloads instead of reusing the old file.
        key = hashlib.sha1(f"{source_identity}|q{c['max_height']}".encode()).hexdigest()[:10]
        work = root / ".cache" / key; work.mkdir(parents=True, exist_ok=True)

        self.step(1, STEPS[0])
        if is_url:
            video = self.find_video(work) if c["use_cache"] else None
            if video: self.log("Using the earlier download."); self.prog(1)
            else: video = self.download(work)
        else:
            video = Path(src); self.prog(1)
        total = self.duration(video)
        title = video.stem
        info, meta = work / "video.info.json", {}
        if info.exists():
            try: meta = json.loads(info.read_text(encoding="utf-8"))
            except ValueError: pass
        if not isinstance(meta, dict): meta = {}
        title = meta.get("title") or title
        self.wm_text = (c["wm_text"] or creator_handle(meta)) if c["wm_enabled"] else ""
        if c["wm_enabled"]: self.log(f"Watermark: {self.wm_text}" if self.wm_text else "Watermark: no creator name found (type one on the Watermark page), so it is skipped.")
        self.log(f"Video: {title} ({total / 60:.1f} min)")
        self.video, self.work = video, work
        self.creator = c["wm_text"] or creator_handle(meta)
        self.src_url = str(src) if str(src).startswith("http") else ""
        if c["split_enabled"]: self.regions = self.pick_regions(video, total, work)

        seg_file = work / transcript_cache_name(c)
        cached = False
        if c["use_cache"] and seg_file.exists():
            try:
                candidate_segs = json.loads(seg_file.read_text(encoding="utf-8"))
                if isinstance(candidate_segs, list) and all(
                    isinstance(x, dict) and math.isfinite(float(x["start"])) and math.isfinite(float(x["end"]))
                    and 0 <= float(x["start"]) <= float(x["end"]) <= total + 0.05
                    and isinstance(x.get("text"), str) and (not c["edit_enabled"] or words_ok(x)) for x in candidate_segs):
                    segs, cached = candidate_segs, True
                else:
                    self.log("Cached transcript was invalid; transcribing again.")
            except (OSError, ValueError, TypeError, KeyError):
                self.log("Cached transcript was unreadable; transcribing again.")
        if cached:
            self.step(2, STEPS[1]); self.prog(1); self.step(3, STEPS[2] + " (cached)"); self.prog(1)
            self.log("Using the earlier transcript.")
        else:
            self.step(2, STEPS[1]); audio = self.audio(video); self.prog(1)
            self.step(3, STEPS[2])
            try:
                segs = self.transcribe(audio, total)
            finally:
                try: os.unlink(audio)
                except OSError: pass
            seg_file.write_text(json.dumps(segs), encoding="utf-8")
        if not segs: raise RuntimeError("No speech found in this video.")
        self.log(f"Transcript: {len(segs)} segments.")

        self.step(4, STEPS[3]); clips = self.pick(segs, total)
        if not clips: raise RuntimeError("The AI didn't return usable clips. Try again, or turn off the AI picker.")
        self.prog(1)
        if c["review"]:
            clips = self.ask_review(clips, video, total)
            if not clips: raise RuntimeError("No clips were selected.")

        if c["edit_enabled"]:
            self.step(5, STEPS[4]); self.plan_edits(clips, segs); self.prog(1)

        safe = safe_dirname(title) or key
        out = root / safe if c["subfolder"] else root
        out.mkdir(parents=True, exist_ok=True)
        self.step(6, STEPS[5]); files = self.render(clips, segs, video, work, out)
        if c["write_json"]:
            manifest = out / "manifest.json"
            suffix = 2
            while manifest.exists():
                manifest = out / f"manifest_{suffix}.json"; suffix += 1
            manifest.write_text(json.dumps({"title": title, "source": src, "clips": clips}, indent=2), encoding="utf-8")
        return out, files

    def pick_regions(self, video, total, work):
        """Ask for the face-cam box and the content box. The content box is optional: unmarked means the full frame."""
        c = self.c
        saved_cam, saved_main = parse_region(c["split_cam"]), parse_region(c["split_main"])
        full_frame = (0.0, 0.0, 1.0, 1.0)
        if not c["split_ask"] and saved_cam:
            self.log("Split layout: using the saved face-cam and content areas.")
            return saved_cam, saved_main or full_frame
        holder = {"ev": threading.Event(), "res": None, "video": str(video), "duration": total,
                  "saved": saved_cam, "saved_main": saved_main, "work": str(work), "cfg": c}
        self.log("Split layout: mark the face cam and the content area in the window that opened. Leave the content box unmarked to use the full frame.")
        self.emit("regions", holder)
        while not holder["ev"].wait(0.2):
            try: self.check()
            except Cancelled: self.emit("regions_cancel"); raise
        if holder["res"] is None: raise Cancelled()
        return holder["res"]

    def download(self, work):
        c, h = self.c, self.c["max_height"]
        for f in work.glob("video.*"): f.unlink()
        cmd = (ytdlp_cmd() or ["yt-dlp"]) + ["--newline", "--no-playlist", "--write-info-json", "-f", f"bv*[height<={h}]+ba/b[height<={h}]/b",
               "--merge-output-format", "mp4", "-o", "video.%(ext)s"]
        ff, fp = which("ffmpeg"), which("ffprobe")  # tell yt-dlp exactly where ffmpeg is, so merging never depends on PATH
        if ff and fp and Path(ff).parent == Path(fp).parent: cmd += ["--ffmpeg-location", str(Path(ff).parent)]
        if c["cookies_browser"] != "none": cmd += ["--cookies-from-browser", c["cookies_browser"]]
        cmd += split_args(c["ytdlp_extra"]) + [self.src]
        n, parts = [0], [2]
        def on(line):
            # Count merged streams ("Downloading 1 format(s): 137+140") so a single-file
            # download fills the whole bar instead of stopping at half of it.
            f = re.search(r"format\(s\):\s*([\w.+=/-]+)", line)
            if f: parts[0] = f.group(1).count("+") + 1
            if "Destination:" in line: n[0] += 1
            m = re.search(r"\[download\]\s+([\d.]+)%", line)
            if m: self.prog(min(1, (max(n[0], 1) - 1 + float(m.group(1)) / 100) / parts[0]), f"{m.group(1)}%")
            elif "ERROR" in line: self.log(line)
        rc, out = self.run(cmd, cwd=work, on_line=on)
        v = self.find_video(work)
        if rc != 0 or not v: raise RuntimeError("Download failed:\n" + "\n".join(out[-4:]))
        return v

    def audio(self, video):
        """Extract a temporary mono 16 kHz WAV; avoid holding long audio in RAM."""
        self.check()
        ffmpeg = which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is not installed or not on PATH. Open the Setup page to install it, then try again.")
        fd, audio_path = tempfile.mkstemp(prefix="clipper_audio_", suffix=".wav")
        os.close(fd)
        try:
            rc, lines = self.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", audio_path])
            if rc != 0 or not Path(audio_path).is_file() or Path(audio_path).stat().st_size <= 44:
                raise RuntimeError("Couldn't extract audio from the video: " + " | ".join(lines[-3:]))
            return audio_path
        except Exception:
            try: os.unlink(audio_path)
            except OSError: pass
            raise

    def transcribe(self, audio, total):
        from faster_whisper import WhisperModel
        import ctranslate2
        c = self.c
        # Decode the audio once, before any model is loaded. Reading the audio does not depend on the GPU, CPU or compute type,
        # so a failure here can never be fixed by the fallbacks below. Fail once with a clear message instead.
        try:
            from faster_whisper import decode_audio
            if ensure_av_compat(): self.log("PyAV 19 or newer found: working around the option it removed (Setup > PyAV can switch to a matching version).")
            self.emit("prog", self.i, None, "Reading audio")
            wave = decode_audio(str(audio), sampling_rate=16000)
        except Cancelled: raise
        except Exception as e:
            if is_av_mismatch(e): raise RuntimeError(av_mismatch_help()) from e
            raise RuntimeError(f"Whisper could not read the audio: {e}") from e
        gpu = c["device"] == "cuda" or (c["device"] == "auto" and ctranslate2.get_cuda_device_count() > 0)
        # CPU compute types to try in order: int8 (fastest) -> int8_float32 (compat) -> float32 (safest).
        # Fall back through compatible CPU compute types if the installed CTranslate2 build rejects one.
        cpu_types = ["int8", "int8_float32", "float32"]
        last_err = None
        for use_gpu in ([True, False] if gpu else [False]):
            if use_gpu:
                attempts = [("float16", "GPU")]
            else:
                attempts = [(ct, "CPU") for ct in cpu_types]
            for compute_type, label in attempts:
                try:
                    self.log(f"Loading Whisper '{c['whisper_model']}' on {label} (compute_type={compute_type}; first use downloads it)...")
                    self.emit("prog", self.i, None, "Loading model")
                    cpu_threads = max(1, min(8, (os.cpu_count() or 4) - 1))
                    model = WhisperModel(
                        c["whisper_model"], device="cuda" if use_gpu else "cpu",
                        compute_type=compute_type, cpu_threads=cpu_threads, num_workers=1)
                    hing = is_hinglish(c["language"])
                    it, info = model.transcribe(wave, language="hi" if hing else (c["language"] or None), initial_prompt=HINGLISH_PROMPT if hing else None,
                                                beam_size=c["beam_size"], vad_filter=c["vad_filter"], word_timestamps=bool(c["edit_enabled"]))
                    segs = []
                    for s in it:
                        self.check()
                        seg = {"start": s.start, "end": s.end, "text": hinglish_text(s.text.strip(), sentence=True) if hing else s.text.strip()}
                        if c["edit_enabled"]: seg["words"] = [{"start": w.start, "end": w.end, "word": hinglish_text(w.word) if hing else w.word} for w in (s.words or [])]
                        segs.append(seg)
                        self.prog(min(s.end / total, 1), f"{s.end / 60:.1f} of {total / 60:.1f} min")
                    return segs
                except Cancelled: raise
                except Exception as e:
                    if is_av_mismatch(e): raise RuntimeError(av_mismatch_help()) from e
                    last_err = e
                    if use_gpu:
                        self.log(f"GPU failed ({e}). Falling back to the CPU.")
                        break  # break inner loop, outer loop continues to CPU
                    else:
                        # On CPU, try the next compute_type before giving up.
                        if compute_type != cpu_types[-1]:
                            self.log(f"CPU with {compute_type} failed ({e}). Trying next mode...")
                        # If this was the last attempt, just let the loop end;
                        # the clear error message below will be shown.
        # If we get here, every attempt failed. Show a clear, actionable message.
        if last_err:
            raise RuntimeError(
                f"Whisper could not run. Last error: {last_err}\n"
                "Common fixes:\n"
                "  - If you have an NVIDIA GPU: install the CUDA 12 libraries from the Setup page (nvidia-cublas-cu12, nvidia-cudnn-cu12), then restart Clipper.\n"
                "  - If you don't have an NVIDIA GPU: go to the Transcribe settings page and set 'Run Whisper on' to 'cpu' (not 'auto').\n"
                "  - If you still get 'mkl_malloc: failed to allocate memory', close other apps to free RAM, or use a smaller Whisper model (tiny or base)."
                + ("\n  - If the error mentions a missing DLL: install the Microsoft Visual C++ Redistributable (winget install -e --id Microsoft.VCRedist.2015+.x64), then restart Clipper." if WIN else "")
            ) from last_err
        return []

    # picking moments
    def pick(self, segs, total):
        c, n = self.c, self.c["num_clips"]
        if c["smart_pick"]: return self.smart_pick(segs, total)
        if not c["use_ai"]:
            self.log("AI is off: spreading clips evenly.")
            return self.finish([{"start": total * (k + .5) / n} for k in range(n)], segs, total)
        host = c["ollama_host"].rstrip("/")
        if not ollama_up(host):
            raise RuntimeError("Ollama isn't running. Press Start on the Setup page, or run: ollama serve (or turn off the AI picker).")
        # group segments into ~20 second lines, then split into chunks that fit the context window
        lines, cur = [], None
        for s in segs:
            if cur and s["end"] - cur[0] < 20: cur[1] += " " + s["text"]
            else:
                if cur: lines.append(f"[{int(cur[0])}s] {cur[1]}")
                cur = [s["start"], s["text"]]
        lines.append(f"[{int(cur[0])}s] {cur[1]}")
        budget, chunks, part, size = int(max(c["num_ctx"] - 1500, 1000) * 3), [], [], 0
        for ln in lines:
            if size + len(ln) > budget and part: chunks.append(part); part, size = [], 0
            part.append(ln); size += len(ln) + 1
        chunks.append(part)
        per = n if len(chunks) == 1 else max(2, math.ceil(n * 2 / len(chunks)))
        if len(chunks) > 1: self.log(f"Long video: asking the AI in {len(chunks)} parts.")
        focus = (f"\nFocus on: {c['focus']}." if c["focus"] else "") + hinglish_note(c)
        cands = []
        for k, ch in enumerate(chunks, 1):
            prompt = (f"You select short-form video moments from a transcript.\n"
                      f"Choose up to {per} distinct, self-contained moments from THIS transcript part.{focus}\n"
                      "Prioritize: (1) a compelling hook in the first seconds, (2) a complete thought or story, "
                      "(3) useful, surprising, emotional, funny, or actionable content, and (4) clear context without invented facts.\n"
                      "Avoid greetings, dead air, incomplete sentences, repeated points, and moments that need missing context. "
                      "Do not invent dialogue or timestamps. Use the timestamp nearest the beginning of each moment. "
                      "Score consistently: 9-10 exceptional standalone moment; 7-8 strong; 5-6 usable; 1-4 weak (do not return weak moments).\n"
                      "For each return start time in seconds, score 1-10, concise title, and an accurate Instagram caption "
                      "with a hook, 1-2 useful sentences, and up to 5 relevant hashtags. Return valid JSON only in this shape: "
                      '{"clips":[{"start":123,"score":8,"title":"...","description":"..."}]}\n\nTranscript:\n' + "\n".join(ch))
            cands += self.ask(prompt, f"part {k}/{len(chunks)}")
            self.prog(k / len(chunks), f"part {k}/{len(chunks)}")
        return self.finish(cands, segs, total)

    def ask(self, prompt, tag, key="clips"):  # key=None returns the whole JSON object instead of one list
        c, host = self.c, self.c["ollama_host"].rstrip("/")
        for attempt in range(1, c["ai_retries"] + 1):
            text, tokens = "", 0
            body = {"model": c["ollama_model"], "prompt": prompt, "format": "json", "stream": True,
                    "options": {"num_ctx": c["num_ctx"], "temperature": c["temperature"]}}
            try:
                with ollama_post(host, "/api/generate", body, 900) as r:
                    for line in r:
                        self.check()
                        if not line.strip(): continue
                        d = json.loads(line)
                        if "error" in d: raise OllamaError("Ollama: " + d["error"])
                        text += d.get("response", ""); tokens += 1
                        if tokens % 5 == 0: self.emit("prog", self.i, None, f"AI thinking, {tag}, {tokens} tokens")
                        if d.get("done"): break
            except Cancelled:
                raise
            except OllamaError:
                raise  # a real model error will not fix itself; don't waste retries on it
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise RuntimeError(f"Model '{c['ollama_model']}' isn't installed. Download it on the AI picker page, or run: ollama pull {c['ollama_model']}") from None
                detail = e.read(200).decode(errors="replace")
                if attempt < c["ai_retries"]:
                    self.log(f"Ollama request failed ({e.code}; attempt {attempt}/{c['ai_retries']}); retrying...")
                    self.check(); time.sleep(min(attempt, 3)); self.check(); continue
                raise RuntimeError(f"Ollama error {e.code}: {detail}") from None
            except (urllib.error.URLError, OSError, ValueError, RuntimeError) as e:
                if attempt < c["ai_retries"]:
                    self.log(f"Ollama stream failed ({e}; attempt {attempt}/{c['ai_retries']}); retrying...")
                    self.check(); time.sleep(min(attempt, 3)); self.check(); continue
                raise RuntimeError(f"Ollama request failed after {attempt} attempt(s): {e}") from e
            try:
                payload = json.loads(text)
                if key is None:
                    if isinstance(payload, dict): return payload
                    raise ValueError("JSON response was not an object")
                clips = payload.get(key) if isinstance(payload, dict) else None
                if isinstance(clips, list):
                    return clips
                raise ValueError(f"JSON response did not contain a {key} list")
            except (ValueError, TypeError, AttributeError):
                self.log(f"Bad AI output (attempt {attempt}). Retrying...")
        return []

    # smart picker -------------------------------------------------------------------------------
    def loudness(self, hop=0.5):
        """RMS level every `hop` seconds from the video's audio (cached next to the video); None when it can't be measured."""
        f = self.work / f"loudness_{hop}.json"
        if self.c["use_cache"] and f.is_file():
            try:
                v = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(v, list) and len(v) >= 10 and all(isinstance(x, (int, float)) for x in v): return v
            except (OSError, ValueError): pass
        try:
            rc, lines = self.run(["ffmpeg", "-v", "error", "-i", str(self.video), "-vn", "-af",
                                  f"aresample=16000,asetnsamples=n={int(16000 * hop)}:p=0,astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
                                  "-f", "null", "-"])
        except RuntimeError: return None
        vals = parse_loudness(lines)
        if rc != 0 or len(vals) < 10:
            self.log("Audio energy couldn't be measured, so moments are ranked on the transcript only."); return None
        try: f.write_text(json.dumps([round(x, 1) for x in vals]), encoding="utf-8")
        except OSError: pass
        return vals

    def rerank(self, cands, segs):
        """Ask the AI to rate the candidates and write their metadata. Returns {candidate index: (score, metadata)}."""
        c = self.c
        items = [(k + 1, w["end"] - w["start"], clip_text(" ".join(s["text"].strip() for s in segs[w["i"]:w["j"] + 1]))) for k, w in enumerate(cands)]
        budget, batches, cur, size = int(max(c["num_ctx"] - 2500, 1500) * 3), [], [], 0
        for it in items:
            if cur and size + len(it[2]) > budget: batches.append(cur); cur, size = [], 0
            cur.append(it); size += len(it[2]) + 40
        batches.append(cur)
        ids, out = {it[0] for it in items}, {}
        for b, batch in enumerate(batches, 1):
            self.check()
            for raw in self.ask(rerank_prompt(batch, c), f"rating {b}/{len(batches)}"):
                r = parse_rating(raw, {it[0] for it in batch} & ids)
                if r and r[0] - 1 not in out: out[r[0] - 1] = (r[1], r[2])
            self.prog(0.2 + 0.7 * b / len(batches), f"rating {b}/{len(batches)}")
        self.log(f"AI rated {len(out)} of {len(cands)} candidates.")
        return out

    def smart_pick(self, segs, total):
        c, n = self.c, self.c["num_clips"]
        loud = self.loudness() if self.video else None
        self.prog(0.1, "scoring moments")
        wins = rank_windows(segs, total, c["min_len"], c["max_len"], loud)
        if len(wins) < max(3, n): wins = rank_windows(segs, total, c["min_len"], c["max_len"], loud, relaxed=True) or wins  # transcript without sentence punctuation
        if not wins:
            self.log("Smart picker: no clip of the requested length fits this transcript (check the shortest and longest clip settings). Spreading clips evenly.")
            return self.finish([{"start": total * (k + .5) / n} for k in range(n)], segs, total)
        cands = select_candidates(wins, min(24, max(n * 3, 8)))
        self.log(f"Smart picker: scored {len(wins)} possible clips ({'audio and transcript' if loud else 'transcript only'}); {len(cands)} strong candidates.")
        rated = {}
        if c["use_ai"]:
            if ollama_up(c["ollama_host"].rstrip("/")):
                try: rated = self.rerank(cands, segs)
                except (OllamaError, RuntimeError) as e: self.log(f"The AI rating failed ({e}). Using the audio and transcript scores only.")
            else: self.log("Ollama isn't reachable, so clips are chosen from the audio and transcript scores only.")
        ranked = []
        for k, w in enumerate(cands):
            llm = rated[k][0] if k in rated else None
            ranked.append((0.35 * w["heur"] + 0.65 * llm if llm is not None else w["heur"] * (0.85 if rated else 1.0), k, llm))
        ranked.sort(reverse=True)
        gate, out, used, sigs, skipped = c["pick_min_score"], [], [], [], 0
        for final, k, llm in ranked:
            if len(out) >= n: break
            if rated and (llm if llm is not None else final) < gate: skipped += 1; continue
            clip = self.make_clip(cands[k], segs, total, final, rated.get(k, (None, None))[1])
            sig = self._content_signature(clip["start"], clip["end"], segs)
            if any(clip["start"] < ue and clip["end"] > us for us, ue in used) or any(self._similar_content(sig, s) for s in sigs): continue
            out.append(clip); used.append((clip["start"], clip["end"])); sigs.append(sig)
        if not out and ranked:
            self.log("No moment reached the minimum AI score, so the best one is used anyway.")
            clip = self.make_clip(cands[ranked[0][1]], segs, total, ranked[0][0], rated.get(ranked[0][1], (None, None))[1]); out.append(clip)
        if len(out) < n: self.log(f"Only {len(out)} moment(s) were strong enough" + (f" ({skipped} rated below {gate:g})" if skipped else "") + ".")
        for k, cl in enumerate(out, 1): self.log(f"Clip {k}: {cl['score']}/10, {fmt_t(cl['start'])} to {fmt_t(cl['end'])}: {cl['title']}")
        return out

    def idf(self, segs):
        if getattr(self, "_idf", None) is None or self._idf[0] is not segs: self._idf = (segs, keyword_idf(segs))
        return self._idf[1]

    def make_clip(self, w, segs, total, final, meta):
        """A finished clip dict: refined start and end plus title, hook, caption, hashtags and keywords."""
        c = self.c
        a, b = refine_bounds(w, segs, c["min_len"], total)
        a, b = max(0.0, a - max(0.0, float(c["pad_start"]))), min(total, b + max(0.0, float(c["pad_end"])))
        fb, m = fallback_meta(" ".join(s["text"].strip() for s in segs[w["i"]:w["j"] + 1]), self.idf(segs)), meta or {}
        title = clean_title(m.get("title")) or fb["title"]
        caption = (m.get("caption") or "").strip() or fb["caption"]
        extras = [t for t in re.split(r"[\s,]+", c["extra_hashtags"]) if t.strip("#")]
        tags = clean_tags(m.get("hashtags") or fb["hashtags"], extras, c["hashtag_count"])
        credit = f"Credit: {self.creator}" if c["credit_line"] and self.creator else ""
        return {"start": a, "end": b, "title": title, "description": compose_caption(caption, tags, credit), "use": True, "score": round(final, 1),
                "hook": m.get("hook", "").strip(), "hashtags": tags, "keywords": m.get("keywords") or fb["keywords"], "why": w.get("why", []), "ratings": m.get("ratings", {})}

    def snap(self, start, segs, total):
        c = self.c
        i = min(range(len(segs)), key=lambda k: abs(segs[k]["start"] - start))
        if not c["snap"]: return start, min(total, start + (c["min_len"] + c["max_len"]) / 2), i
        a, j = segs[i]["start"], i
        while j < len(segs) - 1 and segs[j]["end"] - a < c["min_len"]: j += 1
        while j > i and segs[j]["end"] - a > c["max_len"]: j -= 1
        return a, segs[j]["end"], i

    @staticmethod
    def _content_signature(start, end, segs):
        """Build a normalized transcript signature for a candidate time range."""
        words = []
        for segment in segs:
            try:
                if float(segment["end"]) > start and float(segment["start"]) < end:
                    words.extend(re.findall(r"\w+", str(segment.get("text", "")).lower()))
            except (KeyError, TypeError, ValueError):
                continue
        return words

    @staticmethod
    def _similar_content(left, right):
        """Conservative duplicate test; short clips need near-identical wording."""
        if not left or not right:
            return False
        a, b = " ".join(left), " ".join(right)
        if min(len(left), len(right)) < 8:
            return a == b
        # Compare token sets and sequence similarity; require a high threshold to
        # avoid discarding distinct moments that merely share common vocabulary.
        sa, sb = set(left), set(right)
        jaccard = len(sa & sb) / max(1, len(sa | sb))
        sequence = SequenceMatcher(None, a, b, autojunk=False).ratio()
        return jaccard >= 0.82 or sequence >= 0.90

    def finish(self, cands, segs, total):
        c, out, used, used_content = self.c, [], [], []
        rejected = {}

        def reject(reason):
            rejected[reason] = rejected.get(reason, 0) + 1

        # A missing transcript cannot be snapped safely. Return no AI-picked clips
        # rather than letting an empty segment list crash the whole pipeline.
        if not segs:
            self.log("AI selection skipped: the transcript contains no usable segments.")
            return []
        try:
            total = float(total)
            if not math.isfinite(total) or total <= 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            self.log("AI selection skipped: the video duration is invalid.")
            return []

        # Normalize and validate before sorting. In particular, do not call .get()
        # on arbitrary Ollama output or allow NaN scores to affect sort ordering.
        valid = []
        if not isinstance(cands, (list, tuple)):
            cands = []
            reject("response was not a candidate list")
        for candidate in cands:
            if not isinstance(candidate, dict):
                reject("candidate was not an object")
                continue
            try:
                start = float(candidate["start"])
                if not math.isfinite(start) or start < 0 or start >= total:
                    reject("start timestamp was outside the video")
                    continue
            except (KeyError, TypeError, ValueError, OverflowError):
                reject("start timestamp was missing or invalid")
                continue

            raw_score = candidate.get("score", 5)
            try:
                score = float(raw_score)
                if not math.isfinite(score) or not 1 <= score <= 10:
                    reject("score was outside 1-10 or invalid")
                    continue
            except (TypeError, ValueError, OverflowError):
                reject("score was outside 1-10 or invalid")
                continue
            valid.append((score, start, candidate))

        for score, start, candidate in sorted(valid, key=lambda item: -item[0]):
            try:
                a, b, i = self.snap(start, segs, total)
                a, b = float(a), float(b)
                if not (math.isfinite(a) and math.isfinite(b) and 0 <= a < b <= total):
                    reject("snapped clip boundaries were invalid")
                    continue
                signature = self._content_signature(a, b, segs)
                if any(self._similar_content(signature, previous) for previous in used_content):
                    reject("repeated transcript content")
                    continue
                title = str(candidate.get("title") or " ".join(segs[i]["text"].split()[:6])).strip()
                desc = str(candidate.get("description") or f"{title}\n\n#reels #shorts #fyp #viral #video").strip()
                pad_start = max(0.0, float(c["pad_start"]))
                pad_end = max(0.0, float(c["pad_end"]))
                clip_start = max(0.0, a - pad_start)
                clip_end = min(total, b + pad_end)
                if not (math.isfinite(clip_start) and math.isfinite(clip_end) and clip_start < clip_end):
                    reject("final clip boundaries were invalid")
                    continue
                # A candidate that overlaps an already selected clip is trimmed to fit,
                # losing as little as possible; only what still collides is dropped.
                for used_start, used_end in used:
                    if clip_start < used_end and clip_end > used_start:
                        if b <= used_start: clip_end = used_start           # sits before the old clip: shave the tail
                        elif a >= used_end: clip_start = used_end           # starts after the old clip: shave the head
                        elif used_end - clip_start <= clip_end - used_start: clip_start = used_end
                        else: clip_end = used_start
                if clip_end - clip_start < min(c["min_len"], b - a):
                    reject("duplicate or overlapping clip after trimming")
                    continue
                if any(clip_start < used_end and clip_end > used_start for used_start, used_end in used):
                    reject("duplicate or overlapping clip after padding")  # safety net
                    continue
            except (IndexError, KeyError, TypeError, ValueError, OverflowError):
                reject("candidate could not be converted into a clip")
                continue
            used.append((clip_start, clip_end))
            used_content.append(signature)
            out.append({"start": clip_start, "end": clip_end, "title": title, "description": desc, "use": True})
            if len(out) >= c["num_clips"]: break

        if rejected:
            details = "; ".join(f"{count} {reason}" for reason, count in rejected.items())
            self.log(f"AI selection: rejected {sum(rejected.values())} candidate(s) ({details}).")
        self.log(f"AI selection: kept {len(out)} of {len(cands)} candidate(s).")
        return sorted(out, key=lambda x: x["start"])

    def ask_review(self, clips, video, total):
        holder = {"ev": threading.Event(), "res": None,
                  "duration": total,
                  "min_len": self.c["min_len"], "max_len": self.c["max_len"]}
        self.emit("review", clips, str(video), holder)
        while not holder["ev"].wait(0.2):
            try:
                self.check()
            except Cancelled:
                self.emit("review_cancel")
                raise
        if holder["res"] is None: raise Cancelled()
        return [x for x in holder["res"] if x["use"]]

    # rendering
    def fit_expr(self, W, H): return fit_filter(self.c, W, H)
    def split_expr(self, W, H, pre, lay): return split_filter(self.c, W, H, pre, lay)

    def vf(self, W, H, sub, pre="", ass=None, wm=None, layout=None):
        c = self.c
        if layout: fit = self.split_expr(W, H, pre, layout)
        else:
            fit = self.fit_expr(W, H)
            if pre: fit = pre + "," + fit
        if sub:
            h = c["cap_color"].lstrip("#").upper()
            h = h if re.fullmatch(r"[0-9A-F]{6}", h) else "FFFFFF"
            al, mv = cap_place(c, H, layout)
            fit += (f",subtitles={sub}:force_style='FontSize={c['cap_size']},Alignment={al},Outline={c['cap_outline']},"
                    f"Bold={int(c['cap_bold'])},MarginV={mv:.0f},PrimaryColour=&H00{h[4:6]}{h[2:4]}{h[0:2]}'")
        if ass: fit += ",ass=" + ass
        if wm: fit += ",ass=" + wm
        return fit

    # auto-edit: planning
    def plan_edits(self, clips, segs):
        c = self.c; host = c["ollama_host"].rstrip("/")
        ai = c["use_ai"] and ollama_up(host)
        if c["use_ai"] and not ai: self.log("Ollama isn't reachable, so the edits are planned with simple rules.")
        for k, cl in enumerate(clips, 1):
            self.check()
            length = cl["end"] - cl["start"]
            words = clip_words(segs, cl["start"], cl["end"])
            raw = None
            if ai and words:
                try: raw = self.ask(plan_prompt(words, length, c["focus"]), f"edit plan {k}/{len(clips)}", key=None)
                except (OllamaError, RuntimeError) as e: self.log(f"The AI edit plan failed ({e}). Using simple rules for this clip.")
                if not isinstance(raw, dict): raw = None
            if cl.get("hook"): raw = dict(raw or {}, hook=cl["hook"])  # the hook written together with the title and caption
            plan = make_plan(raw, words, length, c)
            cl["edit"] = plan
            self.log(f"Clip {k}: {len(plan['zooms'])} zoom(s), {len(plan['callouts'])} text pop-up(s)" + (", hook title" if plan["hook"] else "")
                     + (" (AI plan)." if plan["source"] == "ai" else " (simple rules)."))
            self.prog(k / len(clips), f"clip {k} of {len(clips)}")

    # auto-edit: ffmpeg pieces
    @staticmethod
    def has_filter(name):
        return re.search(r"^\s*\S+\s+%s\s" % re.escape(name), sh(["ffmpeg", "-hide_banner", "-filters"], timeout=20)[1], re.M) is not None

    @staticmethod
    def dims(path):
        """(width, height) of the video as ffmpeg sees it, honouring phone rotation; None when unreadable."""
        ffprobe = which("ffprobe")
        if not ffprobe: return None
        try:
            r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:stream_side_data=rotation:stream_tags=rotate",
                                "-of", "json", str(path)], capture_output=True, timeout=20, **proc_kw())
            s = json.loads(r.stdout)["streams"][0]
            w, h, rot = int(s["width"]), int(s["height"]), 0
            for sd in s.get("side_data_list") or []:
                if "rotation" in sd: rot = int(float(sd["rotation"]))
            rot = rot or int(float((s.get("tags") or {}).get("rotate", 0)))
            return (h, w) if abs(rot) % 180 == 90 else (w, h)
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, IndexError, TypeError): return None

    def sfx_files(self, work):
        """Synthesise two tiny sound effects once (no files to ship); returns {name: path} or None."""
        specs = {"whoosh": "anoisesrc=color=pink:amplitude=0.7:duration=0.55:sample_rate=48000,highpass=f=500,lowpass=f=6000,afade=t=in:st=0:d=0.28,afade=t=out:st=0.28:d=0.27",
                 "pop": "aevalsrc=0.9*sin(2*PI*680*t)*exp(-26*t):s=48000:d=0.18"}
        paths = {}
        for n, graph in specs.items():
            p = work / f"sfx_{n}.wav"
            if not (p.is_file() and p.stat().st_size > 1000):
                rc, _ = self.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", graph, "-ac", "2", "-ar", "48000", str(p)])
                if rc != 0 or not p.is_file(): return None
            paths[n] = p
        return paths

    @staticmethod
    def zoom_chain(plan, dims):
        """Smooth zoom (and slow pan) as one scale+crop pair driven by the frame time, in the source picture."""
        zs = plan.get("zooms") or []
        if not zs or not dims: return ""
        sw, sh = dims
        ease = lambda x: "(0.5-0.5*cos(PI*clip(%s,0,1)))" % x
        z, pan = "1", ""
        for q in zs:
            up, down = "(t-%.3f)/%.3f" % (q["t0"], q["r"]), "(t-%.3f)/%.3f" % (q["t1"], q["r"])
            z += "+%.3f*(%s-%s)" % (q["amp"], ease(up), ease(down))
            if q["pan"]:
                pan += "%+.3f*between(t,%.3f,%.3f)*(2*clip((t-%.3f)/%.3f,0,1)-1)" % (0.45 * q["pan"], q["t0"], q["end"], q["t0"], q["end"] - q["t0"])
        return ("scale=w='2*ceil(iw*(%s)/2)':h='2*ceil(ih*(%s)/2)':eval=frame:flags=bicubic,"
                "crop=%d:%d:x='(in_w-out_w)/2*(1%s)':y='(in_h-out_h)*0.4'" % (z, z, sw, sh, pan))

    def audio_graph(self, plan, sfx):
        """Mix the sound effects into the clip's audio. Returns (extra ffmpeg inputs, filter graph) or ([], None)."""
        wh = [max(0, int(round((q["t0"] - 0.05) * 1000))) for q in plan.get("zooms") or []]
        pp = [int(round(t * 1000)) for t in ([0.2] if plan.get("hook") else []) + [q["t"] for q in plan.get("callouts") or []]]
        if not wh and not pp: return [], None
        parts, mix, inputs, n = ["[0:a:0]aformat=sample_rates=48000:channel_layouts=stereo[am]"], ["[am]"], [], 1
        for tag, path, delays, vol in (("w", sfx["whoosh"], wh, 0.30), ("p", sfx["pop"], pp, 0.45)):
            if not delays: continue
            inputs += ["-i", str(path)]
            labs = ["[%s%d]" % (tag, j) for j in range(len(delays))]
            parts.append("[%d:a]" % n + (("asplit=%d" % len(delays)) + "".join(labs) if len(delays) > 1 else "anull" + labs[0]))
            for j, d in enumerate(delays):
                parts.append("%sadelay=%d|%d,volume=%.2f[%sd%d]" % (labs[j], d, d, vol, tag, j)); mix.append("[%sd%d]" % (tag, j))
            n += 1
        parts.append("".join(mix) + "amix=inputs=%d:duration=first:normalize=0,alimiter=limit=0.97%s[aout]" % (len(mix), ("," + LOUDNORM) if self.c["loudnorm"] else ""))
        return inputs, ";".join(parts)

    def edit_cmd(self, a, length, video, W, H, ass, plan, dims, sfx, codec, out_path, wm=None, layout=None):
        c = self.c
        graph = "[0:v]" + self.vf(W, H, None, self.zoom_chain(plan, dims), ass, wm, layout) + "[v]"
        inputs, ag = self.audio_graph(plan, sfx) if sfx else ([], None)
        if ag: graph += ";" + ag
        aud = ["-c:a", "aac", "-b:a", c["audio_bitrate"]] + (["-af", LOUDNORM] if c["loudnorm"] and not ag else [])
        return ["ffmpeg", "-n", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-ss", str(a), "-t", str(length), "-i", str(video), *inputs,
                "-filter_complex", graph, "-map", "[v]", "-map", "[aout]" if ag else "0:a:0?", *codec, "-pix_fmt", "yuv420p", *aud,
                "-movflags", "+faststart", *split_args(c["extra_ffmpeg"]), str(out_path)]

    def render(self, clips, segs, video, work, out):
        c = self.c
        rw, rh = map(int, c["aspect"].split(":"))
        W = c["out_width"] // 2 * 2; H = round(W * rh / rw / 2) * 2
        have_nv = "h264_nvenc" in sh(["ffmpeg", "-hide_banner", "-encoders"])[1]
        if have_nv:  # one tiny test encode, so a broken GPU encoder fails once instead of once per clip
            rc, _ = self.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2",
                              "-pix_fmt", "yuv420p", "-c:v", "h264_nvenc", "-f", "null", "-"])
            if rc != 0:
                have_nv = False
                self.log("The GPU encoder (NVENC) isn't usable here, so the CPU encoder is used.")
        order = {"auto": ["nvenc", "x264"] if have_nv else ["x264"], "nvenc": ["nvenc", "x264"], "x264": ["x264"]}[c["encoder"]]
        if not have_nv: order = [e for e in order if e != "nvenc"] or ["x264"]
        q = str(c["quality"])
        codecs = {"nvenc": ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", q, "-b:v", "0"],
                  "x264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", q]}
        audio = ["-c:a", "aac", "-b:a", c["audio_bitrate"]] + (["-af", LOUDNORM] if c["loudnorm"] else [])
        edit_on = bool(c["edit_enabled"]) and any(cl.get("edit") for cl in clips)
        has_ass = (edit_on or bool(self.wm_text)) and self.has_filter("ass")
        ass_ok = edit_on and has_ass
        if edit_on and not has_ass: self.log("This ffmpeg build has no ASS (libass) support, so animated captions and graphics are skipped.")
        wm = None
        if self.wm_text:
            if has_ass: wm = "wm.ass"; (work / wm).write_text(build_wm_ass(c, W, H, self.wm_text), encoding="utf-8")
            else: self.log("This ffmpeg build has no ASS (libass) support, so the watermark is skipped.")
        split = bool(c["split_enabled"] and self.regions)
        sd = self.dims(video) if split or (edit_on and c["edit_zoom"]) else None
        layout, ys = None, None
        if split:
            if sd:
                cam_px = region_px(self.regions[0], *sd)
                main_frac = self.regions[1] if len(self.regions) > 1 and self.regions[1] else (0.0, 0.0, 1.0, 1.0)
                main_px, patch, how = exclude_cam(region_px(main_frac, *sd), cam_px, c["split_overlap"])
                layout = {"cam": cam_px, "main": main_px, "patch": patch, "cam_h": layout_sizes(c)[2], "top": c["split_cam_pos"] == "top"}
                if how == "crop": self.log("The face cam is on top of the content box, so the content panel uses only the part without the cam.")
                elif how == "fill": self.log("The face cam is on top of the content box, so it is painted over in the content panel.")
                fy = layout["cam_h"] / H  # keep the hook and pop-ups next to the seam, off the face
                # distance of the hook / pop-ups from the seam (as a fraction of the screen), kept clear of wherever the captions are
                d1, d2 = {("top", "middle"): (0.14, 0.26), ("bottom", "middle"): (0.17, 0.29), ("bottom", "bottom"): (0.26, 0.38)}.get(
                    ("top" if layout["top"] else "bottom", c["cap_position"]), (0.09, 0.20))
                ys = (fy + d1, fy + d2) if layout["top"] else (1 - fy - d1, 1 - fy - d2)
                if layout["cam"][2] * 2.5 < W:
                    self.log(f"The face cam is only {layout['cam'][2]} px wide in the source and gets stretched to {W} px, so it will look soft. A higher download quality helps.")
            else: self.log("Couldn't read the video size, so the split layout is skipped.")
        dims = (layout["main"][2:] if layout else sd) if edit_on and c["edit_zoom"] else None
        if edit_on and c["edit_zoom"] and not dims: self.log("Couldn't read the video size, so zoom is skipped.")
        sfx = self.sfx_files(work) if edit_on and c["edit_sfx"] else None
        if edit_on and c["edit_sfx"] and not sfx: self.log("Couldn't create the sound effects, so they are skipped.")
        files = []
        for k, cl in enumerate(clips, 1):
            a, b = cl["start"], cl["end"]; length = b - a
            name = output_stem(out, c.get("prefix", "clip"), k)
            rows = [s for s in segs if s["end"] > a and s["start"] < b]
            sub = None
            if c["captions"] and rows:
                sub = f"sub_{k}.srt"
                (work / sub).write_text("".join(f"{n}\n{srt_t(s['start'] - a)} --> {srt_t(min(s['end'], b) - a)}\n{s['text']}\n\n" for n, s in enumerate(rows, 1)), encoding="utf-8")
                if c["write_srt"]: shutil.copy(work / sub, out / f"{name}.srt")
            plan, ass = (cl.get("edit") if edit_on else None), None
            if plan and ass_ok:
                text = build_ass(c, W, H, length, clip_words(segs, a, b), plan, ys, layout)
                if text: ass = f"sub_{k}.ass"; (work / ass).write_text(text, encoding="utf-8")
            output_path = out / f"{name}.mp4"
            # edited render first; if it fails on every encoder, fall back to the plain captioned clip
            attempts = ([(e, True) for e in order] if plan else []) + [(e, False) for e in order]
            done = False
            for enc, edited in attempts:
                self.log(f"Rendering {name} ({fmt_t(a)} to {fmt_t(b)}, {'GPU' if enc == 'nvenc' else 'CPU'} encoder{', with edits' if edited else ''})...")
                def on(line, k=k, length=length):
                    m = re.match(r"out_time_us=(\d+)", line)
                    if m: self.prog(min(1, (k - 1 + int(m.group(1)) / 1e6 / length) / len(clips)), f"clip {k} of {len(clips)}")
                if edited:
                    cmd = self.edit_cmd(a, length, video, W, H, ass, plan, dims, sfx, codecs[enc], output_path, wm, layout)
                else:
                    cmd = ["ffmpeg", "-n", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-ss", str(a), "-t", str(length), "-i", str(video),
                           "-vf", self.vf(W, H, sub, wm=wm, layout=layout), *codecs[enc], "-pix_fmt", "yuv420p", *audio, "-movflags", "+faststart", *split_args(c["extra_ffmpeg"]), str(output_path)]
                rc, lines = self.run(cmd, cwd=work, on_line=on)
                if rc == 0 and output_path.is_file() and output_path.stat().st_size > 0:
                    done = True; break
                try: output_path.unlink()  # remove an incomplete file before trying the fallback
                except OSError: pass
                self.log(f"{enc} failed: " + " | ".join(x for x in lines if not re.match(r"\w+=", x))[-300:])
                if edited and enc == order[-1]: self.log("The edited render failed, so this clip is rendered without edits.")
            if not done: self.log(f"Skipped {name}: it couldn't be rendered."); continue
            yt = youtube_fields(cl["title"], cl["description"], cl.get("keywords"), self.src_url)
            if c["write_txt"]: (out / f"{name}.txt").write_text(post_text(cl, yt), encoding="utf-8")
            cover = None
            if c["cover_export"]:  # a frame just after the hook has appeared
                cover = out / f"{name}_cover.jpg"
                rc, _ = self.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{min(1.0, max(0.2, length * 0.3)):.2f}", "-i", str(output_path), "-frames:v", "1", "-q:v", "2", str(cover)])
                if rc != 0 or not cover.is_file(): cover = None
            files.append({"path": str(out / f"{name}.mp4"), "title": cl["title"], "description": cl["description"], "cover": str(cover) if cover else "",
                          "yt_title": yt["title"], "yt_description": yt["description"], "yt_tags": yt["tags"]})
        return files

