"""ClipForge as a browser UI in a WebView2 window.

The window is HTML, so it is the mockup rather than an impression of it. Python keeps every
job it already had - index, analyse, render, judge - and the page asks for it through a small
api object instead of drawing widgets itself.
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_DIR))

import clipforge as cf

UI = TOOL_DIR / "ui" / "index.html"


class ClipServer(http.server.BaseHTTPRequestHandler):
    """Serves one clip at a time to the window, and nothing outside the media folder.

    WebView2 will not read a file:// video from a page it loaded over http, so the page asks
    this for a loopback URL. Byte ranges are honoured, otherwise the scrubber in the player
    cannot move.
    """

    root = None
    token = None

    def log_message(self, *args):  # noqa: A003 - keep the server quiet
        pass

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's name
        parts = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parts.query)
        if query.get("token", [""])[0] != self.token:
            self.send_error(403)
            return
        try:
            target = Path(query.get("path", [""])[0]).resolve()
        except (OSError, RuntimeError):
            self.send_error(404)
            return
        root = Path(self.root).resolve()
        if root not in target.parents or not target.is_file():
            self.send_error(404)
            return
        size = target.stat().st_size
        start, end = 0, size - 1
        header = self.headers.get("Range", "")
        if header.startswith("bytes="):
            first, _, last = header[6:].partition("-")
            try:
                start = int(first or 0)
                end = int(last) if last else size - 1
            except ValueError:
                start, end = 0, size - 1
            end = max(start, min(end, size - 1))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            with target.open("rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = fh.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (OSError, ConnectionError):
            pass


class Api:
    """What the page can ask Python to do."""

    def __init__(self):
        self.cfg = cf.load_config()
        self.lines = []
        self.status = "Ready."
        self.progress = 0
        self.running = False
        self.error = None
        self.review = []
        self.stop = threading.Event()
        self._window = None  # private: pywebview would otherwise walk the window object
        self.lock = threading.Lock()
        self._hardware = None
        self._media = None
        self._media_token = secrets.token_hex(16)
        self.per_clip = None
        self.render_seconds = 0.0
        self.render_clips = 0

    # ---------------------------------------------------------------- state

    def hello(self):
        return {
            "version": cf.VERSION,
            "media_root": str(cf.media_root(self.cfg)),
            "notices": list(cf.CONFIG_NOTICES),
            "reasons": list(cf.REJECT_REASONS),
            "reason_keys": cf.REASON_KEYS,
            "reason_notes": cf.REASON_NOTES,
            "presets": list((self.cfg.get("presets") or {}).keys()),
        }

    def logo(self):
        """The app's own icon, for the brand mark at the top of the rail."""
        icon = TOOL_DIR / "icon.png"
        try:
            return {"data": "data:image/png;base64," + base64.b64encode(icon.read_bytes()).decode("ascii")}
        except OSError:
            return {"data": ""}

    def hardware(self):
        """Which encoder and device this machine will use.

        Probing for both costs seconds, so the window paints first and asks for this after.
        """
        if self._hardware is None:
            self._hardware = {"encoder": self._encoder(), "device": self._device()}
        return dict(self._hardware)

    def update_available(self):
        latest, checked = cf.update_status(self.cfg, max_age=0)
        return {"latest": latest if checked else None}

    def _encoder(self):
        try:
            return cf.pick_encoder(self.cfg)
        except cf.Fail:
            return "unavailable"

    def _device(self):
        try:
            _, device = cf.whisper_model(self.cfg, self.cfg["model"])
            return device
        except cf.Fail as exc:
            return f"unavailable ({exc})"

    def settings(self):
        cfg = self.cfg
        return {
            "folder": cfg.get("last_folder") or str(Path.home() / "Videos" / "Recordings"),
            "vod": cfg.get("last_vod") or "",
            "recursive": bool(cfg.get("recursive", False)),
            "chat": bool(cfg.get("chat", True)),
            "fit": cfg.get("fit", "blur"),
            "captions": cfg.get("captions", "karaoke"),
            "parallel": cf.safe_worker_count(cfg.get("render_workers", 1))[0],
            "count": int(cfg.get("count", 2)),
            "length": int(cfg.get("length", 25)),
            "preset": "auto",
            "model": cfg.get("model", "medium.en"),
            "fps": "auto",
            "vocabulary": cfg.get("prompt", ""),
            "output": cfg.get("clip_output") or str(cf.media_root(cfg) / "clips"),
            "subfolder": bool(cfg.get("clip_output_subfolder", True)),
        }

    def save(self, data):
        """Remember what the page had filled in, so the next launch looks the same."""
        mapping = {
            "folder": "last_folder", "vod": "last_vod", "recursive": "recursive",
            "chat": "chat", "fit": "fit", "captions": "captions", "count": "count",
            "length": "length", "model": "model", "vocabulary": "prompt",
            "output": "clip_output", "subfolder": "clip_output_subfolder",
        }
        for key, cfg_key in mapping.items():
            if key in data:
                self.cfg[cfg_key] = data[key]
        try:
            cf.save_config(self.cfg)
        except OSError:
            pass
        return True

    def choose_folder(self):
        import webview

        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        return result[0] if result else None

    def choose_output(self):
        return self.choose_folder()

    def preview(self, path):
        """A loopback URL for one clip, so the window can play it without leaving the page."""
        if self._media is None:
            handler = type("BoundClipServer", (ClipServer,),
                           {"root": str(cf.media_root(self.cfg)), "token": self._media_token})
            self._media = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            self._media.daemon_threads = True
            threading.Thread(target=self._media.serve_forever, daemon=True).start()
        query = urllib.parse.urlencode({"path": str(path), "token": self._media_token})
        return {"url": f"http://127.0.0.1:{self._media.server_address[1]}/clip?{query}"}

    def open_path(self, path):
        try:
            if Path(path).is_dir():
                cf.open_folder(Path(path))
            else:
                os.startfile(path)
            return True
        except OSError:
            return False

    # ---------------------------------------------------------------- run

    def start(self, payload):
        if self.running:
            return {"ok": False, "why": "already running"}
        self.running = True
        self.stop.clear()
        self.progress = 0
        self.error = None
        with self.lock:
            self.lines = []
            self.review = []
        self.say("starting…")
        self.save(payload)
        thread = threading.Thread(target=self._run, args=(payload,), daemon=True)
        thread.start()
        return {"ok": True}

    def cancel(self):
        self.stop.set()
        return True

    def _run(self, payload):
        try:
            if payload.get("source") == "vod":
                records = self._run_vod(payload)
            else:
                records = self._run_folder(payload)
            with self.lock:
                self.review = records
            self.status = f"{len(records)} clip(s) ready."
            self.progress = 100
        except cf.Fail as exc:
            self.error = str(exc)
            self.status = "Stopped on a problem."
        except Exception as exc:  # noqa: BLE001 - never leave the page spinning
            self.error = f"{exc.__class__.__name__}: {exc}"
            self.status = "Stopped on a problem."
        finally:
            self.running = False
            self.say("finished.")

    def _dest(self, label, payload):
        base = Path(payload.get("output") or cf.media_root(self.cfg) / "clips")
        base.mkdir(parents=True, exist_ok=True)
        return base / label if payload.get("subfolder", True) else base

    def _opts(self, payload):
        return argparse.Namespace(
            fit=payload.get("fit", "blur"), captions=payload.get("captions", "karaoke"),
            caps=False, focus=0.5,
            fps=None if payload.get("fps") in (None, "auto") else int(payload["fps"]),
            pad=0.2, snap=True, open=False)

    def _clips(self, entry, meta, data, picks, payload, out_dir):
        ns = self._opts(payload)
        started = time.time()
        made, failed = cf.render_many(self.cfg, entry, meta, data, picks, ns, flat_dir=out_dir)
        if made:
            self.render_seconds += time.time() - started
            self.render_clips += len(made)
            self.per_clip = round(self.render_seconds / self.render_clips, 1)
        for item, exc in failed:
            self.say(f"  render failed at {cf.fmt_ts(item['start'])}: {exc}")
        records = []
        for item, clip in zip(picks, made):
            records.append({"path": str(clip), "entry": entry.name, "duration": item.get("duration"),
                            "start": item["start"], "end": item["end"], "rank": item["rank"],
                            "score": item["score"], "features": item["features"],
                            "weights": item.get("weights")})
        return records

    def _run_folder(self, payload):
        folder = Path(payload.get("folder", ""))
        if not folder.is_dir():
            raise cf.Fail("That folder isn't there any more.")
        pattern = "**/*.mp4" if payload.get("recursive") else "*.mp4"
        files = sorted(folder.glob(pattern))
        if not files:
            raise cf.Fail("No .mp4 files in that folder.")
        everything = []
        for index, path in enumerate(files, 1):
            if self.stop.is_set():
                break
            self.say(f"({index}/{len(files)}) {path.name}")
            entry, meta, data = self._prepare(path)
            out_dir = self._dest(cf.safe_name(path.stem), payload)
            picks = self._analyze(entry, meta, data, payload)
            if not picks:
                self.say("  nothing worth cutting there")
                continue
            everything.extend(self._clips(entry, meta, data, picks, payload, out_dir))
        return everything

    def _run_vod(self, payload):
        url = (payload.get("vod") or "").strip()
        if not url:
            raise cf.Fail("Paste a VOD link first.")
        info = cf.fetch_remote_meta(self.cfg, url)
        self.say(f"VOD: {info.get('title')}")
        entry = None
        for candidate in cf.entries(self.cfg):
            meta = json.loads((candidate / "source.json").read_text(encoding="utf-8"))
            if meta.get("kind") == "remote" and str(meta.get("url", "")).split("?")[0] == \
                    url.split("?")[0]:
                entry = candidate
                break
        if entry is None:
            cf.cmd_add(self.cfg, argparse.Namespace(source=url, name=None))
            entry = cf.find_entry(self.cfg, cf.slugify(info["title"]))
        if payload.get("chat") and not (entry / "chat.json").exists():
            try:
                messages = cf.fetch_twitch_chat(url, entry / "chat.json", on_line=self.say)
                self.say(f"  {len(messages)} chat messages")
            except Exception as exc:  # noqa: BLE001 - chat is a bonus, never a blocker
                self.say(f"  chat unavailable ({exc})")
        entry, meta, data = self._index(entry)
        out_dir = self._dest(cf.safe_name(info["title"]), payload)
        picks = self._analyze(entry, meta, data, payload)
        return self._clips(entry, meta, data, picks, payload, out_dir)

    def _prepare(self, path):
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
        return self._index(entry)

    def _index(self, entry):
        meta = json.loads((entry / "source.json").read_text(encoding="utf-8"))
        wanted = self.cfg.get("model", "medium.en")
        stored = cf.transcript_model(entry)
        if not (entry / "transcript.json").exists() or (stored and stored != wanted):
            self.status = f"Transcribing {entry.name} with {wanted}…"
            self.say(f"  transcribing {entry.name} ({wanted})")
            cf.cmd_index(self.cfg, argparse.Namespace(name=entry.name, model=None, language=None))
        elif not (entry / "audio.wav").exists():
            cf.ensure_audio(self.cfg, entry, meta)
        return entry, meta, cf.load_transcript(entry)

    def _analyze(self, entry, meta, data, payload):
        self.status = "Scoring every signal…"
        self.say("  scoring")
        preset = None if payload.get("preset") in (None, "auto") else payload["preset"]
        length = int(payload.get("length") or 0) or None
        if preset:
            length = (self.cfg.get("presets") or {}).get(preset, {}).get("length") or length
        ranked, summary = cf.analyze(self.cfg, entry, meta, data, preset=preset,
                                     want=int(payload.get("count", 2)) * 2,
                                     min_words=cf.min_caption_words(payload.get("captions")),
                                     avoid=cf.past_windows(self.cfg, entry.name),
                                     length=length)
        note = cf.used_up_note(summary)
        if note:
            self.say(f"  {note}")
        picks = ranked[: int(payload.get("count", 2))]
        for item in picks:
            item["weights"] = summary.get("weights")
        return picks

    # ---------------------------------------------------------------- job log

    def say(self, line):
        with self.lock:
            self.lines.append(line)
            self.lines = self.lines[-400:]

    def poll(self, cursor=0):
        with self.lock:
            lines = self.lines[cursor:]
            total = len(self.lines)
            review = list(self.review) if not self.running else []
        return {"lines": lines, "cursor": total, "progress": self.progress,
                "status": self.status, "running": self.running, "error": self.error,
                "review": review, "per_clip": self.per_clip}

    # ---------------------------------------------------------------- judging

    def list_clips(self):
        rows = cf.disk_clips(self.cfg)
        rows.sort(key=lambda row: (row.get("verdict") is not None, row["path"]))
        root = cf.media_root(self.cfg)
        for row in rows:
            row["source"] = self._source(row["path"], root)
            try:
                row["added"] = int(Path(row["path"]).stat().st_mtime)
            except OSError:
                row["added"] = 0
        return rows

    @staticmethod
    def _source(path, root):
        """Which batch or session a clip came from, so a long list can be grouped.

        Clips sit under clips/<batch>/ and ready/<session>/<take>/, and the folder that holds
        them is what the page groups on.
        """
        try:
            parts = Path(path).relative_to(root).parts
        except ValueError:
            return Path(path).parent.name or "Clips"
        if len(parts) <= 1:
            return "Clips"
        if parts[0] in ("clips", "ready"):
            if len(parts) >= 3:
                return parts[1]
            return "Clips" if parts[0] == "clips" else "Sessions"
        return parts[0]

    def _cover(self, clip):
        """The cover ClipForge wrote beside a clip, shrunk once and kept for the list."""
        clip = Path(clip)
        cover = clip.with_name(clip.stem + "-thumb.jpg")
        if not cover.exists():
            return None
        try:
            stamp = int(cover.stat().st_mtime)
        except OSError:
            return None
        cache = cf.media_root(self.cfg) / "cache" / "thumbs"
        key = hashlib.md5(f"{cover}|{stamp}".encode("utf-8")).hexdigest()
        small = cache / f"{key}.jpg"
        if not small.exists():
            cache.mkdir(parents=True, exist_ok=True)
            try:
                subprocess.run([cf.ffmpeg_path(self.cfg), "-hide_banner", "-loglevel", "error",
                                "-y", "-i", str(cover), "-vf", "scale=-2:200", str(small)],
                               check=True, capture_output=True, timeout=30, **cf.NO_WINDOW)
            except (subprocess.SubprocessError, OSError):
                return None
        try:
            return "data:image/jpeg;base64," + base64.b64encode(small.read_bytes()).decode("ascii")
        except OSError:
            return None

    def thumbs(self):
        """A small still of every clip that has a cover, keyed by clip path."""
        rows = cf.disk_clips(self.cfg, durations=False)
        found = {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            for row, data in zip(rows, pool.map(lambda row: self._cover(row["path"]), rows)):
                if data:
                    found[row["path"]] = data
        return found

    def judge(self, path, verdict, reason=None):
        rows = {str(row.get("clip")): row for row in cf.load_feedback(self.cfg)}
        record = rows.get(str(path)) or {"path": str(path), "features": {}, "weights": {}}
        if record.get("verdict") == "reject" and reason:
            cf.update_reason(self.cfg, path, reason)
        elif record.get("verdict") == verdict:
            pass
        else:
            cf.record_feedback(self.cfg, path, verdict, record, reason=reason)
        return self.list_clips()

    def stats(self):
        feedback = cf.load_feedback(self.cfg)
        split = cf.signal_split(feedback)
        learned = cf.learned_multipliers(self.cfg)
        weights = {name: round(float(weight) * learned.get(name, 1.0), 2)
                   for name, weight in (self.cfg.get("weights") or {}).items()}
        reasons = {}
        for row in feedback:
            key = row.get("reason")
            if key:
                key = cf.REASON_ALIASES.get(key, key)
                reasons[key] = reasons.get(key, 0) + 1
        return {"judged": len(feedback),
                "signals": [{"name": name, "weight": weights.get(name, 0),
                             "separation": (split.get(name) or {}).get("separation", 0),
                             "kept": (split.get(name) or {}).get("n_kept", 0),
                             "rejected": (split.get(name) or {}).get("n_rejected", 0)}
                            for name in weights],
                "reasons": reasons}

    def open_repo(self):
        webbrowser.open(cf.REPO_URL)
        return True


def _claim_taskbar():
    """Give the WebView window ClipForge's own taskbar identity and icon.

    Windows groups taskbar buttons by process identity, so a window started by pythonw
    otherwise shows Python's icon; the icon is then set on the frame window directly because
    the WebView2 frame does not take one from tk.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ClipForge.App")
    except Exception:  # noqa: BLE001 - a plain taskbar icon at worst
        return
    icon_path = TOOL_DIR / "icon.ico"
    if not icon_path.exists():
        return
    import threading
    import time

    def apply():
        hwnd = 0
        for _ in range(80):
            hwnd = ctypes.windll.user32.FindWindowW(None, "ClipForge")
            if hwnd:
                break
            time.sleep(0.25)
        if not hwnd:
            return
        for kind in (1, 0):
            handle = ctypes.windll.user32.LoadImageW(None, str(icon_path), 1, 32, 32, 0x10)
            if handle:
                ctypes.windll.user32.SendMessageW(hwnd, 0x0080, kind, handle)

    threading.Thread(target=apply, daemon=True).start()


_INSTANCE_LOCK = None


def _already_running():
    """One ClipForge at a time: a second window would write judgements over the first.

    Returns True when another copy owns the lock, after bringing its window forward.
    """
    global _INSTANCE_LOCK
    if os.name != "nt":
        return False
    import ctypes

    _INSTANCE_LOCK = ctypes.windll.kernel32.CreateMutexW(None, False, "ClipForge.SingleInstance")
    if ctypes.windll.kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
        return False
    hwnd = ctypes.windll.user32.FindWindowW(None, "ClipForge")
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 9)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    return True


def run():
    import webview

    if _already_running():
        return 0
    _claim_taskbar()
    api = Api()
    window = webview.create_window("ClipForge", str(UI), js_api=api, width=1357, height=920,
                                   min_size=(960, 640), background_color="#131316")
    api._window = window
    webview.start(gui="edgechromium", debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(run())
