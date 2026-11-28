import random, subprocess
from pathlib import Path
from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QRadioButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView, QFileDialog)
from clipper.sysutil import which, quiet_kw, proc_kw
from clipper.settings import fmt_clip_t, parse_t, validate_clip_times
from clipper.moments import layout_sizes, region_px, exclude_cam, fmt_region, describe_score, youtube_fields
from clipper.edit import split_filter, clip_words
from clipper.qt.theme import ACC
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen, QPixmap, QShortcut

def _btn(text, fn, primary=False):
    b = QPushButton(text); b.setAutoDefault(False); b.setDefault(False); b.setCursor(Qt.PointingHandCursor)
    if primary: b.setObjectName("primary")
    b.clicked.connect(lambda _=False: fn())
    return b


class _Holder(QDialog):
    """A dialog that always answers the pipeline thread exactly once: with a result, or with None when it is closed."""
    def _finish(self, res):
        if getattr(self, "_done", False): return
        self._done = True; self.holder["res"] = res; self.holder["ev"].set()

    def reject(self):
        self._finish(None); super().reject()


# ───────────────────────── Review ─────────────────────────
class ReviewDialog(_Holder):
    def __init__(self, parent, clips, video, holder):
        super().__init__(parent)
        self.setWindowTitle("Review and select clips"); self.resize(1120, 700); self.setMinimumSize(900, 580)
        self.clips, self.video, self.holder = clips, video, holder
        self.segs = holder.get("segs") or []
        self.cur, self.busy, self.shown = None, False, ("", "")
        self.cap_range, self.cap_orig = (0.0, 0.0), ""

        lay = QHBoxLayout(self); lay.setContentsMargins(20, 18, 20, 18); lay.setSpacing(20)
        left = QVBoxLayout(); left.setSpacing(6)
        head = QLabel("Pick the clips to render"); head.setStyleSheet("font-size:11pt;font-weight:700;")
        self.count = QLabel(); self.count.setObjectName("hint")
        self.table = QTableWidget(len(clips), 6)
        self.table.setHorizontalHeaderLabels(["Use", "Start", "End", "Length", "Score", "Title"])
        self.table.verticalHeader().setVisible(False); self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows); self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers); self.table.setShowGrid(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        for col, w in enumerate((46, 72, 72, 64, 56)): self.table.setColumnWidth(col, w)
        self.table.setMinimumWidth(520)
        left.addWidget(head); left.addWidget(self.count); left.addWidget(self.table, 1)
        lay.addLayout(left)

        r = QGridLayout(); r.setColumnStretch(1, 1); r.setVerticalSpacing(8)
        self.use = QCheckBox("Include this clip"); r.addWidget(self.use, 0, 0, 1, 2)
        self.s, self.e, self.t = QLineEdit(), QLineEdit(), QLineEdit()
        for i, (lab, w) in enumerate((("Start", self.s), ("End", self.e), ("Title", self.t)), 1):
            r.addWidget(QLabel(lab), i, 0); r.addWidget(w, i, 1)
        tip = QLabel("Times accept seconds (90) or m:ss (1:30)."); tip.setObjectName("hint"); r.addWidget(tip, 4, 1)
        r.addWidget(QLabel("Instagram description"), 5, 0, 1, 2)
        self.d = QPlainTextEdit(); r.addWidget(self.d, 6, 0, 1, 2); r.setRowStretch(6, 1)
        r.addWidget(QLabel("Caption text (fix mistakes here, word timing is kept)"), 7, 0, 1, 2)
        self.cap = QPlainTextEdit(); r.addWidget(self.cap, 8, 0, 1, 2); r.setRowStretch(8, 2)
        self.why = QLabel(); self.why.setObjectName("hint"); self.why.setWordWrap(True); r.addWidget(self.why, 9, 0, 1, 2)
        bar = QHBoxLayout(); bar.addStretch(1)
        bar.addWidget(_btn("Preview", self.preview)); bar.addWidget(_btn("Cancel", self.reject))
        bar.addWidget(_btn("Render selected", self.accept, primary=True))
        r.addLayout(bar, 10, 0, 1, 2)
        lay.addLayout(r, 1)

        for i in range(len(clips)): self.refresh_row(i)
        self.update_count()
        self.use.clicked.connect(lambda _=False: self.update_count())
        self.table.itemSelectionChanged.connect(self.pick)
        self.table.cellDoubleClicked.connect(lambda row, _c: self.toggle_row(row))
        if clips: self.table.selectRow(0)
        self.finished.connect(self.deleteLater)

    def row(self, i):
        c = self.clips[i]
        score = f"{c['score']:.1f}" if c.get("score") is not None else "-"
        return ("yes" if c["use"] else "no", fmt_clip_t(c["start"]), fmt_clip_t(c["end"]), f"{c['end'] - c['start']:.1f}s", score, c["title"])

    def refresh_row(self, i):
        for col, txt in enumerate(self.row(i)):
            it = self.table.item(i, col)
            if it is None: it = QTableWidgetItem(); self.table.setItem(i, col, it)
            it.setText(txt)

    def transcript(self, a, b):
        return " ".join(w[2] for w in clip_words(self.segs, a, b))

    def update_count(self):
        selected = sum(bool(c["use"]) for c in self.clips)
        if self.cur is not None:
            selected += int(self.use.isChecked()) - int(bool(self.clips[self.cur]["use"]))
        self.count.setText(f"{selected} of {len(self.clips)} selected · double-click a row to toggle")

    def toggle_row(self, idx):
        if self.cur is not None and not self.commit(): return
        self.clips[idx]["use"] = not self.clips[idx]["use"]
        self.refresh_row(idx)
        if self.cur == idx: self.use.setChecked(self.clips[idx]["use"])
        self.update_count()

    def commit(self):
        if self.cur is None: return True
        c = self.clips[self.cur]; a, b = c["start"], c["end"]
        # Only re-parse times the user edited: AI clips can legitimately fall outside min/max (e.g. the last clip),
        # and un-ticking a clip must never be blocked.
        if self.use.isChecked() and (self.s.text(), self.e.text()) != self.shown:
            try:
                if self.s.text() != self.shown[0]: a = parse_t(self.s.text())
                if self.e.text() != self.shown[1]: b = parse_t(self.e.text())
                if 0 < b - self.holder["duration"] < 0.05: b = self.holder["duration"]
                validate_clip_times(a, b, self.holder["duration"], self.holder["min_len"], self.holder["max_len"])
            except (ValueError, TypeError, OverflowError) as exc:
                QMessageBox.critical(self, "Invalid clip times", str(exc) or "Use seconds (90) or m:ss (1:30).")
                return False
        txt = " ".join(self.cap.toPlainText().split())
        if txt == self.cap_orig: c.pop("cap_edit", None)
        else: c["cap_edit"] = {"text": txt, "start": self.cap_range[0], "end": self.cap_range[1]}
        c.update(start=a, end=b, title=self.t.text().strip(), description=self.d.toPlainText().strip(), use=self.use.isChecked())
        self.refresh_row(self.cur); self.update_count()
        return True

    def pick(self):
        if self.busy: return
        rows = self.table.selectionModel().selectedRows()
        if not rows: return
        new = rows[0].row()
        if new == self.cur: return
        if not self.commit():  # bad times: stay on the old clip
            self.busy = True; self.table.selectRow(self.cur); self.busy = False
            return
        self.cur = new; c = self.clips[new]
        self.use.setChecked(bool(c["use"])); self.s.setText(f"{c['start']:.2f}"); self.e.setText(f"{c['end']:.2f}")
        self.t.setText(c["title"]); self.d.setPlainText(c["description"]); self.shown = (self.s.text(), self.e.text())
        ed = c.get("cap_edit")
        self.cap_range = (ed["start"], ed["end"]) if ed else (c["start"], c["end"])
        self.cap_orig = self.transcript(*self.cap_range)
        self.cap.setPlainText(ed["text"] if ed else self.cap_orig)
        self.why.setText(describe_score(c) or "No score: this clip wasn't picked by the smart picker.")
        self.update_count()

    def preview(self):
        if self.cur is None or not self.commit(): return
        c = self.clips[self.cur]; ffplay = which("ffplay")
        if not ffplay:
            QMessageBox.critical(self, "ffplay not found", "ffplay ships with ffmpeg. Install ffmpeg from the Setup page to preview."); return
        try: subprocess.Popen([ffplay, "-hide_banner", "-loglevel", "error", "-autoexit", "-ss", str(c["start"]), "-t", str(c["end"] - c["start"]), self.video], **quiet_kw())
        except OSError as e: QMessageBox.critical(self, "Couldn't start ffplay", str(e))

    def accept(self):
        if self.commit(): self._finish(self.clips); super().accept()

    def reject(self):
        if not getattr(self, "_done", False) and QMessageBox.question(
                self, "Clipper", "Cancel this job? Nothing will be rendered.") != QMessageBox.Yes: return
        super().reject()


# ───────────────────────── Face cam / content picker ─────────────────────────
class RegionCanvas(QWidget):
    changed = Signal()
    COLORS = {"cam": ACC, "main": "#4ade80"}
    NAMES = {"cam": "FACE CAM", "main": "CONTENT"}
    HANDLE, MIN = 10, 8
    ORDER = ("tl", "tr", "bl", "br", "t", "b", "l", "r")
    CURSORS = {"in": Qt.SizeAllCursor, "tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor, "tr": Qt.SizeBDiagCursor,
               "bl": Qt.SizeBDiagCursor, "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor, "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor}

    def __init__(self):
        super().__init__()
        self.pix, self.dw, self.dh = None, 0, 0
        self.rect = {"cam": None, "main": None}; self.key, self.act, self.effective = "cam", None, None
        self.setMouseTracking(True); self.setFocusPolicy(Qt.StrongFocus); self.setCursor(Qt.CrossCursor)
        self.setFixedSize(640, 360)

    def set_image(self, pix):
        self.pix, self.dw, self.dh = pix, pix.width(), pix.height()
        self.setFixedSize(self.dw, self.dh); self.update()

    def touched(self):
        self.update(); self.changed.emit()

    @staticmethod
    def handles(r):
        x0, y0, x1, y1 = r; mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        return {"tl": (x0, y0), "t": (mx, y0), "tr": (x1, y0), "l": (x0, my), "r": (x1, my), "bl": (x0, y1), "b": (mx, y1), "br": (x1, y1)}

    def hit(self, r, x, y):
        if not r: return None
        hs = self.handles(r)
        for name in self.ORDER:
            if abs(x - hs[name][0]) <= self.HANDLE and abs(y - hs[name][1]) <= self.HANDLE: return name
        return "in" if r[0] <= x <= r[2] and r[1] <= y <= r[3] else None

    def paintEvent(self, _):
        p = QPainter(self)
        if self.pix: p.drawPixmap(0, 0, self.pix)
        font = self.font(); font.setBold(True); font.setPointSize(9); p.setFont(font)
        for key in ("main", "cam"):  # the face cam is drawn on top
            r = self.rect[key]
            if not r: continue
            col, on = QColor(self.COLORS[key]), key == self.key
            p.setPen(QPen(col, 3 if on else 2)); p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(r[0], r[1], r[2] - r[0], r[3] - r[1]))
            p.drawText(int(r[0] + 6), int(r[1] + 16), self.NAMES[key])
            if on:
                p.setPen(QPen(Qt.black, 1)); p.setBrush(col)
                for hx, hy in self.handles(r).values(): p.drawRect(QRectF(hx - 4, hy - 4, 8, 8))
        eff = self.effective() if self.effective else None
        if eff and eff[2] != "none":  # the cam overlaps the content: show what the content panel really uses
            (x, y, w, h), patch, how = eff
            label = "CONTENT USED (cam cut away)"
            if how == "fill" and patch: x, y, w, h = x + patch[0], y + patch[1], patch[2], patch[3]; label = "PAINTED OVER"
            pen = QPen(QColor(self.COLORS["main"]), 2); pen.setStyle(Qt.DashLine); p.setPen(pen); p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(x, y, w, h)); p.drawText(int(x + 6), int(y + 34), label)

    def clamp(self, e):
        p = e.position(); return min(max(p.x(), 0), self.dw), min(max(p.y(), 0), self.dh)

    def drawn(self, start, x, y):
        x0, x1 = sorted((min(max(start[0], 0), self.dw), x)); y0, y1 = sorted((min(max(start[1], 0), self.dh), y))
        return (x0, y0, x1, y1)

    def resized(self, hnd, r0, x, y):
        x0, y0, x1, y1 = r0
        if "l" in hnd: x0 = min(x, x1 - self.MIN)
        if "r" in hnd: x1 = max(x, x0 + self.MIN)
        if "t" in hnd: y0 = min(y, y1 - self.MIN)
        if "b" in hnd: y1 = max(y, y0 + self.MIN)
        x0, y0 = max(0, x0), max(0, y0); x1, y1 = min(self.dw, x1), min(self.dh, y1)
        return (x0, y0, max(x0 + self.MIN, x1), max(y0 + self.MIN, y1))

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or not self.dw: return
        self.setFocus()
        x, y = self.clamp(e); r = self.rect[self.key]; part = self.hit(r, x, y)
        self.act = ("move", (x, y), r) if part == "in" else ("size", part, r) if part else ("new", (x, y), r)

    def mouseMoveEvent(self, e):
        x, y = self.clamp(e)
        if not self.act:
            self.setCursor(self.CURSORS.get(self.hit(self.rect[self.key], x, y), Qt.CrossCursor)); return
        kind, a, r0 = self.act; key = self.key
        if kind == "move":
            w, h = r0[2] - r0[0], r0[3] - r0[1]
            nx = min(max(r0[0] + x - a[0], 0), self.dw - w); ny = min(max(r0[1] + y - a[1], 0), self.dh - h)
            self.rect[key] = (nx, ny, nx + w, ny + h)
        elif kind == "size": self.rect[key] = self.resized(a, r0, x, y)
        else: self.rect[key] = self.drawn(a, x, y)
        self.touched()

    def mouseReleaseEvent(self, _):
        act, self.act = self.act, None
        if act and act[0] == "new":
            r = self.rect[self.key]
            if not r or r[2] - r[0] < self.MIN or r[3] - r[1] < self.MIN:
                self.rect[self.key] = act[2]; self.touched()

    def keyPressEvent(self, e):
        d = {Qt.Key_Left: (-1, 0), Qt.Key_Right: (1, 0), Qt.Key_Up: (0, -1), Qt.Key_Down: (0, 1)}.get(e.key())
        r = self.rect[self.key]
        if not d or not r: return super().keyPressEvent(e)
        k = 10 if e.modifiers() & Qt.ShiftModifier else 1
        w, h = r[2] - r[0], r[3] - r[1]
        nx = min(max(r[0] + d[0] * k, 0), self.dw - w); ny = min(max(r[1] + d[1] * k, 0), self.dh - h)
        self.rect[self.key] = (nx, ny, nx + w, ny + h); self.touched()


class RegionDialog(_Holder):
    PV_EMPTY = "Draw the face-cam\nbox to see the result"

    def __init__(self, parent, holder):
        super().__init__(parent)
        self.setWindowTitle("Mark the face cam and content")
        self.app, self.holder = parent, holder
        W, H, cam_h = layout_sizes(holder["cfg"]); scale = 450 / H
        self.pv_size = (round(W * scale / 2) * 2, 450, min(max(round(cam_h * scale / 2) * 2, 2), 448))
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(220); self.timer.timeout.connect(self.preview)

        v = QVBoxLayout(self); v.setContentsMargins(20, 16, 20, 16); v.setSpacing(10)
        v.addWidget(QLabel("Mark the face cam and the content")); v.itemAt(0).widget().setStyleSheet("font-weight:700;")
        info = QLabel("Pick which box you are editing, then drag to draw it. Drag inside a box to move it. Drag any edge or corner handle to resize "
                      "only that side. Arrow keys nudge the box (Shift = 10 px). The content box is optional: leave it unmarked to use the whole frame.")
        info.setObjectName("hint"); info.setWordWrap(True); info.setMaximumWidth(900); v.addWidget(info)
        sel = QHBoxLayout(); sel.addWidget(QLabel("Editing:"))
        self.cv = RegionCanvas(); self.cv.effective = self.effective; self.cv.changed.connect(self.timer.start)
        for key, label in (("cam", "Face cam"), ("main", "Content")):
            rb = QRadioButton(label); rb.setStyleSheet(f"color:{RegionCanvas.COLORS[key]};font-weight:700;"); rb.setChecked(key == "cam")
            rb.toggled.connect(lambda on, k=key: self.set_key(k) if on else None); sel.addWidget(rb)
        sel.addStretch(1); v.addLayout(sel)

        body = QHBoxLayout(); body.setSpacing(16); body.addWidget(self.cv, 0, Qt.AlignTop)
        side = QVBoxLayout(); t = QLabel("Result"); t.setStyleSheet("font-weight:700;")
        sub = QLabel("face cam + content"); sub.setObjectName("hint")
        self.pv = QLabel(self.PV_EMPTY); self.pv.setAlignment(Qt.AlignCenter); self.pv.setFixedSize(self.pv_size[0], self.pv_size[1])
        self.pv.setStyleSheet("background:#000000;color:#8a8a8a;")
        side.addWidget(t); side.addWidget(sub); side.addWidget(self.pv); side.addStretch(1); body.addLayout(side)
        v.addLayout(body)

        bar = QHBoxLayout()
        bar.addWidget(_btn("Different frame", self.other)); bar.addWidget(_btn("Clear this box", self.clear)); bar.addStretch(1)
        bar.addWidget(_btn("Cancel", self.reject)); bar.addWidget(_btn("Use this layout", self.accept, primary=True))
        v.addLayout(bar)

        dur = holder["duration"]
        if self.load(min(dur * 0.25, max(dur - 1, 0.0))):
            for key, r in (("cam", holder.get("saved")), ("main", holder.get("saved_main"))):
                if r and not (key == "main" and r[2] > 0.995 and r[3] > 0.995):  # a full-frame content box is the default
                    self.cv.rect[key] = (r[0] * self.cv.dw, r[1] * self.cv.dh, (r[0] + r[2]) * self.cv.dw, (r[1] + r[3]) * self.cv.dh)
            self.cv.touched()
        self.finished.connect(self.deleteLater)

    def set_key(self, k):
        self.cv.key = k; self.cv.update()

    def load(self, t):
        png = Path(self.holder["work"]) / "split_frame.png"
        try:
            r = subprocess.run([which("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", self.holder["video"], "-frames:v", "1",
                                "-vf", "scale=800:450:force_original_aspect_ratio=decrease", str(png)], capture_output=True, timeout=60, **proc_kw())
            ok = r.returncode == 0 and png.is_file()
        except (OSError, subprocess.SubprocessError): ok = False
        pix = QPixmap(str(png)) if ok else None
        if not pix or pix.isNull():
            QMessageBox.critical(self, "Clipper", "Couldn't read a picture from the video."); return False
        self.cv.set_image(pix); self.adjustSize()
        return True

    def other(self):
        self.cv.act = None
        if self.load(random.uniform(0.05, 0.95) * self.holder["duration"]):
            self.cv.rect["cam"] = self.cv.rect["main"] = None; self.cv.touched()

    def clear(self):
        self.cv.rect[self.cv.key] = None; self.cv.touched()

    def crop_of(self, r):
        cv = self.cv
        return region_px((r[0] / cv.dw, r[1] / cv.dh, (r[2] - r[0]) / cv.dw, (r[3] - r[1]) / cv.dh), cv.dw, cv.dh)

    def main_px(self):
        r = self.cv.rect["main"]
        return self.crop_of(r) if r else (0, 0, self.cv.dw // 2 * 2, self.cv.dh // 2 * 2)

    def effective(self):
        cam = self.cv.rect["cam"]
        if not (cam and self.cv.dw and self.cv.dh): return None
        return exclude_cam(self.main_px(), self.crop_of(cam), self.holder["cfg"]["split_overlap"])

    def preview(self):
        cam = self.cv.rect["cam"]
        if not (cam and self.cv.dw):
            self.pv.setPixmap(QPixmap()); self.pv.setText(self.PV_EMPTY); return
        c, (W, H, cam_h), work = self.holder["cfg"], self.pv_size, Path(self.holder["work"])
        main, patch, _ = self.effective()
        lay = {"cam": self.crop_of(cam), "main": main, "patch": patch, "cam_h": cam_h, "top": c["split_cam_pos"] == "top"}
        out = work / "split_preview.png"
        try:
            r = subprocess.run([which("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error", "-i", str(work / "split_frame.png"), "-vf", split_filter(c, W, H, "", lay),
                                "-frames:v", "1", str(out)], capture_output=True, timeout=30, **proc_kw())
            ok = r.returncode == 0 and out.is_file()
        except (OSError, subprocess.SubprocessError): ok = False
        pix = QPixmap(str(out)) if ok else None
        if pix and not pix.isNull(): self.pv.setPixmap(pix)
        else: self.pv.setPixmap(QPixmap()); self.pv.setText("Preview unavailable")

    def frac(self, r):
        cv = self.cv
        return (r[0] / cv.dw, r[1] / cv.dh, (r[2] - r[0]) / cv.dw, (r[3] - r[1]) / cv.dh)

    def too_small(self, r):
        return not r or r[2] - r[0] < RegionCanvas.MIN or r[3] - r[1] < RegionCanvas.MIN

    def accept(self):
        cam, main = self.cv.rect["cam"], self.cv.rect["main"]
        if self.too_small(cam):
            QMessageBox.information(self, "Clipper", "Draw the face-cam rectangle first."); return
        if main and self.too_small(main):
            QMessageBox.information(self, "Clipper", "The content box is too small. Redraw it, or use Clear this box to use the full frame."); return
        cam_f, main_f = self.frac(cam), self.frac(main) if main else (0.0, 0.0, 1.0, 1.0)
        self.app.set["split_cam"](fmt_region(cam_f)); self.app.set["split_main"](fmt_region(main_f))
        self._finish((cam_f, main_f)); super().accept()

class UploadDialog(QDialog):
    """Choose which finished clips to upload to YouTube. Click a box, double-click a row, or press Space to tick a clip."""
    def __init__(self, parent, files, uploaded, on_ok):
        super().__init__(parent)
        self.setWindowTitle("Upload to YouTube"); self.resize(820, 500); self.setMinimumSize(640, 360)
        self.on_ok, self.uploaded, self.files, self._loading = on_ok, {str(Path(p)) for p in uploaded}, [], False
        v = QVBoxLayout(self); v.setContentsMargins(20, 16, 20, 16); v.setSpacing(8)
        head = QLabel("Pick the clips to upload"); head.setStyleSheet("font-size:11pt;font-weight:700;")
        self.count = QLabel(); self.count.setObjectName("hint")
        self.table = QTableWidget(0, 4); self.table.setHorizontalHeaderLabels(["Upload", "File", "YouTube title", ""])
        self.table.verticalHeader().setVisible(False); self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows); self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers); self.table.setShowGrid(False)
        hdr = self.table.horizontalHeader(); hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        for col, w in ((0, 70), (1, 180), (3, 90)): self.table.setColumnWidth(col, w)
        v.addWidget(head); v.addWidget(self.count); v.addWidget(self.table, 1)
        bar = QHBoxLayout()
        bar.addWidget(_btn("Select all", lambda: self.set_all(True))); bar.addWidget(_btn("Select none", lambda: self.set_all(False)))
        bar.addWidget(_btn("Add video file…", self.add_files)); bar.addStretch(1)
        bar.addWidget(_btn("Cancel", self.reject)); bar.addWidget(_btn("Upload selected", self.ok, primary=True))
        v.addLayout(bar)
        for f in files:
            if Path(f["path"]).is_file(): self.append(f, tick=str(Path(f["path"])) not in self.uploaded)  # already-uploaded clips start unticked
        self.update_count()
        self.table.itemChanged.connect(lambda _it: None if self._loading else self.update_count())
        self.table.cellDoubleClicked.connect(lambda row, col: self.toggle(row) if col != 0 else None)
        QShortcut(QKeySequence("Space"), self.table, activated=self.space, context=Qt.WidgetShortcut)
        self.finished.connect(self.deleteLater)

    def append(self, f, tick=True):
        p = str(Path(f["path"]))
        if any(str(Path(x["path"])) == p for x in self.files): return
        self.files.append(f); r = self.table.rowCount(); self.table.insertRow(r); self._loading = True
        chk = QTableWidgetItem(); chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        chk.setCheckState(Qt.Checked if tick else Qt.Unchecked); self.table.setItem(r, 0, chk)
        for col, txt in ((1, Path(p).name), (2, f.get("yt_title") or f.get("title") or ""), (3, "uploaded" if p in self.uploaded else "")):
            it = QTableWidgetItem(txt); it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable); self.table.setItem(r, col, it)
        self._loading = False

    def is_on(self, i): return self.table.item(i, 0).checkState() == Qt.Checked

    def toggle(self, i):
        self.table.item(i, 0).setCheckState(Qt.Unchecked if self.is_on(i) else Qt.Checked)

    def space(self):
        r = self.table.currentRow()
        if r >= 0: self.toggle(r)

    def set_all(self, on):
        self._loading = True
        for i in range(len(self.files)): self.table.item(i, 0).setCheckState(Qt.Checked if on else Qt.Unchecked)
        self._loading = False; self.update_count()

    def update_count(self):
        n = sum(self.is_on(i) for i in range(len(self.files)))
        self.count.setText(f"{n} of {len(self.files)} selected · click a box, double-click a row, or press Space to tick it")

    def add_files(self):
        """For clips from an earlier session (or any other video): title comes from the file name, plus the #Shorts tag."""
        for p in QFileDialog.getOpenFileNames(self, "Add video file", "", "Video (*.mp4 *.mov *.mkv *.webm);;All files (*)")[0]:
            yt = youtube_fields(Path(p).stem.replace("_", " "), "", [], "")
            self.append({"path": p, "title": yt["title"], "description": "", "yt_title": yt["title"], "yt_description": yt["description"], "yt_tags": yt["tags"]})
        self.update_count()

    def ok(self):
        chosen = [f for i, f in enumerate(self.files) if self.is_on(i)]
        if not chosen: QMessageBox.information(self, "Clipper", "Tick at least one clip to upload."); return
        self.accept(); self.on_ok(chosen)
