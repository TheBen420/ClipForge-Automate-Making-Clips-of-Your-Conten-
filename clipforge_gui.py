#!/usr/bin/env python3
"""ClipForge desktop app - pick a folder or a Twitch VOD, get postable vertical clips."""

import argparse
import json
import os
import queue
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import webbrowser
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_DIR))

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import clipforge as cf

VIDEO_EXT = (".mp4", ".mkv", ".mov", ".webm", ".flv", ".ts", ".m4v", ".avi")
BG = "#131316"
FG = "#ececed"
MUTED = "#9798a1"
ACCENT = "#3ba55d"
RAIL = "#101013"
RAIL_ON = "#23242b"
CARD = "#191a1e"
PANEL = CARD
BORDER = "#2c2d34"
FIELD = "#24252b"


def apply_dark_styles(root):
    """Dark theme shared by the main window and the judging window."""
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", background=BG, foreground=FG, fieldbackground=FIELD)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
    style.configure("Muted.TLabel", foreground=MUTED, font=("Segoe UI", 9))
    style.configure("Title.TLabel", font=("Segoe UI Semibold", 16))
    style.configure("Card.TFrame", background=CARD)
    style.configure("Card.TLabel", background=CARD, foreground=FG)
    style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED,
                    font=("Segoe UI", 9))
    style.configure("Card.TCheckbutton", background=CARD, foreground=FG,
                    font=("Segoe UI", 10), focuscolor=CARD, indicatorbackground=FIELD,
                    indicatorforeground=ACCENT, fieldbackground=FIELD)
    style.map("Card.TCheckbutton",
              background=[("active", "#23242b"), ("selected", CARD), ("disabled", CARD)],
              foreground=[("active", "#ffffff"), ("disabled", "#6f6f78")],
              indicatorbackground=[("selected", ACCENT), ("active", "#2f3038"),
                                   ("disabled", "#232327")])
    style.configure("Card.TNotebook", background=CARD, borderwidth=0,
                    tabmargins=(0, 0, 0, 0))
    style.configure("Card.TNotebook.Tab", background=CARD, foreground=MUTED, padding=(12, 5))
    style.map("Card.TNotebook.Tab", background=[("selected", "#23242b")],
              foreground=[("selected", FG)])
    style.configure("TButton", font=("Segoe UI", 10), padding=6)
    style.configure("Go.TButton", font=("Segoe UI Semibold", 11), padding=(18, 9),
                    background=ACCENT, foreground="#ffffff", borderwidth=0)
    style.map("Go.TButton",
              background=[("active", "#329355"), ("disabled", "#2c3a30")],
              foreground=[("disabled", "#7d8a80")])
    style.map("TButton", background=[("active", "#33333a")])
    style.configure("TCombobox", padding=4, fieldbackground=FIELD, background=FIELD,
                    foreground=FG, arrowcolor=FG, bordercolor="#3a3a42",
                    lightcolor=FIELD, darkcolor=FIELD,
                    selectbackground="#34343c", selectforeground=FG)
    style.map("TCombobox",
              fieldbackground=[("readonly", FIELD), ("disabled", "#232327")],
              foreground=[("readonly", FG), ("disabled", "#6f6f78")],
              selectbackground=[("readonly", "#34343c")],
              selectforeground=[("readonly", FG)],
              background=[("readonly", FIELD), ("active", "#33333a")],
              arrowcolor=[("readonly", FG), ("active", "#ffffff")])
    style.configure("TCheckbutton", background=BG, foreground=FG, font=("Segoe UI", 10),
                    focuscolor=BG, indicatorbackground=FIELD,
                    indicatorforeground=ACCENT, fieldbackground=FIELD)
    style.map("TCheckbutton",
              background=[("active", "#26262c"), ("selected", BG), ("disabled", BG)],
              foreground=[("active", "#ffffff"), ("disabled", "#6f6f78")],
              indicatorbackground=[("selected", ACCENT), ("active", "#2f2f37"),
                                   ("disabled", "#232327")],
              indicatorforeground=[("selected", "#ffffff"), ("active", ACCENT)])
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure("TNotebook.Tab", background="#232327", foreground=MUTED,
                    padding=(16, 7), font=("Segoe UI", 10), borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", "#2f2f37"), ("active", "#2a2a30")],
              foreground=[("selected", FG), ("active", FG)])
    style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD)

    root.option_add("*TCombobox*Listbox.background", "#1b1b1f")
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", "#34343c")
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    root.option_add("*TCombobox*Listbox.borderWidth", "0")
    root.option_add("*TCombobox*Listbox.font", "{Segoe UI} 10")


class ScrollFrame(tk.Frame):
    """A tall panel that scrolls, so a small screen cannot hide a control."""

    def __init__(self, parent, bg=BG):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = tk.Frame(self.canvas, bg=bg)
        self.window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.body.bind("<Configure>", self._on_body)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", lambda _e: self._wheel(True))
        self.canvas.bind("<Leave>", lambda _e: self._wheel(False))

    def _on_body(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self.window, width=event.width)

    def _wheel(self, on):
        if on:
            self.canvas.bind_all("<MouseWheel>", self._scroll)
        else:
            self.canvas.unbind_all("<MouseWheel>")

    def _scroll(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


class StreamToQueue:
    """Sends pipeline print() output to the GUI log instead of a console."""

    def __init__(self, q):
        self.q = q

    def write(self, text):
        if text:
            self.q.put(("log", text))

    def flush(self):
        pass


def apply_icon(window):
    """Give the window ClipForge's own icon so the taskbar and title bar agree.

    Windows groups taskbar buttons by process identity, and a window started by pythonw
    inherits Python's, which shows Python's icon however the window's own icon is set. Every
    window that takes the icon therefore claims the identity too, so a judging window opened
    in its own process looks like ClipForge rather than like a Python script.
    """
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ClipForge.App")
        except Exception:  # noqa: BLE001 - never fatal, a plain taskbar icon at worst
            pass
    icon = TOOL_DIR / "icon.ico"
    if os.name == "nt" and icon.exists():
        try:
            window.iconbitmap(str(icon))
            window.iconbitmap(default=str(icon))
        except tk.TclError:
            pass
    photo = TOOL_DIR / "icon.png"
    if photo.exists():
        try:
            window.iconphoto(True, tk.PhotoImage(file=str(photo)))
        except tk.TclError:
            pass


class App:
    def __init__(self, root):
        self.root = root
        self.queue = queue.Queue()
        self.stop = threading.Event()
        self.running = False
        self.clips_made = []
        self.vod_info = None
        self.cfg = cf.load_config()
        self.config_notices = list(cf.CONFIG_NOTICES)
        self.ready_root = cf.media_root(self.cfg) / "ready"
        self.clips_root = cf.media_root(self.cfg) / "clips"
        self.run_dir = None
        root.title("ClipForge")
        apply_icon(root)
        width = min(1080, root.winfo_screenwidth() - 140)
        height = min(950, root.winfo_screenheight() - 150)
        root.geometry(f"{width}x{height}")
        root.minsize(900, 620)
        root.configure(bg=BG)
        self.build_styles()

        shell = ttk.Frame(root)
        shell.pack(fill="both", expand=True)
        self.build_rail(shell)

        self.content = ttk.Frame(shell)
        self.content.pack(side="left", fill="both", expand=True)

        head = ttk.Frame(self.content)
        head.pack(fill="x", padx=22, pady=(16, 2))
        titles = ttk.Frame(head)
        titles.pack(side="left")
        self.page_title = ttk.Label(titles, text="Make clips", style="Title.TLabel")
        self.page_title.pack(anchor="w")
        self.page_note = ttk.Label(titles, text="Clips land in one folder, named after the source video.",
                                   style="Muted.TLabel")
        self.page_note.pack(anchor="w")
        self.hardware_pill = tk.Label(head, text="checking hardware", bg=RAIL_ON, fg=MUTED,
                                      font=("Segoe UI", 8), padx=10, pady=4)
        self.hardware_pill.pack(side="right")
        self.header_open = ttk.Button(head, text="Open clips folder", command=self.open_output)
        self.header_open.pack(side="right", padx=(0, 8))

        self.pages = {key: ttk.Frame(self.content, padding=(22, 10, 22, 16))
                      for key in ("make", "judge", "stats")}

        scroll = ScrollFrame(self.pages["make"])
        scroll.pack(fill="both", expand=True)
        make = scroll.body
        source = self.section(make, "Source")
        self.nb = ttk.Notebook(source, style="Card.TNotebook")
        self.nb.pack(fill="x")
        self.build_folder_tab()
        self.build_vod_tab()
        self.nb.bind("<<NotebookTabChanged>>", lambda _e: self.refresh_hint())

        self.build_options(make)
        self.build_destination(make)
        run_card = self.section(make, "Run", expand=True)
        self.build_actions(run_card)
        self.build_log(run_card)

        self.build_judge_page(self.pages["judge"])
        self.build_stats_page(self.pages["stats"])
        self.show_page("make")
        self.root.after(1500, self.check_update)

        for notice in self.config_notices:
            self.queue.put(("log", f"  ({notice})\n"))
        self.check_environment()
        self.root.after(120, self.drain)

    # ------------------------------------------------------------ shell

    def build_rail(self, parent):
        rail = tk.Frame(parent, bg=RAIL, width=196)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)

        brand = tk.Frame(rail, bg=RAIL)
        brand.pack(fill="x", padx=12, pady=(16, 18))
        logo = TOOL_DIR / "icon.png"
        if logo.exists():
            try:
                image = tk.PhotoImage(file=str(logo))
                step = max(1, image.width() // 24)
                self.brand_logo = image.subsample(step, step)
                tk.Label(brand, image=self.brand_logo, bg=RAIL).pack(side="left", padx=(0, 9))
            except tk.TclError:
                pass
        titles = tk.Frame(brand, bg=RAIL)
        titles.pack(side="left")
        tk.Label(titles, text="ClipForge", bg=RAIL, fg=FG,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w")
        self.version_label = tk.Label(titles, text=f"v{cf.VERSION}", bg=RAIL, fg=MUTED,
                                      font=("Segoe UI", 8))
        self.version_label.pack(anchor="w")

        self.nav_buttons = {}
        for key, label in (("make", "Make clips"), ("judge", "Judge clips"),
                           ("stats", "Selection stats")):
            button = tk.Button(rail, text=label, anchor="w", relief="flat", bd=0,
                               bg=RAIL, fg=MUTED, activebackground=RAIL_ON,
                               activeforeground=FG, highlightthickness=0,
                               font=("Segoe UI", 10), padx=12, pady=7, cursor="hand2",
                               command=lambda k=key: self.show_page(k))
            button.pack(fill="x", padx=8, pady=1)
            self.nav_buttons[key] = button
            if key == "judge":
                self.judge_nav = tk.Label(rail, text="", bg=RAIL, fg="#5d5e67",
                                          font=("Segoe UI", 8), anchor="e")
                self.judge_nav.pack(fill="x", padx=20, pady=(0, 2))

        foot = tk.Frame(rail, bg=RAIL)
        foot.pack(side="bottom", fill="x", padx=12, pady=14)
        self.update_btn = ttk.Button(foot, text="Update available", command=self.open_update)
        tk.Label(foot, text="Judging a clip teaches the weighting", bg=RAIL, fg="#5d5e67",
                 font=("Segoe UI", 8), wraplength=160, justify="left").pack(anchor="w",
                                                                           pady=(8, 0))

    def show_page(self, key):
        titles = {"make": ("Make clips", "Clips land in one folder, named after the source video."),
                  "judge": ("Judge clips", "Kept against rejected is what teaches the weighting."),
                  "stats": ("Selection stats", "What your judgements say about each signal.")}
        for page_key, page in self.pages.items():
            page.pack_forget()
            current = page_key == key
            self.nav_buttons[page_key].configure(bg=RAIL_ON if current else RAIL,
                                                 fg=FG if current else MUTED)
        self.pages[key].pack(fill="both", expand=True)
        self.page_key = key
        self.page_title.configure(text=titles[key][0])
        self.page_note.configure(text=titles[key][1])
        if key == "judge":
            self.ensure_judge_panel()
        elif key == "stats":
            self.refresh_stats()

    def build_judge_page(self, parent):
        head = tk.Frame(parent, bg=BG)
        head.pack(fill="x", pady=(0, 10))
        self.judge_count = tk.Label(head, text="reading the library…", bg=BG, fg=MUTED,
                                    font=("Segoe UI", 9))
        self.judge_count.pack(side="left")
        ttk.Button(head, text="Judge last run", command=self.judge_last_run).pack(side="right")
        ttk.Button(head, text="Open in a window", command=self.judge_in_window).pack(
            side="right", padx=(0, 8))
        ttk.Button(head, text="Reload", command=lambda: self.load_judge_records(force=True)
                   ).pack(side="right", padx=(0, 8))
        self.judge_holder = tk.Frame(parent, bg=BG)
        self.judge_holder.pack(fill="both", expand=True)
        self.judge_panel = None

    def load_judge_records(self, force=False):
        if self.judge_panel is not None and not force:
            return
        self.judge_count.configure(text="reading the library…")

        def run():
            try:
                self.queue.put(("judge_records", cf.disk_clips(self.cfg)))
            except cf.Fail as exc:
                self.queue.put(("judge_count", f"could not read the library: {exc}"))

        threading.Thread(target=run, daemon=True).start()

    def ensure_judge_panel(self):
        if self.judge_panel is None:
            self.judge_panel = JudgePanel(self.judge_holder, self.cfg, [])
            self.judge_panel.bind_hotkeys(self.root)
            self.load_judge_records(force=True)

    def judge_last_run(self):
        try:
            rows, target = cf.review_records(self.cfg)
        except cf.Fail as exc:
            messagebox.showinfo("ClipForge", str(exc))
            return
        open_review_window(self.cfg, rows, target, root=self.root,
                           name=Path(str(target)).stem[:22])

    def judge_in_window(self):
        rows = self.judge_panel.records if self.judge_panel else []
        if not rows:
            messagebox.showinfo("ClipForge", "Nothing to judge yet - make some clips first.")
            return
        open_review_window(self.cfg, list(rows), "every clip on disk", root=self.root,
                           name="all clips")
    def build_stats_page(self, parent):
        head = tk.Frame(parent, bg=BG)
        head.pack(fill="x", pady=(0, 10))
        self.stats_note = tk.Label(head, text="Read from every judgement in feedback.jsonl",
                                   bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.stats_note.pack(side="left")
        ttk.Button(head, text="Refresh", command=self.refresh_stats).pack(side="right")

        signals_card = self.section(parent, "Signals, and what they earned")
        self.signal_table = ttk.Treeview(signals_card, columns=("signal", "weight", "sep", "bar"),
                                         show="headings", height=6)
        for key, text, width, anchor in (("signal", "Signal", 120, "w"),
                                         ("weight", "Weight", 90, "e"),
                                         ("sep", "Separation", 100, "e"),
                                         ("bar", "", 260, "w")):
            self.signal_table.heading(key, text=text)
            self.signal_table.column(key, width=width, anchor=anchor)
        self.signal_table.pack(fill="x")

        reasons_card = self.section(parent, "Why clips were dropped")
        self.reason_table = ttk.Treeview(reasons_card, columns=("reason", "share", "bar"),
                                         show="headings", height=5)
        for key, text, width, anchor in (("reason", "Reason", 120, "w"),
                                         ("share", "Share", 90, "e"),
                                         ("bar", "", 260, "w")):
            self.reason_table.heading(key, text=text)
            self.reason_table.column(key, width=width, anchor=anchor)
        self.reason_table.pack(fill="x")

    def refresh_stats(self):
        def run():
            try:
                feedback = cf.load_feedback(self.cfg)
                split = cf.signal_split(feedback)
                learned = cf.learned_multipliers(self.cfg)
                effective = {name: float(weight) * learned.get(name, 1.0)
                             for name, weight in (self.cfg.get("weights") or {}).items()}
                reasons = {}
                for row in feedback:
                    key = row.get("reason")
                    if key:
                        key = cf.REASON_ALIASES.get(key, key)
                        reasons[key] = reasons.get(key, 0) + 1
                self.queue.put(("stats_tables", (split, effective, reasons, len(feedback))))
            except Exception as exc:  # noqa: BLE001 - a report must never crash the app
                self.queue.put(("stats_tables_error", str(exc)))

        threading.Thread(target=run, daemon=True).start()

    def fill_stats(self, split, effective, reasons, judged):
        def bar(value, width=24):
            filled = int(round(min(abs(value), 1.0) * width))
            return "█" * filled + "·" * (width - filled)

        self.signal_table.delete(*self.signal_table.get_children())
        for name in sorted(effective, key=lambda key: -abs(split.get(key, {}).get("separation", 0))):
            row = split.get(name) or {}
            separation = float(row.get("separation", 0) or 0)
            self.signal_table.insert("", "end",
                                     values=(name, f"{effective[name]:.2f}",
                                             f"{separation:+.2f}" if row else "no data",
                                             bar(separation)))

        self.reason_table.delete(*self.reason_table.get_children())
        total = sum(reasons.values()) or 1
        for name, count in sorted(reasons.items(), key=lambda item: -item[1]):
            self.reason_table.insert("", "end",
                                     values=(name, f"{count / total * 100:.0f}%",
                                             bar(count / total)))
        if not reasons:
            self.reason_table.insert("", "end", values=("nothing judged yet", "", ""))
        self.stats_note.configure(text=f"{judged} judgement(s) behind these numbers")

    def build_styles(self):
        apply_dark_styles(self.root)

    def text_entry(self, parent, variable):
        entry = tk.Entry(parent, textvariable=variable, font=("Segoe UI", 10),
                         bg=FIELD, fg=FG, insertbackground=FG, relief="flat",
                         highlightthickness=1, highlightbackground="#3a3a42",
                         highlightcolor=ACCENT)
        entry.pack(side="left", fill="x", expand=True, ipady=7, padx=(0, 8))
        return entry

    # ------------------------------------------------------------ tabs

    def build_folder_tab(self):
        tab = ttk.Frame(self.nb, padding=12, style="Card.TFrame")
        self.nb.add(tab, text="Local folder")

        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Label(row, text="Recordings folder", style="CardMuted.TLabel").pack(anchor="w")
        line = ttk.Frame(row)
        line.pack(fill="x", pady=(3, 0))
        self.folder_var = tk.StringVar()
        self.text_entry(line, self.folder_var)
        ttk.Button(line, text="Choose folder…", command=self.browse).pack(side="left")

        self.recursive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(tab, text="Include subfolders", variable=self.recursive_var,
                        style="Card.TCheckbutton").pack(anchor="w", pady=(10, 0))
        self.folder_hint = ttk.Label(tab, text="Every video in the folder gets its own clips.",
                                     style="CardMuted.TLabel")
        self.folder_hint.pack(anchor="w", pady=(6, 0))

    def build_vod_tab(self):
        tab = ttk.Frame(self.nb, padding=12, style="Card.TFrame")
        self.nb.add(tab, text="Twitch VOD")

        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Label(row, text="VOD link", style="CardMuted.TLabel").pack(anchor="w")
        line = ttk.Frame(row)
        line.pack(fill="x", pady=(3, 0))
        self.vod_url_var = tk.StringVar()
        self.vod_entry = self.text_entry(line, self.vod_url_var)
        self.load_btn = ttk.Button(line, text="Load", command=self.load_vod)
        self.load_btn.pack(side="left")

        self.vod_info_var = tk.StringVar(value="Paste a twitch.tv/videos/… link and press Load.")
        tk.Label(tab, textvariable=self.vod_info_var, bg=CARD, fg=MUTED, font=("Segoe UI", 9),
                 anchor="w", justify="left", wraplength=760).pack(fill="x", pady=(8, 0))
        self.vod_hint = ttk.Label(
            tab, text="It pulls the audio once to find the loud moments, then downloads only "
                      "those windows.", style="CardMuted.TLabel")
        self.vod_hint.pack(anchor="w", pady=(6, 0))
        self.chat_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(tab, text="Use Twitch chat hype (leans on the messages at the peak)",
                        variable=self.chat_var, style="Card.TCheckbutton").pack(
            anchor="w", pady=(8, 0))

    def section(self, parent, title, expand=False):
        """A card with a small heading, the way the mockup groups things."""
        card = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="both" if expand else "x", expand=expand, pady=(0, 10))
        if title:
            tk.Label(card, text=title, bg=CARD, fg="#6b6c76", font=("Segoe UI", 8),
                     anchor="w").pack(fill="x", padx=14, pady=(10, 0))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="both" if expand else "x", expand=expand, padx=14, pady=(6, 10))
        return inner

    def build_options(self, parent):
        self.fit_var = tk.StringVar(value=self.cfg["fit"])
        self.cap_var = tk.StringVar(value=self.cfg["captions"])
        self.count_var = tk.StringVar(value="2")
        self.length_var = tk.StringVar(value="25")
        self.preset_var = tk.StringVar(value="auto")
        self.preset_var.trace_add("write", self.apply_preset)

        body = self.section(parent, "How to cut them")
        grid = tk.Frame(body, bg=CARD)
        grid.pack(fill="x")

        def labelled(text, column, widget):
            box = ttk.Frame(grid, style="Card.TFrame")
            box.grid(row=0, column=column, sticky="ew", padx=(0, 14))
            grid.columnconfigure(column, weight=1)
            ttk.Label(box, text=text, style="CardMuted.TLabel").pack(anchor="w")
            widget(box)

        labelled("Framing", 0, lambda p: ttk.Combobox(p, textvariable=self.fit_var, width=9,
                                                      state="readonly",
                                                      values=["blur", "crop", "black"]).pack(
            fill="x"))
        labelled("Captions", 1, lambda p: ttk.Combobox(p, textvariable=self.cap_var, width=10,
                                                       state="readonly",
                                                       values=["karaoke", "block", "off"]).pack(
            fill="x"))
        labelled("Clips to make", 2, lambda p: ttk.Combobox(
            p, textvariable=self.count_var, width=5, state="readonly",
            values=[str(n) for n in range(1, 21)]).pack(fill="x"))
        labelled("Clip length", 3, lambda p: ttk.Combobox(
            p, textvariable=self.length_var, width=5, state="readonly",
            values=["15", "20", "25", "30", "45", "60"]).pack(fill="x"))
        labelled("Content type", 4, lambda p: ttk.Combobox(
            p, textvariable=self.preset_var, width=13, state="readonly",
            values=["auto"] + list((self.cfg.get("presets") or {}).keys())).pack(fill="x"))

        second = tk.Frame(body, bg=CARD)
        second.pack(fill="x", pady=(12, 0))
        self.model_var = tk.StringVar(value=self.cfg.get("model", "medium.en"))
        self.model_var.trace_add("write", self.save_model)
        self.fps_var = tk.StringVar(value="auto")

        def labelled_second(text, column, widget):
            box = ttk.Frame(second, style="Card.TFrame")
            box.grid(row=0, column=column, sticky="ew", padx=(0, 14))
            second.columnconfigure(column, weight=1)
            ttk.Label(box, text=text, style="CardMuted.TLabel").pack(anchor="w")
            widget(box)

        labelled_second("Transcription", 0, lambda p: ttk.Combobox(
            p, textvariable=self.model_var, width=11, state="readonly",
            values=["small.en", "medium.en", "large-v3"]).pack(fill="x"))
        labelled_second("Frame rate", 1, lambda p: ttk.Combobox(
            p, textvariable=self.fps_var, width=7, state="readonly",
            values=["auto", "60", "30"]).pack(fill="x"))
        self.apply_preset()

        vocab = tk.Frame(body, bg=CARD)
        vocab.pack(fill="x", pady=(12, 0))
        ttk.Label(vocab, text="Vocabulary", style="CardMuted.TLabel").pack(anchor="w")
        line = tk.Frame(vocab, bg=CARD)
        line.pack(fill="x", pady=(3, 0))
        self.prompt_var = tk.StringVar(value=self.cfg.get("prompt", ""))
        entry = tk.Entry(line, textvariable=self.prompt_var, font=("Segoe UI", 10), bg=FIELD,
                         fg=FG, insertbackground=FG, relief="flat", highlightthickness=1,
                         highlightbackground="#3a3a42", highlightcolor=ACCENT)
        entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.prompt_var.trace_add("write", self.save_vocabulary)
        tk.Label(vocab, text="Names it keeps mis-hearing, comma separated", bg=CARD, fg="#6b6c76",
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(3, 0))

    def build_destination(self, parent):
        body = self.section(parent, "Save clips to")
        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x")
        self.output_var = tk.StringVar(value=self.default_output())
        self.text_entry(row, self.output_var)
        ttk.Button(row, text="Choose…", command=self.browse_output).pack(side="left")

        self.subfolder_var = tk.BooleanVar(value=bool(self.cfg.get("clip_output_subfolder", True)))
        ttk.Checkbutton(body, text="Put clips in a subfolder named after the source",
                        variable=self.subfolder_var,
                        style="Card.TCheckbutton").pack(anchor="w", pady=(8, 0))

    def build_actions(self, parent):
        actions = tk.Frame(parent, bg=CARD)
        actions.pack(fill="x", pady=(0, 10))
        self.go_btn = ttk.Button(actions, text="Make Clips", style="Go.TButton", command=self.start)
        self.go_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Stop", command=self.request_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.open_btn = self.header_open
        chips = tk.Frame(actions, bg=CARD)
        chips.pack(side="right")
        workers = cf.safe_worker_count(self.cfg.get("render_workers", 1))[0]
        for text in (f"{workers} render at a time",):
            tk.Label(chips, text=text, bg=RAIL_ON, fg=MUTED, font=("Segoe UI", 8),
                     padx=8, pady=3).pack(side="left", padx=(6, 0))

        self.progress = ttk.Progressbar(parent, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(2, 6))
        self.status_var = tk.StringVar(value="Ready.")
        tk.Label(parent, textvariable=self.status_var, bg=CARD, fg=MUTED,
                 font=("Segoe UI", 9), anchor="w").pack(fill="x")

    def save_vocabulary(self, *_args):
        text = self.prompt_var.get().strip()
        if text != (self.cfg.get("prompt") or ""):
            self.cfg["prompt"] = text
            try:
                cf.save_config(self.cfg)
            except OSError:
                pass

    def save_model(self, *_args):
        chosen = self.model_var.get()
        if chosen and chosen != self.cfg.get("model"):
            self.cfg["model"] = chosen
            try:
                cf.save_config(self.cfg)
            except OSError:
                pass

    def apply_preset(self, *_args):
        """A content type sets the weights and the look that suits it."""
        name = self.preset_var.get()
        preset = (self.cfg.get("presets") or {}).get(name)
        if not preset:
            return
        if preset.get("fit") in ("blur", "crop", "black"):
            self.fit_var.set(preset["fit"])
        if preset.get("captions") in ("karaoke", "block", "off"):
            self.cap_var.set(preset["captions"])
        if preset.get("length"):
            self.length_var.set(str(int(preset["length"])))

    def open_judge(self, records=None):
        """A finished run lands in the judging tab, already selected."""
        if records:
            self.ensure_judge_panel()
            self.judge_panel.set_records(records)
        self.show_page("judge")

    def check_update(self):
        """Ask GitHub which build is published, every launch, and say so either way."""
        def run():
            try:
                latest, checked = cf.update_status(self.cfg, max_age=0)
            except Exception:  # noqa: BLE001 - never let a network hiccup touch the app
                latest, checked = None, False
            if checked and latest:
                newer = cf.parse_version(latest) > cf.parse_version(cf.VERSION)
                self.queue.put(("version", (latest, newer)))

        threading.Thread(target=run, daemon=True).start()

    def open_update(self):
        try:
            webbrowser.open(cf.REPO_URL)
        except Exception:  # noqa: BLE001
            messagebox.showinfo("ClipForge", f"A newer version is at {cf.REPO_URL}")

    def show_stats(self):
        def run():
            original = sys.stdout
            sys.stdout = StreamToQueue(self.queue)
            try:
                cf.cmd_stats(self.cfg, argparse.Namespace(ranks=5))
            except Exception as exc:  # noqa: BLE001 - the report must never crash the app
                self.queue.put(("log", f"stats failed: {exc}\n"))
            finally:
                sys.stdout = original
        threading.Thread(target=run, daemon=True).start()

    def build_log(self, parent):
        self.log = ScrolledText(parent, height=8, bg="#141417", fg="#c9c9d1", relief="flat",
                                font=("Consolas", 9), insertbackground=FG, wrap="word",
                                highlightthickness=1, highlightbackground="#2a2a30")
        self.log.pack(fill="both", expand=True, pady=(8, 0))
        self.log.configure(state="disabled")

    # ------------------------------------------------------------ helpers

    def check_environment(self):
        try:
            self.write_line(f"ffmpeg: {cf.ffmpeg_path(self.cfg)}")
        except cf.Fail as exc:
            self.write_line(f"PROBLEM: {exc}")
            self.go_btn.state(["disabled"])
            self.status_var.set("ffmpeg missing - run `cf doctor`")
            return
        self.status_var.set("Ready. Choose a folder, or load a VOD.")
        threading.Thread(target=self.report_hardware, daemon=True).start()

    def report_hardware(self):
        """Which encoder and device this machine will actually use, for support."""
        try:
            encoder = cf.pick_encoder(self.cfg)
        except cf.Fail as exc:
            encoder = f"unavailable ({exc})"
        try:
            _, device = cf.whisper_model(self.cfg, self.cfg["model"])
        except cf.Fail as exc:
            device = f"unavailable ({exc})"
        self.queue.put(("log", f"encoder: {encoder}   whisper: {device}"))
        self.queue.put(("hardware", f"{device}  ·  {encoder}"))

    def current_tab(self):
        return 1 if self.nb.index(self.nb.select()) == 1 else 0

    def refresh_hint(self):
        self.vod_info_var.set(self.vod_info_var.get())

    def write_line(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip("\n") + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def browse(self):
        folder = filedialog.askdirectory(title="Folder with your recordings")
        if folder:
            self.folder_var.set(str(Path(folder)))

    def default_output(self):
        saved = (self.cfg.get("clip_output") or "").strip()
        return saved or str(self.clips_root)

    def browse_output(self):
        start = self.output_var.get().strip() or str(self.clips_root)
        folder = filedialog.askdirectory(title="Where should clips go?", initialdir=start)
        if folder:
            self.output_var.set(str(Path(folder)))

    def open_output(self):
        target = self.run_dir if self.run_dir else self.ready_root
        cf.open_folder(target)

    def request_stop(self):
        self.stop.set()
        self.status_var.set("Stopping after the current step…")

    # ------------------------------------------------------------ vod metadata

    def load_vod(self):
        url = self.vod_url_var.get().strip()
        if not url:
            self.vod_info_var.set("Paste a VOD link first.")
            return
        self.load_btn.state(["disabled"])
        self.vod_info_var.set("Reading the VOD…")
        threading.Thread(target=self.fetch_vod, args=(url,), daemon=True).start()

    def fetch_vod(self, url):
        try:
            info = cf.fetch_remote_meta(self.cfg, url)
            self.vod_info = {"url": url, "title": info["title"], "duration": info["duration"]}
            self.queue.put(("vod_info",
                            f"{info['title']}  •  {cf.fmt_ts(info['duration'])}"))
        except cf.Fail as exc:
            self.vod_info = None
            detail = [ln for ln in str(exc).splitlines() if ln.strip()]
            reason = detail[-1] if detail else "unknown error"
            self.queue.put(("log", f"yt-dlp could not read that VOD:\n{exc}\n"))
            self.queue.put(("vod_error",
                            f"Could not read that VOD — {reason.strip()[:200]}\n"
                            "If it plays only for subscribers, set "
                            "ytdlp_cookies_from_browser in config.json (e.g. \"chrome\")."))
        finally:
            self.queue.put(("vod_loaded", None))

    # ------------------------------------------------------------ run

    def start(self):
        preset = self.preset_var.get()
        opts = {
            "fit": self.fit_var.get(),
            "captions": self.cap_var.get(),
            "count": int(self.count_var.get()),
            "length": float(self.length_var.get()),
            "preset": None if preset == "auto" else preset,
            "fps": None if self.fps_var.get() == "auto" else int(self.fps_var.get()),
            "chat": bool(getattr(self, "chat_var", None) and self.chat_var.get()),
        }
        mode = "vod" if self.current_tab() == 1 else "folder"
        if mode == "folder":
            folder = Path(self.folder_var.get().strip())
            if not folder.is_dir():
                messagebox.showwarning("ClipForge", "Choose a folder first.")
                return
            opts["recursive"] = self.recursive_var.get()
            source, label = folder, folder.name
        else:
            url = self.vod_url_var.get().strip()
            if not url:
                messagebox.showwarning("ClipForge", "Paste a VOD link first.")
                return
            if not self.vod_info:
                self.load_vod()
                messagebox.showinfo("ClipForge", "Reading the VOD first - press Make Clips "
                                                "again once the title appears.")
                return
            source, label = url, cf.safe_name(self.vod_info["title"])

        destination = Path(self.output_var.get().strip() or str(self.clips_root))
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showwarning("ClipForge", f"Can't write to {destination}\n\n{exc}")
            return
        subfolder = self.subfolder_var.get()
        self.run_dir = destination / label if subfolder else destination
        self.cfg["clip_output"] = str(destination)
        self.cfg["clip_output_subfolder"] = subfolder
        try:
            cf.save_config(self.cfg)
        except OSError:
            pass

        if self.running:
            return
        self.stop.clear()
        self.clips_made = []
        self.running = True
        self.go_btn.state(["disabled"])
        self.stop_btn.state(["!disabled"])
        self.open_btn.state(["disabled"])
        self.progress["value"] = 0
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        threading.Thread(target=self.work, args=(mode, source, opts), daemon=True).start()

    def work(self, mode, source, opts):
        original_out, original_hook = sys.stdout, cf.PROGRESS_HOOK
        sys.stdout = StreamToQueue(self.queue)
        cf.PROGRESS_HOOK = self.forward_child_output
        try:
            if mode == "folder":
                self.run_batch(source, opts)
            else:
                self.run_vod(source, opts)
        except cf.Fail as exc:
            self.queue.put(("error", str(exc)))
        except Exception:  # noqa: BLE001 - surface anything unexpected in the window
            import traceback
            self.queue.put(("error", traceback.format_exc(limit=3)))
        finally:
            sys.stdout = original_out
            cf.PROGRESS_HOOK = original_hook
            self.queue.put(("finished", None))

    def forward_child_output(self, line):
        """Show download progress in the log, but not encoder chatter."""
        if re.match(r"^\[(libx264|libx265|aac|h264|ffmpeg|mov,mp4)\b", line):
            return
        if line.startswith("frame=") or line.startswith("size="):
            return
        self.queue.put(("log", line + "\n"))

    def run_batch(self, folder, opts):
        pattern = "**/*" if opts["recursive"] else "*"
        candidates = sorted(p for p in folder.glob(pattern)
                            if p.is_file() and p.suffix.lower() in VIDEO_EXT)
        files, busy = [], []
        for path in candidates:
            if time.time() - path.stat().st_mtime < 60:
                busy.append(path)
            else:
                files.append(path)
        for path in busy:
            self.queue.put(("log", f"Skipping {path.name} - still being written "
                                   "(wait a minute and run again)"))
        if not files:
            self.queue.put(("error", f"No finished video files found in {folder}"))
            return

        workers = max(1, min(cf.MAX_WORKERS,
                             int(opts.get("workers") or self.cfg.get("workers", 1))))
        self.queue.put(("log", f"Found {len(files)} recording(s) in {folder} "
                               f"({workers} at a time)"))
        self.queue.put(("log", f"Clips from this run go to:\n  {self.run_dir}\n"))

        records = []
        counter = {"done": 0}
        lock = threading.Lock()

        def work(index, path):
            if self.stop.is_set():
                return
            self.queue.put(("status", f"({index}/{len(files)}) {path.name}"))
            try:
                records.extend(self.process_file(path, opts))
            except cf.Fail as exc:
                self.queue.put(("log", f"  {path.name} skipped: {exc}\n"))
            finally:
                with lock:
                    counter["done"] += 1
                    self.queue.put(("progress", counter["done"] / len(files) * 100))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for index, path in enumerate(files, 1):
                pool.submit(work, index, path)
            while counter["done"] < len(files):
                if self.stop.is_set():
                    break
                time.sleep(0.2)

        if records:
            self.queue.put(("review", records))
        self.queue.put(("log", f"\nDone. {len(self.clips_made)} clip(s) in {self.run_dir}"))

    def process_file(self, path, opts):
        """Transcribe one recording and render its clips, recording what the signals did."""
        started = time.time()
        entry, meta, data = self.prepare(path)
        ranked, summary = cf.analyze(self.cfg, entry, meta, data,
                                     preset=opts.get("preset"), want=opts["count"] * 2,
                                     min_words=cf.min_caption_words(opts.get("captions")),
                                     avoid=cf.past_windows(self.cfg, entry.name))
        note = cf.used_up_note(summary)
        if note:
            self.queue.put(("log", f"  {note}\n"))
        ns = argparse.Namespace(fit=opts["fit"], captions=opts["captions"], caps=False,
                                focus=0.5, fps=opts.get("fps"), pad=0.2, snap=True, open=False)
        picks = ranked[: opts["count"]]
        made, failed = cf.render_many(self.cfg, entry, meta, data, picks, ns,
                                      flat_dir=self.run_dir)
        for item, exc in failed:
            self.queue.put(("log", f"  render failed at {cf.fmt_ts(item['start'])}: {exc}\n"))

        records = []
        for item, clip in zip(picks, made):
            self.clips_made.append(self.run_dir)
            records.append({"path": str(clip), "entry": entry.name, "start": item["start"],
                            "end": item["end"], "rank": item["rank"], "score": item["score"],
                            "features": item["features"], "weights": summary.get("weights"),
                            "preset": opts.get("preset"), "version": cf.VERSION})
        session = cf.write_session(self.cfg, "folder", str(path), entry.name, records,
                                   summary, time.time() - started,
                                   {"preset": opts.get("preset"), "count": opts["count"],
                                    "length": opts["length"]})
        self.queue.put(("log", f"  {path.name}: {len(records)} clip(s), "
                               f"session {session.name}"))
        return records

    def run_vod(self, url, opts):
        self.queue.put(("log", f"VOD: {url}"))
        self.queue.put(("log", f"Clips from this run go to:\n  {self.run_dir}\n"))
        self.queue.put(("progress", 3))
        self.queue.put(("status", "Fetching audio from the VOD…"))
        started = time.time()
        entry, meta, data = self.prepare_vod(url)

        if opts.get("chat") and not (entry / "chat.json").exists():
            self.queue.put(("status", "Downloading VOD chat…"))
            try:
                messages = cf.fetch_twitch_chat(url, entry / "chat.json",
                                                on_line=self.forward_child_output)
                self.queue.put(("log", f"  {len(messages)} chat messages\n"))
            except Exception as exc:  # noqa: BLE001 - chat is a bonus, never a blocker
                self.queue.put(("log", f"  chat unavailable ({exc}) - ranking without it\n"))

        self.queue.put(("progress", 45))
        self.queue.put(("status", "Scoring every signal…"))
        ranked, summary = cf.analyze(self.cfg, entry, meta, data,
                                     preset=opts.get("preset"), want=opts["count"] * 2,
                                     min_words=cf.min_caption_words(opts.get("captions")),
                                     avoid=cf.past_windows(self.cfg, entry.name))
        note = cf.used_up_note(summary)
        if note:
            self.queue.put(("log", f"  {note}\n"))
        if not ranked:
            self.queue.put(("error", "No candidate moments found in this VOD."))
            return

        ns = argparse.Namespace(fit=opts["fit"], captions=opts["captions"], caps=False,
                                focus=0.5, fps=opts.get("fps"), pad=0.2, snap=True, open=False)
        picks = ranked[: opts["count"]]
        self.queue.put(("status", f"Rendering {len(picks)} clip(s)…"))
        made, failed = cf.render_many(self.cfg, entry, meta, data, picks, ns,
                                      flat_dir=self.run_dir)
        for item, exc in failed:
            self.queue.put(("log", f"  render failed at {cf.fmt_ts(item['start'])}: {exc}\n"))

        records = []
        for item, clip in zip(picks, made):
            self.clips_made.append(self.run_dir)
            records.append({"path": str(clip), "entry": entry.name, "start": item["start"],
                            "end": item["end"], "rank": item["rank"], "score": item["score"],
                            "features": item["features"], "weights": summary.get("weights"),
                            "preset": opts.get("preset"), "version": cf.VERSION})
        session = cf.write_session(self.cfg, "vod", url, entry.name, records, summary,
                                   time.time() - started,
                                   {"preset": opts.get("preset"), "count": opts["count"],
                                    "length": opts["length"]})
        self.queue.put(("log", f"session -> {session.name}"))
        self.queue.put(("review", records))
        self.queue.put(("progress", 100))
        self.queue.put(("log", f"\nDone. {len(self.clips_made)} clip(s) in {self.run_dir}"))


    # ------------------------------------------------------------ library

    def prepare(self, path):
        """Add the recording to the library and transcribe it once."""
        resolved = path.resolve()
        entry = None
        for candidate in cf.entries(self.cfg):
            meta = json.loads((candidate / "source.json").read_text(encoding="utf-8"))
            if meta.get("kind") == "local" and Path(meta["path"]).resolve() == resolved:
                entry = candidate
                break
        if entry is None:
            cf.cmd_add(self.cfg, argparse.Namespace(source=str(resolved), name=None))
            entry = cf.find_entry(self.cfg, cf.slugify(path.name))
        return self.load_or_index(entry)

    def prepare_vod(self, url):
        """Add the VOD to the library, then transcribe it (audio only downloads once)."""
        entry = None
        for candidate in cf.entries(self.cfg):
            meta = json.loads((candidate / "source.json").read_text(encoding="utf-8"))
            if meta.get("kind") == "remote" and str(meta.get("url", "")).split("?")[0] == \
                    url.split("?")[0]:
                entry = candidate
                break
        if entry is None:
            cf.cmd_add(self.cfg, argparse.Namespace(source=url, name=None))
            title = (self.vod_info or {}).get("title") or cf.fetch_remote_meta(self.cfg, url)["title"]
            entry = cf.find_entry(self.cfg, cf.slugify(title))
        return self.load_or_index(entry)

    def load_or_index(self, entry):
        meta = json.loads((entry / "source.json").read_text(encoding="utf-8"))
        wanted = self.cfg.get("model", "medium.en")
        stored = cf.transcript_model(entry)
        if not (entry / "transcript.json").exists():
            self.queue.put(("status", f"Transcribing {entry.name} with {wanted}…"))
            cf.cmd_index(self.cfg, argparse.Namespace(name=entry.name, model=None, language=None))
        elif stored and stored != wanted:
            self.queue.put(("log", f"  re-transcribing {entry.name} with {wanted} "
                                   f"(cached pass used {stored})"))
            cf.cmd_index(self.cfg, argparse.Namespace(name=entry.name, model=None, language=None))
        elif not (entry / "audio.wav").exists():
            cf.ensure_audio(self.cfg, entry, meta)

        data = cf.load_transcript(entry)
        if not data.get("words"):
            peak = cf.audio_peak_db(self.cfg, entry)
            if peak is not None and peak < -45:
                self.queue.put(("log", f"  {entry.name}: the audio track is silent (peak {peak:.0f} dB) - "
                                       "no speech to caption, so this one is picked on motion and "
                                       "energy alone" + chr(10)))
        return entry, meta, data

    # ------------------------------------------------------------ event pump

    def drain(self):
        if not self.root.winfo_exists():
            return
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self.write_line(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "progress":
                    self.progress["value"] = payload
                elif kind == "vod_info":
                    self.vod_info_var.set(payload)
                elif kind == "vod_error":
                    self.vod_info_var.set(payload)
                    self.write_line(f"ERROR: {payload}")
                elif kind == "vod_loaded":
                    self.load_btn.state(["!disabled"])
                elif kind == "review":
                    self.open_judge(payload)
                elif kind == "version":
                    latest, newer = payload
                    self.version_label.configure(text=f"v{cf.VERSION}  ·  latest {latest}")
                    if newer:
                        self.update_btn.configure(text=f"Version {latest} is out")
                        self.update_btn.pack(anchor="w", pady=(0, 6))
                elif kind == "hardware":
                    self.hardware_pill.configure(text=payload)
                elif kind == "judge_records":
                    if self.judge_panel is not None:
                        self.judge_panel.set_records(payload)
                        never = sum(1 for row in payload if not row.get("verdict"))
                        self.judge_count.configure(
                            text=f"{len(payload)} clip(s) on disk  ·  {never} never judged")
                    else:
                        self.queue.put(("judge_records_cached", payload))
                elif kind == "judge_count":
                    self.judge_count.configure(text=payload)
                elif kind == "stats_tables":
                    self.fill_stats(*payload)
                elif kind == "stats_tables_error":
                    self.stats_note.configure(text=f"stats failed: {payload}")
                elif kind == "error":
                    self.write_line(f"\nERROR: {payload}")
                    self.status_var.set("Failed - see the log below.")
                elif kind == "finished":
                    self.running = False
                    self.go_btn.state(["!disabled"])
                    self.stop_btn.state(["disabled"])
                    if self.clips_made:
                        self.open_btn.state(["!disabled"])
                        self.status_var.set(f"{len(self.clips_made)} clip(s) ready.")
                    elif self.stop.is_set():
                        self.status_var.set("Stopped.")
                    else:
                        self.status_var.set("No clips made - see the log below.")
        except queue.Empty:
            pass
        except tk.TclError:  # window closed mid-run
            return
        try:
            self.root.after(120, self.drain)
        except tk.TclError:
            pass


class JudgePanel:
    """The clip table, filter and verdict panel, shared by the tab and the window."""

    def __init__(self, parent, cfg, records, on_change=None):
        self.cfg = cfg
        self.records = list(records)
        self.on_change = on_change

        body = tk.Frame(parent, bg=BG)
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        left.pack(side="left", fill="both", expand=True)

        top = tk.Frame(left, bg=CARD)
        top.pack(fill="x", padx=10, pady=(10, 6))
        self.query = tk.StringVar()
        field = tk.Entry(top, textvariable=self.query, font=("Segoe UI", 9), bg=FIELD, fg=FG,
                         insertbackground=FG, relief="flat", highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=ACCENT)
        field.pack(side="left", fill="x", expand=True, ipady=4)
        self.summary = tk.StringVar()
        tk.Label(top, textvariable=self.summary, bg=CARD, fg=MUTED,
                 font=("Segoe UI", 8)).pack(side="left", padx=(10, 0))

        table = tk.Frame(left, bg=CARD)
        table.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        columns = ("num", "clip", "at", "len", "score", "verdict")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse")
        for key, text, width, anchor in (("num", "#", 40, "center"), ("clip", "Clip", 340, "w"),
                                         ("at", "At", 88, "w"), ("len", "Len", 50, "w"),
                                         ("score", "Score", 60, "e"), ("verdict", "Verdict", 150, "w")):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "clip"))
        bar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        for tag, colour in (("keep", "#6ddc90"), ("maybe", "#dfc069"),
                            ("reject", "#e08a8a"), ("none", "#c9c9d1")):
            self.tree.tag_configure(tag, foreground=colour)

        panel = tk.Frame(body, bg=CARD, width=258, highlightthickness=1,
                         highlightbackground=BORDER)
        panel.pack(side="right", fill="y", padx=(10, 0))
        panel.pack_propagate(False)

        actions = tk.Frame(panel, bg=CARD)
        actions.pack(fill="x", padx=12, pady=(12, 2))
        tk.Button(actions, text="Keep  k", relief="flat", bd=0, bg=ACCENT, fg="#0b1a10",
                  activebackground="#45b96a", font=("Segoe UI Semibold", 9), padx=9, pady=5,
                  cursor="hand2", command=lambda: self.judge("keep")).pack(side="left")
        tk.Button(actions, text="Maybe  m", relief="flat", bd=0, bg=FIELD, fg=FG,
                  activebackground="#33333a", font=("Segoe UI", 9), padx=9, pady=5,
                  cursor="hand2", command=lambda: self.judge("maybe")).pack(side="left",
                                                                           padx=(6, 0))
        tk.Label(panel, text="Rejected because", bg=CARD, fg=MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=12, pady=(12, 4))
        for reason in cf.REJECT_REASONS:
            tk.Button(panel, text=f"{reason}   {cf.REASON_KEYS[reason]}", anchor="w",
                      relief="flat", bd=0, bg=FIELD, fg=FG, activebackground="#33333a",
                      font=("Segoe UI", 9), padx=9, pady=5, cursor="hand2",
                      command=lambda r=reason: self.judge("reject", r)).pack(
                fill="x", padx=12, pady=1)
        tk.Label(panel, text="boring and wrong moment blame the signals. late start, cut "
                            "short and bad edit blame the window, bad captions the transcript.",
                            bg=CARD, fg="#6b6c76",
                 font=("Segoe UI", 8), justify="left", wraplength=230).pack(
            anchor="w", padx=12, pady=(10, 0))

        self.counts = tk.StringVar()
        tk.Label(panel, textvariable=self.counts, bg=CARD, fg=MUTED, justify="left",
                 anchor="w", font=("Consolas", 9)).pack(anchor="w", padx=12, pady=(12, 0))

        self.position = tk.StringVar()
        tk.Label(panel, textvariable=self.position, bg=CARD, fg="#6b6c76", justify="left",
                 anchor="w", font=("Segoe UI", 8), wraplength=230).pack(
            anchor="w", padx=12, pady=(10, 12))

        self.tree.bind("<<TreeviewSelect>>", self.show_position)
        self.tree.bind("<Double-Button-1>", lambda _event: self.play())
        self.query.trace_add("write", lambda *_args: self.refresh(select=self.current()))
        self.refresh()

    def bind_hotkeys(self, window):
        """Keys live on the window, not the table: a focused entry must still type."""
        def guarded(handler):
            def run(event):
                widget = window.focus_get()
                if isinstance(widget, (tk.Entry, tk.Text)):
                    return None
                handler()
                return "break"

            return run

        window.bind("k", guarded(lambda: self.judge("keep")))
        window.bind("m", guarded(lambda: self.judge("maybe")))
        window.bind("r", guarded(lambda: self.judge("reject")))
        window.bind("<space>", guarded(self.play))
        for reason in cf.REJECT_REASONS:
            window.bind(cf.REASON_KEYS[reason],
                        guarded(lambda r=reason: self.judge("reject", r)))

    def set_records(self, records):
        self.records = list(records)
        self.refresh(select_first_unjudged=True)

    def verdict_text(self, record):
        if not record.get("verdict"):
            return "unjudged"
        if record.get("verdict") == "reject":
            return "rejected · " + (record.get("reason") or "no reason")
        return record["verdict"]

    def row_values(self, index, record):
        at = cf.fmt_ts(float(record["start"])) if record.get("start") is not None else ""
        length = f"{float(record['duration']):.0f}s" if record.get("duration") else ""
        score = f"{float(record['score']):.2f}" if record.get("score") is not None else ""
        return (index + 1, Path(record["path"]).name, at, length, score,
                self.verdict_text(record))

    def refresh(self, select=None, select_first_unjudged=False):
        self.tree.delete(*self.tree.get_children())
        needle = self.query.get().strip().lower()
        for index, record in enumerate(self.records):
            values = self.row_values(index, record)
            if needle and needle not in str(values[1]).lower() \
                    and needle not in str(values[5]).lower():
                continue
            self.tree.insert("", "end", iid=str(index), values=values,
                             tags=(record.get("verdict") or "none",))
        kept = sum(1 for row in self.records if row.get("verdict") == "keep")
        maybes = sum(1 for row in self.records if row.get("verdict") == "maybe")
        dropped = sum(1 for row in self.records if row.get("verdict") == "reject")
        left = len(self.records) - kept - maybes - dropped
        self.summary.set(f"{kept} kept  ·  {maybes} maybe  ·  {dropped} rejected  ·  {left} to go")
        reasoned = sum(1 for row in self.records if row.get("reason"))
        self.counts.set(chr(10).join([f"judged {len(self.records) - left} of {len(self.records)}",
                                      f"reasons {reasoned}"]))
        target = None
        if select_first_unjudged:
            target = next((i for i, row in enumerate(self.records) if not row.get("verdict")),
                          None)
        elif select is not None:
            target = select
        if target is not None and self.records:
            target = min(max(target, 0), len(self.records) - 1)
            if self.tree.exists(str(target)):
                self.tree.selection_set(str(target))
                self.tree.see(str(target))
                self.show_position()
        if self.on_change:
            self.on_change()

    def show_position(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        index = int(selection[0])
        line = f"row {index + 1} of {len(self.records)}"
        reason = self.records[index].get("reason")
        if reason:
            line += f" · {reason}: {cf.REASON_NOTES.get(reason, '')}"
        self.position.set(line)

    def current(self):
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def play(self):
        index = self.current()
        if index is None:
            return
        try:
            os.startfile(self.records[index]["path"])
        except OSError as exc:
            messagebox.showwarning("ClipForge", f"Can't play that file - {exc}")

    def judge(self, verdict, reason=None):
        index = self.current()
        if index is None:
            return
        record = self.records[index]
        if record.get("verdict") == "reject" and reason:
            cf.update_reason(self.cfg, record["path"], reason)
            record["reason"] = reason
            self.refresh(select=index + 1)
            return
        if record.get("verdict") == verdict:
            self.refresh(select=index + 1)
            return
        cf.record_feedback(self.cfg, record["path"], verdict, record, reason=reason)
        record["verdict"] = verdict
        record["reason"] = reason
        self.refresh(select=index + 1)


def open_review_window(cfg, records, target, root=None, name=None):
    """The same panel in its own window, for judging without the main one open."""
    own_root = root is None
    window = tk.Tk() if own_root else tk.Toplevel(root)
    apply_dark_styles(window)
    apply_icon(window)
    window.title(f"ClipForge - judge clips: {name}" if name else "ClipForge - judge clips")
    window.configure(bg=BG)
    window.geometry("1070x620")
    window.minsize(880, 460)

    judged = {str(row.get("clip") or ""): row for row in cf.load_feedback(cfg)}
    for record in records:
        previous = judged.get(record["path"]) or {}
        record["verdict"] = previous.get("verdict")
        record["reason"] = previous.get("reason")

    header = tk.Frame(window, bg=BG)
    header.pack(fill="x", padx=14, pady=(12, 8))
    tk.Label(header, text="Judge clips", bg=BG, fg=FG,
             font=("Segoe UI Semibold", 12)).pack(side="left")
    tk.Label(header, text=Path(str(target)).name or "", bg=BG, fg=MUTED,
             font=("Segoe UI", 9)).pack(side="left", padx=(10, 0))

    holder = tk.Frame(window, bg=BG)
    holder.pack(fill="both", expand=True, padx=14, pady=(0, 12))
    panel = JudgePanel(holder, cfg, records)

    panel.bind_hotkeys(window)
    panel.tree.focus_set()

    if own_root:
        window.mainloop()
        kept = sum(1 for row in records if row.get("verdict") == "keep")
        dropped = sum(1 for row in records if row.get("verdict") == "reject")
        print(f"{kept} kept, {dropped} rejected - `cf stats` now has this run to learn from")
    return window


def main():
    try:
        import clipforge_web
        if os.environ.get("CLIPFORGE_TK", "") != "1":
            return clipforge_web.run()
    except Exception:  # noqa: BLE001 - no webview runtime, no pywebview: use tk
        pass
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - not fatal, just crisper text when it works
        pass
    root = tk.Tk()
    try:
        app = App(root)
    except cf.Fail as exc:
        from tkinter import messagebox

        root.withdraw()
        messagebox.showerror("ClipForge", f"{exc}\n\nFix that value in\n{cf.CONFIG_PATH}\n\n"
                                          "or delete the file to start from the defaults.")
        root.destroy()
        return 1
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg in ("judge", "stats", "make"):
            app.show_page(arg)
        elif arg.lower().startswith("http"):
            app.vod_url_var.set(arg)
            app.nb.select(1)
        elif Path(arg).is_dir():
            app.folder_var.set(str(Path(arg).resolve()))
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
