import json, os, platform, queue, re, shutil, subprocess, sys, threading, time, traceback
import urllib.error, urllib.request, webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from clipper.sysutil import VERSION, WIN, NOWIN, MONO, quiet_kw, kill_tree, which
from clipper.setup import (system_info, mgr_label, gpu_info, have_mod, check_all, plan_for,
    merge_plans, is_admin, elevation, run_plan, setup_script, WHISPER_REPOS, WHISPER_MB,
    whisper_cached, ollama_up, ollama_models, ollama_post, SUGGESTED)
from clipper.settings import (SCHEMA, PRESETS, STEPS, WEIGHTS, SETTINGS_FILE, cast, fmt_t)
from clipper.moments import YT_CATEGORIES
from clipper.pipeline import Pipeline, Cancelled
from clipper.ui.theme import *
from clipper.ui.widgets import Tip, Btn, Switch, Scrolled, NavItem
from clipper.ui.dialogs import Review, RegionPicker, UploadPicker
class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title(f"Clipper {VERSION} — Video to Short Clips"); self.geometry("1280x860"); self.minsize(1040, 700)
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        ww, wh = min(1280, sw - 80), min(860, sh - 100)
        self.geometry(f"{ww}x{wh}+{max(0, (sw-ww)//2)}+{max(0, (sh-wh)//2)}")
        self.q, self.cancel_ev, self.pipe = queue.Queue(), threading.Event(), None
        self.get, self.set, self.rows, self.t_step, self.running = {}, {}, [], time.time(), False
        self.pages, self.nav, self.chips, self.scroller = {}, {}, [], None
        self.model_cbs, self.pulling, self.pull_cancel, self.job, self.job_busy, self.models_loaded, self.setup_info = [], False, threading.Event(), None, False, False, None
        self.last_files = []  # finished clips (for the YouTube upload button)
        self.uploaded = set()
        self.asked_setup = False  # the "install what is missing?" question is asked once per launch
        self.style_ui(); self.build(); self.build_menus(); self.load(); self.toggle_adv()
        for seq in ("<Button-4>", "<Button-5>", "<MouseWheel>"): self.bind_all(seq, self.wheel)
        self.bind_all("<Control-o>", self.main_key(self.browse))
        self.bind_all("<Control-Return>", self.main_key(self.start))
        def esc_stop():
            if self.running and messagebox.askyesno("Clipper", "Cancel the running job?", parent=self): self.stop()
        self.bind_all("<Escape>", self.main_key(esc_stop))
        self.bind_all("<Control-1>", self.main_key(lambda: self.show_page("Progress")))
        for idx, page_name in enumerate(SCHEMA, 2):
            if idx <= 9:
                self.bind_all(f"<Control-{idx}>", self.main_key(lambda n=page_name: self.show_page(n)))
        self.after(100, self.poll); self.after(600, self.setup_check); self.protocol("WM_DELETE_WINDOW", self.on_close)

    def style_ui(self):
        global FAM
        FAM = pick_font()
        for n in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(n).configure(family=FAM, size=10)
        tkfont.nametofont("TkHeadingFont").configure(size=12, weight="bold")
        s = ttk.Style(self); s.theme_use("clam"); self.configure(bg=BG)
        s.configure(".", background=BG, foreground=FG, fieldbackground=FIELD, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, troughcolor=FIELD, font=(FAM, 10), focuscolor=BG)
        s.configure("Muted.TLabel", foreground=MUTED)
        s.configure("Card.TFrame", background=PANEL)
        for st in ("TEntry", "TSpinbox"):
            s.configure(st, padding=(8, 6), fieldbackground=FIELD, insertcolor=FG, bordercolor=BORDER, lightcolor=FIELD, darkcolor=FIELD, arrowcolor=FG, background=FIELD)
            s.map(st, bordercolor=[("focus", ACC)], lightcolor=[("focus", ACC)], darkcolor=[("focus", ACC)], fieldbackground=[("readonly", FIELD), ("disabled", PANEL)], foreground=[("disabled", MUTED)])
        s.configure("TCombobox", padding=(8, 5), arrowcolor=FG, background=FIELD, selectbackground=FIELD, selectforeground=FG, bordercolor=BORDER, lightcolor=FIELD, darkcolor=FIELD)
        s.map("TCombobox", fieldbackground=[("readonly", FIELD), ("disabled", PANEL)], foreground=[("readonly", FG), ("disabled", MUTED)], selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)],
              background=[("active", "#2e3441"), ("readonly", FIELD)], arrowcolor=[("readonly", FG)], bordercolor=[("focus", ACC)], lightcolor=[("focus", ACC)], darkcolor=[("focus", ACC)])
        s.configure("TCheckbutton", background=BG, indicatorbackground=FIELD, indicatorforeground=ACC_FG, indicatormargin=4)
        s.map("TCheckbutton", background=[("active", BG)], indicatorbackground=[("selected", ACC)])
        s.configure("Slim.Horizontal.TProgressbar", background=ACC, troughcolor=FIELD, borderwidth=0, thickness=8, lightcolor=ACC, darkcolor=ACC)
        s.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=FG, borderwidth=0, rowheight=34, font=(FAM, 10))
        s.map("Treeview", background=[("selected", "#245b60")], foreground=[("selected", FG)])
        s.configure("Treeview.Heading", background=SIDE, foreground=MUTED, borderwidth=0, padding=(9, 8), font=(FAM, 9, "bold"))
        for name, trough in (("Vertical.TScrollbar", BG), ("Log.Vertical.TScrollbar", PANEL)):
            s.layout(name, [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
            s.configure(name, background="#2c313c", troughcolor=trough, bordercolor=trough, lightcolor="#2c313c", darkcolor="#2c313c", relief="flat", width=10)
            s.map(name, background=[("active", "#3a4150")])
        for k, v in (("background", FIELD), ("foreground", FG), ("selectBackground", ACC), ("selectForeground", ACC_FG)): self.option_add(f"*TCombobox*Listbox.{k}", v)

    @staticmethod
    def card(parent): return tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
    def header(self, parent, title, sub, row):
        h = tk.Frame(parent, bg=BG); h.grid(row=row, column=0, sticky="ew", padx=30, pady=(8, 20))
        tk.Label(h, text=title, bg=BG, fg=FG, font=(FAM, 21, "bold")).pack(anchor="w")
        tk.Label(h, text=sub, bg=BG, fg=MUTED, font=(FAM, 10)).pack(anchor="w", pady=(2, 0))

    def build(self):
        side = tk.Frame(self, bg=SIDE, width=214); side.pack(side="left", fill="y"); side.pack_propagate(False)
        brand = tk.Frame(side, bg=SIDE); brand.pack(fill="x", padx=22, pady=(26, 0))
        mark = tk.Canvas(brand, width=30, height=30, bg=SIDE, highlightthickness=0); mark.pack(side="left", padx=(0, 10))
        mark.create_oval(1, 1, 29, 29, fill=ACC, outline=ACC)
        mark.create_polygon(12, 8, 22, 15, 12, 22, fill=ACC_FG, outline=ACC_FG)
        tk.Label(brand, text="CLIPPER", bg=SIDE, fg=FG, font=(FAM, 17, "bold")).pack(side="left")
        tk.Label(side, text="VIDEO WORKSPACE", bg=SIDE, fg=MUTED, font=(FAM, 8, "bold")).pack(anchor="w", padx=23, pady=(5, 25))
        self.nav["Progress"] = NavItem(side, "Progress", lambda: self.show_page("Progress")); self.nav["Progress"].pack(fill="x")
        tk.Label(side, text="WORKSPACE SETTINGS", bg=SIDE, fg=MUTED, font=(FAM, 8, "bold")).pack(anchor="w", padx=22, pady=(22, 8))
        for name in SCHEMA:
            self.nav[name] = NavItem(side, name, lambda n=name: self.show_page(n)); self.nav[name].pack(fill="x")
        tk.Label(side, text="TOOLS", bg=SIDE, fg=MUTED, font=(FAM, 8, "bold")).pack(anchor="w", padx=22, pady=(22, 8))
        self.nav["Setup"] = NavItem(side, "Setup", lambda: self.show_page("Setup")); self.nav["Setup"].pack(fill="x")
        bot = tk.Frame(side, bg=SIDE); bot.pack(side="bottom", fill="x", padx=20, pady=22)
        tk.Label(bot, text="QUICK PRESET", bg=SIDE, fg=MUTED, font=(FAM, 8, "bold")).pack(anchor="w")
        pc = ttk.Combobox(bot, values=list(PRESETS), state="readonly"); pc.set("Choose a preset"); pc.pack(fill="x", pady=(5, 18)); pc.bind("<<ComboboxSelected>>", lambda _: self.preset(pc.get()))
        Tip(pc, "Applies a group of settings. You can still change each one afterwards.")
        row = tk.Frame(bot, bg=SIDE); row.pack(fill="x")
        tk.Label(row, text="Advanced options", bg=SIDE, fg=FG, font=(FAM, 9)).pack(side="left")
        self.adv = tk.BooleanVar(value=False); Switch(row, self.adv, bg=SIDE).pack(side="right")
        self.adv.trace_add("write", lambda *_: self.toggle_adv())

        main = tk.Frame(self, bg=BG); main.pack(side="left", fill="both", expand=True)
        top = tk.Frame(main, bg=BG); top.pack(fill="x", padx=30, pady=(24, 18)); top.columnconfigure(0, weight=1)
        self.url = tk.StringVar()
        box = tk.Frame(top, bg=FIELD, highlightthickness=1, highlightbackground=BORDER); box.grid(row=0, column=0, sticky="ew")
        e = tk.Entry(box, textvariable=self.url, bg=FIELD, fg=FG, insertbackground=FG, relief="flat", bd=0, highlightthickness=0, font=(FAM, 11), selectbackground=ACC, selectforeground=ACC_FG)
        e.pack(fill="x", padx=16, ipady=12)
        ph = tk.Label(box, text="Paste a video URL or select a local video file", bg=FIELD, fg=MUTED, font=(FAM, 10)); ph.place(x=16, rely=0.5, anchor="w"); ph.bind("<Button-1>", lambda _: e.focus_set())
        self.url.trace_add("write", lambda *_: ph.place_forget() if self.url.get() else ph.place(x=16, rely=0.5, anchor="w"))
        e.bind("<Return>", lambda _: self.start()); e.bind("<FocusIn>", lambda _: box.configure(highlightbackground=ACC)); e.bind("<FocusOut>", lambda _: box.configure(highlightbackground=BORDER)); e.focus_set()
        Btn(top, "Choose file", self.browse).grid(row=0, column=1, padx=(10, 0))
        self.go_btn = Btn(top, "Make clips", self.start, kind="primary"); self.go_btn.grid(row=0, column=2, padx=(10, 0))
        self.stop_btn = Btn(top, "Cancel", self.stop, state="disabled"); self.stop_btn.grid(row=0, column=3, padx=(10, 0))

        self.content = tk.Frame(main, bg=BG); self.content.pack(fill="both", expand=True, padx=(0, 4)); self.content.rowconfigure(0, weight=1); self.content.columnconfigure(0, weight=1)
        self.build_progress()
        for name, items in SCHEMA.items(): self.build_page(name, items)
        self.build_setup(); self.show_page("Progress")

    def main_key(self, fn):
        """Wrap a keyboard shortcut so it only fires when the main window has focus, not a picker or review window."""
        def handler(e):
            try:
                if e.widget.winfo_toplevel() is not self: return None
            except (AttributeError, KeyError, tk.TclError): return None
            fn()
            return "break"
        return handler

    def build_menus(self):
        """Native menus make common actions discoverable and keyboard-accessible."""
        bar = tk.Menu(self, tearoff=False)
        file_menu = tk.Menu(bar, tearoff=False)
        file_menu.add_command(label="Choose video…", accelerator="Ctrl+O", command=self.browse)
        file_menu.add_command(label="Make clips", accelerator="Ctrl+Enter", command=self.start)
        file_menu.add_command(label="Cancel current job", accelerator="Esc", command=self.stop)
        file_menu.add_separator()
        file_menu.add_command(label="Open output folder", command=lambda: self.open_path(self.last_out()))
        file_menu.add_separator()
        file_menu.add_command(label="Exit", accelerator="Alt+F4", command=self.on_close)
        bar.add_cascade(label="File", menu=file_menu)

        view_menu = tk.Menu(bar, tearoff=False)
        view_menu.add_command(label="Progress", accelerator="Ctrl+1", command=lambda: self.show_page("Progress"))
        for idx, page_name in enumerate(SCHEMA, 2):
            view_menu.add_command(label=page_name, accelerator=f"Ctrl+{idx}" if idx <= 9 else "", command=lambda n=page_name: self.show_page(n))
        view_menu.add_command(label="Setup", command=lambda: self.show_page("Setup"))
        view_menu.add_separator()
        view_menu.add_checkbutton(label="Advanced options", variable=self.adv)
        bar.add_cascade(label="View", menu=view_menu)

        tools_menu = tk.Menu(bar, tearoff=False)
        tools_menu.add_command(label="AI model picker", command=lambda: self.show_page("AI picker"))
        tools_menu.add_command(label="YouTube settings", command=lambda: self.show_page("YouTube"))
        tools_menu.add_command(label="Setup and dependencies", command=lambda: self.show_page("Setup"))
        tools_menu.add_separator()
        tools_menu.add_command(label="Clear download cache…", command=self.clear_cache)
        tools_menu.add_command(label="Reset settings…", command=self.reset)
        bar.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(bar, tearoff=False)
        help_menu.add_command(label="Keyboard shortcuts", command=self.show_shortcuts)
        help_menu.add_command(label="About Clipper", command=self.show_about)
        bar.add_cascade(label="Help", menu=help_menu)
        self.configure(menu=bar)
        self.app_menu = bar

    def show_shortcuts(self):
        messagebox.showinfo(
            "Keyboard shortcuts",
            "Ctrl + O       Choose a video file\n"
            "Ctrl + Enter   Start making clips\n"
            "Escape         Cancel the current job\n"
            "Ctrl + 1       Open Progress\n"
            "Ctrl + 2…      Open settings pages\n"
            "Alt + F4       Close Clipper",
            parent=self,
        )

    def show_about(self):
        messagebox.showinfo(
            "About Clipper",
            f"Clipper {VERSION} — Video to Short Clips\n\n"
            "A local video clipping workspace powered by yt-dlp, faster-whisper, "
            "Ollama and FFmpeg.\n\n"
            f"Python {sys.version.split()[0]} on {platform.system()} {platform.release()}\n"
            f"Settings: {SETTINGS_FILE}",
            parent=self,
        )

    def build_progress(self):
        pg = tk.Frame(self.content, bg=BG); pg.columnconfigure(0, weight=1); pg.rowconfigure(2, weight=1)
        self.header(pg, "Progress", "Each step shows its own progress and an estimate of the time left.", 0)
        c = self.card(pg); c.grid(row=1, column=0, sticky="ew", padx=30, pady=(0, 18))
        inn = tk.Frame(c, bg=PANEL); inn.pack(fill="x", padx=24, pady=20); inn.columnconfigure(0, weight=1)
        self.status = tk.StringVar(value="Ready."); tk.Label(inn, textvariable=self.status, bg=PANEL, fg=FG, font=(FAM, 16, "bold"), anchor="w").grid(row=0, column=0, sticky="w")
        self.eta = tk.StringVar(); tk.Label(inn, textvariable=self.eta, bg=PANEL, fg=MUTED, font=(FAM, 10), anchor="w").grid(row=1, column=0, sticky="w", pady=(2, 14))
        self.step_pb = ttk.Progressbar(inn, maximum=100, style="Slim.Horizontal.TProgressbar"); self.step_pb.grid(row=2, column=0, sticky="ew")
        chips = tk.Frame(inn, bg=PANEL); chips.grid(row=3, column=0, sticky="w", pady=(12, 0))
        for i, n in enumerate(STEPS, 1):
            lab = tk.Label(chips, text=f"{i}  {n}", bg=PANEL, fg=MUTED, font=(FAM, 9)); lab.pack(side="left", padx=(0, 16)); self.chips.append(lab)
        tk.Label(inn, text="Overall", bg=PANEL, fg=MUTED, font=(FAM, 9)).grid(row=4, column=0, sticky="w", pady=(18, 5))
        self.all_pb = ttk.Progressbar(inn, maximum=100, style="Slim.Horizontal.TProgressbar"); self.all_pb.grid(row=5, column=0, sticky="ew")
        lc = self.card(pg); lc.grid(row=2, column=0, sticky="nsew", padx=28); lc.columnconfigure(0, weight=1); lc.rowconfigure(0, weight=1)
        self.logbox = tk.Text(lc, bg=PANEL, fg=LOG_FG, relief="flat", wrap="word", state="disabled", padx=16, pady=12, font=(MONO, 9), highlightthickness=0, spacing1=2)
        self.logbox.grid(row=0, column=0, sticky="nsew"); sb = ttk.Scrollbar(lc, command=self.logbox.yview, style="Log.Vertical.TScrollbar"); sb.grid(row=0, column=1, sticky="ns", padx=(0, 4), pady=4); self.logbox["yscrollcommand"] = sb.set
        self.logbox.tag_configure("err", foreground=DANGER); self.logbox.tag_configure("ok", foreground=SUCCESS)
        bt = tk.Frame(pg, bg=BG); bt.grid(row=3, column=0, sticky="e", padx=28, pady=16)
        Btn(bt, "Open output folder", lambda: self.open_path(self.last_out())).pack(side="left", padx=(0, 10))
        self.yt_btn = Btn(bt, "Upload to YouTube", self.upload_button_clicked); self.yt_btn.pack(side="left", padx=(0, 10))
        self.yt_stop = Btn(bt, "Stop upload", self.yt_stop_clicked, state="disabled"); self.yt_stop.pack(side="left", padx=(0, 10))
        Btn(bt, "Clear cache", self.clear_cache).pack(side="left", padx=(0, 10)); Btn(bt, "Reset settings", self.reset).pack(side="left")
        self.pages["Progress"] = pg

    def build_page(self, name, items):
        sc = Scrolled(self.content); body = sc.body; body.columnconfigure(0, weight=1)
        self.header(body, name, SUBTITLES.get(name, ""), 0)
        c = self.card(body); c.grid(row=1, column=0, sticky="ew", padx=30, pady=(0, 28))
        inn = tk.Frame(c, bg=PANEL); inn.pack(fill="x", padx=28, pady=16); inn.columnconfigure(0, weight=1, minsize=300); inn.columnconfigure(1, weight=0)
        for r, it in enumerate(items): self.add_row(inn, r, *it)
        if name == "AI picker": c.grid_configure(pady=(0, 16)); self.build_models_card(body, 2)
        if name == "YouTube": c.grid_configure(pady=(0, 16)); self.build_yt_guide_card(body, 2)
        self.pages[name] = sc

    def show_page(self, name):
        for p in self.pages.values(): p.grid_remove()
        self.pages[name].grid(row=0, column=0, sticky="nsew")
        for n, item in self.nav.items(): item.set_active(n == name)
        self.scroller = self.pages[name] if isinstance(self.pages[name], Scrolled) else None
        if name == "AI picker" and not self.models_loaded: self.models_loaded = True; self.refresh_models(quiet=True)
    def wheel(self, e):
        try:
            if self.scroller is None or e.widget.winfo_toplevel() is not self or isinstance(e.widget, tk.Text): return
        except (AttributeError, KeyError, tk.TclError): return
        self.scroller.cv.yview_scroll((-1 if (e.num == 4 or getattr(e, "delta", 0) > 0) else 1), "units")

    def font_list(self):
        """Installed font families, sorted, without Windows' duplicate '@' vertical variants."""
        if not hasattr(self, "_fonts"):
            self._fonts = sorted({n for n in tkfont.families(self) if n and not n.startswith("@")}, key=str.lower)
        return self._fonts

    def add_row(self, f, r, key, label, kind, default, opts, hint, adv):
        blk = tk.Frame(f, bg=PANEL); blk.grid(row=r, column=0, sticky="nw", pady=15, padx=(0, 30))
        tk.Label(blk, text=label, bg=PANEL, fg=FG, font=(FAM, 10), anchor="w").pack(anchor="w")
        if hint: tk.Label(blk, text=hint, bg=PANEL, fg=MUTED, font=(FAM, 9), wraplength=390, justify="left", anchor="w").pack(anchor="w", pady=(3, 0))
        if kind == "bool":
            v = tk.BooleanVar(value=default); w = Switch(f, v); self.get[key], self.set[key] = v.get, v.set
        elif kind == "choice":
            v = tk.StringVar(value=str(default)); w = ttk.Combobox(f, textvariable=v, values=[str(o) for o in opts], state="readonly", width=22); self.get[key], self.set[key] = v.get, v.set
        elif kind in ("int", "float"):
            v = tk.StringVar(value=str(default)); w = ttk.Spinbox(f, textvariable=v, from_=opts[0], to=opts[1], increment=opts[2] if len(opts) > 2 else 1, width=10); self.get[key], self.set[key] = v.get, v.set
        elif kind == "text":
            w = tk.Text(f, height=3, width=34, bg=FIELD, fg=FG, relief="flat", wrap="word", insertbackground=FG, highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACC, padx=8, pady=6, font=(FAM, 10))
            self.get[key] = lambda w=w: w.get("1.0", "end").strip(); self.set[key] = lambda s, w=w: (w.delete("1.0", "end"), w.insert("1.0", s))
        elif kind == "path":
            v = tk.StringVar(value=default); w = tk.Frame(f, bg=PANEL); ttk.Entry(w, textvariable=v, width=30).pack(side="left")
            Btn(w, "Browse", lambda v=v: v.set(filedialog.askdirectory(initialdir=v.get()) or v.get()), bg=PANEL, small=True).pack(side="left", padx=(8, 0)); self.get[key], self.set[key] = v.get, v.set
        elif kind == "ollama":
            v = tk.StringVar(value=default); w = tk.Frame(f, bg=PANEL); cb = ttk.Combobox(w, textvariable=v, width=24); cb.pack(side="left")
            # Pre-populate with the suggested models so the dropdown is never empty, even before
            # Ollama is started or any model is downloaded. The user can pick a suggestion and
            # download it from the "Download more models" card below.
            cb["values"] = list(SUGGESTED)
            self.model_cbs.append(cb)
            Btn(w, "Refresh", lambda cb=cb: self.refresh_models(cb), bg=PANEL, small=True).pack(side="left", padx=(8, 0)); self.get[key], self.set[key] = v.get, v.set
        elif kind == "font":
            v = tk.StringVar(value=default); w = ttk.Combobox(f, textvariable=v, values=self.font_list(), width=22); self.get[key], self.set[key] = v.get, v.set
        else:
            v = tk.StringVar(value=default); w = ttk.Entry(f, textvariable=v, width=36); self.get[key], self.set[key] = v.get, v.set
        w.grid(row=r, column=1, sticky="ne" if kind == "text" else "e", pady=12); self.rows.append(([blk, w], adv))

    def toggle_adv(self):
        for ws, adv in self.rows:
            if adv:
                for w in ws: w.grid() if self.adv.get() else w.grid_remove()

    # ───────── AI picker: download more Ollama models ─────────
    def build_models_card(self, body, row):
        c = self.card(body); c.grid(row=row, column=0, sticky="ew", padx=30, pady=(0, 28))
        inn = tk.Frame(c, bg=PANEL); inn.pack(fill="x", padx=26, pady=18); inn.columnconfigure(0, weight=1)
        tk.Label(inn, text="Download more models", bg=PANEL, fg=FG, font=(FAM, 11, "bold")).grid(row=0, column=0, sticky="w", columnspan=2)
        self.gpu_note = tk.StringVar(value="Checking your GPU...")
        tk.Label(inn, textvariable=self.gpu_note, bg=PANEL, fg=MUTED, font=(FAM, 9), wraplength=620, justify="left", anchor="w").grid(row=1, column=0, sticky="w", columnspan=2, pady=(3, 14))
        tk.Label(inn, text="Installed", bg=PANEL, fg=FG, font=(FAM, 10)).grid(row=2, column=0, sticky="w")
        self.installed_var = tk.StringVar(value="Press Refresh in the Ollama model row to list them.")
        tk.Label(inn, textvariable=self.installed_var, bg=PANEL, fg=MUTED, font=(FAM, 9), wraplength=620, justify="left", anchor="w").grid(row=3, column=0, sticky="w", columnspan=2, pady=(2, 14))
        tk.Label(inn, text="Model to download", bg=PANEL, fg=FG, font=(FAM, 10)).grid(row=4, column=0, sticky="w")
        ctl = tk.Frame(inn, bg=PANEL); ctl.grid(row=5, column=0, sticky="w", pady=(6, 0), columnspan=2)
        self.pull_var = tk.StringVar(value="llama3.2:3b")
        pc = ttk.Combobox(ctl, textvariable=self.pull_var, values=list(SUGGESTED), width=30); pc.pack(side="left")
        self.pull_btn = Btn(ctl, "Download", self.pull_model, kind="primary", bg=PANEL, small=True); self.pull_btn.pack(side="left", padx=(10, 0))
        self.pull_stop = Btn(ctl, "Cancel", lambda: self.pull_cancel.set(), bg=PANEL, small=True, state="disabled"); self.pull_stop.pack(side="left", padx=(8, 0))
        self.pull_hint = tk.StringVar()
        tk.Label(inn, textvariable=self.pull_hint, bg=PANEL, fg=MUTED, font=(FAM, 9), wraplength=620, justify="left", anchor="w").grid(row=6, column=0, sticky="w", pady=(6, 0), columnspan=2)
        self.pull_pb = ttk.Progressbar(inn, maximum=100, style="Slim.Horizontal.TProgressbar"); self.pull_pb.grid(row=7, column=0, sticky="ew", pady=(14, 4), columnspan=2)
        self.pull_status = tk.StringVar()
        tk.Label(inn, textvariable=self.pull_status, bg=PANEL, fg=MUTED, font=(FAM, 9), anchor="w").grid(row=8, column=0, sticky="w", columnspan=2)
        def hint(*_):
            n = self.pull_var.get().strip(); g = SUGGESTED.get(n)
            self.pull_hint.set(f"About {g[0]} GB. {g[1]}" if g else "Any model name from ollama.com/library works, for example llama3.2:1b.")
        self.pull_var.trace_add("write", hint); hint()

    def pull_model(self):
        name = self.pull_var.get().strip()
        if not name: return
        if self.pulling: return
        self.pulling = True; self.pull_cancel.clear(); self.pull_btn.state(["disabled"]); self.pull_stop.state(["!disabled"]); self.pull_pb["value"] = 0; self.pull_status.set("Starting...")
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
                        self.q.put(("pull_prog", top if tot else None, f"{d.get('status', '')}  {done / 1e9:.2f} of {tot / 1e9:.2f} GB" if tot else d.get("status", "")))
            except urllib.error.HTTPError as e: err = f"Ollama error {e.code}: {e.read()[:150].decode(errors='replace')}"
            except urllib.error.URLError: err = "Couldn't reach Ollama. Start it on the Setup page first."
            except (OSError, ValueError) as e: err = str(e)
            self.q.put(("pull_done", name, err))
        threading.Thread(target=run, daemon=True).start()

    def refresh_models(self, cb=None, quiet=False):
        host = self.get["ollama_host"]().rstrip("/")
        threading.Thread(target=lambda: self.q.put(("models", ollama_models(host), quiet)), daemon=True).start()

    def on_models(self, models, quiet):
        if models is None:
            self.installed_var.set("Couldn't reach Ollama. Start it on the Setup page.")
            if not quiet: messagebox.showwarning("Ollama", "Couldn't reach Ollama. Is it running? You can start it on the Setup page.")
            return
        installed_names = [n for n, _ in models]
        # Build the dropdown list: installed models first, then suggested models that aren't
        # already installed. This way the dropdown is never empty, and installed models are
        # easy to find at the top.
        values = list(installed_names)
        for s in SUGGESTED:
            if s not in values: values.append(s)
        for cb in self.model_cbs: cb["values"] = values
        if installed_names:
            self.installed_var.set(", ".join(f"{n} ({s / 1e9:.1f} GB)" for n, s in models))
        else:
            self.installed_var.set("No models installed yet. Pick a suggestion from the dropdown, or download one below.")
    def on_pull_prog(self, frac, text):
        if frac is None:
            if str(self.pull_pb["mode"]) != "indeterminate": self.pull_pb.configure(mode="indeterminate"); self.pull_pb.start(12)
        else:
            if str(self.pull_pb["mode"]) == "indeterminate": self.pull_pb.stop(); self.pull_pb.configure(mode="determinate")
            self.pull_pb["value"] = frac * 100
        self.pull_status.set(text)
    def on_pull_done(self, name, err):
        self.pulling = False; self.pull_pb.stop(); self.pull_pb.configure(mode="determinate"); self.pull_btn.state(["!disabled"]); self.pull_stop.state(["disabled"])
        self.pull_pb["value"] = 0 if err else 100
        self.pull_status.set(err or f"Downloaded {name}. Pick it in the Ollama model row above to use it.")
        if not err: self.refresh_models(quiet=True)

    # ───────── YouTube upload ─────────
    def build_yt_guide_card(self, body, row):
        c = self.card(body); c.grid(row=row, column=0, sticky="ew", padx=30, pady=(0, 28))
        inn = tk.Frame(c, bg=PANEL); inn.pack(fill="x", padx=26, pady=18)
        tk.Label(inn, text="Get YouTube upload credentials", bg=PANEL, fg=FG, font=(FAM, 11, "bold")).pack(anchor="w")
        tk.Label(inn, text="Clipper needs a client_secret.json from Google Cloud to upload. Here is how to get one:",
                 bg=PANEL, fg=MUTED, font=(FAM, 9), wraplength=620, justify="left", anchor="w").pack(anchor="w", pady=(4, 10))
        steps = [
            "1.  Go to console.cloud.google.com  →  create a new project (or pick an existing one).",
            "2.  APIs & Services  →  Library  →  search 'YouTube Data API v3'  →  Enable it.",
            "3.  APIs & Services  →  OAuth consent screen  →  External  →  fill in a name. Add yourself as a Test user.",
            "4.  APIs & Services  →  Credentials  →  Create Credentials  →  OAuth client ID.",
            "5.  Application type: Desktop app.  Give it a name, then Create.",
            "6.  Click 'Download JSON'.  Rename the file to client_secret.json.",
            "7.  Put client_secret.json in your home folder (or set its path above).",
            "8.  Turn on 'Upload clips to YouTube' above. The first upload opens a browser to sign in.",
        ]
        for s in steps:
            tk.Label(inn, text=s, bg=PANEL, fg=FG, font=(FAM, 9), anchor="w", justify="left").pack(anchor="w", pady=1)
        btns = tk.Frame(inn, bg=PANEL); btns.pack(anchor="w", pady=(12, 0))
        Btn(btns, "Open Google Cloud Console", lambda: webbrowser.open("https://console.cloud.google.com/"), kind="primary", bg=PANEL, small=True).pack(side="left", padx=(0, 8))
        Btn(btns, "Test connection", self.yt_test_connection, bg=PANEL, small=True).pack(side="left")

    def yt_test_connection(self):
        """Quick check: are the YouTube packages installed, and does the client_secret exist?"""
        if not have_mod("googleapiclient") or not have_mod("google_auth_oauthlib"):
            messagebox.showwarning("YouTube", "The YouTube packages aren't installed.\nGo to the Setup page and install 'YouTube upload packages', then try again.", parent=self)
            return
        cs = Path(self.get["yt_client_secret"]()).expanduser()
        if not cs.is_file():
            messagebox.showwarning("YouTube", f"client_secret.json not found at:\n{cs}\n\nDownload it from Google Cloud Console (see the steps above) and place it there, or set its path in the YouTube settings.", parent=self)
            return
        messagebox.showinfo("YouTube", f"client_secret.json found at:\n{cs}\n\nEverything looks ready. The first upload will open a browser to sign in to Google.", parent=self)

    def upload_button_clicked(self):
        if self.running:
            messagebox.showinfo("Clipper", "Wait for the current job to finish, or cancel it first.", parent=self)
            return
        if getattr(self, "yt_cancel", None) and self.yt_stop.disabled is False:
            messagebox.showinfo("Clipper", "An upload is already running.", parent=self)
            return
        files = [f for f in (self.last_files or []) if Path(f["path"] if isinstance(f, dict) else f).is_file()]
        files = [f if isinstance(f, dict) else {"path": str(f), "title": Path(f).stem} for f in files]
        UploadPicker(self, files, self.uploaded, self.upload_youtube)   

    def upload_youtube(self, files):
        """Upload clips to YouTube in a background thread. 'files' is a list of dicts with path/title/description."""
        c = self.cfg()
        if not have_mod("googleapiclient") or not have_mod("google_auth_oauthlib"):
            self.on_log("YouTube upload: packages not installed. Go to Setup to install them.")
            return
        cs_path = Path(c["yt_client_secret"]).expanduser()
        if not cs_path.is_file():
            self.on_log(f"YouTube upload: client_secret.json not found at {cs_path}. See the YouTube settings page for steps.")
            return
        token_path = SETTINGS_FILE.parent / "youtube_token.json"
        privacy = c["yt_privacy"]
        hashtags_in_title = c["yt_hashtags_in_title"]
        default_tags = [t.strip() for t in c["yt_default_tags"].split(",") if t.strip()]
        cancel_ev = threading.Event(); self.yt_cancel = cancel_ev
        self.yt_btn.state(["disabled"]); self.yt_stop.state(["!disabled"])
        self.status.set(f"Uploading {len(files)} clip(s) to YouTube...")
        def emit(*a): self.q.put(a)
        def run():
            try:
                from google.oauth2.credentials import Credentials
                from google_auth_oauthlib.flow import InstalledAppFlow
                from google.auth.transport.requests import Request
                from googleapiclient.discovery import build
                from googleapiclient.http import MediaFileUpload
            except ImportError as e:
                self.q.put(("log", f"YouTube upload: missing package ({e}). Install 'YouTube upload packages' on the Setup page."))
                self.q.put(("yt_done",))
                return
            SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
            # --- authenticate (load saved token, or run OAuth browser flow) ---
            creds = None
            if token_path.is_file():
                try: creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
                except Exception: creds = None
            if creds and creds.expired and creds.refresh_token:
                try: creds.refresh(Request())
                except Exception: creds = None
            if not creds or not creds.valid:
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(str(cs_path), SCOPES)
                    creds = flow.run_local_server(port=0)  # opens the default browser
                except Exception as e:
                    self.q.put(("log", f"YouTube auth failed: {e}"))
                    self.q.put(("yt_done",))
                    return
                try:
                    token_path.parent.mkdir(parents=True, exist_ok=True)
                    token_path.write_text(creds.to_json(), encoding="utf-8")
                    os.chmod(token_path, 0o600)  # the token grants upload access; keep it private
                except OSError: pass
            try:
                youtube = build("youtube", "v3", credentials=creds, static_discovery=False)
            except Exception as e:
                self.q.put(("log", f"YouTube: couldn't connect to the API: {e}"))
                self.q.put(("yt_done",))
                return
            # --- upload each clip ---
            uploaded = 0
            for i, f in enumerate(files, 1):
                if cancel_ev.is_set(): break
                path = f["path"] if isinstance(f, dict) else str(f)
                title = (f.get("yt_title") or f.get("title") or Path(path).stem) if isinstance(f, dict) else Path(path).stem
                desc = (f.get("yt_description") or f.get("description") or "") if isinstance(f, dict) else ""
                # Move hashtags from description to title if enabled
                desc_tags = re.findall(r"#\w+", desc)
                if hashtags_in_title and desc_tags:
                    tag_str = " " + " ".join(desc_tags[:5])
                    if tag_str.strip() not in title:
                        title = (title + tag_str)[:100]  # YouTube title limit is 100 chars
                tags, size = [], 0
                for t in (list(f.get("yt_tags") or []) if isinstance(f, dict) else []) + default_tags + [t.lstrip("#") for t in desc_tags]:
                    if t and t.lower() not in {x.lower() for x in tags} and size + len(t) + 1 <= 450: tags.append(t); size += len(t) + 1
                body = {
                    "snippet": {"title": title[:100], "description": desc[:5000], "tags": tags, "categoryId": YT_CATEGORIES.get(c["yt_category"], "22")},
                    "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
                }
                self.q.put(("log", f"YouTube: uploading {Path(path).name} ({i}/{len(files)}) as {privacy}..."))
                try:
                    media = MediaFileUpload(path, mimetype="video/mp4", resumable=True, chunksize=10 * 1024 * 1024)
                    request = youtube.videos().insert(part=",".join(body.keys()), body=body, media_body=media)
                    response = None
                    while response is None:
                        if cancel_ev.is_set(): break
                        status, response = request.next_chunk()
                        if status:
                            self.q.put(("log", f"  {Path(path).name}: {int(status.progress() * 100)}%"))
                    if response and response.get("id"):
                        vid = response["id"]
                        self.q.put(("log", f"YouTube: uploaded {Path(path).name} → https://www.youtube.com/watch?v={vid}"))
                        uploaded += 1
                        self.q.put(("yt_uploaded",path))
                    elif response and response.get("error"):
                        self.q.put(("log", f"YouTube: error uploading {Path(path).name}: {response['error'].get('message', '?')}"))
                except Exception as e:
                    self.q.put(("log", f"YouTube: error uploading {Path(path).name}: {e}"))
            note = " (cancelled)" if cancel_ev.is_set() else ""
            self.q.put(("log", f"YouTube: done{note}. {uploaded} of {len(files)} clip(s) uploaded." if uploaded else "YouTube: no clips were uploaded."))
            self.q.put(("yt_done",))
        threading.Thread(target=run, daemon=True).start()

    def yt_stop_clicked(self):
        if getattr(self, "yt_cancel", None): self.yt_cancel.set()
        self.yt_stop.state(["disabled"])
        self.on_log("YouTube upload: stopping after the current chunk...")
    def on_yt_done(self):
        self.yt_btn.state(["!disabled"]); self.yt_stop.state(["disabled"])
        self.status.set("Ready.")
    def on_yt_uploaded(self, path):
        self.uploaded.add(str(Path(path)))

    # ───────── Setup page ─────────
    def section(self, parent, row, title, sub=""):
        c = self.card(parent); c.grid(row=row, column=0, sticky="ew", padx=30, pady=(0, 18))
        tk.Label(c, text=title, bg=PANEL, fg=FG, font=(FAM, 11, "bold")).pack(anchor="w", padx=24, pady=(16, 0))
        if sub: tk.Label(c, text=sub, bg=PANEL, fg=MUTED, font=(FAM, 9), wraplength=700, justify="left").pack(anchor="w", padx=24, pady=(2, 0))
        inn = tk.Frame(c, bg=PANEL); inn.pack(fill="x", padx=24, pady=(6, 12)); inn.columnconfigure(1, weight=1); return inn

    def status_row(self, f, r, state, name, why, detail, buttons):
        col = {"ok": ACC, "missing": DANGER, "warn": WARNING, "skip": MUTED}[state]
        tk.Label(f, text="●", bg=PANEL, fg=col, font=(FAM, 10)).grid(row=r, column=0, sticky="nw", padx=(0, 12), pady=(11, 0))
        blk = tk.Frame(f, bg=PANEL); blk.grid(row=r, column=1, sticky="w", pady=8)
        tk.Label(blk, text=name, bg=PANEL, fg=FG, font=(FAM, 10), anchor="w").pack(anchor="w")
        tk.Label(blk, text=why, bg=PANEL, fg=MUTED, font=(FAM, 9), anchor="w", justify="left", wraplength=270).pack(anchor="w")
        tk.Label(f, text=detail, bg=PANEL, fg=MUTED, font=(FAM, 9), anchor="e", justify="right", wraplength=130).grid(row=r, column=2, sticky="e", padx=14)
        bf = tk.Frame(f, bg=PANEL); bf.grid(row=r, column=3, sticky="e")
        for text, cmd in buttons: Btn(bf, text, cmd, bg=PANEL, small=True).pack(side="left", padx=(6, 0))

    def build_setup(self):
        sc = Scrolled(self.content); body = sc.body; body.columnconfigure(0, weight=1)
        self.header(body, "Setup", "See what is installed and install what is missing. Nothing runs unless you press a button.", 0)
        c = self.card(body); c.grid(row=1, column=0, sticky="ew", padx=30, pady=(0, 18)); inn = tk.Frame(c, bg=PANEL); inn.pack(fill="x", padx=24, pady=18)
        self.sys_var = tk.StringVar(value="Checking this computer...")
        tk.Label(inn, textvariable=self.sys_var, bg=PANEL, fg=FG, font=(FAM, 10), justify="left", anchor="w").pack(anchor="w")
        bt = tk.Frame(inn, bg=PANEL); bt.pack(anchor="w", pady=(14, 0))
        for text, cmd, kind in (("Check again", self.setup_check, "secondary"), ("Install enabled dependencies", self.setup_install_all, "primary"), ("Save setup script", self.setup_save_script, "secondary"), ("Copy all commands", self.setup_copy_all, "secondary")):
            Btn(bt, text, cmd, kind=kind, bg=PANEL).pack(side="left", padx=(0, 10))
        self.dep_f = self.section(body, 2, "Requirements", "Programs and Python packages Clipper uses.")
        self.svc_f = self.section(body, 3, "Ollama", "Optional local AI service. Required only when the AI picker is enabled.")
        self.wh_f = self.section(body, 4, "Whisper models", "Speech models. Each is downloaded once and reused. The size you use is set on the Transcribe page.")
        lc = self.card(body); lc.grid(row=5, column=0, sticky="ew", padx=30, pady=(0, 28))
        top = tk.Frame(lc, bg=PANEL); top.pack(fill="x", padx=24, pady=(16, 6))
        tk.Label(top, text="Install log", bg=PANEL, fg=FG, font=(FAM, 11, "bold")).pack(side="left")
        self.job_stop = Btn(top, "Cancel", self.job_cancel, bg=PANEL, small=True, state="disabled"); self.job_stop.pack(side="right")
        self.job_pb = ttk.Progressbar(lc, maximum=100, mode="indeterminate", style="Slim.Horizontal.TProgressbar")
        self.setup_log = tk.Text(lc, height=10, bg=PANEL, fg=LOG_FG, relief="flat", wrap="word", state="disabled", padx=10, pady=6, font=(MONO, 9), highlightthickness=0)
        self.setup_log.pack(fill="x", padx=14, pady=(0, 14)); self.setup_log.tag_configure("err", foreground=DANGER); self.setup_log.tag_configure("ok", foreground=SUCCESS)
        self.pages["Setup"] = sc

    def slog(self, t):
        self.setup_log.configure(state="normal"); self.setup_log.insert("end", t + "\n", "err" if t.startswith(("Error", "Couldn't", "FAILED")) else "")
        try:
            if int(self.setup_log.index("end-1c").split(".")[0]) > 2000:
                self.setup_log.delete("1.0", "301.0")
        except (ValueError, tk.TclError): pass
        self.setup_log.see("end"); self.setup_log.configure(state="disabled")
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
            self.q.put(("setup_checked", dict(sysname=name, mgr=mgr, gpu=nv, py=sys.version.split()[0], rows=rows, up=ollama_up(host), models=ollama_models(host), wgpu=wgpu,
                                             whisper={n: whisper_cached(n) for n in WHISPER_REPOS}, fw=fw, host=host)))
        threading.Thread(target=run, daemon=True).start()

    def on_setup_checked(self, i):
        self.setup_info = i; nv = i["gpu"]
        gpu = f"NVIDIA GPU: {nv[0]}" + (f", {nv[1]} GB VRAM" if nv[1] else "") if nv else "No NVIDIA GPU found, so Whisper and encoding use the CPU"
        if nv and i["wgpu"] is not None: gpu += ". Whisper can use it." if i["wgpu"] else ". Whisper can't use it yet: install the CUDA 12 libraries below."
        self.sys_var.set(f"{i['sysname']}  ·  package manager: {mgr_label(i['mgr'])}  ·  Python {i['py']}\n{gpu}")
        self.gpu_note.set(f"Your GPU: {nv[0]}" + (f", {nv[1]} GB. Models up to about {max(nv[1] - 2, 1):.0f} GB fit fully on it. Larger ones run partly on the CPU and are slower." if nv[1] else ".") if nv else "No NVIDIA GPU found, so models run on the CPU. Prefer the small ones.")
        can_elevate = WIN or is_admin() or bool(shutil.which("pkexec")); mgr = i["mgr"]
        for w in self.dep_f.winfo_children(): w.destroy()
        for r, d in enumerate(i["rows"]):
            tag = {"recommended": "  ·  recommended", "optional": "  ·  optional"}.get(d["level"], "")
            pl = plan_for(d, mgr, nv is not None); btns = []
            if d["state"] in ("missing", "warn"):
                if pl["kind"] in ("pip", "winget", "download") or (pl["kind"] == "sys" and (not pl["root"] or can_elevate)):
                    btns.append(("Install", lambda d=d: self.setup_install([d])))
                btns.append(("Copy command", lambda t=pl["text"]: self.copy_text(t)))
                if pl.get("url"): btns.append(("Website", lambda u=pl["url"]: webbrowser.open(u)))
            elif d["state"] == "ok" and d.get("updatable"): btns.append(("Update", lambda d=d: self.setup_install([d], upgrade=True)))
            self.status_row(self.dep_f, r, d["state"], d["name"] + tag, d["why"], d["detail"], btns)
        for w in self.svc_f.winfo_children(): w.destroy()
        have = any(d["id"] == "ollama" and d["state"] == "ok" for d in i["rows"]); b = []
        if i["up"]: st, det = "ok", f"Running at {i['host']}" + (f"  ·  {len(i['models'])} model(s) installed" if i["models"] is not None else "")
        elif have: st, det = "warn", "Installed but not running"; b.append(("Start", self.setup_start_ollama))
        else: st, det = "missing", "Not installed"
        b.append(("Manage models", lambda: self.show_page("AI picker")))
        self.status_row(self.svc_f, 0, st, "Ollama service", "Optional; needed only when the AI picker is enabled.", det, b)
        for w in self.wh_f.winfo_children(): w.destroy()
        for r, n in enumerate(WHISPER_REPOS):
            got = i["whisper"][n]; btn = [] if got or not i["fw"] else [("Download", lambda n=n: self.setup_whisper(n))]
            self.status_row(self.wh_f, r, "ok" if got else "warn", n, f"About {WHISPER_MB[n] / 1000:.1f} GB" if WHISPER_MB[n] >= 1000 else f"About {WHISPER_MB[n]} MB", "downloaded" if got else ("not downloaded yet" if i["fw"] else "install faster-whisper first"), btn)
        self.nav["Setup"].set_badge(any(d["state"] == "missing" for d in i["rows"]))
        if not self.asked_setup and any(d["state"] == "missing" for d in i["rows"]): self.asked_setup = True; self.after(300, self.offer_install)
        # on_models() merges installed + suggested models into the dropdown, so we just delegate
        # to it when we have a valid (possibly empty) model list.
        if i["models"] is not None: self.on_models(i["models"], True)

    def offer_install(self):
        """On launch, offer to install whatever the app can't run without (and the recommended extras), in one click."""
        i = self.setup_info
        if not i or self.job_busy or self.running: return
        todo = [d for d in i["rows"] if d["state"] in ("missing", "warn")]
        if not self.get["use_ai"](): todo = [d for d in todo if d["id"] != "ollama"]
        if not self.get["yt_enabled"](): todo = [d for d in todo if d["id"] != "yt"]
        if not self.get["face_track"](): todo = [d for d in todo if d["id"] != "cv"]
        if not any(d["state"] == "missing" for d in todo): return
        names = "\n".join("   -  " + d["name"] + (" (large download, about 1 GB)" if d["id"] == "cuda" else "") for d in todo)
        if messagebox.askyesno("Clipper setup", "Clipper needs a few things that aren't installed yet:\n\n" + names + "\n\nInstall them now? You can follow the progress on the Setup page.", parent=self):
            self.show_page("Setup"); self.setup_install(todo)

    def copy_text(self, t):
        self.clipboard_clear(); self.clipboard_append(t); self.update(); self.slog("Copied: " + t)
    def setup_copy_all(self):
        i = getattr(self, "setup_info", None)
        if not i: return
        todo = [d for d in i["rows"] if d["state"] in ("missing", "warn")]
        if not self.get["use_ai"]():
            todo = [d for d in todo if d["id"] != "ollama"]
        if not self.get["yt_enabled"]():
            todo = [d for d in todo if d["id"] != "yt"]
        if not self.get["face_track"](): todo = [d for d in todo if d["id"] != "cv"]
        if not todo: self.slog("Everything required by the current settings is installed."); return
        self.copy_text("\n".join(pl["text"] for pl in merge_plans([plan_for(d, i["mgr"], i["gpu"] is not None) for d in todo], i["mgr"])))
    def setup_save_script(self):
        mgr = (getattr(self, "setup_info", None) or {}).get("mgr") or system_info()[1]
        if WIN: path = filedialog.asksaveasfilename(initialfile="setup_clipper.ps1", defaultextension=".ps1", filetypes=[("PowerShell script", "*.ps1")])
        else: path = filedialog.asksaveasfilename(initialfile="setup_clipper.sh", defaultextension=".sh", filetypes=[("Shell script", "*.sh")])
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="") as fh: fh.write(setup_script(mgr))
        if WIN: self.slog(f"Saved {path}. Run it with: powershell -ExecutionPolicy Bypass -File \"{Path(path).name}\"")
        else:
            os.chmod(path, 0o755)
            self.slog(f"Saved {path}. Run it with: bash {Path(path).name}")

    def start_job(self, title, fn, cancellable=True):
        if self.job_busy: messagebox.showinfo("Setup", "Another task is still running. Wait for it or press Cancel in the install log."); return
        self.job_busy = True; self.job = {"cancel": False, "p": None}; self.job_pb.pack(fill="x", padx=24, pady=(0, 8), before=self.setup_log); self.job_pb.start(12); self.job_stop.state(["!disabled"] if cancellable else ["disabled"]); self.slog("— " + title)
        job = self.job
        def run():
            ok = False
            try: ok = bool(fn(lambda t: self.q.put(("setup_log", t)), job))
            except Exception as e: self.q.put(("setup_log", f"Error: {e}"))
            self.q.put(("setup_done", title, ok))
        threading.Thread(target=run, daemon=True).start()
    def job_cancel(self):
        if self.job: self.job["cancel"] = True; p = self.job.get("p")
        if self.job and p and p.poll() is None: kill_tree(p)
    def on_setup_done(self, title, ok):
        self.job_busy = False; self.job_pb.stop(); self.job_pb.pack_forget(); self.job_stop.state(["disabled"]); self.slog(("Done: " if ok else "FAILED: ") + title)
        if ok and "CUDA" in title: self.slog("Restart Clipper so the GPU libraries are loaded.")
        self.setup_check()

    def setup_install(self, rows, upgrade=False):
        i = self.setup_info; mgr = i["mgr"]
        esc = elevation(gui=True)
        plans = merge_plans([plan_for(d, mgr, i["gpu"] is not None, upgrade) for d in rows], mgr)
        auto = [pl for pl in plans if pl["kind"] in ("pip", "sys", "winget", "download")]; manual = [pl for pl in plans if pl["kind"] == "manual"]
        for pl in manual: self.slog("Install this one yourself: " + pl["text"])
        if any(pl["kind"] == "sys" and pl["root"] for pl in auto) and esc is None:
            for pl in auto:
                if pl["kind"] == "sys":
                    self.slog("Needs admin rights and 'pkexec' wasn't found. Run in a terminal: " + pl["text"])
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
        i = getattr(self, "setup_info", None)
        if not i: return
        todo = [d for d in i["rows"] if d["state"] in ("missing", "warn")]
        if not self.get["use_ai"]():
            todo = [d for d in todo if d["id"] != "ollama"]
        if not self.get["yt_enabled"]():
            todo = [d for d in todo if d["id"] != "yt"]
        if not self.get["face_track"](): todo = [d for d in todo if d["id"] != "cv"]
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
            if not ollama_bin:
                emit("Ollama is not installed or not on PATH. Install it from the Setup page first.")
                return False
            if ollama_up(host): emit("Ollama is already running."); return True
            emit("Starting: ollama serve")
            if WIN: subprocess.Popen([ollama_bin, "serve"], **dict(quiet_kw(), creationflags=NOWIN | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP))
            else: subprocess.Popen([ollama_bin, "serve"], start_new_session=True, **quiet_kw())
            for _ in range(30):
                if ollama_up(host): emit("Ollama is running."); return True
                time.sleep(0.5)
            emit("Ollama didn't start in time. Try: ollama serve  (in a terminal)"); return False
        self.start_job("Start Ollama", fn, cancellable=False)

    # settings
    def cfg(self):
        d = {}
        for items in SCHEMA.values():
            for key, label, kind, default, *_ in items:
                try: d[key] = cast(kind, default, self.get[key]())
                except ValueError: raise ValueError(f"'{label}' needs a number.")
        return d
    def save(self):
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {k: self.get[k]() for k in self.get}; data["_adv"] = self.adv.get(); data["_version"] = VERSION
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8"); os.replace(tmp, SETTINGS_FILE)
    def load(self):
        try: data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError): return
        if "ollama_host" in data and "localhost" in str(data["ollama_host"]):
            data["ollama_host"] = "http://127.0.0.1:11434"  # migrate old IPv6-prone setting
        for k, v in data.items():
            if k in self.set:
                try: self.set[k](v)
                except (ValueError, TypeError, tk.TclError): pass  # ignore one bad saved value
        self.adv.set(bool(data.get("_adv", False)))
    def preset(self, name):
        for k, v in PRESETS[name].items():
            if k in self.set: self.set[k](v)
        self.on_log(f"Preset applied: {name}")
    def reset(self):
        if not messagebox.askyesno("Reset settings", "Restore every setting to its default?"): return
        for items in SCHEMA.values():
            for key, _, _, default, *_ in items: self.set[key](default)
    # actions
    def browse(self):
        p = filedialog.askopenfilename(filetypes=[("Video", "*.mp4 *.mkv *.mov *.webm *.avi"), ("All files", "*.*")])
        if p: self.url.set(p)
    def last_out(self): return Path(self.get["output_dir"]()).expanduser()
    def open_path(self, p):
        Path(p).mkdir(parents=True, exist_ok=True)
        if WIN:
            try: os.startfile(str(p))
            except OSError as e: self.on_log(f"Couldn't open the folder: {e}")
            return
        opener = which("xdg-open")
        if not opener:
            self.on_log("Saved folder: " + str(p) + " (xdg-open is not installed).")
            return
        subprocess.Popen([opener, str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def clear_cache(self):
        cache = self.last_out() / ".cache"
        if cache.exists() and messagebox.askyesno("Clear cache", "Delete downloaded videos and transcripts? Finished clips are kept."): shutil.rmtree(cache, ignore_errors=True); self.on_log("Cache cleared.")
    def start(self):
        src = self.url.get().strip().strip('"\'')
        if self.running: return
        if not src: messagebox.showinfo("Clipper", "Paste a video link or choose a file first."); return
        try: cfg = self.cfg()
        except ValueError as e: messagebox.showerror("Invalid setting", str(e)); return
        if cfg["min_len"] >= cfg["max_len"]: messagebox.showerror("Invalid setting", "The shortest clip must be shorter than the longest."); return
        self.save(); self.cancel_ev.clear(); self.running = True
        self.pipe = Pipeline(cfg, src, lambda *a: self.q.put(a), self.cancel_ev)
        self.go_btn.state(["disabled"]); self.stop_btn.state(["!disabled"]); self.show_page("Progress")
        self.logbox.configure(state="normal"); self.logbox.delete("1.0", "end"); self.logbox.configure(state="disabled")
        self.step_pb["value"] = self.all_pb["value"] = 0; self.eta.set(""); self.paint_chips(0)
        threading.Thread(target=self.work, daemon=True).start()
    def work(self):
        try: out, files = self.pipe.go(); self.q.put(("done", str(out), files))
        except Cancelled: self.q.put(("cancelled",))
        except Exception as e: self.q.put(("error", str(e), traceback.format_exc()))
    def stop(self):
        if not self.running: return  # the File menu can call this when nothing is running
        self.cancel_ev.set(); self.stop_btn.state(["disabled"]); self.status.set("Cancelling...")
        if self.pipe: self.pipe.kill()
    def on_close(self):
        if self.running and not messagebox.askyesno("Clipper", "A job is running. Cancel it and quit?"): return
        self.cancel_ev.set(); self.pull_cancel.set()
        if getattr(self, "yt_cancel", None): self.yt_cancel.set()
        if self.job: self.job["cancel"] = True
        if self.pipe: self.pipe.kill()
        try: self.save()
        except OSError: pass
        self.destroy()

    # events from the worker thread
    def poll(self):
        # Drain a bounded batch: large log/progress bursts must not monopolize Tk's event loop.
        processed = 0
        while processed < 250:
            try: m = self.q.get_nowait()
            except queue.Empty: break
            try: getattr(self, "on_" + m[0])(*m[1:])
            except Exception as e:
                try: self.on_log(f"UI event error ({m[0]}): {e}")
                except Exception: pass
            processed += 1
        self.after(20 if not self.q.empty() else 100, self.poll)
    def paint_chips(self, cur):
        for k, lab in enumerate(self.chips, 1): lab.configure(fg=ACC if k < cur else (FG if k == cur else MUTED))
    def on_log(self, t):
        tag = "err" if t.startswith(("Error", "Couldn't", "GPU failed")) else ("ok" if t.startswith("Saved") else "")
        self.logbox.configure(state="normal"); self.logbox.insert("end", t + "\n", tag)
        try:
            if int(self.logbox.index("end-1c").split(".")[0]) > 4000:
                self.logbox.delete("1.0", "501.0")
        except (ValueError, tk.TclError): pass
        self.logbox.see("end"); self.logbox.configure(state="disabled")
    def on_step(self, i, name):
        self.t_step = time.time(); self.status.set(f"Step {i} of {len(STEPS)}: {name}"); self.step_pb.stop(); self.step_pb.configure(mode="determinate"); self.step_pb["value"] = 0
        self.all_pb["value"] = sum(WEIGHTS[:i - 1]) * 100; self.eta.set(""); self.paint_chips(i)
    def on_prog(self, i, frac, text):
        if frac is None:
            if str(self.step_pb["mode"]) != "indeterminate": self.step_pb.configure(mode="indeterminate"); self.step_pb.start(12)
            self.eta.set(text); return
        if str(self.step_pb["mode"]) == "indeterminate": self.step_pb.stop(); self.step_pb.configure(mode="determinate")
        frac = max(0.0, min(1.0, frac)); el = time.time() - self.t_step
        left = f"   about {fmt_t(el / frac - el)} left" if 0.03 < frac < 1 else ""
        self.step_pb["value"] = frac * 100; self.eta.set(f"{frac * 100:.0f}%   {text}{left}")
        self.all_pb["value"] = (sum(WEIGHTS[:i - 1]) + WEIGHTS[i - 1] * frac) * 100
    def on_review(self, clips, video, holder):
        self.status.set("Waiting for your review..."); self.eta.set("Choose which clips to render")
        self.review_window = Review(self, clips, video, holder)
    def on_regions(self, holder):
        self.status.set("Waiting for you to mark the face cam and content..."); self.eta.set("The content box is optional: unmarked uses the full video frame")
        self.review_window = RegionPicker(self, holder)
    def on_regions_cancel(self): self.on_review_cancel()
    def on_review_cancel(self):
        window = getattr(self, "review_window", None)
        if window and window.winfo_exists():
            window.destroy()
        self.review_window = None
    def finished(self, msg):
        self.running = False; self.go_btn.state(["!disabled"]); self.stop_btn.state(["disabled"]); self.step_pb.stop(); self.step_pb.configure(mode="determinate"); self.status.set(msg)
    def on_done(self, out, files):
        self.step_pb["value"] = self.all_pb["value"] = 100; self.eta.set(""); self.paint_chips(len(STEPS) + 1)
        self.finished(f"Done. {len(files)} clip(s) saved.")
        self.last_files = files  # keep for the Upload button
        for f in files: self.on_log("Saved " + (f["path"] if isinstance(f, dict) else str(f)))
        if files and self.get["open_done"](): self.open_path(out)
        # Auto-upload to YouTube if enabled
        if files and self.get["yt_enabled"]() and self.get["yt_auto_upload"]():
            self.upload_youtube(files)
    def on_cancelled(self): self.finished("Cancelled."); self.eta.set(""); self.on_log("Cancelled.")
    def on_error(self, msg, tb):
        self.finished("Something went wrong."); self.eta.set(""); self.on_log("Error: " + msg); self.on_log(tb)

