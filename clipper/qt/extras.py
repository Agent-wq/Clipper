import html, json, os, re, shutil, subprocess, threading, time, urllib.error, webbrowser
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QVBoxLayout, QWidget)
from clipper.sysutil import WIN, NOWIN, kill_tree, quiet_kw, which
from clipper.setup import (SUGGESTED, WHISPER_MB, WHISPER_REPOS, check_all, elevation, gpu_info, have_mod, is_admin, merge_plans,
                           mgr_label, ollama_models, ollama_post, ollama_up, plan_for, run_plan, setup_script, system_info, whisper_cached)
from clipper.settings import SETTINGS_FILE
from clipper.moments import YT_CATEGORIES
from clipper.qt.dialogs import UploadDialog
from clipper.qt.theme import DANGER, FG, MUTED, SUCCESS
from clipper.qt.widgets import Combo

WARN = "#e8b86d"


class Extras:
    """Setup page, Ollama model download and YouTube upload. Mixed into MainWindow."""

    def init_extras(self):
        self.model_cbs, self.models_loaded = [], False
        self.pulling, self.pull_cancel = False, threading.Event()
        self.job, self.job_busy, self.setup_info, self.asked_setup = None, False, None, False
        self.uploaded, self.yt_cancel, self.yt_running = set(), None, False
        QTimer.singleShot(600, self.setup_check)

    def page_shown(self, name):
        if name == "Picking" and not self.models_loaded:
            self.models_loaded = True; self.refresh_models(quiet=True)

    # ───────── small UI helpers ─────────
    def _card(self, title=None, sub=None):
        card = QFrame(); card.setObjectName("card")
        v = QVBoxLayout(card); v.setContentsMargins(24, 18, 24, 18); v.setSpacing(8)
        if title:
            t = QLabel(title); t.setStyleSheet("font-size:11pt;font-weight:700;"); v.addWidget(t)
        if sub:
            s = QLabel(sub); s.setObjectName("hint"); s.setWordWrap(True); v.addWidget(s)
        return card, v

    @staticmethod
    def _clear(layout):
        while layout.count():
            w = layout.takeAt(0).widget()
            if w: w.deleteLater()

    @staticmethod
    def _btn(text, fn, primary=False):
        b = QPushButton(text); b.setCursor(Qt.PointingHandCursor)
        if primary: b.setObjectName("primary")
        b.clicked.connect(lambda _=False: fn())
        return b

    def status_row(self, box, state, name, why, detail, buttons):
        col = {"ok": SUCCESS, "missing": DANGER, "warn": WARN, "skip": MUTED}[state]
        row = QWidget(); h = QHBoxLayout(row); h.setContentsMargins(0, 6, 0, 6); h.setSpacing(12)
        dot = QLabel("●"); dot.setStyleSheet(f"color:{col};")
        mid = QVBoxLayout(); mid.setSpacing(2); w = QLabel(why); w.setObjectName("hint"); w.setWordWrap(True)
        mid.addWidget(QLabel(name)); mid.addWidget(w)
        d = QLabel(detail); d.setObjectName("hint"); d.setWordWrap(True); d.setFixedWidth(190); d.setAlignment(Qt.AlignRight | Qt.AlignTop)
        h.addWidget(dot, 0, Qt.AlignTop); h.addLayout(mid, 1); h.addWidget(d, 0, Qt.AlignTop)
        for text, cmd in buttons: h.addWidget(self._btn(text, cmd), 0, Qt.AlignTop)
        box.addWidget(row)

    # ───────── Setup page ─────────
    def build_setup(self):
        sc, lay = self.make_page("Setup", "See what is installed and install what is missing. Nothing runs unless you press a button.")
        top, v = self._card()
        self.sys_lbl = QLabel("Checking this computer..."); self.sys_lbl.setWordWrap(True); v.addWidget(self.sys_lbl)
        bt = QHBoxLayout(); bt.setSpacing(10)
        bt.addWidget(self._btn("Check again", self.setup_check))
        bt.addWidget(self._btn("Install enabled dependencies", self.setup_install_all, primary=True))
        bt.addWidget(self._btn("Save setup script", self.setup_save_script))
        bt.addWidget(self._btn("Copy all commands", self.setup_copy_all)); bt.addStretch(1)
        v.addLayout(bt); lay.addWidget(top)
        boxes = []
        for title, sub in (("Requirements", "Programs and Python packages Clipper uses."),
                           ("Ollama", "Optional local AI service. Required only when the AI picker is enabled."),
                           ("Whisper models", "Speech models. Each is downloaded once and reused. The size you use is set on the Transcribe page.")):
            card, cv = self._card(title, sub); box = QVBoxLayout(); box.setSpacing(0); cv.addLayout(box); lay.addWidget(card); boxes.append(box)
        self.dep_box, self.svc_box, self.wh_box = boxes
        card, cv = self._card()
        head = QHBoxLayout(); t = QLabel("Install log"); t.setStyleSheet("font-size:11pt;font-weight:700;"); head.addWidget(t); head.addStretch(1)
        self.job_stop = self._btn("Cancel", self.job_cancel); self.job_stop.setEnabled(False); head.addWidget(self.job_stop); cv.addLayout(head)
        self.job_pb = QProgressBar(); self.job_pb.setRange(0, 0); self.job_pb.setTextVisible(False); self.job_pb.hide(); cv.addWidget(self.job_pb)
        self.setup_log = QPlainTextEdit(); self.setup_log.setObjectName("log"); self.setup_log.setReadOnly(True)
        self.setup_log.setMaximumBlockCount(2000); self.setup_log.setFixedHeight(220); cv.addWidget(self.setup_log)
        lay.addWidget(card); lay.addStretch(1)
        self.pages["Setup"] = sc; self.stack.addWidget(sc)

    def slog(self, t):
        col = DANGER if t.startswith(("Error", "Couldn't", "FAILED")) else FG
        self.setup_log.appendHtml(f'<pre style="margin:0;color:{col}">{html.escape(t)}</pre>')

    def on_setup_log(self, t): self.slog(t)

    def setup_check(self):
        host = self.get["ollama_host"]().rstrip("/")
        def run():
            nv = gpu_info(); name, mgr = system_info(); rows = check_all(nv is not None)
            fw = any(r["id"] == "fw" and r["state"] == "ok" for r in rows)
            wgpu = None
            if nv and fw:
                try: import ctranslate2; wgpu = ctranslate2.get_cuda_device_count() > 0
                except Exception: wgpu = False
            import sys
            self.emit("setup_checked", dict(sysname=name, mgr=mgr, gpu=nv, py=sys.version.split()[0], rows=rows, up=ollama_up(host), models=ollama_models(host),
                                            wgpu=wgpu, whisper={n: whisper_cached(n) for n in WHISPER_REPOS}, fw=fw, host=host))
        threading.Thread(target=run, daemon=True).start()

    def on_setup_checked(self, i):
        self.setup_info = i; nv = i["gpu"]; mgr = i["mgr"]
        gpu = (f"NVIDIA GPU: {nv[0]}" + (f", {nv[1]} GB VRAM" if nv[1] else "")) if nv else "No NVIDIA GPU found, so Whisper and encoding use the CPU"
        if nv and i["wgpu"] is not None: gpu += ". Whisper can use it." if i["wgpu"] else ". Whisper can't use it yet: install the CUDA 12 libraries below."
        self.sys_lbl.setText(f"{i['sysname']}  ·  package manager: {mgr_label(mgr)}  ·  Python {i['py']}\n{gpu}")
        if nv:
            fit = f", {nv[1]} GB. Models up to about {max(nv[1] - 2, 1):.0f} GB fit fully on it. Larger ones run partly on the CPU and are slower." if nv[1] else "."
            self.gpu_note.setText(f"Your GPU: {nv[0]}{fit}")
        else: self.gpu_note.setText("No NVIDIA GPU found, so models run on the CPU. Prefer the small ones.")
        can_elevate = WIN or is_admin() or bool(shutil.which("pkexec"))
        self._clear(self.dep_box)
        for d in i["rows"]:
            tag = {"recommended": "  ·  recommended", "optional": "  ·  optional"}.get(d["level"], "")
            pl = plan_for(d, mgr, nv is not None); btns = []
            if d["state"] in ("missing", "warn"):
                if pl["kind"] in ("pip", "winget", "download") or (pl["kind"] == "sys" and (not pl["root"] or can_elevate)):
                    btns.append(("Install", lambda d=d: self.setup_install([d])))
                btns.append(("Copy command", lambda t=pl["text"]: self.copy_text(t)))
                if pl.get("url"): btns.append(("Website", lambda u=pl["url"]: webbrowser.open(u)))
            elif d["state"] == "ok" and d.get("updatable"): btns.append(("Update", lambda d=d: self.setup_install([d], upgrade=True)))
            self.status_row(self.dep_box, d["state"], d["name"] + tag, d["why"], d["detail"], btns)
        self._clear(self.svc_box)
        have = any(d["id"] == "ollama" and d["state"] == "ok" for d in i["rows"]); b = []
        if i["up"]: st, det = "ok", f"Running at {i['host']}" + (f"  ·  {len(i['models'])} model(s) installed" if i["models"] is not None else "")
        elif have: st, det = "warn", "Installed but not running"; b.append(("Start", self.setup_start_ollama))
        else: st, det = "missing", "Not installed"
        b.append(("Manage models", lambda: self.show_page("Picking")))
        self.status_row(self.svc_box, st, "Ollama service", "Optional; needed only when the AI picker is enabled.", det, b)
        self._clear(self.wh_box)
        for n in WHISPER_REPOS:
            got = i["whisper"][n]; btn = [] if got or not i["fw"] else [("Download", lambda n=n: self.setup_whisper(n))]
            size = f"About {WHISPER_MB[n] / 1000:.1f} GB" if WHISPER_MB[n] >= 1000 else f"About {WHISPER_MB[n]} MB"
            self.status_row(self.wh_box, "ok" if got else "warn", n, size, "downloaded" if got else ("not downloaded yet" if i["fw"] else "install faster-whisper first"), btn)
        missing = any(d["state"] == "missing" for d in i["rows"])
        self.nav["Setup"].setText("Setup  ●" if missing else "Setup")
        if not self.asked_setup and missing: self.asked_setup = True; QTimer.singleShot(300, self.offer_install)
        if i["models"] is not None: self.on_models(i["models"], True)

    def todo_rows(self):
        i = self.setup_info
        if not i: return []
        todo = [d for d in i["rows"] if d["state"] in ("missing", "warn")]
        for flag, dep in (("use_ai", "ollama"), ("yt_enabled", "yt"), ("face_track", "cv")):
            if not self.get[flag](): todo = [d for d in todo if d["id"] != dep]
        return todo

    def offer_install(self):
        """On launch, offer to install what the app can't run without, in one click."""
        if not self.setup_info or self.job_busy or self.running: return
        todo = self.todo_rows()
        if not any(d["state"] == "missing" for d in todo): return
        names = "\n".join("   -  " + d["name"] + (" (large download, about 1 GB)" if d["id"] == "cuda" else "") for d in todo)
        if QMessageBox.question(self, "Clipper setup", "Clipper needs a few things that aren't installed yet:\n\n" + names +
                                "\n\nInstall them now? You can follow the progress on the Setup page.") == QMessageBox.Yes:
            self.show_page("Setup"); self.setup_install(todo)

    def copy_text(self, t):
        QApplication.clipboard().setText(t); self.slog("Copied: " + t)

    def setup_copy_all(self):
        i = self.setup_info
        if not i: return
        todo = self.todo_rows()
        if not todo: self.slog("Everything required by the current settings is installed."); return
        self.copy_text("\n".join(pl["text"] for pl in merge_plans([plan_for(d, i["mgr"], i["gpu"] is not None) for d in todo], i["mgr"])))

    def setup_save_script(self):
        mgr = (self.setup_info or {}).get("mgr") or system_info()[1]
        name, flt = ("setup_clipper.ps1", "PowerShell script (*.ps1)") if WIN else ("setup_clipper.sh", "Shell script (*.sh)")
        path = QFileDialog.getSaveFileName(self, "Save setup script", name, flt)[0]
        if not path: return
        with open(path, "w", encoding="utf-8", newline="") as fh: fh.write(setup_script(mgr))
        if WIN: self.slog(f"Saved {path}. Run it with: powershell -ExecutionPolicy Bypass -File \"{Path(path).name}\"")
        else: os.chmod(path, 0o755); self.slog(f"Saved {path}. Run it with: bash {Path(path).name}")

    def start_job(self, title, fn, cancellable=True):
        if self.job_busy: QMessageBox.information(self, "Setup", "Another task is still running. Wait for it or press Cancel in the install log."); return
        self.job_busy = True; job = self.job = {"cancel": False, "p": None}
        self.job_pb.show(); self.job_stop.setEnabled(cancellable); self.slog("— " + title)
        def run():
            ok = False
            try: ok = bool(fn(lambda t: self.emit("setup_log", t), job))
            except Exception as e: self.emit("setup_log", f"Error: {e}")
            self.emit("setup_done", title, ok)
        threading.Thread(target=run, daemon=True).start()

    def job_cancel(self):
        if not self.job: return
        self.job["cancel"] = True; p = self.job.get("p")
        if p and p.poll() is None: kill_tree(p)

    def on_setup_done(self, title, ok):
        self.job_busy = False; self.job_pb.hide(); self.job_stop.setEnabled(False); self.slog(("Done: " if ok else "FAILED: ") + title)
        if ok and "CUDA" in title: self.slog("Restart Clipper so the GPU libraries are loaded.")
        self.setup_check()

    def setup_install(self, rows, upgrade=False):
        i = self.setup_info; mgr = i["mgr"]; esc = elevation(gui=True)
        plans = merge_plans([plan_for(d, mgr, i["gpu"] is not None, upgrade) for d in rows], mgr)
        auto = [pl for pl in plans if pl["kind"] in ("pip", "sys", "winget", "download")]
        for pl in plans:
            if pl["kind"] == "manual": self.slog("Install this one yourself: " + pl["text"])
        if any(pl["kind"] == "sys" and pl["root"] for pl in auto) and esc is None:
            for pl in auto:
                if pl["kind"] == "sys": self.slog("Needs admin rights and 'pkexec' wasn't found. Run in a terminal: " + pl["text"])
            auto = [pl for pl in auto if pl["kind"] != "sys"]
        if not auto: return
        def fn(emit, job):
            ok = True
            for pl in auto:
                if job["cancel"]: return False
                ok = run_plan(pl, emit, esc or [], job) and ok
            return ok
        self.start_job("Install " + ", ".join(d["name"] for d in rows), fn)

    def setup_install_all(self):
        if not self.setup_info: return
        todo = self.todo_rows()
        if not todo: self.slog("Everything required by the current settings is installed."); return
        self.setup_install(todo)

    def setup_whisper(self, name):
        def fn(emit, job):
            from faster_whisper import download_model
            emit(f"Downloading Whisper '{name}' (about {WHISPER_MB[name]} MB). This can't be cancelled and shows no progress until it finishes.")
            download_model(name); emit("Downloaded."); return True
        self.start_job(f"Download Whisper model {name}", fn, cancellable=False)

    def setup_start_ollama(self):
        host = self.get["ollama_host"]().rstrip("/")
        def fn(emit, job):
            ollama_bin = which("ollama")
            if not ollama_bin: emit("Ollama is not installed or not on PATH. Install it from the Setup page first."); return False
            if ollama_up(host): emit("Ollama is already running."); return True
            emit("Starting: ollama serve")
            if WIN: subprocess.Popen([ollama_bin, "serve"], **dict(quiet_kw(), creationflags=NOWIN | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP))
            else: subprocess.Popen([ollama_bin, "serve"], start_new_session=True, **quiet_kw())
            for _ in range(30):
                if ollama_up(host): emit("Ollama is running."); return True
                time.sleep(0.5)
            emit("Ollama didn't start in time. Try: ollama serve  (in a terminal)"); return False
        self.start_job("Start Ollama", fn, cancellable=False)

    # ───────── AI picker: download more Ollama models ─────────
    def build_models_card(self):
        card, v = self._card("Download more models")
        self.gpu_note = QLabel("Checking your GPU..."); self.gpu_note.setObjectName("hint"); self.gpu_note.setWordWrap(True); v.addWidget(self.gpu_note)
        v.addWidget(QLabel("Installed"))
        self.installed_lbl = QLabel("Press Refresh in the Ollama model row to list them."); self.installed_lbl.setObjectName("hint"); self.installed_lbl.setWordWrap(True)
        v.addWidget(self.installed_lbl); v.addWidget(QLabel("Model to download"))
        ctl = QHBoxLayout(); ctl.setSpacing(8)
        self.pull_combo = Combo(); self.pull_combo.setEditable(True); self.pull_combo.addItems(list(SUGGESTED)); self.pull_combo.setCurrentText("llama3.2:3b")
        self.pull_combo.setMinimumWidth(260)
        self.pull_btn = self._btn("Download", self.pull_model, primary=True)
        self.pull_stop = self._btn("Cancel", self.pull_cancel.set); self.pull_stop.setEnabled(False)
        ctl.addWidget(self.pull_combo); ctl.addWidget(self.pull_btn); ctl.addWidget(self.pull_stop); ctl.addStretch(1); v.addLayout(ctl)
        self.pull_hint = QLabel(); self.pull_hint.setObjectName("hint"); self.pull_hint.setWordWrap(True); v.addWidget(self.pull_hint)
        self.pull_pb = QProgressBar(); self.pull_pb.setRange(0, 1000); self.pull_pb.setTextVisible(False); v.addWidget(self.pull_pb)
        self.pull_status = QLabel(); self.pull_status.setObjectName("hint"); v.addWidget(self.pull_status)
        self.pull_combo.editTextChanged.connect(self.pull_hint_update); self.pull_hint_update()
        return card

    def pull_hint_update(self, *_):
        g = SUGGESTED.get(self.pull_combo.currentText().strip())
        self.pull_hint.setText(f"About {g[0]} GB. {g[1]}" if g else "Any model name from ollama.com/library works, for example llama3.2:1b.")

    def pull_model(self):
        name = self.pull_combo.currentText().strip()
        if not name or self.pulling: return
        self.pulling = True; self.pull_cancel.clear(); self.pull_btn.setEnabled(False); self.pull_stop.setEnabled(True)
        self.pull_pb.setRange(0, 1000); self.pull_pb.setValue(0); self.pull_status.setText("Starting...")
        host = self.get["ollama_host"]().rstrip("/")
        def run():
            layers, top, err = {}, 0.0, None
            try:
                with ollama_post(host, "/api/pull", {"model": name, "stream": True}, 120) as r:
                    for line in r:
                        if self.pull_cancel.is_set(): err = "Cancelled. Run it again to resume where it stopped."; break
                        if not line.strip(): continue
                        d = json.loads(line)
                        if "error" in d: err = d["error"]; break
                        if d.get("total"): layers[d.get("digest", "?")] = (d.get("completed", 0), d["total"])
                        tot, done = sum(t for _, t in layers.values()), sum(c for c, _ in layers.values())
                        top = max(top, done / tot) if tot else top
                        self.emit("pull_prog", top if tot else None, f"{d.get('status', '')}  {done / 1e9:.2f} of {tot / 1e9:.2f} GB" if tot else d.get("status", ""))
            except urllib.error.HTTPError as e: err = f"Ollama error {e.code}: {e.read()[:150].decode(errors='replace')}"
            except urllib.error.URLError: err = "Couldn't reach Ollama. Start it on the Setup page first."
            except (OSError, ValueError) as e: err = str(e)
            self.emit("pull_done", name, err)
        threading.Thread(target=run, daemon=True).start()

    def on_pull_prog(self, frac, text):
        if frac is None: self.pull_pb.setRange(0, 0)
        else: self.pull_pb.setRange(0, 1000); self.pull_pb.setValue(int(frac * 1000))
        self.pull_status.setText(text)

    def on_pull_done(self, name, err):
        self.pulling = False; self.pull_btn.setEnabled(True); self.pull_stop.setEnabled(False)
        self.pull_pb.setRange(0, 1000); self.pull_pb.setValue(0 if err else 1000)
        self.pull_status.setText(err or f"Downloaded {name}. Pick it in the Ollama model row above to use it.")
        if not err: self.refresh_models(quiet=True)

    def refresh_models(self, quiet=False):
        host = self.get["ollama_host"]().rstrip("/")
        threading.Thread(target=lambda: self.emit("models", ollama_models(host), quiet), daemon=True).start()

    def on_models(self, models, quiet):
        if models is None:
            self.installed_lbl.setText("Couldn't reach Ollama. Start it on the Setup page.")
            if not quiet: QMessageBox.warning(self, "Ollama", "Couldn't reach Ollama. Is it running? You can start it on the Setup page.")
            return
        names = [n for n, _ in models]
        values = names + [s for s in SUGGESTED if s not in names]  # installed first, then suggestions
        for cb in self.model_cbs:
            cur = cb.currentText(); cb.clear(); cb.addItems(values); cb.setCurrentText(cur)
        self.installed_lbl.setText(", ".join(f"{n} ({s / 1e9:.1f} GB)" for n, s in models) if names
                                   else "No models installed yet. Pick a suggestion from the dropdown, or download one below.")

    # ───────── YouTube ─────────
    def build_yt_card(self):
        card, v = self._card("Get YouTube upload credentials", "Clipper needs a client_secret.json from Google Cloud to upload. Here is how to get one:")
        for s in ("1.  Go to console.cloud.google.com  →  create a new project (or pick an existing one).",
                  "2.  APIs & Services  →  Library  →  search 'YouTube Data API v3'  →  Enable it.",
                  "3.  APIs & Services  →  OAuth consent screen  →  External  →  fill in a name. Add yourself as a Test user.",
                  "4.  APIs & Services  →  Credentials  →  Create Credentials  →  OAuth client ID.",
                  "5.  Application type: Desktop app.  Give it a name, then Create.",
                  "6.  Click 'Download JSON'.  Rename the file to client_secret.json.",
                  "7.  Put client_secret.json in your home folder (or set its path above).",
                  "8.  Turn on 'Upload clips to YouTube' above. The first upload opens a browser to sign in."):
            l = QLabel(s); l.setWordWrap(True); v.addWidget(l)
        bt = QHBoxLayout(); bt.setSpacing(8)
        bt.addWidget(self._btn("Open Google Cloud Console", lambda: webbrowser.open("https://console.cloud.google.com/"), primary=True))
        bt.addWidget(self._btn("Test connection", self.yt_test_connection)); bt.addStretch(1); v.addLayout(bt)
        return card

    def yt_test_connection(self):
        if not have_mod("googleapiclient") or not have_mod("google_auth_oauthlib"):
            QMessageBox.warning(self, "YouTube", "The YouTube packages aren't installed.\nGo to the Setup page and install 'YouTube upload packages', then try again."); return
        cs = Path(self.get["yt_client_secret"]()).expanduser()
        if not cs.is_file():
            QMessageBox.warning(self, "YouTube", f"client_secret.json not found at:\n{cs}\n\nDownload it from Google Cloud Console (see the steps above) and place it there, or set its path in the YouTube settings."); return
        QMessageBox.information(self, "YouTube", f"client_secret.json found at:\n{cs}\n\nEverything looks ready. The first upload will open a browser to sign in to Google.")

    def upload_button_clicked(self):
        if self.running: QMessageBox.information(self, "Clipper", "Wait for the current job to finish, or cancel it first."); return
        if self.yt_running: QMessageBox.information(self, "Clipper", "An upload is already running."); return
        files = [f for f in (self.last_files or []) if Path(f["path"] if isinstance(f, dict) else f).is_file()]
        files = [f if isinstance(f, dict) else {"path": str(f), "title": Path(f).stem} for f in files]
        self.upload_win = UploadDialog(self, files, self.uploaded, self.upload_youtube); self.upload_win.open()

    def upload_youtube(self, files):
        """Upload clips in a background thread. 'files' is a list of dicts with path/title/description."""
        try: c = self.cfg()
        except ValueError as e: self.on_log("Error: " + str(e)); return
        if not have_mod("googleapiclient") or not have_mod("google_auth_oauthlib"):
            self.on_log("YouTube upload: packages not installed. Go to Setup to install them."); return
        cs_path = Path(c["yt_client_secret"]).expanduser()
        if not cs_path.is_file():
            self.on_log(f"YouTube upload: client_secret.json not found at {cs_path}. See the YouTube settings page for steps."); return
        token_path = SETTINGS_FILE.parent / "youtube_token.json"
        privacy, hashtags_in_title = c["yt_privacy"], c["yt_hashtags_in_title"]
        default_tags = [t.strip() for t in c["yt_default_tags"].split(",") if t.strip()]
        cancel_ev = threading.Event(); self.yt_cancel = cancel_ev; self.yt_running = True
        self.yt_btn.setEnabled(False); self.yt_stop.setEnabled(True); self.yt_stop.show()
        self.status.setText(f"Uploading {len(files)} clip(s) to YouTube...")
        def run():
            try:
                from google.oauth2.credentials import Credentials
                from google_auth_oauthlib.flow import InstalledAppFlow
                from google.auth.transport.requests import Request
                from googleapiclient.discovery import build
                from googleapiclient.http import MediaFileUpload
            except ImportError as e:
                self.emit("log", f"YouTube upload: missing package ({e}). Install 'YouTube upload packages' on the Setup page."); self.emit("yt_done"); return
            SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
            creds = None
            if token_path.is_file():
                try: creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
                except Exception: creds = None
            if creds and creds.expired and creds.refresh_token:
                try: creds.refresh(Request())
                except Exception: creds = None
            if not creds or not creds.valid:
                try:
                    creds = InstalledAppFlow.from_client_secrets_file(str(cs_path), SCOPES).run_local_server(port=0)  # opens the default browser
                except Exception as e:
                    self.emit("log", f"YouTube auth failed: {e}"); self.emit("yt_done"); return
                try:
                    token_path.parent.mkdir(parents=True, exist_ok=True); token_path.write_text(creds.to_json(), encoding="utf-8")
                    os.chmod(token_path, 0o600)  # the token grants upload access; keep it private
                except OSError: pass
            try: youtube = build("youtube", "v3", credentials=creds, static_discovery=False)
            except Exception as e:
                self.emit("log", f"YouTube: couldn't connect to the API: {e}"); self.emit("yt_done"); return
            uploaded = 0
            for i, f in enumerate(files, 1):
                if cancel_ev.is_set(): break
                path = f["path"] if isinstance(f, dict) else str(f)
                title = (f.get("yt_title") or f.get("title") or Path(path).stem) if isinstance(f, dict) else Path(path).stem
                desc = (f.get("yt_description") or f.get("description") or "") if isinstance(f, dict) else ""
                desc_tags = re.findall(r"#\w+", desc)
                if hashtags_in_title and desc_tags:
                    tag_str = " " + " ".join(desc_tags[:5])
                    if tag_str.strip() not in title: title = (title + tag_str)[:100]  # YouTube title limit is 100 chars
                tags, size = [], 0
                for t in (list(f.get("yt_tags") or []) if isinstance(f, dict) else []) + default_tags + [t.lstrip("#") for t in desc_tags]:
                    if t and t.lower() not in {x.lower() for x in tags} and size + len(t) + 1 <= 450: tags.append(t); size += len(t) + 1
                body = {"snippet": {"title": title[:100], "description": desc[:5000], "tags": tags, "categoryId": YT_CATEGORIES.get(c["yt_category"], "22")},
                        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
                self.emit("log", f"YouTube: uploading {Path(path).name} ({i}/{len(files)}) as {privacy}...")
                try:
                    media = MediaFileUpload(path, mimetype="video/mp4", resumable=True, chunksize=10 * 1024 * 1024)
                    request = youtube.videos().insert(part=",".join(body.keys()), body=body, media_body=media)
                    response = None
                    while response is None:
                        if cancel_ev.is_set(): break
                        status, response = request.next_chunk()
                        if status: self.emit("log", f"  {Path(path).name}: {int(status.progress() * 100)}%")
                    if response and response.get("id"):
                        self.emit("log", f"YouTube: uploaded {Path(path).name} → https://www.youtube.com/watch?v={response['id']}")
                        uploaded += 1; self.emit("yt_uploaded", path)
                    elif response and response.get("error"):
                        self.emit("log", f"YouTube: error uploading {Path(path).name}: {response['error'].get('message', '?')}")
                except Exception as e:
                    self.emit("log", f"YouTube: error uploading {Path(path).name}: {e}")
            note = " (cancelled)" if cancel_ev.is_set() else ""
            self.emit("log", f"YouTube: done{note}. {uploaded} of {len(files)} clip(s) uploaded." if uploaded else "YouTube: no clips were uploaded.")
            self.emit("yt_done")
        threading.Thread(target=run, daemon=True).start()

    def yt_stop_clicked(self):
        if self.yt_cancel: self.yt_cancel.set()
        self.yt_stop.setEnabled(False); self.on_log("YouTube upload: stopping after the current chunk...")

    def on_yt_done(self):
        self.yt_running = False; self.yt_btn.setEnabled(True); self.yt_stop.setEnabled(False); self.yt_stop.hide(); self.status.setText("Ready.")

    def on_yt_uploaded(self, path):
        self.uploaded.add(str(Path(path)))
