import tkinter as tk 
from clipper.ui.theme import *

class Tip:
    """A single, delayed tooltip whose callbacks are cleaned up with its widget."""
    DELAY_MS = 400

    def __init__(self, w, text):
        self.w, self.t, self.tw = w, text, None
        self.show_job = self.hide_job = None
        if text:
            w.bind("<Enter>", self.schedule_show, add="+")
            w.bind("<Leave>", self.hide, add="+")
            w.bind("<Destroy>", self.on_destroy, add="+")

    def _cancel_job(self, attr):
        job = getattr(self, attr)
        if job is not None:
            try: self.w.after_cancel(job)
            except tk.TclError: pass
            setattr(self, attr, None)

    def schedule_show(self, _=None):
        self._cancel_job("hide_job")
        self._cancel_job("show_job")
        try: self.show_job = self.w.after(self.DELAY_MS, self.show)
        except tk.TclError: self.show_job = None

    def show(self):
        self.show_job = None
        try:
            if not self.w.winfo_exists() or not self.w.winfo_ismapped(): return
            px, py = self.w.winfo_pointerxy()
            x, y = self.w.winfo_rootx(), self.w.winfo_rooty()
            if not (x <= px < x + self.w.winfo_width() and y <= py < y + self.w.winfo_height()): return
            if self.tw is not None:
                try:
                    if self.tw.winfo_exists(): return
                except tk.TclError: pass
            self.tw = tk.Toplevel(self.w)
            self.tw.wm_overrideredirect(True)
            self.tw.wm_geometry(f"+{x + 10}+{y + self.w.winfo_height() + 4}")
            tk.Label(self.tw, text=self.t, bg=FIELD, fg=FG, padx=8, pady=5,
                     justify="left", wraplength=380).pack()
        except tk.TclError:
            self.tw = None

    def hide(self, _=None):
        self._cancel_job("show_job")
        self._cancel_job("hide_job")
        self._destroy_tip()

    def _destroy_tip(self):
        tip, self.tw = self.tw, None
        if tip is not None:
            try:
                if tip.winfo_exists(): tip.destroy()
            except tk.TclError: pass

    def on_destroy(self, event=None):
        if event is not None and event.widget is not self.w: return
        self._cancel_job("show_job")
        self._cancel_job("hide_job")
        self._destroy_tip()



class Btn(tk.Canvas):
    """Flat rounded button. state(["disabled"]) / state(["!disabled"]) work like ttk."""
    KINDS = {"primary": (ACC, ACC_HI, ACC_FG, "#1f4a47", "#6f9a95"), "secondary": (FIELD, "#27384f", FG, PANEL, MUTED)}
    def __init__(self, master, text, command, kind="secondary", bg=BG, small=False, state="normal"):
        self.font = tkfont.Font(family=FAM, size=10, weight="bold" if kind == "primary" else "normal")
        self.bh = 30 if small else 38; self.bw = self.font.measure(text) + (28 if small else 38)
        super().__init__(master, width=self.bw, height=self.bh, bg=bg, highlightthickness=0, bd=0)
        self.text, self.cmd, self.kind, self.hover, self.disabled, self.r = text, command, kind, 0.0, state == "disabled", 8 if small else 10
        self._hover_job = None
        self.bind("<Enter>", lambda _: self._hv(True)); self.bind("<Leave>", lambda _: self._hv(False)); self.bind("<Button-1>", self._click)
        self.draw()
    def _hv(self, on):
        if self.disabled: return
        if self._hover_job:
            try: self.after_cancel(self._hover_job)
            except tk.TclError: pass
            self._hover_job = None
        target = 1.0 if on else 0.0
        def tick():
            delta = target - self.hover
            if abs(delta) < 0.04:
                self.hover = target; self._hover_job = None; self.draw(); return
            self.hover += delta * 0.48
            self.draw()
            self._hover_job = self.after(12, tick)
        tick()
    def _click(self, _=None):
        if not self.disabled: self.cmd()
    def state(self, spec):
        for s in spec:
            if s == "disabled": self.disabled = True
            elif s == "!disabled": self.disabled = False
        self.draw()
    def draw(self):
        if not self.winfo_exists(): return
        self.delete("all"); fill, hi, fg, dfill, dfg = self.KINDS[self.kind]
        c, t = (dfill, dfg) if self.disabled else (mix_color(fill, hi, self.hover), fg)
        rrect(self, 1, 1, self.bw - 1, self.bh - 1, self.r, fill=c, outline=BORDER if self.kind == "secondary" else c)
        self.create_text(self.bw / 2, self.bh / 2, text=self.text, fill=t, font=self.font)
        self.configure(cursor="arrow" if self.disabled else "hand2")


class Switch(tk.Canvas):
    def __init__(self, master, variable, bg=PANEL):
        super().__init__(master, width=46, height=26, bg=bg, highlightthickness=0, bd=0, cursor="hand2", takefocus=1)
        self.var = variable; self.bind("<Button-1>", self.toggle); self.bind("<space>", self.toggle)
        variable.trace_add("write", lambda *_: self.draw()); self.draw()
    def toggle(self, _=None): self.var.set(not self.var.get()); return "break"
    def draw(self):
        if not self.winfo_exists(): return
        self.delete("all"); on = bool(self.var.get()); col = ACC if on else "#3a4a60"
        self.create_oval(2, 2, 24, 24, fill=col, outline=col); self.create_oval(22, 2, 44, 24, fill=col, outline=col); self.create_rectangle(13, 2, 33, 24, fill=col, outline=col)
        x = 33 if on else 13; self.create_oval(x - 9, 4, x + 9, 22, fill="#ffffff" if on else "#c3cfdf", outline="")

class Scrolled(tk.Frame):
    def __init__(self, master):
        super().__init__(master, bg=BG)
        self.cv = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.columnconfigure(0, weight=1); self.rowconfigure(0, weight=1)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.cv.yview); self.cv.configure(yscrollcommand=self.sb.set)
        self.cv.grid(row=0, column=0, sticky="nsew"); self.sb.grid(row=0, column=1, sticky="ns")
        self.body = tk.Frame(self.cv, bg=BG); self.win = self.cv.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self.fit); self.cv.bind("<Configure>", self.fit)
    def fit(self, _=None):
        self.cv.itemconfigure(self.win, width=self.cv.winfo_width()); self.cv.configure(scrollregion=self.cv.bbox("all"))
        if self.body.winfo_reqheight() <= self.cv.winfo_height(): self.sb.grid_remove(); self.cv.yview_moveto(0)
        else: self.sb.grid()

class NavItem(tk.Frame):
    def __init__(self, master, text, command):
        super().__init__(master, bg=SIDE, cursor="hand2")
        self.active = False
        self.hovered = False
        self.hover_t = 0.0
        self._anim_job = None
        self.bar = tk.Frame(self, width=3, bg=SIDE)
        self.bar.pack(side="left", fill="y")
        self.lbl = tk.Label(self, text=text, bg=SIDE, fg=MUTED, anchor="w", padx=19, pady=9, font=(FAM, 10))
        self.lbl.pack(side="left", fill="x", expand=True)
        self.badge = tk.Label(self, text="●", bg=SIDE, fg=DANGER, font=(FAM, 7))
        for w in (self, self.lbl, self.badge):
            w.bind("<Button-1>", lambda _, fn=command: fn())
            w.bind("<Enter>", lambda _: self.paint(True))
            w.bind("<Leave>", lambda _: self.paint(False))
        self.paint(False, animate=False)

    def paint(self, hover=False, animate=True):
        self.hovered = bool(hover)
        target = 1.0 if (self.active or self.hovered) else 0.0
        if self._anim_job:
            try: self.after_cancel(self._anim_job)
            except tk.TclError: pass
            self._anim_job = None
        if not animate:
            self.hover_t = target
            self._paint_colors()
            return
        def tick():
            delta = target - self.hover_t
            if abs(delta) < 0.04:
                self.hover_t = target
                self._anim_job = None
                self._paint_colors()
                return
            self.hover_t += delta * 0.45
            self._paint_colors()
            self._anim_job = self.after(12, tick)
        tick()

    def _paint_colors(self):
        bg = mix_color(SIDE, "#1a2740", self.hover_t)
        fg = mix_color(MUTED, FG, self.hover_t)
        self.configure(bg=bg)
        self.lbl.configure(bg=bg, fg=fg)
        self.bar.configure(bg=ACC if self.active else bg)
        self.badge.configure(bg=bg)

    def set_active(self, on):
        self.active = bool(on)
        self.paint(self.hovered)
    def set_badge(self, on):
        self.badge.pack(side="right", padx=16) if on else self.badge.pack_forget()

