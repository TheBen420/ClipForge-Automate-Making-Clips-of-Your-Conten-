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
BG = "#1b1b1f"
FG = "#e8e8ea"
MUTED = "#9a9aa2"
ACCENT = "#3ba55d"
FIELD = "#2a2a30"


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
    """Give the window ClipForge's own icon so the taskbar and title bar agree."""
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

        # Windows groups taskbar buttons by process identity. Without its own AppUserModelID
        # the window inherits pythonw.exe's identity and the taskbar keeps Python's icon,
        # however the window's own icon is set.
        if os.name == "nt":
            try:
                import ctypes

                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ClipForge.App")
            except Exception:  # noqa: BLE001 - never fatal, just a plain taskbar icon
                pass
        root.title("ClipForge")
        apply_icon(root)
        root.geometry("880x900")
        root.minsize(800, 780)
        root.configure(bg=BG)
        self.build_styles()

        outer = ttk.Frame(root, padding=18)
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer)
        head.pack(fill="x")
        titles = ttk.Frame(head)
        titles.pack(side="left")
        ttk.Label(titles, text="ClipForge", style="Title.TLabel").pack(anchor="w")
        ttk.Label(titles, text="Clips land in one folder, named after the source video.",
                  style="Title.TLabel",
                  font=("Segoe UI", 10)).pack(anchor="w")
        self.update_btn = ttk.Button(head, text="Update available",
                                     command=self.open_update)
        self.version_label = ttk.Label(head, text=f"v{cf.VERSION}", style="Title.TLabel",
                                       font=("Segoe UI", 9))
        self.version_label.pack(side="right", padx=(0, 10), pady=(6, 0))
        self.root.after(1500, self.check_update)

        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="x")
        self.build_folder_tab()
        self.build_vod_tab()
        self.nb.bind("<<NotebookTabChanged>>", lambda _e: self.refresh_hint())

        self.build_options(outer)
        self.build_destination(outer)
        self.build_actions(outer)
        self.build_log(outer)

        for notice in self.config_notices:
            self.queue.put(("log", f"  ({notice})\n"))
        self.check_environment()
        self.root.after(120, self.drain)

    # ------------------------------------------------------------ styling

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
        tab = ttk.Frame(self.nb, padding=12)
        self.nb.add(tab, text="Local folder")

        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Label(row, text="Recordings folder", style="Muted.TLabel").pack(anchor="w")
        line = ttk.Frame(row)
        line.pack(fill="x", pady=(3, 0))
        self.folder_var = tk.StringVar()
        self.text_entry(line, self.folder_var)
        ttk.Button(line, text="Choose folder…", command=self.browse).pack(side="left")

        self.recursive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(tab, text="Include subfolders", variable=self.recursive_var) \
            .pack(anchor="w", pady=(10, 0))
        self.folder_hint = ttk.Label(tab, text="Every video in the folder gets its own clips.",
                                     style="Muted.TLabel")
        self.folder_hint.pack(anchor="w", pady=(6, 0))

    def build_vod_tab(self):
        tab = ttk.Frame(self.nb, padding=12)
        self.nb.add(tab, text="Twitch VOD")

        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Label(row, text="VOD link", style="Muted.TLabel").pack(anchor="w")
        line = ttk.Frame(row)
        line.pack(fill="x", pady=(3, 0))
        self.vod_url_var = tk.StringVar()
        self.vod_entry = self.text_entry(line, self.vod_url_var)
        self.load_btn = ttk.Button(line, text="Load", command=self.load_vod)
        self.load_btn.pack(side="left")

        self.vod_info_var = tk.StringVar(value="Paste a twitch.tv/videos/… link and press Load.")
        tk.Label(tab, textvariable=self.vod_info_var, bg=BG, fg=MUTED, font=("Segoe UI", 9),
                 anchor="w", justify="left", wraplength=760).pack(fill="x", pady=(8, 0))
        self.vod_hint = ttk.Label(
            tab, text="It pulls the audio once to find the loud moments, then downloads only "
                      "those windows.", style="Muted.TLabel")
        self.vod_hint.pack(anchor="w", pady=(6, 0))
        self.chat_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(tab, text="Use Twitch chat hype (leans on the messages at the peak)",
                        variable=self.chat_var).pack(anchor="w", pady=(8, 0))

    def build_options(self, parent):
        opts = ttk.Frame(parent)
        opts.pack(fill="x", pady=(14, 0))

        self.fit_var = tk.StringVar(value=self.cfg["fit"])
        self.cap_var = tk.StringVar(value=self.cfg["captions"])
        self.count_var = tk.StringVar(value="2")
        self.length_var = tk.StringVar(value="25")
        self.preset_var = tk.StringVar(value="auto")
        self.preset_var.trace_add("write", self.apply_preset)

        def labelled(text, widget):
            box = ttk.Frame(opts)
            box.pack(side="left", padx=(0, 18))
            ttk.Label(box, text=text, style="Muted.TLabel").pack(anchor="w")
            widget(box)

        labelled("Framing", lambda p: ttk.Combobox(p, textvariable=self.fit_var, width=9,
                                                   state="readonly",
                                                   values=["blur", "crop", "black"]).pack())
        labelled("Captions", lambda p: ttk.Combobox(p, textvariable=self.cap_var, width=10,
                                                    state="readonly",
                                                    values=["karaoke", "block", "off"]).pack())
        labelled("Clips to make", lambda p: ttk.Combobox(p, textvariable=self.count_var, width=5,
                                                         state="readonly",
                                                         values=[str(n) for n in range(1, 21)]
                                                         ).pack())
        labelled("Clip length (s)", lambda p: ttk.Combobox(p, textvariable=self.length_var, width=5,
                                                           state="readonly",
                                                           values=["15", "20", "25", "30", "45",
                                                                   "60"]).pack())
        self.model_var = tk.StringVar(value=self.cfg.get("model", "medium.en"))
        labelled("Transcription", lambda p: ttk.Combobox(p, textvariable=self.model_var, width=11,
                                                         state="readonly",
                                                         values=["small.en", "medium.en",
                                                                 "large-v3"]).pack())
        self.model_var.trace_add("write", self.save_model)
        self.fps_var = tk.StringVar(value="auto")
        labelled("Frame rate", lambda p: ttk.Combobox(p, textvariable=self.fps_var, width=7,
                                                      state="readonly",
                                                      values=["auto", "60", "30"]).pack())
        labelled("Content type", lambda p: ttk.Combobox(p, textvariable=self.preset_var, width=13,
                                                        state="readonly",
                                                        values=["auto"] + list(
                                                            (self.cfg.get("presets") or {}).keys())
                                                        ).pack())
        self.apply_preset()

        vocab = ttk.Frame(parent)
        vocab.pack(fill="x", pady=(10, 0))
        ttk.Label(vocab, text="Vocabulary", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        self.prompt_var = tk.StringVar(value=self.cfg.get("prompt", ""))
        entry = tk.Entry(vocab, textvariable=self.prompt_var, font=("Segoe UI", 10), bg=FIELD,
                         fg=FG, insertbackground=FG, relief="flat", highlightthickness=1,
                         highlightbackground="#3a3a42", highlightcolor=ACCENT)
        entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.prompt_var.trace_add("write", self.save_vocabulary)
        ttk.Label(parent, style="Muted.TLabel", wraplength=790, justify="left",
                  text="Vocabulary: names and terms it keeps mis-hearing, comma separated. It "
                       "biases the transcript toward them and improves punctuation too."
                  ).pack(anchor="w", pady=(4, 0))

    def build_destination(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(14, 0))
        ttk.Label(row, text="Save clips to", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        self.output_var = tk.StringVar(value=self.default_output())
        self.text_entry(row, self.output_var)
        ttk.Button(row, text="Choose…", command=self.browse_output).pack(side="left")

        self.subfolder_var = tk.BooleanVar(value=bool(self.cfg.get("clip_output_subfolder", True)))
        ttk.Checkbutton(parent, text="Put clips in a subfolder named after the source",
                        variable=self.subfolder_var).pack(anchor="w", pady=(8, 0))

    def build_actions(self, parent):
        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=(16, 10))
        self.go_btn = ttk.Button(actions, text="Make Clips", style="Go.TButton", command=self.start)
        self.go_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Stop", command=self.request_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.open_btn = ttk.Button(actions, text="Open clips folder", command=self.open_output,
                                   state="disabled")
        self.open_btn.pack(side="right")
        ttk.Button(actions, text="Judge clips", command=self.open_judge).pack(side="right",
                                                                             padx=(0, 8))
        ttk.Button(actions, text="Selection stats", command=self.show_stats).pack(side="right",
                                                                                  padx=(0, 8))

        self.progress = ttk.Progressbar(parent, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(2, 6))
        self.status_var = tk.StringVar(value="Ready.")
        tk.Label(parent, textvariable=self.status_var, bg=BG, fg=MUTED, font=("Segoe UI", 9),
                 anchor="w").pack(fill="x")

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
        """Judge clips in the dedicated window, never in this one."""
        if records:
            rows, target = list(records), self.run_dir or ""
        else:
            try:
                rows = cf.unjudged_clips(self.cfg)
                if not rows:
                    rows, target = cf.review_records(self.cfg)
                    target = str(target)
                else:
                    target = f"{len(rows)} clip(s) never judged, in {cf.media_root(self.cfg)}"
            except cf.Fail as exc:
                messagebox.showinfo("ClipForge", str(exc))
                return
        if not rows:
            messagebox.showinfo("ClipForge", "Every clip on disk has been judged already.")
            return
        open_review_window(self.cfg, rows, target, root=self.root,
                          name=Path(str(target)).name if records else "unjudged on disk")

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
        self.log = ScrolledText(parent, height=13, bg="#141417", fg="#c9c9d1", relief="flat",
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
                        self.update_btn.pack(side="right", padx=(0, 10), pady=(6, 0))
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


def open_review_window(cfg, records, target, root=None, name=None):
    """Judge a finished run, so a closed app never costs you the feedback."""
    own_root = root is None
    window = tk.Tk() if own_root else tk.Toplevel(root)
    apply_dark_styles(window)
    apply_icon(window)
    window.title(f"ClipForge - judge clips: {name}" if name
                 else "ClipForge - judge clips")
    window.configure(bg=BG)
    window.geometry("760x420")

    judged = {str(row.get("clip") or ""): row for row in cf.load_feedback(cfg)}
    for record in records:
        previous = judged.get(record["path"]) or {}
        record["verdict"] = previous.get("verdict")
        record["reason"] = previous.get("reason")

    status = tk.StringVar()
    listing = tk.Listbox(window, height=12, bg="#141417", fg="#c9c9d1", selectbackground="#34343c",
                         selectforeground="#ffffff", relief="flat", font=("Consolas", 10),
                         activestyle="none", highlightthickness=1, highlightbackground="#2a2a30")
    listing.pack(fill="both", expand=True, padx=12, pady=(12, 6))

    def label_for(index):
        record = records[index]
        mark = {"keep": "kept    ", "maybe": "maybe   ",
                "reject": "rejected"}.get(record.get("verdict"), "unjudged")
        reason = record.get("reason")
        if reason:
            mark = f"{mark} {reason:<12}"
        length = record.get("duration")
        suffix = f"  {float(length):.0f}s" if length else ""
        return f"{mark}  {Path(record['path']).name}{suffix}"

    def refresh(select_first_unjudged=False):
        listing.delete(0, "end")
        for index in range(len(records)):
            listing.insert("end", label_for(index))
        kept = sum(1 for row in records if row.get("verdict") == "keep")
        how_many = sum(1 for row in records if row.get("verdict") == "maybe")
        dropped = sum(1 for row in records if row.get("verdict") == "reject")
        left = len(records) - kept - how_many - dropped
        if select_first_unjudged:
            for index, record in enumerate(records):
                if not record.get("verdict"):
                    listing.selection_set(index)
                    listing.see(index)
                    break
        status.set(f"{kept} kept, {how_many} borderline, {dropped} rejected, {left} to go  -  "
                   f"space plays, k keeps, m borderline, then a reason or r to reject")

    def current():
        selection = listing.curselection()
        return int(selection[0]) if selection else None

    def play():
        index = current()
        if index is not None:
            try:
                os.startfile(records[index]["path"])
            except OSError as exc:
                messagebox.showwarning("ClipForge", f"Can't play that file\n\n{exc}")

    def judge(verdict, reason=None):
        index = current()
        if index is None:
            return
        record = records[index]
        if not record.get("verdict"):
            cf.record_feedback(cfg, record["path"], verdict, record, reason=reason)
            record["verdict"] = verdict
            record["reason"] = reason
        refresh(select_first_unjudged=True)

    def open_folder():
        cf.open_folder(Path(records[0]["path"]).parent)

    buttons = ttk.Frame(window)
    buttons.pack(fill="x", padx=12)
    ttk.Button(buttons, text="Play (space)", command=play).pack(side="left")
    ttk.Button(buttons, text="Keep (k)", command=lambda: judge("keep")).pack(side="left", padx=(8, 0))
    ttk.Button(buttons, text="Borderline (m)",
               command=lambda: judge("maybe")).pack(side="left", padx=(8, 0))
    ttk.Button(buttons, text="Open folder", command=open_folder).pack(side="right")

    why = ttk.Frame(window)
    why.pack(fill="x", padx=12, pady=(8, 0))
    ttk.Label(why, text="Rejected because", style="Muted.TLabel").pack(side="left", padx=(0, 8))
    for reason in cf.REJECT_REASONS:
        keys = "".join(word[0] for word in reason.split())
        ttk.Button(why, text=f"{reason} ({keys})", width=20,
                   command=lambda r=reason: judge("reject", r)).pack(side="left", padx=(0, 6))
    ttk.Button(why, text="No reason (r)", width=14,
               command=lambda: judge("reject")).pack(side="left")
    ttk.Label(window, textvariable=status, style="Muted.TLabel").pack(fill="x", padx=12, pady=(8, 10))
    ttk.Label(window, text=f"run: {target}", foreground=MUTED, background=BG).pack(fill="x", padx=12, pady=(0, 8))

    window.bind("<space>", lambda _event: play())
    window.bind("k", lambda _event: judge("keep"))
    window.bind("m", lambda _event: judge("maybe"))
    window.bind("r", lambda _event: judge("reject"))
    for reason in cf.REJECT_REASONS:
        keys = "".join(word[0] for word in reason.split())
        window.bind(keys, lambda _event, r=reason: judge("reject", r))
    listing.bind("<Double-Button-1>", lambda _event: play())

    refresh(select_first_unjudged=True)
    if own_root:
        window.mainloop()
        kept = sum(1 for row in records if row.get("verdict") == "keep")
        dropped = sum(1 for row in records if row.get("verdict") == "reject")
        print(f"{kept} kept, {dropped} rejected - `cf stats` now has this run to learn from")
    return window


def main():
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
        if arg.lower().startswith("http"):
            app.vod_url_var.set(arg)
            app.nb.select(1)
        elif Path(arg).is_dir():
            app.folder_var.set(str(Path(arg).resolve()))
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
