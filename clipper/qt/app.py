import html, json, os, shutil, subprocess, sys, threading, time, traceback
from pathlib import Path
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFontDatabase, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget)
from clipper.sysutil import VERSION, WIN, which
from clipper.setup import SUGGESTED
from clipper.settings import PRESETS, SCHEMA, SETTINGS_FILE, STEPS, WEIGHTS, cast, fmt_t
from clipper.pipeline import Cancelled, Pipeline
from clipper.qt import theme
from clipper.qt.theme import ACC, DANGER, FG, MUTED, SUBTITLES, SUCCESS
from clipper.qt.widgets import Combo, DSpin, Spin, Switch
from clipper.qt.dialogs import RegionDialog, ReviewDialog
from clipper.qt.extras import Extras


class Bridge(QObject):
    """Carries events from the worker thread to the UI thread."""
    event = Signal(object)


class MainWindow(Extras, QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Clipper {VERSION} — Video to Short Clips")
        self.resize(1280, 860); self.setMinimumSize(1040, 700)
        self.get, self.set, self.rows, self.pages, self.nav, self.chips = {}, {}, [], {}, {}, []
        self.cancel_ev, self.pipe, self.running, self.last_files, self.t_step = threading.Event(), None, False, [], time.time()
        self.fonts = sorted({f for f in QFontDatabase.families() if f and not f.startswith("@")}, key=str.lower)
        self.bridge = Bridge(); self.bridge.event.connect(self.dispatch)
        self.emit = lambda *a: self.bridge.event.emit(a)
        self.init_extras()
        self.build(); self.load(); self.toggle_adv(); self.show_page("Progress")
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.browse)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.start)
        QShortcut(QKeySequence("Esc"), self, activated=self.esc_stop)

    # ───────── layout ─────────
    def build(self):
        root = QWidget(); root.setObjectName("root"); self.setCentralWidget(root)
        lay = QHBoxLayout(root); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(0)
        lay.addWidget(self.build_side()); lay.addWidget(self.build_main(), 1)

    def build_side(self):
        side = QFrame(); side.setObjectName("side"); side.setFixedWidth(220)
        v = QVBoxLayout(side); v.setContentsMargins(0, 24, 0, 0); v.setSpacing(0)
        brand = QLabel("CLIPPER"); brand.setObjectName("brand"); brand.setIndent(22)
        sub = QLabel("VIDEO WORKSPACE"); sub.setObjectName("section"); sub.setIndent(22); sub.setContentsMargins(0, 4, 0, 18)
        v.addWidget(brand); v.addWidget(sub)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget(); nv = QVBoxLayout(body); nv.setContentsMargins(0, 0, 0, 0); nv.setSpacing(0)
        self.group = QButtonGroup(self); self.group.setExclusive(True)

        def nav(name):
            b = QPushButton(name); b.setObjectName("nav"); b.setCheckable(True); b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, n=name: self.show_page(n))
            self.group.addButton(b); self.nav[name] = b; nv.addWidget(b)

        def label(text):
            l = QLabel(text); l.setObjectName("section"); l.setIndent(22); l.setContentsMargins(0, 18, 0, 8); nv.addWidget(l)
        nav("Progress"); label("WORKSPACE SETTINGS")
        for name in SCHEMA: nav(name)
        label("TOOLS"); nav("Setup")
        nv.addStretch(1)
        scroll.setWidget(body); v.addWidget(scroll, 1)

        bot = QWidget(); bv = QVBoxLayout(bot); bv.setContentsMargins(20, 14, 20, 20); bv.setSpacing(6)
        lab = QLabel("QUICK PRESET"); lab.setObjectName("section"); bv.addWidget(lab)
        pc = Combo(); pc.addItem("Choose a preset"); pc.addItems(list(PRESETS))
        pc.activated.connect(lambda i: self.preset(pc.itemText(i)) if i > 0 else None); bv.addWidget(pc)
        row = QHBoxLayout(); row.setContentsMargins(0, 12, 0, 0); row.addWidget(QLabel("Advanced options")); row.addStretch(1)
        self.adv = Switch(); self.adv.toggled.connect(lambda _: self.toggle_adv()); row.addWidget(self.adv); bv.addLayout(row)
        v.addWidget(bot)
        return side

    def build_main(self):
        main = QWidget(); v = QVBoxLayout(main); v.setContentsMargins(30, 24, 30, 0); v.setSpacing(18)
        top = QHBoxLayout(); top.setSpacing(10)
        self.url = QLineEdit(); self.url.setPlaceholderText("Paste a video URL or select a local video file")
        self.url.setMinimumHeight(44); self.url.returnPressed.connect(self.start)
        b_file = QPushButton("Choose file"); b_file.clicked.connect(self.browse)
        self.go_btn = QPushButton("Make clips"); self.go_btn.setObjectName("primary"); self.go_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("Cancel"); self.stop_btn.setEnabled(False); self.stop_btn.clicked.connect(self.stop)
        top.addWidget(self.url, 1)
        for b in (b_file, self.go_btn, self.stop_btn):
            b.setMinimumHeight(44); b.setCursor(Qt.PointingHandCursor); top.addWidget(b)
        self.stack = QStackedWidget()
        v.addLayout(top); v.addWidget(self.stack, 1)
        self.build_progress()
        for name, items in SCHEMA.items(): self.build_page(name, items)
        self.build_setup()
        return main

    def make_page(self, title, sub):
        sc = QScrollArea(); sc.setWidgetResizable(True); sc.setFrameShape(QFrame.NoFrame)
        body = QWidget(); lay = QVBoxLayout(body); lay.setContentsMargins(0, 8, 14, 28); lay.setSpacing(18)
        t = QLabel(title); t.setObjectName("title"); s = QLabel(sub); s.setObjectName("subtitle"); s.setWordWrap(True)
        lay.addWidget(t); lay.addWidget(s); lay.addSpacing(4)
        sc.setWidget(body); return sc, lay

    def build_page(self, name, items):
        sc, lay = self.make_page(name, SUBTITLES.get(name, ""))
        card = QFrame(); card.setObjectName("card")
        cl = QVBoxLayout(card); cl.setContentsMargins(24, 4, 24, 4); cl.setSpacing(0)
        for i, it in enumerate(items): cl.addWidget(self.add_row(i, *it))
        lay.addWidget(card)
        if name == "AI picker": lay.addWidget(self.build_models_card())
        if name == "YouTube": lay.addWidget(self.build_yt_card())
        lay.addStretch(1)
        self.pages[name] = sc; self.stack.addWidget(sc)

    def add_row(self, idx, key, label, kind, default, opts, hint, adv):
        row = QWidget(); outer = QVBoxLayout(row); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)
        if idx:
            d = QFrame(); d.setObjectName("divider"); d.setFixedHeight(1); outer.addWidget(d)
        inner = QHBoxLayout(); inner.setContentsMargins(0, 14, 0, 14); inner.setSpacing(30); outer.addLayout(inner)
        left = QVBoxLayout(); left.setSpacing(3); left.addWidget(QLabel(label))
        if hint:
            h = QLabel(hint); h.setObjectName("hint"); h.setWordWrap(True); left.addWidget(h)
        inner.addLayout(left, 1)
        inner.addWidget(self.make_control(key, kind, default, opts), 0, Qt.AlignRight | Qt.AlignTop)
        self.rows.append((row, adv)); return row

    def make_control(self, key, kind, default, opts):
        g, s = self.get, self.set
        if kind == "bool":
            w = Switch(bool(default)); g[key] = w.isChecked; s[key] = lambda v, w=w: w.setChecked(bool(v))
        elif kind == "choice":
            w = Combo(); w.addItems([str(o) for o in opts]); w.setCurrentText(str(default)); w.setMinimumWidth(220)
            g[key] = w.currentText; s[key] = w.setCurrentText
        elif kind == "int":
            w = Spin(); w.setRange(int(opts[0]), int(opts[1])); w.setSingleStep(int(opts[2]) if len(opts) > 2 else 1)
            w.setValue(int(default)); w.setMinimumWidth(110)
            g[key] = w.value; s[key] = lambda v, w=w: w.setValue(int(float(v)))
        elif kind == "float":
            w = DSpin(); w.setDecimals(2); w.setRange(float(opts[0]), float(opts[1])); w.setSingleStep(float(opts[2]) if len(opts) > 2 else 0.1)
            w.setValue(float(default)); w.setMinimumWidth(110)
            g[key] = w.value; s[key] = lambda v, w=w: w.setValue(float(v))
        elif kind == "text":
            w = QPlainTextEdit(); w.setFixedSize(300, 76)
            g[key] = lambda w=w: w.toPlainText().strip(); s[key] = lambda v, w=w: w.setPlainText(str(v))
        elif kind == "ollama":
            w = QWidget(); h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(8)
            cb = Combo(); cb.setEditable(True); cb.addItems(list(SUGGESTED)); cb.setCurrentText(str(default)); cb.setMinimumWidth(240)
            b = QPushButton("Refresh"); b.clicked.connect(lambda _=False: self.refresh_models())
            h.addWidget(cb); h.addWidget(b); self.model_cbs.append(cb)
            g[key] = cb.currentText; s[key] = cb.setCurrentText
        elif kind == "font":
            w = Combo(); w.setEditable(True); w.addItems(self.fonts); w.setCurrentText(str(default)); w.setMinimumWidth(240)
            g[key] = w.currentText; s[key] = w.setCurrentText
        elif kind == "path":
            w = QWidget(); h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(8)
            e = QLineEdit(str(default)); e.setMinimumWidth(240); b = QPushButton("Browse")
            b.clicked.connect(lambda _=False, e=e, d=str(default): self.browse_path(e, d))
            h.addWidget(e); h.addWidget(b)
            g[key] = e.text; s[key] = lambda v, e=e: e.setText(str(v))
        else:
            w = QLineEdit(str(default)); w.setMinimumWidth(300)
            g[key] = w.text; s[key] = lambda v, w=w: w.setText(str(v))
        return w

    def browse_path(self, e, default):
        if default.lower().endswith(".json"): p = QFileDialog.getOpenFileName(self, "Choose file", e.text(), "JSON (*.json);;All files (*)")[0]
        else: p = QFileDialog.getExistingDirectory(self, "Choose folder", e.text())
        if p: e.setText(p)

    def toggle_adv(self):
        for row, adv in self.rows:
            if adv: row.setVisible(self.adv.isChecked())

    def show_page(self, name):
        self.stack.setCurrentWidget(self.pages[name]); self.nav[name].setChecked(True); self.page_shown(name)

    def build_progress(self):
        pg = QWidget(); v = QVBoxLayout(pg); v.setContentsMargins(0, 8, 0, 20); v.setSpacing(18)
        t = QLabel("Progress"); t.setObjectName("title")
        s = QLabel("Each step shows its own progress and an estimate of the time left."); s.setObjectName("subtitle")
        v.addWidget(t); v.addWidget(s)
        card = QFrame(); card.setObjectName("card"); c = QVBoxLayout(card); c.setContentsMargins(24, 20, 24, 20); c.setSpacing(6)
        self.status = QLabel("Ready."); self.status.setObjectName("status")
        self.eta = QLabel(""); self.eta.setObjectName("hint")
        self.step_pb = QProgressBar(); self.step_pb.setRange(0, 1000); self.step_pb.setTextVisible(False)
        self.all_pb = QProgressBar(); self.all_pb.setRange(0, 1000); self.all_pb.setTextVisible(False)
        chips = QHBoxLayout(); chips.setSpacing(16); chips.setContentsMargins(0, 10, 0, 0)
        for i, n in enumerate(STEPS, 1):
            l = QLabel(f"{i}  {n}"); l.setStyleSheet(f"color:{MUTED};"); chips.addWidget(l); self.chips.append(l)
        chips.addStretch(1)
        ov = QLabel("Overall"); ov.setObjectName("hint"); ov.setContentsMargins(0, 14, 0, 2)
        for w in (self.status, self.eta, self.step_pb): c.addWidget(w)
        c.addLayout(chips); c.addWidget(ov); c.addWidget(self.all_pb)
        v.addWidget(card)
        self.logbox = QPlainTextEdit(); self.logbox.setObjectName("log"); self.logbox.setReadOnly(True); self.logbox.setMaximumBlockCount(4000)
        f = QFontDatabase.systemFont(QFontDatabase.FixedFont); f.setPointSize(9); self.logbox.setFont(f)
        v.addWidget(self.logbox, 1)
        bt = QHBoxLayout(); bt.addStretch(1)
        self.yt_btn = QPushButton("Upload to YouTube"); self.yt_btn.clicked.connect(self.upload_button_clicked)
        self.yt_stop = QPushButton("Stop upload"); self.yt_stop.setEnabled(False); self.yt_stop.clicked.connect(self.yt_stop_clicked)
        bt.addWidget(self.yt_btn); bt.addWidget(self.yt_stop)
        for text, fn in (("Open output folder", lambda: self.open_path(self.last_out())), ("Clear cache", self.clear_cache), ("Reset settings", self.reset)):
            b = QPushButton(text); b.setCursor(Qt.PointingHandCursor); b.clicked.connect(lambda _=False, fn=fn: fn()); bt.addWidget(b)
        v.addLayout(bt)
        self.pages["Progress"] = pg; self.stack.addWidget(pg)

    # ───────── settings ─────────
    def cfg(self):
        d = {}
        for items in SCHEMA.values():
            for key, label, kind, default, *_ in items:
                try: d[key] = cast(kind, default, self.get[key]())
                except ValueError: raise ValueError(f"'{label}' needs a number.") from None
        return d

    def save(self):
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {k: self.get[k]() for k in self.get}; data["_adv"] = self.adv.isChecked(); data["_version"] = VERSION
        tmp = SETTINGS_FILE.with_suffix(".tmp"); tmp.write_text(json.dumps(data, indent=2), encoding="utf-8"); os.replace(tmp, SETTINGS_FILE)

    def load(self):
        try: data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError): return
        if "ollama_host" in data and "localhost" in str(data["ollama_host"]): data["ollama_host"] = "http://127.0.0.1:11434"
        for k, v in data.items():
            if k in self.set:
                try: self.set[k](v)
                except (ValueError, TypeError): pass
        self.adv.setChecked(bool(data.get("_adv", False)))

    def preset(self, name):
        for k, v in PRESETS[name].items():
            if k in self.set: self.set[k](v)
        self.on_log(f"Preset applied: {name}")

    def reset(self):
        if QMessageBox.question(self, "Reset settings", "Restore every setting to its default?") != QMessageBox.Yes: return
        for items in SCHEMA.values():
            for key, _, _, default, *_ in items: self.set[key](default)

    # ───────── actions ─────────
    def browse(self):
        p = QFileDialog.getOpenFileName(self, "Choose a video", "", "Video (*.mp4 *.mkv *.mov *.webm *.avi);;All files (*)")[0]
        if p: self.url.setText(p)

    def last_out(self): return Path(self.get["output_dir"]()).expanduser()

    def open_path(self, p):
        Path(p).mkdir(parents=True, exist_ok=True)
        if WIN:
            try: os.startfile(str(p))
            except OSError as e: self.on_log(f"Couldn't open the folder: {e}")
            return
        opener = which("xdg-open")
        if not opener: self.on_log(f"Saved folder: {p} (xdg-open is not installed)."); return
        subprocess.Popen([opener, str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def clear_cache(self):
        cache = self.last_out() / ".cache"
        if cache.exists() and QMessageBox.question(self, "Clear cache", "Delete downloaded videos and transcripts? Finished clips are kept.") == QMessageBox.Yes:
            shutil.rmtree(cache, ignore_errors=True); self.on_log("Cache cleared.")

    def start(self):
        src = self.url.text().strip().strip('"\'')
        if self.running: return
        if not src: QMessageBox.information(self, "Clipper", "Paste a video link or choose a file first."); return
        try: cfg = self.cfg()
        except ValueError as e: QMessageBox.critical(self, "Invalid setting", str(e)); return
        if cfg["min_len"] >= cfg["max_len"]: QMessageBox.critical(self, "Invalid setting", "The shortest clip must be shorter than the longest."); return
        self.save(); self.cancel_ev.clear(); self.running = True
        self.pipe = Pipeline(cfg, src, self.emit, self.cancel_ev)
        self.go_btn.setEnabled(False); self.stop_btn.setEnabled(True); self.show_page("Progress")
        self.logbox.clear(); self.step_pb.setRange(0, 1000); self.step_pb.setValue(0); self.all_pb.setValue(0); self.eta.setText(""); self.paint_chips(0)
        threading.Thread(target=self.work, daemon=True).start()

    def work(self):
        try: out, files = self.pipe.go(); self.emit("done", str(out), files)
        except Cancelled: self.emit("cancelled")
        except Exception as e: self.emit("error", str(e), traceback.format_exc())

    def stop(self):
        if not self.running: return
        self.cancel_ev.set(); self.stop_btn.setEnabled(False); self.status.setText("Cancelling...")
        if self.pipe: self.pipe.kill()

    def esc_stop(self):
        if self.running and QMessageBox.question(self, "Clipper", "Cancel the running job?") == QMessageBox.Yes: self.stop()

    def closeEvent(self, e):
        if self.running and QMessageBox.question(self, "Clipper", "A job is running. Cancel it and quit?") != QMessageBox.Yes:
            e.ignore(); return
        self.cancel_ev.set()
        self.pull_cancel.set()
        if self.yt_cancel: self.yt_cancel.set()
        if self.job: self.job["cancel"] = True
        if self.pipe: self.pipe.kill()
        try: self.save()
        except OSError: pass
        e.accept()

    # ───────── events from the worker thread ─────────
    def dispatch(self, m):
        try: getattr(self, "on_" + m[0])(*m[1:])
        except Exception as e: self.on_log(f"UI event error ({m[0]}): {e}")

    def paint_chips(self, cur):
        for k, lab in enumerate(self.chips, 1): lab.setStyleSheet(f"color:{ACC if k < cur else (FG if k == cur else MUTED)};")

    def on_log(self, t):
        col = DANGER if t.startswith(("Error", "Couldn't", "GPU failed")) else SUCCESS if t.startswith("Saved") else FG
        self.logbox.appendHtml(f'<pre style="margin:0;color:{col}">{html.escape(t)}</pre>')

    def on_step(self, i, name):
        self.t_step = time.time(); self.status.setText(f"Step {i} of {len(STEPS)}: {name}")
        self.step_pb.setRange(0, 1000); self.step_pb.setValue(0); self.all_pb.setValue(int(sum(WEIGHTS[:i - 1]) * 1000)); self.eta.setText(""); self.paint_chips(i)

    def on_prog(self, i, frac, text):
        if frac is None: self.step_pb.setRange(0, 0); self.eta.setText(text); return
        self.step_pb.setRange(0, 1000)
        frac = max(0.0, min(1.0, frac)); el = time.time() - self.t_step
        left = f"   about {fmt_t(el / frac - el)} left" if 0.03 < frac < 1 else ""
        self.step_pb.setValue(int(frac * 1000)); self.eta.setText(f"{frac * 100:.0f}%   {text}{left}")
        self.all_pb.setValue(int((sum(WEIGHTS[:i - 1]) + WEIGHTS[i - 1] * frac) * 1000))

    def on_review(self, clips, video, holder):
        self.status.setText("Waiting for your review..."); self.eta.setText("Choose which clips to render")
        self.review_window = ReviewDialog(self, clips, video, holder); self.review_window.open()

    def on_regions(self, holder):
        self.status.setText("Waiting for you to mark the face cam and content...")
        self.eta.setText("The content box is optional: unmarked uses the full video frame")
        self.review_window = RegionDialog(self, holder); self.review_window.open()

    def close_dialog(self):
        w, self.review_window = getattr(self, "review_window", None), None
        if w is not None:
            try: w.close()  # the pipeline is already cancelled; closing just tidies up
            except RuntimeError: pass  # the dialog was already deleted

    def on_review_cancel(self): self.close_dialog()
    def on_regions_cancel(self): self.close_dialog()

    def finished(self, msg):
        self.running = False; self.go_btn.setEnabled(True); self.stop_btn.setEnabled(False); self.step_pb.setRange(0, 1000); self.status.setText(msg)

    def on_done(self, out, files):
        self.step_pb.setValue(1000); self.all_pb.setValue(1000); self.eta.setText(""); self.paint_chips(len(STEPS) + 1)
        self.finished(f"Done. {len(files)} clip(s) saved."); self.last_files = files
        for f in files: self.on_log("Saved " + (f["path"] if isinstance(f, dict) else str(f)))
        if files and self.get["open_done"](): self.open_path(out)
        if files and self.get["yt_enabled"]() and self.get["yt_auto_upload"](): self.upload_youtube(files)

    def on_cancelled(self): self.finished("Cancelled."); self.eta.setText(""); self.on_log("Cancelled.")

    def on_error(self, msg, tb):
        self.finished("Something went wrong."); self.eta.setText(""); self.on_log("Error: " + msg); self.on_log(tb)


def run():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    theme.apply(app)
    win = MainWindow(); win.show()
    return app.exec()
