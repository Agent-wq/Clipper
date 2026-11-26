import random, subprocess
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from clipper.sysutil import which, quiet_kw, proc_kw
from clipper.settings import fmt_clip_t, parse_t, validate_clip_times
from clipper.moments import layout_sizes, region_px, exclude_cam, fmt_region, youtube_fields
from clipper.edit import split_filter
from clipper.ui.theme import *
from clipper.ui.widgets import Btn
class Review(tk.Toplevel):
    def __init__(self, app, clips, video, holder):
        super().__init__(app); self.title("Review and select clips"); self.geometry("1120x700"); self.minsize(900, 580); self.configure(bg=BG)
        self.clips, self.video, self.holder, self.cur, self.busy, self.shown = clips, video, holder, None, False, ("", "")
        self.transient(app); self.protocol("WM_DELETE_WINDOW", self.cancel)
        left = ttk.Frame(self, padding=12); left.pack(side="left", fill="y")
        ttk.Label(left, text="Pick the clips to render", font=(FAM, 11, "bold")).pack(anchor="w", pady=(0, 4))
        self.count_var = tk.StringVar()
        ttk.Label(left, textvariable=self.count_var, style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        self.tree = ttk.Treeview(left, columns=("use", "start", "end", "length", "title"), show="headings", height=15, selectmode="browse")
        for col, w, t in (("use", 42, "Use"), ("start", 72, "Start"), ("end", 72, "End"), ("length", 62, "Length"), ("title", 220, "Title")):
            self.tree.heading(col, text=t); self.tree.column(col, width=w, stretch=(col == "title"))
        self.tree.pack(fill="y", expand=True); self.tree.bind("<<TreeviewSelect>>", self.pick)
        self.tree.bind("<Double-1>", self.toggle_selected)
        right = ttk.Frame(self, padding=12); right.pack(side="right", fill="both", expand=True); right.columnconfigure(1, weight=1)
        self.use, self.s, self.e, self.t = tk.BooleanVar(), tk.StringVar(), tk.StringVar(), tk.StringVar()
        ttk.Checkbutton(right, text="Include this clip", variable=self.use, command=self.update_count).grid(row=0, column=0, columnspan=2, sticky="w", pady=4)
        for r, (lab, var) in enumerate((("Start", self.s), ("End", self.e), ("Title", self.t)), 1):
            ttk.Label(right, text=lab).grid(row=r, column=0, sticky="w", pady=4); ttk.Entry(right, textvariable=var).grid(row=r, column=1, sticky="ew", pady=4)
        ttk.Label(right, text="Times accept seconds (90) or m:ss (1:30).", style="Muted.TLabel").grid(row=4, column=1, sticky="w")
        ttk.Label(right, text="Instagram description").grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 2))
        self.d = tk.Text(right, height=9, bg=FIELD, fg=FG, relief="flat", wrap="word", insertbackground=FG, highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACC, padx=8, pady=6, font=(FAM, 10)); self.d.grid(row=6, column=0, columnspan=2, sticky="nsew"); right.rowconfigure(6, weight=1)
        bar = ttk.Frame(right); bar.grid(row=7, column=0, columnspan=2, sticky="e", pady=(12, 0))
        Btn(bar, "Preview", self.preview).pack(side="left", padx=4)
        Btn(bar, "Cancel", self.cancel).pack(side="left", padx=4)
        Btn(bar, "Render selected", self.ok, kind="primary").pack(side="left", padx=4)
        for i in range(len(clips)): self.tree.insert("", "end", iid=str(i), values=self.row(i))
        self.update_count()
        if clips: self.tree.selection_set("0")

    def row(self, i):
        c = self.clips[i]
        return ("yes" if c["use"] else "no", fmt_clip_t(c["start"]), fmt_clip_t(c["end"]), f"{c['end'] - c['start']:.1f}s", c["title"])

    def update_count(self):
        selected = sum(bool(c["use"]) for c in self.clips)
        if self.cur is not None:
            selected += int(bool(self.use.get())) - int(bool(self.clips[self.cur]["use"]))
        self.count_var.set(f"{selected} of {len(self.clips)} selected · double-click a row to toggle")

    def toggle_selected(self, event=None):
        item = self.tree.identify_row(event.y) if event is not None else ""
        if not item: return
        if self.cur is not None and not self.commit(): return
        idx = int(item); self.clips[idx]["use"] = not self.clips[idx]["use"]
        self.tree.item(item, values=self.row(idx))
        if self.cur == idx: self.use.set(self.clips[idx]["use"])
        self.update_count()
        self.tree.selection_set(item)
    def commit(self):
        if self.cur is None: return True
        c = self.clips[self.cur]; a, b = c["start"], c["end"]
        # The editor shows rounded values, and AI clips can legitimately fall outside min/max
        # (e.g. the last clip of a video). So only re-parse and validate times the user edited,
        # and don't block un-ticking a clip.
        if self.use.get() and (self.s.get(), self.e.get()) != self.shown:
            try:
                if self.s.get() != self.shown[0]: a = parse_t(self.s.get())
                if self.e.get() != self.shown[1]: b = parse_t(self.e.get())
                if 0 < b - self.holder["duration"] < 0.05: b = self.holder["duration"]
                validate_clip_times(a, b, self.holder["duration"], self.holder["min_len"], self.holder["max_len"])
            except (ValueError, TypeError, OverflowError) as exc:
                messagebox.showerror("Invalid clip times", str(exc) or "Use seconds (90) or m:ss (1:30).", parent=self)
                return False
        c.update(start=a, end=b, title=self.t.get().strip(), description=self.d.get("1.0", "end").strip(), use=self.use.get())
        self.tree.item(str(self.cur), values=self.row(self.cur)); self.update_count(); return True
    def pick(self, _=None):
        if self.busy or not self.tree.selection(): return
        if not self.commit(): self.busy = True; self.tree.selection_set(str(self.cur)); self.busy = False; return
        self.cur = int(self.tree.selection()[0]); c = self.clips[self.cur]
        self.use.set(c["use"]); self.s.set(f"{c['start']:.2f}"); self.e.set(f"{c['end']:.2f}"); self.t.set(c["title"])
        self.d.delete("1.0", "end"); self.d.insert("1.0", c["description"]); self.shown = (self.s.get(), self.e.get()); self.update_count()
    def preview(self):
        if self.cur is None or not self.commit(): return
        c = self.clips[self.cur]
        ffplay = which("ffplay")
        if not ffplay:
            messagebox.showerror("ffplay not found", "ffplay ships with ffmpeg. Install ffmpeg from the Setup page to preview.", parent=self)
            return
        try: subprocess.Popen([ffplay, "-hide_banner", "-loglevel", "error", "-autoexit", "-ss", str(c["start"]), "-t", str(c["end"] - c["start"]), self.video], **quiet_kw())
        except OSError as e: messagebox.showerror("Couldn't start ffplay", str(e), parent=self)
    def ok(self):
        if self.commit(): self.holder["res"] = self.clips; self.holder["ev"].set(); self.destroy()
    def cancel(self): self.holder["res"] = None; self.holder["ev"].set(); self.destroy()

class RegionPicker(tk.Toplevel):
    """Pick two freely resizable rectangles: the face cam and the main content. An unmarked content box means the full frame."""
    COLORS = {"cam": "#35c7b2", "main": "#e8b86d"}
    NAMES = {"cam": "FACE CAM", "main": "CONTENT"}
    PV_EMPTY = "Draw the face-cam\nbox to see the result"
    HANDLE, MIN = 10, 8
    ORDER = ("tl", "tr", "bl", "br", "t", "b", "l", "r")

    def __init__(self, app, holder):
        super().__init__(app)
        self.title("Mark the face cam and content")
        self.configure(bg=BG)
        self.transient(app)
        self.app, self.hold, self.dw, self.dh, self.img, self.act = app, holder, 0, 0, None, None
        self.pv_job, self.pv_img = None, None
        W, H, cam_h = layout_sizes(holder["cfg"])
        scale = 450 / H
        self.pv_size = (round(W * scale / 2) * 2, 450, min(max(round(cam_h * scale / 2) * 2, 2), 448))
        self.rect = {"cam": None, "main": None}
        self.key = tk.StringVar(value="cam")  # which box the mouse and arrow keys edit
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=18, pady=(16, 4))
        tk.Label(top, text="Mark the face cam and the content", bg=BG, fg=FG, font=(FAM, 10, "bold")).pack(anchor="w")
        tk.Label(top, text="Pick which box you are editing, then drag to draw it. Drag inside a box to move it. Drag any edge or corner handle to resize only that side. "
                           "Both boxes can be any shape. The content box is optional: leave it unmarked to use the whole frame.",
                 bg=BG, fg=MUTED, font=(FAM, 9), wraplength=780, justify="left").pack(anchor="w", pady=(3, 8))
        sel = tk.Frame(top, bg=BG)
        sel.pack(anchor="w")
        tk.Label(sel, text="Editing:", bg=BG, fg=MUTED, font=(FAM, 9)).pack(side="left", padx=(0, 8))
        for key, label in (("cam", "Face cam"), ("main", "Content")):
            tk.Radiobutton(sel, text=label, variable=self.key, value=key, command=self.draw, bg=BG, fg=self.COLORS[key], selectcolor=BG,
                           activebackground=BG, activeforeground=self.COLORS[key], highlightthickness=0, bd=0, font=(FAM, 10, "bold"),
                           cursor="hand2").pack(side="left", padx=(0, 14))
        body = tk.Frame(self, bg=BG)
        body.pack(padx=18, pady=8)
        self.cv = tk.Canvas(body, bg="#000000", highlightthickness=0, cursor="crosshair", takefocus=True)
        self.cv.pack(side="left")
        side = tk.Frame(body, bg=BG)
        side.pack(side="left", padx=(16, 0), anchor="n")
        tk.Label(side, text="Result", bg=BG, fg=FG, font=(FAM, 10, "bold")).pack(anchor="w")
        tk.Label(side, text="face cam + content", bg=BG, fg=MUTED, font=(FAM, 8)).pack(anchor="w", pady=(0, 6))
        self.pv = tk.Label(side, text=self.PV_EMPTY, bg="#000000", fg=MUTED, font=(FAM, 9), width=28, height=14, justify="center")
        self.pv.pack()
        self.cv.bind("<ButtonPress-1>", self.press)
        self.cv.bind("<B1-Motion>", self.drag)
        self.cv.bind("<ButtonRelease-1>", self.release)
        self.cv.bind("<Motion>", self.hover)
        for name, (dx, dy) in {"Left": (-1, 0), "Right": (1, 0), "Up": (0, -1), "Down": (0, 1)}.items():
            self.cv.bind(f"<{name}>", lambda e, dx=dx, dy=dy: self.nudge(dx, dy))
            self.cv.bind(f"<Shift-{name}>", lambda e, dx=dx, dy=dy: self.nudge(dx * 10, dy * 10))
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=18, pady=(0, 16))
        Btn(bar, "Different frame", self.other).pack(side="left")
        Btn(bar, "Clear this box", self.clear).pack(side="left", padx=(10, 0))
        Btn(bar, "Use this layout", self.ok, kind="primary").pack(side="right")
        Btn(bar, "Cancel", self.cancel).pack(side="right", padx=(0, 10))

        dur = holder["duration"]
        if self.load(min(dur * 0.25, max(dur - 1, 0.0))):
            for key, r in (("cam", holder.get("saved")), ("main", holder.get("saved_main"))):
                if r and not (key == "main" and r[2] > 0.995 and r[3] > 0.995):  # a full-frame content box is the default, so it is not drawn
                    self.rect[key] = (r[0] * self.dw, r[1] * self.dh, (r[0] + r[2]) * self.dw, (r[1] + r[3]) * self.dh)
            self.draw()
        # On Windows, a transient Toplevel can otherwise appear behind the main window or
        # fail to receive the first mouse interaction. Activate it after Tk has mapped it.
        self.after(80, self._activate_picker)

    def _activate_picker(self):
        try:
            if not self.winfo_exists():
                return
            self.lift()
            self.focus_force()
            self.cv.focus_set()
            self.grab_set()
        except tk.TclError:
            pass

    def load(self, t):
        png = Path(self.hold["work"]) / "split_frame.png"
        try:
            r = subprocess.run([which("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", self.hold["video"], "-frames:v", "1",
                                "-vf", "scale=800:450:force_original_aspect_ratio=decrease", str(png)], capture_output=True, timeout=60, **proc_kw())
            ok = r.returncode == 0 and png.is_file()
        except (OSError, subprocess.SubprocessError):
            ok = False
        if not ok:
            messagebox.showerror("Clipper", "Couldn't read a picture from the video.", parent=self)
            return False
        try:
            self.img = tk.PhotoImage(file=str(png))
            self.dw, self.dh = self.img.width(), self.img.height()
            self.cv.configure(width=self.dw, height=self.dh)
            self.cv.delete("all")
            self.cv.create_image(0, 0, image=self.img, anchor="nw")
            self.draw()
            return True
        except (OSError, tk.TclError) as e:
            messagebox.showerror("Clipper", f"Couldn't display the video frame: {e}", parent=self)
            return False

    def other(self):
        self.act = None
        if self.load(random.uniform(0.05, 0.95) * self.hold["duration"]):
            self.rect["cam"] = self.rect["main"] = None
            self.draw()

    @staticmethod
    def handles(r):
        x0, y0, x1, y1 = r
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        return {"tl": (x0, y0), "t": (mx, y0), "tr": (x1, y0), "l": (x0, my),
                "r": (x1, my), "bl": (x0, y1), "b": (mx, y1), "br": (x1, y1)}

    def hit(self, r, x, y):
        if not r:
            return None
        hs = self.handles(r)
        for name in self.ORDER:
            if abs(x - hs[name][0]) <= self.HANDLE and abs(y - hs[name][1]) <= self.HANDLE:
                return name
        return "in" if r[0] <= x <= r[2] and r[1] <= y <= r[3] else None

    def pick(self, x, y):
        key = self.key.get()
        return key, self.hit(self.rect[key], x, y)

    def draw(self):
        self.cv.delete("r")
        active = self.key.get()
        for key in ("main", "cam"):  # the face cam is drawn on top
            r = self.rect[key]
            if not r:
                continue
            col, on = self.COLORS[key], key == active
            self.cv.create_rectangle(*r, outline=col, width=3 if on else 2, tags="r")
            self.cv.create_text(r[0] + 6, r[1] + 4, text=self.NAMES[key], anchor="nw", fill=col, font=(FAM, 10, "bold"), tags="r")
            if on:
                for hx, hy in self.handles(r).values():
                    self.cv.create_rectangle(hx - 4, hy - 4, hx + 4, hy + 4, fill=col, outline="#000000", tags="r")
        eff = self.effective()
        if eff and eff[2] != "none":  # the face cam overlaps the content: show what the content panel will really use
            (x, y, w, h), patch, how = eff
            label = "CONTENT USED (cam cut away)"
            if how == "fill" and patch:
                x, y, w, h = x + patch[0], y + patch[1], patch[2], patch[3]
                label = "PAINTED OVER"
            self.cv.create_rectangle(x, y, x + w, y + h, outline=self.COLORS["main"], width=2, dash=(6, 4), tags="r")
            self.cv.create_text(x + 6, y + 20, text=label, anchor="nw", fill=self.COLORS["main"], font=(FAM, 9, "bold"), tags="r")
        self.schedule_preview()

    def clamp(self, e):
        # Canvas coordinates are used rather than raw screen coordinates to avoid
        # DPI/window-border offsets on Windows.
        x, y = self.cv.canvasx(e.x), self.cv.canvasy(e.y)
        return min(max(x, 0), self.dw), min(max(y, 0), self.dh)

    def press(self, e):
        self.cv.focus_set()
        x, y = self.clamp(e)
        key, part = self.pick(x, y)
        r = self.rect[key]
        self.act = ("move", (x, y), r, key) if part == "in" else ("size", part, r, key) if part else ("new", (x, y), r, key)
        return "break"

    def drag(self, e):
        if not self.act:
            return "break"
        x, y = self.clamp(e)
        kind, a, r0, key = self.act
        if kind == "move":
            w, h = r0[2] - r0[0], r0[3] - r0[1]
            nx = min(max(r0[0] + x - a[0], 0), self.dw - w)
            ny = min(max(r0[1] + y - a[1], 0), self.dh - h)
            self.rect[key] = (nx, ny, nx + w, ny + h)
        elif kind == "size":
            self.rect[key] = self.resized(a, r0, x, y)
        else:
            self.rect[key] = self.drawn(a, x, y)
        self.draw()
        return "break"

    def drawn(self, start, x, y):
        x0, y0 = start
        x0, x1 = sorted((min(max(x0, 0), self.dw), min(max(x, 0), self.dw)))
        y0, y1 = sorted((min(max(y0, 0), self.dh), min(max(y, 0), self.dh)))
        return (x0, y0, x1, y1)

    def resized(self, hnd, r0, x, y):
        x0, y0, x1, y1 = r0
        x = min(max(x, 0), self.dw)
        y = min(max(y, 0), self.dh)
        if "l" in hnd:
            x0 = min(x, x1 - self.MIN)
        if "r" in hnd:
            x1 = max(x, x0 + self.MIN)
        if "t" in hnd:
            y0 = min(y, y1 - self.MIN)
        if "b" in hnd:
            y1 = max(y, y0 + self.MIN)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.dw, x1), min(self.dh, y1)
        return (x0, y0, max(x0 + self.MIN, x1), max(y0 + self.MIN, y1))

    def release(self, _=None):
        act, self.act = self.act, None
        if act and act[0] == "new":
            key = act[3]
            r = self.rect[key]
            if not r or r[2] - r[0] < self.MIN or r[3] - r[1] < self.MIN:
                self.rect[key] = act[2]
                self.draw()
        return "break"

    def hover(self, e):
        x, y = self.clamp(e)
        part = self.pick(x, y)[1]
        cur = {"in": "fleur", "tl": "top_left_corner", "tr": "top_right_corner", "bl": "bottom_left_corner", "br": "bottom_right_corner",
               "l": "sb_h_double_arrow", "r": "sb_h_double_arrow", "t": "sb_v_double_arrow", "b": "sb_v_double_arrow"}.get(part, "crosshair")
        try:
            self.cv.configure(cursor=cur)
        except tk.TclError:
            self.cv.configure(cursor="crosshair")

    def nudge(self, dx, dy):
        key = self.key.get()
        r = self.rect[key]
        if not r:
            return "break"
        w, h = r[2] - r[0], r[3] - r[1]
        nx = min(max(r[0] + dx, 0), self.dw - w)
        ny = min(max(r[1] + dy, 0), self.dh - h)
        self.rect[key] = (nx, ny, nx + w, ny + h)
        self.draw()
        return "break"

    def crop_of(self, r):
        return region_px(((r[0] / self.dw), (r[1] / self.dh), (r[2] - r[0]) / self.dw, (r[3] - r[1]) / self.dh), self.dw, self.dh)

    def main_px(self):
        r = self.rect["main"]
        return self.crop_of(r) if r else (0, 0, self.dw // 2 * 2, self.dh // 2 * 2)

    def effective(self):
        cam = self.rect["cam"]
        if not (cam and self.dw and self.dh):
            return None
        return exclude_cam(self.main_px(), self.crop_of(cam), self.hold["cfg"]["split_overlap"])

    def schedule_preview(self):
        if self.pv_job:
            try:
                self.after_cancel(self.pv_job)
            except tk.TclError:
                pass
        self.pv_job = self.after(220, self.preview)

    def preview(self):
        self.pv_job = None
        if not self.winfo_exists():
            return
        cam = self.rect["cam"]
        if not (cam and self.dw):
            self.pv_img = None
            self.pv.configure(image="", text=self.PV_EMPTY, width=28, height=14)
            return
        c, (W, H, cam_h), work = self.hold["cfg"], self.pv_size, Path(self.hold["work"])
        main, patch, _ = self.effective()
        lay = {"cam": self.crop_of(cam), "main": main, "patch": patch, "cam_h": cam_h, "top": c["split_cam_pos"] == "top"}
        out = work / "split_preview.png"
        try:
            r = subprocess.run([which("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error", "-i", str(work / "split_frame.png"), "-vf", split_filter(c, W, H, "", lay),
                                "-frames:v", "1", str(out)], capture_output=True, timeout=30, **proc_kw())
            ok = r.returncode == 0 and out.is_file()
        except (OSError, subprocess.SubprocessError):
            ok = False
        if ok:
            try:
                self.pv_img = tk.PhotoImage(file=str(out))
                self.pv.configure(image=self.pv_img, text="", width=0, height=0)
            except (OSError, tk.TclError):
                self.pv.configure(image="", text="Preview unavailable", width=28, height=14)

    def destroy(self):
        if self.pv_job:
            try:
                self.after_cancel(self.pv_job)
            except tk.TclError:
                pass
            self.pv_job = None
        try:
            if self.grab_current() == self:
                self.grab_release()
        except tk.TclError:
            pass
        super().destroy()

    def clear(self):
        self.rect[self.key.get()] = None
        self.draw()

    def frac(self, r):
        return (r[0] / self.dw, r[1] / self.dh, (r[2] - r[0]) / self.dw, (r[3] - r[1]) / self.dh)

    def too_small(self, r):
        return not r or r[2] - r[0] < self.MIN or r[3] - r[1] < self.MIN

    def ok(self):
        cam, main = self.rect["cam"], self.rect["main"]
        if self.too_small(cam):
            self.key.set("cam")
            messagebox.showinfo("Clipper", "Draw the face-cam rectangle first.", parent=self)
            return
        if main and self.too_small(main):
            self.key.set("main")
            messagebox.showinfo("Clipper", "The content box is too small. Redraw it, or use Clear this box to use the full frame.", parent=self)
            return
        cam_f, main_f = self.frac(cam), self.frac(main) if main else (0.0, 0.0, 1.0, 1.0)
        self.app.set["split_cam"](fmt_region(cam_f))
        self.app.set["split_main"](fmt_region(main_f))
        self.hold["res"] = (cam_f, main_f)
        self.hold["ev"].set()
        self.destroy()

    def cancel(self):
        self.hold["res"] = None
        self.hold["ev"].set()
        self.destroy()
class UploadPicker(tk.Toplevel):
    """Choose which finished clips to upload to YouTube. Click the box (or double-click / press Space) to tick a clip."""
    def __init__(self, app, files, uploaded, on_ok):
        super().__init__(app); self.title("Upload to YouTube"); self.geometry("820x500"); self.minsize(640, 360); self.configure(bg=BG)
        self.transient(app); self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.on_ok, self.uploaded = on_ok, {str(Path(p)) for p in uploaded}
        self.files, self.checked = [], []
        tk.Label(self, text="Pick the clips to upload", bg=BG, fg=FG, font=(FAM, 11, "bold")).pack(anchor="w", padx=18, pady=(16, 2))
        self.count_var = tk.StringVar()
        tk.Label(self, textvariable=self.count_var, bg=BG, fg=MUTED, font=(FAM, 9)).pack(anchor="w", padx=18, pady=(0, 8))
        box = tk.Frame(self, bg=BG); box.pack(fill="both", expand=True, padx=18)
        self.tree = ttk.Treeview(box, columns=("use", "file", "title", "state"), show="headings", selectmode="browse")
        for col, w, t in (("use", 46, "Upload"), ("file", 170, "File"), ("title", 380, "YouTube title"), ("state", 90, "")):
            self.tree.heading(col, text=t); self.tree.column(col, width=w, stretch=(col == "title"), anchor="center" if col == "use" else "w")
        sb = ttk.Scrollbar(box, command=self.tree.yview, style="Vertical.TScrollbar"); self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True); sb.pack(side="left", fill="y")
        self.tree.bind("<Button-1>", self.click); self.tree.bind("<Double-1>", self.dbl); self.tree.bind("<space>", self.space)
        bar = tk.Frame(self, bg=BG); bar.pack(fill="x", padx=18, pady=14)
        Btn(bar, "Select all", lambda: self.set_all(True), small=True).pack(side="left")
        Btn(bar, "Select none", lambda: self.set_all(False), small=True).pack(side="left", padx=(8, 0))
        Btn(bar, "Add video file…", self.add_files, small=True).pack(side="left", padx=(8, 0))
        Btn(bar, "Upload selected", self.ok, kind="primary").pack(side="right")
        Btn(bar, "Cancel", self.destroy).pack(side="right", padx=(0, 10))
        for f in files:
            if Path(f["path"]).is_file(): self.append(f, tick=str(Path(f["path"])) not in self.uploaded)  # clips already uploaded start unticked
        self.update_count()
        self.after(80, lambda: (self.lift(), self.focus_force()))

    def row(self, i):
        f = self.files[i]; p = str(Path(f["path"]))
        return ("☑" if self.checked[i] else "☐", Path(p).name, f.get("yt_title") or f.get("title") or "", "uploaded" if p in self.uploaded else "")

    def append(self, f, tick=True):
        if any(str(Path(x["path"])) == str(Path(f["path"])) for x in self.files): return
        self.files.append(f); self.checked.append(tick)
        self.tree.insert("", "end", iid=str(len(self.files) - 1), values=self.row(len(self.files) - 1))

    def toggle(self, i):
        self.checked[i] = not self.checked[i]; self.tree.item(str(i), values=self.row(i)); self.update_count()

    def click(self, e):
        item = self.tree.identify_row(e.y)
        if item and self.tree.identify_region(e.x, e.y) == "cell" and self.tree.identify_column(e.x) == "#1":
            self.toggle(int(item)); return "break"
    def dbl(self, e):
        item = self.tree.identify_row(e.y)
        if item and self.tree.identify_column(e.x) != "#1": self.toggle(int(item))
    def space(self, _=None):
        sel = self.tree.selection()
        if sel: self.toggle(int(sel[0]))
        return "break"

    def set_all(self, on):
        self.checked = [on] * len(self.files)
        for i in range(len(self.files)): self.tree.item(str(i), values=self.row(i))
        self.update_count()

    def update_count(self):
        self.count_var.set(f"{sum(self.checked)} of {len(self.files)} selected · click a box, double-click a row, or press Space to tick it")

    def add_files(self):
        """For clips from an earlier session (or any other video): they get a title from the file name and the #Shorts tag."""
        for p in filedialog.askopenfilenames(parent=self, filetypes=[("Video", "*.mp4 *.mov *.mkv *.webm"), ("All files", "*.*")]):
            yt = youtube_fields(Path(p).stem.replace("_", " "), "", [], "")
            self.append({"path": p, "title": yt["title"], "description": "", "yt_title": yt["title"], "yt_description": yt["description"], "yt_tags": yt["tags"]})
        self.update_count()

    def ok(self):
        chosen = [f for f, on in zip(self.files, self.checked) if on]
        if not chosen:
            messagebox.showinfo("Clipper", "Tick at least one clip to upload.", parent=self); return
        self.destroy(); self.on_ok(chosen)
