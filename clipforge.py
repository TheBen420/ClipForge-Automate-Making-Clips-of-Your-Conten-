#!/usr/bin/env python3
"""clipforge - turn livestream recordings into ready-to-post vertical clips.

Workflow:  add  ->  index  ->  find / suggest  ->  make  ->  post from ready/
Run `cf doctor` first if anything looks broken.
"""

import argparse
import csv
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
CONFIG_PATH = TOOL_DIR / "config.json"

def _default_media_root():
    """Prefer a roomy data drive, but only one we can actually write to."""
    for drive in ("D:/", "C:/"):
        candidate = Path(drive) / "ClipForge"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return str(candidate)
        except OSError:
            continue
    return str(Path.home() / "ClipForge")


DEFAULT_CONFIG = {
    "media_root": _default_media_root(),
    "watch_dirs": [str(Path.home() / "Videos" / "Recordings")],
    "model": "medium.en",
    "prompt": "",
    "fit": "blur",
    "captions": "karaoke",
    "font": "Arial Black",
    "font_size": 88,
    "caption_margin_bottom": 330,
    "hashtags": ["#gaming", "#twitch", "#clips", "#streamer", "#twitchclips"],
    "ytdlp_cookies_from_browser": "",
    "ffmpeg": "",
    "clip_output": "",
    "clip_output_subfolder": True,
    "weights": {"energy": 1.0, "speech": 0.5, "motion": 0.7, "chat": 1.3, "phrase": 1.2},
    "presets": {
        "boss kill": {"weights": {"energy": 0.6, "speech": 0.0, "motion": 1.4,
                                  "chat": 1.0, "phrase": 0.5},
                      "fit": "crop", "captions": "off", "length": 25},
        "commentary": {"weights": {"energy": 0.4, "speech": 1.2, "motion": 0.1,
                                   "chat": 0.5, "phrase": 1.6},
                       "fit": "blur", "captions": "karaoke", "length": 45},
        "reaction": {"weights": {"energy": 1.5, "speech": 0.8, "motion": 0.4,
                                 "chat": 1.4, "phrase": 0.8},
                     "fit": "blur", "captions": "karaoke", "length": 15},
    },
    "boost_phrases": ["first try", "first time", "clutch", "no way", "insane", "died",
                      "killed me", "watch this", "one shot", "almost", "finally", "worst",
                      "best", "wtf", "broken", "huge"],
    "skip_edges": 60,
    "min_clip": 12,
    # two concurrent renders measured 92% CPU against 31% for one, and bought 4% of the
    # wall clock: heat and fan noise for nothing. One render already fills every core.
    "workers": 1,
    "render_workers": 1,
    "settings_version": 2,
}

# Up to settings_version 1 the defaults rendered two clips at once. config.json is written
# on first run and by the GUI, so a stored "2" is an old default, not a choice: move it once.
CONFIG_MIGRATIONS = {2: lambda cfg: cfg.update({"workers": 1, "render_workers": 1})}

AUDIO_RATE = 16000
MAX_WORKERS = 2  # deliberately capped: a single render already saturates the CPU
# whisper holds one model on the GPU: reuse it, and let only one thread drive it
_MODEL_CACHE = {}
_ENGINE_LOCK = threading.Lock()
_ENCODER_PICK = None
_PROGRESS_LOCK = threading.Lock()
# children of a GUI process would otherwise each pop their own console window
NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}


# ---------------------------------------------------------------- plumbing

class Fail(Exception):
    pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise Fail(f"config.json is not valid JSON: {CONFIG_PATH}")
        cfg.update(stored)
        version = int(stored.get("settings_version", 1) or 1)
        if version < DEFAULT_CONFIG["settings_version"]:
            for step in range(version + 1, DEFAULT_CONFIG["settings_version"] + 1):
                if step in CONFIG_MIGRATIONS:
                    CONFIG_MIGRATIONS[step](cfg)
            cfg["settings_version"] = DEFAULT_CONFIG["settings_version"]
            save_config(cfg)
    else:
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def media_root(cfg):
    root = Path(cfg["media_root"])
    (root / "library").mkdir(parents=True, exist_ok=True)
    (root / "ready").mkdir(parents=True, exist_ok=True)
    return root


def find_exe(name):
    """Prefer the real binary over winget's PATH shim, which respawns outside our control."""
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        pattern = os.path.join(local, "Microsoft", "WinGet", "Packages", "*", "**", "bin", name + ".exe")
        for cand in glob.glob(pattern, recursive=True):
            return cand
    hit = shutil.which(name)
    if hit:
        return hit
    raise Fail(
        f"{name} not found. Install it (ffmpeg: winget install Gyan.FFmpeg) "
        f"or set \"ffmpeg\" in {CONFIG_PATH}."
    )


def ffmpeg_path(cfg):
    if cfg.get("ffmpeg"):
        return cfg["ffmpeg"]
    return find_exe("ffmpeg")


def ffprobe_path(cfg):
    sibling = Path(ffmpeg_path(cfg)).with_name("ffprobe.exe")
    if sibling.exists():
        return str(sibling)
    return find_exe("ffprobe")


PROGRESS_HOOK = None  # set by the GUI to stream child output into its log


class _Completed:
    """Stand-in for a finished subprocess when output was streamed instead of captured."""

    def __init__(self, returncode, output):
        self.returncode = returncode
        self.stdout = output
        self.stderr = output


def run(args, cwd=None, capture=True, stream=False):
    if stream and PROGRESS_HOOK is not None:
        return _run_streaming(args, cwd)
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **NO_WINDOW,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-12:])
        raise Fail(f"command failed ({proc.returncode}): {' '.join(str(a) for a in args[:6])} ...\n{tail}")
    return proc


def _run_streaming(args, cwd=None):
    """Same as run(), but forwards each output line so long downloads show progress."""
    proc = subprocess.Popen(
        args,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **NO_WINDOW,
    )
    collected, tail = [], []
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            collected.append(line)
            tail.append(line)
            tail = tail[-12:]
            try:
                PROGRESS_HOOK(line)
            except Exception:  # noqa: BLE001 - a broken hook must not kill the render
                pass
    proc.wait()
    output = "\n".join(collected)
    if proc.returncode != 0:
        raise Fail(f"command failed ({proc.returncode}): {' '.join(str(a) for a in args[:6])} ...\n"
                   + "\n".join(tail))
    return _Completed(proc.returncode, output)


ENCODER_FLAGS = {
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr",
                   "-cq", "21", "-b:v", "0", "-maxrate", "16M", "-bufsize", "32M",
                   "-profile:v", "high", "-pix_fmt", "yuv420p"],
    "h264_qsv": ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", "22",
                 "-pix_fmt", "nv12"],
    "h264_amf": ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp",
                 "-qp_i", "22", "-qp_p", "22", "-pix_fmt", "yuv420p"],
    "h264_videotoolbox": ["-c:v", "h264_videotoolbox", "-q:v", "60", "-pix_fmt", "yuv420p"],
    "h264_vaapi": ["-c:v", "h264_vaapi", "-qp", "22", "-pix_fmt", "nv12"],
    "libx264": ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-profile:v", "high", "-pix_fmt", "yuv420p"],
}
ENCODER_ORDER = ["h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox", "libx264"]


def working_encoders(cfg):
    """Which h264 encoders actually encode a frame on this machine, in preference order."""
    found = run([ffmpeg_path(cfg), "-hide_banner", "-encoders"]).stdout
    available, usable = [], []
    for name in ENCODER_ORDER:
        if name not in found:
            continue
        available.append(name)
        proc = subprocess.run(
            [ffmpeg_path(cfg), "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=black:s=256x144:d=0.2",
             "-frames:v", "2"] + ENCODER_FLAGS[name] + ["-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **NO_WINDOW)
        if proc.returncode == 0:
            usable.append(name)
    return available, usable


def pick_encoder(cfg):
    """Honour an explicit choice, otherwise test and remember what works here."""
    chosen = (cfg.get("encoder") or "auto").lower()
    if chosen != "auto" and chosen in ENCODER_FLAGS:
        return chosen
    global _ENCODER_PICK
    if _ENCODER_PICK:
        return _ENCODER_PICK
    with _ENGINE_LOCK:
        if _ENCODER_PICK:
            return _ENCODER_PICK
        _, usable = working_encoders(cfg)
        if not usable:
            raise Fail("no working h264 encoder found; install a full ffmpeg build")
        _ENCODER_PICK = usable[0]
    return _ENCODER_PICK


def encoder_args(cfg):
    name = pick_encoder(cfg)
    return name, ENCODER_FLAGS[name]


def open_folder(path):
    """Reveal a folder in whatever file manager this system has."""
    if os.name == "nt":
        subprocess.Popen(["explorer", str(path)], **NO_WINDOW)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def font_families():
    """Installed font families, from fontconfig or Tk; empty when neither is available."""
    families = []
    for cmd in (["fc-list", ":", "family"],):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=8, **NO_WINDOW)
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    families.extend(part.strip() for part in line.split(","))
        except (OSError, subprocess.SubprocessError):
            pass
    if not families:
        try:
            import tkinter
            from tkinter import font as tkfont
            root = tkinter.Tk()
            root.withdraw()
            families = list(tkfont.families(root))
            root.destroy()
        except Exception:  # noqa: BLE001 - no display, no font list
            pass
    return families


def pick_font(cfg):
    """Use the configured caption font if it exists, else the boldest substitute available."""
    wanted = cfg.get("font") or "Arial Black"
    families = font_families()
    if not families:
        return wanted
    lowered = {name.lower() for name in families}
    for candidate in (wanted, "Arial Black", "Impact", "Segoe UI Black", "Arial",
                      "Liberation Sans", "DejaVu Sans", "Verdana"):
        if candidate.lower() in lowered:
            return candidate
    return wanted


def parse_time(text):
    """Accept 5025, '83:45', '1:23:45', '1h2m3s', '1:23:45.5'."""
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().lower()
    if not s:
        raise Fail("empty time value")
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    if re.fullmatch(r"\d+(\.\d+)?h(\d+(\.\d+)?m)?(\d+(\.\d+)?s)?", s) or re.fullmatch(r"\d+m(\d+(\.\d+)?s)?", s):
        parts = re.findall(r"(\d+(?:\.\d+)?)([hms])", s)
        scale = {"h": 3600, "m": 60, "s": 1}
        return sum(float(v) * scale[u] for v, u in parts)
    if re.fullmatch(r"[\d:.]+", s):
        pieces = s.split(":")
        if len(pieces) == 2:
            return float(pieces[0]) * 60 + float(pieces[1])
        if len(pieces) == 3:
            return float(pieces[0]) * 3600 + float(pieces[1]) * 60 + float(pieces[2])
    raise Fail(f"cannot read time '{text}' (use 1:23:45, 83:45, or 5025)")


def fmt_ts(seconds):
    seconds = max(0.0, float(seconds))
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def ts_tag(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}h{m:02d}m{s:02d}s"


def slugify(text):
    text = os.path.basename(str(text)).rsplit(".", 1)[0]
    text = re.sub(r"[^\w\s.-]", "", text, flags=re.UNICODE).strip()
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-").lower() or "untitled"


def human_size(num):
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024


def srt_time(seconds):
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ---------------------------------------------------------------- library

def entries(cfg):
    root = media_root(cfg) / "library"
    return sorted([p for p in root.iterdir() if (p / "source.json").exists()], key=lambda p: p.name)


def find_entry(cfg, name):
    root = media_root(cfg) / "library"
    exact = root / name
    if (exact / "source.json").exists():
        return exact
    matches = [p for p in (root.iterdir() if root.exists() else []) if name.lower() in p.name.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise Fail("ambiguous name, matches: " + ", ".join(m.name for m in matches))
    raise Fail(f"no library entry '{name}'. Run `cf ls` to see what's indexed.")


def load_entry(cfg, name):
    entry = find_entry(cfg, name)
    meta = json.loads((entry / "source.json").read_text(encoding="utf-8"))
    return entry, meta


def save_entry(entry, meta):
    (entry / "source.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def probe(cfg, path):
    out = run([
        ffprobe_path(cfg),
        "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]).stdout
    data = json.loads(out)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    fps = 0.0
    if video and video.get("avg_frame_rate", "0/0") != "0/0":
        num, den = video["avg_frame_rate"].split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    return {
        "duration": float(data.get("format", {}).get("duration") or (video or {}).get("duration") or 0),
        "width": int(video["width"]) if video else 0,
        "height": int(video["height"]) if video else 0,
        "fps": round(fps, 3),
        "has_audio": audio is not None,
    }


def ytdlp_base(cfg):
    """yt-dlp needs to be told where ffmpeg lives; it can't see winget's PATH entry."""
    args = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--newline",
            "--ffmpeg-location", str(Path(ffmpeg_path(cfg)).parent)]
    if cfg.get("ytdlp_cookies_from_browser"):
        args += ["--cookies-from-browser", cfg["ytdlp_cookies_from_browser"]]
    return args


def fetch_remote_meta(cfg, url):
    args = ytdlp_base(cfg) + ["-J", "--no-playlist", url]
    info = json.loads(run(args).stdout)
    return {
        "title": info.get("title") or "untitled",
        "duration": float(info.get("duration") or 0),
        "uploader": info.get("uploader") or "",
    }


# ---------------------------------------------------------------- audio + transcript

def ensure_audio(cfg, entry, meta):
    wav = entry / "audio.wav"
    if wav.exists() and wav.stat().st_size > 1024:
        return wav

    src = meta["path"] if meta.get("kind") == "local" else None
    if src is None:
        raise Fail("remote source has no local audio yet; run `cf index` first")

    tmp = entry / "audio.part.wav"
    run([
        ffmpeg_path(cfg), "-hide_banner", "-loglevel", "error", "-y",
        "-i", src, "-vn", "-ac", "1", "-ar", str(AUDIO_RATE),
        "-c:a", "pcm_s16le", str(tmp),
    ])
    tmp.replace(wav)
    return wav


def whisper_model(cfg, model_name):
    """Load faster-whisper, preferring CUDA and falling back to CPU."""
    site = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    for sub in ("cublas/bin", "cudnn/bin", "cuda_nvrtc/bin"):
        d = site / sub
        if d.exists():
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
    os.add_dll_directory(str(site / "cublas" / "bin")) if (site / "cublas" / "bin").exists() and hasattr(os, "add_dll_directory") else None

    from faster_whisper import WhisperModel

    device = os.environ.get("CLIPFORGE_DEVICE", "")
    if device == "cpu":
        order = [("cpu", "int8"), ("cpu", "int8_float32"), ("cpu", "float32")]
    elif device == "cuda":
        order = [("cuda", "float16"), ("cuda", "int8_float16")]
    else:
        # try the GPU, then progressively cheaper CPU paths for weaker machines
        order = [("cuda", "float16"), ("cuda", "int8_float16"),
                 ("cpu", "int8"), ("cpu", "int8_float32"), ("cpu", "float32")]
    with _ENGINE_LOCK:
        if model_name in _MODEL_CACHE:
            return _MODEL_CACHE[model_name]
        last = None
        for dev, ctype in order:
            try:
                model = WhisperModel(model_name, device=dev, compute_type=ctype)
                _MODEL_CACHE[model_name] = (model, f"{dev}/{ctype}")
                return _MODEL_CACHE[model_name]
            except Exception as exc:  # noqa: BLE001 - cuda init failures are opaque
                last = exc
    raise Fail(f"could not load whisper model '{model_name}': {last}")


def download_audio(cfg, entry, meta):
    raw_tpl = entry / "rawaudio.%(ext)s"
    print(f"downloading audio for {entry.name} ...")
    run(ytdlp_base(cfg) + ["-f", "bestaudio/best", "--no-playlist", "-o", str(raw_tpl), meta["url"]],
        stream=True)
    produced = [p for p in entry.iterdir() if p.name.startswith("rawaudio.")]
    if not produced:
        raise Fail("audio download produced no file")
    src = produced[0]
    tmp = entry / "audio.part.wav"
    run([
        ffmpeg_path(cfg), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-vn", "-ac", "1", "-ar", str(AUDIO_RATE),
        "-c:a", "pcm_s16le", str(tmp),
    ])
    tmp.replace(entry / "audio.wav")
    src.unlink(missing_ok=True)


def transcribe(entry, meta, wav, model_name, language, prompt=None):
    """Run whisper under the engine lock and persist the transcript."""
    model, location = whisper_model(None, model_name)
    print(f"  device: {location}")
    with _ENGINE_LOCK:
        segments, info = model.transcribe(
            str(wav),
            language=language or "en",
            word_timestamps=True,
            vad_filter=True,
            beam_size=5,
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
        )
        segs, words = [], []
        total = meta.get("duration") or 0
        for seg in segments:
            segs.append({"start": round(seg.start, 3), "end": round(seg.end, 3),
                         "text": seg.text.strip()})
            for w in seg.words or []:
                token = w.word.strip()
                if token:
                    words.append({"start": round(w.start, 3), "end": round(w.end, 3),
                                  "word": token})
            if len(segs) % 25 == 0:
                print(f"  ...{fmt_ts(seg.end)} of {fmt_ts(total or info.duration)}", flush=True)

    (entry / "transcript.json").write_text(
        json.dumps({"language": info.language, "model": model_name,
                    "segments": segs, "words": words}, indent=1), encoding="utf-8")
    with open(entry / "transcript.srt", "w", encoding="utf-8") as fh:
        for i, seg in enumerate(segs, 1):
            fh.write(f"{i}\n{srt_time(seg['start'])} --> {srt_time(seg['end'])}\n{seg['text']}\n\n")
    return segs, words


def cmd_index(cfg, args):
    entry, meta = load_entry(cfg, args.name)
    if meta.get("kind") == "remote" and not (entry / "audio.wav").exists():
        download_audio(cfg, entry, meta)

    wav = ensure_audio(cfg, entry, meta)
    model_name = args.model or cfg["model"]
    print(f"transcribing {entry.name} with {model_name} ...")
    started = time.time()
    segs, words = transcribe(entry, meta, wav, model_name, args.language,
                             cfg.get("prompt"))
    print(f"done: {len(segs)} segments, {len(words)} words in {time.time() - started:.0f}s")
    print(f"  {entry / 'transcript.json'}")


def transcript_model(entry):
    """Which whisper model produced the cached transcript, if there is one."""
    path = Path(entry) / "transcript.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("model")
    except json.JSONDecodeError:
        return None


def load_transcript(entry):
    path = entry / "transcript.json"
    if not path.exists():
        raise Fail(f"{entry.name} is not transcribed yet. Run `cf index {entry.name}`.")
    return json.loads(path.read_text(encoding="utf-8"))


def search_segments(segments, query):
    terms = [t for t in re.split(r"\s+", query.lower().strip()) if t]
    if not terms:
        return []
    hits = []
    for seg in segments:
        text = seg["text"].lower()
        if all(t in text for t in terms):
            score = sum(text.count(t) for t in terms)
            hits.append((score, seg))
    hits.sort(key=lambda h: (-h[0], h[1]["start"]))
    return [seg for _, seg in hits]


def cmd_find(cfg, args):
    entry, meta = load_entry(cfg, args.name)
    data = load_transcript(entry)
    query = " ".join(args.query)
    hits = search_segments(data["segments"], query)
    if not hits:
        print(f"no match for '{query}' in {entry.name} ({len(data['segments'])} segments searched)")
        return
    print(f"{len(hits)} match(es) for '{query}' in {entry.name}:\n")
    for seg in hits[: args.limit]:
        before = [s for s in data["segments"] if s["end"] <= seg["start"]][-1:]
        ctx = " ".join(s["text"] for s in before + [seg])
        print(f"  {fmt_ts(seg['start']):>9}  {ctx.strip()}")
    print(f"\nuse: cf make {entry.name} --from {fmt_ts(hits[0]['start'])} --dur 30")


# ---------------------------------------------------------------- highlights

# ---------------------------------------------------------------- signals

def load_rms(entry, bucket=0.25):
    """Per-bucket RMS level in dB from the cached 16k mono wav."""
    import numpy as np

    wav = entry / "audio.wav"
    if not wav.exists():
        raise Fail(f"{entry.name} has no audio.wav; run `cf index {entry.name}` first")
    with wave.open(str(wav), "rb") as fh:
        rate = fh.getframerate()
        raw = fh.readframes(fh.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    step = max(1, int(rate * bucket))
    usable = (len(samples) // step) * step
    frames = samples[:usable].reshape(-1, step)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    return 20 * np.log10(rms + 1e-12), bucket


def motion_stamp(meta, fps, size):
    """Identity of a motion read: the source as it is on disk and the sampling used."""
    try:
        stat = Path(meta["path"]).stat()
    except OSError:
        return None
    return {"path": meta["path"], "size": stat.st_size, "mtime": round(stat.st_mtime, 3),
            "fps": fps, "box": list(size)}


def load_motion(cfg, entry, meta, fps=2, size=(64, 36)):
    """Per-frame visual activity, for footage where audio says nothing useful."""
    import numpy as np

    if meta.get("kind") != "local":
        raise Fail("visual analysis needs a local recording")
    w, h = size
    cache = entry / "motion.json"
    stamp = motion_stamp(meta, fps, size)
    if cache.exists() and stamp:
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            if all(saved.get(k) == v for k, v in stamp.items()):
                return (np.asarray(saved["change"], dtype=np.float32),
                        np.asarray(saved["brightness"], dtype=np.float32), 1.0 / fps)
        except (OSError, ValueError, KeyError, TypeError):
            pass
    proc = subprocess.run(
        [ffmpeg_path(cfg), "-hide_banner", "-loglevel", "error", "-i", meta["path"],
         "-an", "-vf", f"fps={fps},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, **NO_WINDOW,
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]
        raise Fail("could not decode video for motion analysis: " + (tail[0] if tail else ""))
    frames = np.frombuffer(proc.stdout, dtype=np.uint8)
    count = len(frames) // (w * h)
    if count < 8:
        raise Fail("video too short to analyse")
    frames = frames[: count * w * h].reshape(count, h, w).astype(np.float32)
    change = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    brightness = frames.mean(axis=(1, 2))[1:]
    if stamp:
        try:
            cache.write_text(json.dumps({
                **stamp,
                "change": [round(float(v), 3) for v in change],
                "brightness": [round(float(v), 2) for v in brightness],
            }), encoding="utf-8")
        except OSError:
            pass
    return change, brightness, 1.0 / fps


def normalize(values):
    """Put a signal on a comparable scale: median-centred, interquartile width, clipped."""
    import numpy as np

    arr = np.asarray(values, dtype=np.float32)
    if arr.size == 0:
        return arr
    median = float(np.median(arr))
    spread = float(np.percentile(arr, 75) - np.percentile(arr, 25)) or 1.0
    return np.clip((arr - median) / spread, -1.0, 6.0)


def resample(values, from_bucket, to_bucket):
    """Nearest-neighbour resample between sampling rates, so signals can be summed."""
    import numpy as np

    values = np.asarray(values, dtype=np.float32)
    if abs(from_bucket - to_bucket) < 1e-9 or values.size == 0:
        return values
    positions = np.arange(int(values.size * from_bucket / to_bucket)) * to_bucket
    idx = np.clip((positions / from_bucket).astype(int), 0, values.size - 1)
    return values[idx]


# ---------------------------------------------------------------- chat hype

TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
MIN_CHAT_MESSAGES = 5
VERSION = "0.1.1"
REPO = "TheBen420/ClipForge-Automate-Making-Clips-of-Your-Conten-"
REPO_URL = f"https://github.com/{REPO}"
MIN_CHAT_MESSAGES = 5
HYPE_TOKENS = ("lul", "lmao", "omg", "pog", "clip", "clipped", "no way", "holy", "wtf", "insane")


def twitch_video_id(url):
    match = re.search(r"(?:videos/|clip/)([A-Za-z0-9]+)", url or "")
    return match.group(1) if match else None


def fetch_twitch_chat(url, dest, on_line=None):
    """Best-effort chat replay download for a Twitch VOD. Returns the message list.

    Twitch pages replay chat by offset rather than by cursor for anonymous clients -
    asking for the next cursor now trips their integrity check, asking for the next
    hundred messages after a timestamp does not.
    """
    import urllib.request

    video_id = twitch_video_id(url)
    if not video_id or not video_id.isdigit():
        raise Fail("that link has no VOD id to pull chat for")

    query = ("query($id:ID!,$off:Int){video(id:$id){comments(contentOffsetSeconds:$off,first:100)"
             "{edges{node{contentOffsetSeconds message{fragments{text}}}}"
             " pageInfo{hasNextPage}}}}")
    messages, seen = [], set()
    offset = 0
    deadline = time.time() + float(os.environ.get("CLIPFORGE_CHAT_SECONDS", "180"))
    for page in range(400):
        if time.time() > deadline:
            if on_line:
                on_line(f"  chat: stopping at {len(messages)} messages (time budget)")
            break
        body = json.dumps({"query": query,
                           "variables": {"id": video_id, "off": offset}}).encode("utf-8")
        request = urllib.request.Request(
            "https://gql.twitch.tv/gql", data=body,
            headers={"Client-ID": TWITCH_CLIENT_ID, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        comments = ((payload.get("data") or {}).get("video") or {}).get("comments")
        if not comments:
            break
        fresh = 0
        for edge in comments["edges"]:
            node = edge["node"]
            stamp = float(node["contentOffsetSeconds"])
            fragments = (node.get("message") or {}).get("fragments") or []
            text = "".join(fragment.get("text") or "" for fragment in fragments)
            key = (round(stamp, 3), text)
            if key in seen:
                continue
            seen.add(key)
            messages.append({"t": stamp, "text": text})
            fresh += 1
            offset = max(offset, int(stamp))
        if on_line and page % 10 == 0:
            on_line(f"  chat: {len(messages)} messages…")
        if not fresh or not comments["pageInfo"]["hasNextPage"]:
            break
    messages.sort(key=lambda row: row["t"])
    if len(messages) < MIN_CHAT_MESSAGES:
        raise Fail(f"Twitch only kept {len(messages)} chat message(s) for that VOD - "
                   "not enough to weigh moments with")
    dest.write_text(json.dumps(messages), encoding="utf-8")
    return messages


def load_chat(entry):
    path = Path(entry) / "chat.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def chat_series(messages, bucket, size):
    """Messages per bucket, with hype words and shouty messages counted double."""
    import numpy as np

    series = np.zeros(size, dtype=np.float32)
    for message in messages:
        index = int(float(message.get("t", 0)) / bucket)
        if not 0 <= index < size:
            continue
        text = (message.get("text") or "").lower()
        weight = 1.0
        if any(token in text for token in HYPE_TOKENS):
            weight += 1.0
        if text.count("!") >= 2 or text.isupper():
            weight += 0.5
        series[index] += weight
    return series


# ---------------------------------------------------------------- analysis

def boost_hits(text, phrases):
    lowered = text.lower()
    return sum(1 for phrase in phrases if phrase and phrase in lowered)


def snap_bounds(data, peak, length, min_len, total, max_pre=3.0):
    """Start just before the peak and end on the first natural pause after it.

    A window of exactly `length` cuts mid-thought and can drop the line that explains
    what just happened. So the end is the first sentence boundary at or after the target
    that is followed by a real pause, searched up to `length * 0.6` past it - usually
    a few seconds, occasionally a dozen - and only if no pause turns up in that range
    does it fall back to the word boundary closest to the target.
    """
    segments = data.get("segments") or []
    start = max(0.0, peak - 1.5)
    for segment in segments:
        if segment["start"] <= peak <= segment["end"]:
            start = max(segment["start"], peak - max_pre)
            break

    target_end = start + length
    horizon = min(total, target_end + max(6.0, length * 0.6))
    end = None
    for index, segment in enumerate(segments):
        if segment["end"] < target_end - 1.0 or segment["start"] > horizon:
            continue
        if segment["end"] > horizon:
            break
        following = segments[index + 1] if index + 1 < len(segments) else None
        pause_after = following is None or (following["start"] - segment["end"]) >= 0.7
        if pause_after:
            end = segment["end"]
            break

    if end is None:
        cap = start + max(length * 1.4, length + 4.0)
        ends = [word["end"] for word in (data.get("words") or [])
                if start + min_len <= word["end"] <= cap]
        end = min(ends, key=lambda edge: abs(edge - target_end)) if ends else target_end

    end = min(total, max(end, start + min_len))
    return round(start, 2), round(end, 2)


def min_caption_words(mode):
    """Captioned clips want someone talking in them; captionless ones don't care."""
    return 4 if mode in ("karaoke", "block") else 0


def past_windows(cfg, entry_name):
    """Windows already cut for this recording, so a second run finds new material."""
    spans = []
    for path in sessions_dir(cfg).glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if data.get("entry") != entry_name:
            continue
        for clip in data.get("clips") or []:
            start, end = clip.get("start"), clip.get("end")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                spans.append((float(start), float(end)))
    return spans


def overlaps_past(start, end, spans, share=0.5):
    """True when most of this window was already turned into a clip."""
    for past_start, past_end in spans:
        overlap = min(end, past_end) - max(start, past_start)
        if overlap > 0 and overlap >= (end - start) * share:
            return True
    return False


def analyze(cfg, entry, meta, data, preset=None, weights=None, want=40, min_words=0, avoid=None):
    """Score candidate windows from every signal available, returning ranked moments.

    With min_words, windows carrying less speech than that are passed over, which keeps
    captioned clips from landing on a stretch where nobody is talking. With avoid, a list
    of (start, end) spans already clipped, those windows are passed over too - so running
    the same footage twice hands back new moments instead of the same clip again.
    """
    import numpy as np

    merged = dict(cfg.get("weights") or {})
    if preset and preset in (cfg.get("presets") or {}):
        merged.update((cfg["presets"][preset] or {}).get("weights") or {})
    if weights:
        merged.update(weights)

    learned = learned_multipliers(cfg)
    if learned:
        merged = {name: round(weight * learned.get(name, 1.0), 3)
                  for name, weight in merged.items()}

    rms, bucket = load_rms(entry)
    total = len(rms) * bucket
    min_len = float(cfg.get("min_clip", 12))
    if total < min_len:
        raise Fail(f"{entry.name} is only {total:.0f}s long - too short to find a moment in")

    signals = {"energy": normalize(rms)}

    words = data.get("words") or []
    if merged.get("speech", 0) > 0 and words:
        counts = np.zeros(len(rms), dtype=np.float32)
        for word in words:
            index = int(word["start"] / bucket)
            if 0 <= index < len(counts):
                counts[index] += 1
        window = max(1, int(4 / bucket))
        density = np.convolve(counts, np.ones(window) / window, mode="same")
        signals["speech"] = normalize(density)

    if merged.get("motion", 0) > 0:
        try:
            change, brightness, motion_bucket = load_motion(cfg, entry, meta)
            raw = change + 1.5 * np.clip(brightness - np.median(brightness), 0, None)
            signals["motion"] = resample(normalize(raw), motion_bucket, bucket)[:len(rms)]
        except Fail:
            pass

    if merged.get("chat", 0) > 0:
        messages = load_chat(entry)
        if messages:
            signals["chat"] = normalize(chat_series(messages, bucket, len(rms)))

    if merged.get("phrase", 0) > 0 and (cfg.get("boost_phrases") or []):
        hits = np.zeros(len(rms), dtype=np.float32)
        for segment in data.get("segments") or []:
            count = boost_hits(segment.get("text", ""), cfg["boost_phrases"])
            if not count:
                continue
            span = range(max(0, int(segment["start"] / bucket)),
                         min(len(hits), int(segment["end"] / bucket) + 1))
            for index in span:
                hits[index] += count
        if hits.any():
            signals["phrase"] = normalize(hits)

    combined = np.zeros(len(rms), dtype=np.float32)
    for name, series in signals.items():
        weight = float(merged.get(name, 0) or 0)
        if weight and len(series) >= len(combined):
            combined += weight * series[:len(combined)]
        elif weight:
            padded = np.zeros(len(combined), dtype=np.float32)
            padded[:len(series)] = series
            combined += weight * padded

    smooth = max(1, int(3 / bucket))
    combined = np.convolve(combined, np.ones(smooth) / smooth, mode="same")

    win = min(24.0, max(min_len, total * 0.55))
    hop = max(1.5, win / 8)
    edge = min(float(cfg.get("skip_edges", 60)), total * 0.1)
    usable_start = edge
    usable_end = total - edge

    candidates = []
    t = usable_start
    while t + win <= usable_end:
        i0, i1 = int(t / bucket), int((t + win) / bucket)
        chunk = combined[i0:i1]
        if len(chunk) >= 4:
            features = {name: float(np.mean(series[i0:i1]))
                        for name, series in signals.items() if len(series) >= i1}
            score = float(sum(float(merged.get(name, 0) or 0) * value
                              for name, value in features.items()) + 0.5 * chunk.max())
            peak = t + float(np.argmax(chunk)) * bucket
            candidates.append({"t": t, "peak": peak, "score": score, "features": features})
        t += hop

    if not candidates:
        raise Fail(f"{entry.name} has no usable stretch outside the first and last minute")

    features_all = {name: np.array([c["features"].get(name, 0.0) for c in candidates])
                    for name in signals}
    threshold = float(np.median(combined)) - 0.25
    alive = []
    for candidate in candidates:
        i0, i1 = int(candidate["t"] / bucket), int((candidate["t"] + win) / bucket)
        if float(np.mean(combined[i0:i1])) < threshold:
            continue
        alive.append(candidate)
    candidates = alive or candidates

    sorted_times = sorted(word["start"] for word in (data.get("words") or []))
    if min_words and not sorted_times:
        min_words = 0        # nothing transcribed: a speech floor would reject everything

    def words_between(low, high):
        import bisect

        return bisect.bisect_left(sorted_times, high) - bisect.bisect_left(sorted_times, low)

    candidates.sort(key=lambda c: -c["score"])
    results, taken = [], []
    for candidate in candidates:
        if candidate["peak"] > total - min_len:
            continue
        start, end = snap_bounds(data, candidate["peak"], win, min_len, total)
        if min_words and words_between(start, end) < min_words:
            continue
        if avoid and overlaps_past(start, end, avoid):
            continue
        if end - start < min_len * 0.5:
            continue
        if any(start < other[1] - 0.4 and end > other[0] + 0.4 for other in taken):
            continue
        taken.append((start, end))
        text = " ".join(s["text"] for s in data.get("segments") or []
                        if s["end"] > start and s["start"] < end).strip()
        results.append({
            "start": start,
            "end": end,
            "duration": round(end - start, 1),
            "score": round(candidate["score"], 3),
            "rank": len(results) + 1,
            "features": {name: round(value, 3) for name, value in candidate["features"].items()},
            "text": text,
        })
        if len(results) >= want:
            break

    if not results and min_words:
        print(f"  (no window had {min_words} spoken words - falling back to every signal)")
        return analyze(cfg, entry, meta, data, preset=preset, weights=weights, want=want,
                       avoid=avoid)
    if len(results) < want and avoid:
        print("  (every strong moment has already been clipped - reusing earlier windows)")
        return analyze(cfg, entry, meta, data, preset=preset, weights=weights, want=want,
                       min_words=min_words)

    summary = {"total": round(total, 1), "candidates": len(candidates), "window": round(win, 1),
               "weights": {name: round(float(weight), 3) for name, weight in merged.items()}}
    if learned:
        summary["learned"] = learned
    for name, values in features_all.items():
        if values.size:
            summary[name] = {"median": round(float(np.median(values)), 3),
                             "max": round(float(values.max()), 3),
                             "separation": round(float(values.max() / (abs(np.median(values)) or 1)), 2)}
    return results, summary


def score_windows(entry, data, cfg, meta=None, preset=None, want=12):
    """Backwards-compatible wrapper around analyze()."""
    ranked, _ = analyze(cfg, entry, meta, data, preset=preset, want=want)
    for item in ranked:
        item.setdefault("basis", "signals")
    return ranked


def cmd_suggest(cfg, args):
    entry, meta = load_entry(cfg, args.name)
    data = load_transcript(entry)
    ranked, summary = analyze(cfg, entry, meta, data, preset=args.preset, want=args.limit * 3)
    if not ranked:
        print("no candidates found")
        return
    (entry / "highlights.json").write_text(json.dumps(ranked, indent=2), encoding="utf-8")

    names = [n for n in ("energy", "speech", "motion", "chat", "phrase") if n in summary]
    weights = dict(cfg.get("weights") or {})
    if args.preset and args.preset in (cfg.get("presets") or {}):
        weights.update((cfg["presets"][args.preset] or {}).get("weights") or {})
    used = ", ".join(f"{n}×{weights.get(n, 0):g}" for n in names if weights.get(n))
    print(f"{len(ranked)} candidate moments in {entry.name}"
          f"{f' (preset {args.preset})' if args.preset else ''}")
    print(f"signals in play: {used or 'none'}\n")
    for i, item in enumerate(ranked[: args.limit], 1):
        detail = "  ".join(f"{k} {v:+.2f}" for k, v in item["features"].items())
        print(f"{i:2}. {fmt_ts(item['start'])}-{fmt_ts(item['end'])}  score {item['score']:.2f}")
        print(f"    {item['text'][:140] or '(no speech)'}")
        print(f"    {detail}")
        print(f"    cf make {entry.name} --from {fmt_ts(item['start'])} "
              f"--to {fmt_ts(item['end'])}\n")


def cmd_chat(cfg, args):
    entry, meta = load_entry(cfg, args.name)
    if args.file:
        messages = json.loads(Path(args.file).read_text(encoding="utf-8"))
        (entry / "chat.json").write_text(json.dumps(messages), encoding="utf-8")
        print(f"imported {len(messages)} chat messages from {args.file}")
        return
    if meta.get("kind") != "remote":
        raise Fail("chat download needs a Twitch VOD entry, or pass --file with a chat log")
    print(f"downloading chat for {entry.name} …")
    try:
        messages = fetch_twitch_chat(meta.get("url", ""), entry / "chat.json",
                                     on_line=print)
    except Exception as exc:  # noqa: BLE001 - network and API failures are opaque
        raise Fail(f"chat download failed: {exc}")
    print(f"saved {len(messages)} chat messages -> {entry / 'chat.json'}")


# ---------------------------------------------------------------- render

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0.6,0,1,7,0,2,60,60,{margin},1
"""


def ass_escape(text):
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def build_chunks(words, start, end, max_words=4, max_chars=30, max_gap=0.7, max_dur=3.4):
    """Group word timings into caption chunks that fit on screen."""
    picked = [w for w in words if w["start"] < end and w["end"] > start]
    chunks, current = [], []
    for word in picked:
        if current:
            gap = word["start"] - current[-1]["end"]
            span = word["end"] - current[0]["start"]
            text_len = len(" ".join(w["word"] for w in current + [word]))
            if len(current) >= max_words or gap > max_gap or span > max_dur or text_len > max_chars:
                chunks.append(current)
                current = []
        current.append(word)
    if current:
        chunks.append(current)
    return chunks


def write_ass(path, chunks, time_offset, cfg, canvas=(1080, 1920), mode="karaoke", caps=False):
    """Write burn-in subtitles. Word times are source-absolute; the clip starts at time_offset."""
    w, h = canvas
    font = cfg.get("_font") or pick_font(cfg)
    cfg["_font"] = font
    lines = [ASS_HEADER.format(w=w, h=h, font=font, size=cfg["font_size"],
                               margin=cfg["caption_margin_bottom"])]
    lines.append("[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")

    def stamp(t):
        t = max(0.0, t - time_offset)
        hh, rem = divmod(t, 3600)
        mm, ss = divmod(rem, 60)
        return f"{int(hh)}:{int(mm):02d}:{ss:05.2f}"

    for chunk in chunks:
        c_start, c_end = chunk[0]["start"], max(chunk[-1]["end"], chunk[0]["start"] + 0.35)
        text_words = [w["word"] for w in chunk]
        joined = " ".join(text_words)
        if caps:
            joined_caps = joined.upper()
        if mode == "karaoke":
            for index, word in enumerate(chunk):
                active = word["word"].upper() if caps else word["word"]
                rendered = []
                for token in text_words:
                    token = token.upper() if caps else token
                    if token == active:
                        rendered.append(f"{{\\c&H0000D4FF&\\fscx108\\fscy108}}{ass_escape(token)}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}")
                    else:
                        rendered.append(ass_escape(token))
                # hold the phrase until the next word starts, so captions never blink out
                w_end = chunk[index + 1]["start"] if index + 1 < len(chunk) else c_end
                w_end = max(w_end, word["start"] + 0.15)
                fade = "{\\fad(40,0)}" if index == 0 else ""
                lines.append(f"Dialogue: 0,{stamp(word['start'])},{stamp(w_end)},Caption,,0,0,0,,{fade}{' '.join(rendered)}\n")
        else:
            body = joined_caps if caps else joined
            lines.append(f"Dialogue: 0,{stamp(c_start)},{stamp(c_end)},Caption,,0,0,0,,{{\\fad(60,60)}}{ass_escape(body)}\n")
    Path(path).write_text("".join(lines), encoding="utf-8")


def build_video_filter(fit, src_w, src_h, fps, ass_name=None, focus=0.5):
    """Compose the vertical frame. The background is blurred at quarter size, which looks
    identical after scaling up but costs a fraction of a full-resolution blur."""
    canvas_w, canvas_h = 1080, 1920
    chain = f"[0:v]fps={fps},setsar=1,"
    if fit == "crop":
        crop_w = int(src_h * 9 / 16) // 2 * 2
        crop_w = min(crop_w, src_w) if src_w else crop_w
        x = int((src_w - crop_w) * focus) if src_w else 0
        graph = (chain + f"crop={crop_w}:{src_h}:{x}:0,"
                 f"scale={canvas_w}:{canvas_h}:flags=bicubic[v0]")
    else:
        dim = -0.10 if fit == "blur" else -0.45
        sigma = 7 if fit == "blur" else 14
        small_w = canvas_w // 4
        small_h = canvas_h // 4
        graph = (
            chain + "split=2[bg][fg];"
            f"[bg]scale={small_w}:{small_h}:force_original_aspect_ratio=increase,"
            f"crop={small_w}:{small_h},gblur=sigma={sigma},scale={canvas_w}:{canvas_h},"
            f"eq=brightness={dim}:saturation=0.85[b2];"
            f"[fg]scale={canvas_w}:-2:flags=bicubic[f2];"
            "[b2][f2]overlay=0:(H-h)/2[v0]"
        )
    if ass_name:
        return graph + f";[v0]ass={ass_name}[vout]", "[vout]"
    return graph, "[v0]"


def prepare_source(cfg, entry, meta, start, end, work):
    """Return (path, offset) for the source material covering [start, end]."""
    if meta.get("kind") == "local":
        path = Path(meta["path"])
        if not path.exists():
            raise Fail(f"source video missing: {path}")
        return path, start

    pad_before = 6.0
    dl_start = max(0.0, start - pad_before)
    dl_end = end + 4.0
    out = work / "section.%(ext)s"
    args = ytdlp_base(cfg) + [
        "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
        "--download-sections", f"*{dl_start:.2f}-{dl_end:.2f}",
        "--force-keyframes-at-cuts",
        "-o", str(out), meta["url"],
    ]
    print(f"downloading {fmt_ts(dl_start)}-{fmt_ts(dl_end)} from stream ...")
    for attempt in range(3):
        try:
            run(args, cwd=work, stream=True)
            produced = [p for p in work.glob("section.*")
                        if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")]
            if produced:
                return produced[0], start - dl_start
            raise Fail("download produced no video file")
        except Fail:
            if attempt == 2:
                raise
            print(f"  section download failed - retrying ({attempt + 2}/3)")
            for stale in work.glob("section.*"):
                stale.unlink(missing_ok=True)
    raise Fail("download produced no video file")


def audio_peak_db(cfg, entry):
    """Loudest moment in a library entry's audio, so a silent track can be named as such."""
    wav = Path(entry) / "audio.wav"
    if not wav.exists():
        return None
    proc = subprocess.run(
        [ffmpeg_path(cfg), "-hide_banner", "-nostats", "-i", str(wav),
         "-af", "volumedetect", "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", **NO_WINDOW,
    )
    match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB", proc.stderr or "")
    return float(match.group(1)) if match else None


def source_peak_db(cfg, source, offset, duration):
    """Peak level of the section about to be rendered, so faint tracks can be judged."""
    proc = subprocess.run(
        [ffmpeg_path(cfg), "-hide_banner", "-nostats", "-ss", f"{offset:.3f}",
         "-t", f"{duration:.3f}", "-i", str(source), "-vn",
         "-af", "volumedetect", "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", **NO_WINDOW,
    )
    match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB", proc.stderr or "")
    return float(match.group(1)) if match else 0.0


def render(cfg, args, entry, meta, start, end, work, out_path):
    duration = end - start
    source, offset = prepare_source(cfg, entry, meta, start, end, work)
    info = probe(cfg, source)
    fps = info["fps"]
    if getattr(args, "fps", None):
        out_fps = int(args.fps)
    elif fps >= 50:
        out_fps = 60
    else:
        out_fps = 30

    ass_name = None
    caption_mode = args.captions or cfg["captions"]
    if caption_mode in ("karaoke", "block"):
        data = load_transcript(entry)
        chunks = build_chunks(data["words"], start, end)
        if chunks:
            write_ass(work / "subs.ass", chunks, start, cfg,
                      mode=caption_mode, caps=args.caps)
            ass_name = "subs.ass"
        else:
            print("  (no speech in this range, rendering without captions)")

    graph, out_label = build_video_filter(args.fit or cfg["fit"], info["width"], info["height"],
                                          out_fps, ass_name, args.focus)

    common = ["-hide_banner", "-loglevel", "error", "-y", "-ss", f"{offset:.3f}", "-t", f"{duration:.3f}",
              "-i", str(source), "-filter_complex", graph, "-map", out_label]
    audio = ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    if info["has_audio"] and source_peak_db(cfg, source, offset, duration) > -45.0:
        common += ["-map", "0:a:0?"] + audio
    elif info["has_audio"]:
        print("  (source audio is below -45 dB peak - that is noise floor, not sound; "
              "rendering without it)")
        common += ["-an"]
    else:
        common += ["-an"]
    tail = ["-movflags", "+faststart", "-shortest", str(out_path)]

    name, flags = encoder_args(cfg)
    try:
        run([ffmpeg_path(cfg)] + common + flags + tail, cwd=work)
    except Fail as exc:
        if name == "libx264":
            raise
        print(f"  {name} failed, falling back to libx264")
        print(f"  {str(exc).splitlines()[0]}")
        run([ffmpeg_path(cfg)] + common + ENCODER_FLAGS["libx264"] + tail, cwd=work)


def write_post_md(path, cfg, entry, meta, start, end, data, clip_path):
    duration = end - start
    window = [s for s in data["segments"] if s["end"] > start and s["start"] < end]
    transcript = "\n".join(f"[{fmt_ts(s['start'])}] {s['text']}" for s in window)
    inside = [s for s in window if s["start"] >= start - 0.3]
    hook = (inside or window)[0]["text"] if window else ""
    full = " ".join(s["text"] for s in window).strip()
    tags = " ".join(dict.fromkeys(list(cfg["hashtags"]) + [meta.get("game_tag", "")])).strip()
    body = f"""# {meta.get('title') or entry.name} - {fmt_ts(start)} to {fmt_ts(end)} ({duration:.0f}s)

Source: {meta.get('path') or meta.get('url')}
Files: {clip_path.name} (1080x1920), {clip_path.stem}-thumb.jpg, {clip_path.stem}.srt

## Hook candidates
- "{hook}"
- "{full[:110]}"

## Description
{full[:280]}

{tags}

## Full transcript
{transcript}
"""
    Path(path).write_text(body, encoding="utf-8")


def clamp_window(data, total, start, end, pad=0.0, snap=False):
    """Apply padding and, optionally, grow the window to whole transcript sentences."""
    if snap and data.get("segments"):
        edge = 3.0
        near_start = [s for s in data["segments"] if abs(s["start"] - start) <= edge]
        if near_start:
            start = min(near_start, key=lambda s: abs(s["start"] - start))["start"]
        near_end = [s for s in data["segments"] if abs(s["end"] - end) <= edge]
        if near_end:
            end = min(near_end, key=lambda s: abs(s["end"] - end))["end"]
    start = max(0.0, start - pad)
    end = min(total, end + pad) if total else end + pad
    if end - start < 1.0:
        raise Fail("clip range is under a second")
    return start, end


def short_stamp(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def safe_name(text, limit=70):
    cleaned = re.sub(r'[\\/:*?"<>|]', "", str(text)).strip(" .")
    if len(cleaned) > limit:  # long VOD titles would blow past the Windows path limit
        cut = cleaned[:limit]
        cleaned = cut.rsplit(" ", 1)[0] if " " in cut[limit // 2:] else cut
    return cleaned.rstrip(" .-") or "clip"


def unique_stem(folder, stem):
    """Keep two source videos that share a name from overwriting each other."""
    candidate, n = stem, 2
    while any((folder / f"{candidate}{ext}").exists() for ext in (".mp4", ".srt", ".txt")):
        candidate = f"{stem} ({n})"
        n += 1
    return candidate


def render_clip(cfg, entry, meta, data, start, end, args, flat_dir=None):
    """Cut, reframe, caption and export one clip. Returns the clip file itself.

    With flat_dir, the clip and its sidecars land directly in that folder, named
    after the source video; otherwise each clip gets its own folder under ready/.
    """
    total = meta.get("duration") or (data["segments"][-1]["end"] if data["segments"] else 0)
    start, end = clamp_window(data, total, start, end, args.pad or 0.0, getattr(args, "snap", False))

    root = media_root(cfg)
    work = Path(tempfile.mkdtemp(prefix="cf-"))
    print(f"rendering {entry.name} [{fmt_ts(start)} -> {fmt_ts(end)}] = {end - start:.0f}s")
    try:
        clip = work / "clip.mp4"
        render(cfg, args, entry, meta, start, end, work, clip)
        variant = []
        if getattr(args, "fit", None) and args.fit != cfg["fit"]:
            variant.append(args.fit)
        if getattr(args, "captions", None) and args.captions != cfg["captions"]:
            variant.append("nocap" if args.captions == "off" else args.captions)
        suffix = "-" + "-".join(variant) if variant else ""
        if flat_dir:
            suffix = ""  # one setting per run, so the variant is already implied

        if flat_dir:
            target = Path(flat_dir)
            target.mkdir(parents=True, exist_ok=True)
            stem = unique_stem(target, f"{safe_name(meta.get('title') or entry.name)} "
                                       f"{short_stamp(start)}-{short_stamp(end)}{suffix}")
            final = target / f"{stem}.mp4"
            thumb = target / f"{stem}-thumb.jpg"
            srt_path = None  # the batch folder holds the clip and its cover only
            md = None
        else:
            target = root / "ready" / entry.name / f"{ts_tag(start)}-{ts_tag(end)}{suffix}"
            target.mkdir(parents=True, exist_ok=True)
            stem = f"{safe_name(meta.get('title') or entry.name)} " \
                   f"{short_stamp(start)}-{short_stamp(end)}"
            final = target / f"{stem}.mp4"
            thumb = target / f"{stem}-thumb.jpg"
            srt_path = target / f"{stem}.srt"
            md = target / f"{stem}.txt"

        shutil.copy2(clip, final)
        run([ffmpeg_path(cfg), "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{min(2.0, (end - start) / 2):.2f}", "-i", str(final),
             "-frames:v", "1", "-q:v", "2", str(thumb)])

        if srt_path:
            with open(srt_path, "w", encoding="utf-8") as fh:
                for i, chunk in enumerate(build_chunks(data["words"], start, end, max_words=12, max_chars=60), 1):
                    fh.write(f"{i}\n{srt_time(chunk[0]['start'] - start)} --> "
                             f"{srt_time(chunk[-1]['end'] - start)}\n"
                             f"{' '.join(w['word'] for w in chunk)}\n\n")

        if md:
            write_post_md(md, cfg, entry, meta, start, end, data, final)

        print(f"done -> {final}  ({human_size(final.stat().st_size)})")
        print(f"  {thumb}" + (f"\n  {srt_path}\n  {md}" if srt_path else ""))
        if getattr(args, "open", False):
            open_folder(target)
        return final
    finally:
        shutil.rmtree(work, ignore_errors=True)


def render_many(cfg, entry, meta, data, items, ns, flat_dir=None, workers=None):
    """Render several clips at once: ffmpeg decodes on the CPU while NVENC encodes on the GPU."""
    limit = max(1, min(MAX_WORKERS, int(workers or cfg.get("render_workers", 1))))
    done, failed = {}, []

    def one(item):
        return render_clip(cfg, entry, meta, data, item["start"], item["end"], ns,
                           flat_dir=flat_dir)

    with ThreadPoolExecutor(max_workers=limit) as pool:
        futures = {pool.submit(one, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                done[item["rank"]] = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad clip must not stop the rest
                failed.append((item, exc))
    return [done[rank] for rank in sorted(done)], failed


def cmd_make(cfg, args):
    entry, meta = load_entry(cfg, args.name)
    data = load_transcript(entry)
    if not args.start:
        raise Fail("need --from (and --to or --dur)")
    start = parse_time(args.start)
    if args.end:
        end = parse_time(args.end)
    elif args.duration:
        end = start + parse_time(args.duration)
    else:
        end = start + 30.0
    render_clip(cfg, entry, meta, data, start, end, args)


def cmd_auto(cfg, args):
    """Render the top-ranked moments with no timestamps supplied."""
    started = time.time()
    entry, meta = load_entry(cfg, args.name)
    data = load_transcript(entry)
    ranked, summary = analyze(cfg, entry, meta, data, preset=args.preset, want=args.top * 2,
                             avoid=past_windows(cfg, entry.name))
    if not ranked:
        raise Fail("no candidate moments found")
    (entry / "highlights.json").write_text(json.dumps(ranked, indent=2), encoding="utf-8")
    preset = (cfg.get("presets") or {}).get(args.preset or "") or {}
    fit = args.fit or preset.get("fit")
    captions = args.captions or preset.get("captions")

    picks = ranked[: args.top]
    ns = argparse.Namespace(
        name=entry.name, start=None, end=None, duration=None, pad=0.2,
        fit=fit, captions=captions, caps=args.caps, focus=args.focus,
        fps=args.fps, snap=True, open=False,
    )
    made, failed = render_many(cfg, entry, meta, data, picks, ns)
    for item, exc in failed:
        print(f"  skipped {fmt_ts(item['start'])}: {exc}")
    records = [{"path": str(path), "start": item["start"], "end": item["end"],
                "score": item["score"], "rank": item["rank"], "features": item["features"]}
               for item, path in zip(picks, made)]
    session = write_session(cfg, "cli-auto", meta.get("path") or entry.name, entry.name,
                            records, summary, time.time() - started,
                            {"preset": args.preset or None})
    print(f"\n{len(made)} clip(s) ready in {media_root(cfg) / 'ready' / entry.name}")
    print(f"session recorded -> {session}")
    if args.open and made:
        open_folder(made[0].parent)


def cmd_batch(cfg, args):
    path = Path(args.list_file)
    if not path.exists():
        raise Fail(f"no such batch file: {path}")
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            row = [c.strip() for c in row]
            if row and row[0] and not row[0].startswith("#"):
                rows.append(row)
    if not rows:
        raise Fail("batch file is empty")

    header = [c.lower() for c in rows[0]]
    has_header = any(h in ("start", "from", "begin") for h in header)
    if has_header:
        idx_start = next(i for i, h in enumerate(header) if h in ("start", "from", "begin"))
        idx_end = next((i for i, h in enumerate(header) if h in ("end", "to")), None)
        idx_dur = next((i for i, h in enumerate(header) if h in ("dur", "duration", "len")), None)
        rows = rows[1:]
    else:
        idx_start, idx_end, idx_dur = 0, 1, None

    print(f"batch: {len(rows)} clip(s) from {args.name}")
    failures = 0
    for n, row in enumerate(rows, 1):
        ns = argparse.Namespace(
            name=args.name, start=row[idx_start], end=None, duration=None, pad=0.0,
            fit=args.fit, captions=args.captions, caps=args.caps, focus=args.focus,
            fps=args.fps, open=False,
        )
        if idx_dur is not None and len(row) > idx_dur and row[idx_dur]:
            ns.duration = row[idx_dur]
        elif idx_end is not None and len(row) > idx_end and row[idx_end]:
            ns.end = row[idx_end]
        else:
            ns.duration = "30"
        print(f"\n[{n}/{len(rows)}]", end=" ")
        try:
            cmd_make(cfg, ns)
        except Fail as exc:
            failures += 1
            print(f"FAILED: {exc}")
    print(f"\nbatch finished: {len(rows) - failures} ok, {failures} failed")


# ---------------------------------------------------------------- telemetry

def sessions_dir(cfg):
    path = media_root(cfg) / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def feedback_path(cfg):
    return media_root(cfg) / "feedback.jsonl"


def write_session(cfg, mode, source, entry, clips, signals, seconds, extra=None):
    """Record a run: which signals fired, what they scored, and what came out."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    payload = {
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode,
        "source": str(source),
        "entry": entry,
        "seconds": round(seconds, 1),
        "signals": signals,
        "clips": clips,
    }
    payload.update(extra or {})
    path = sessions_dir(cfg) / f"{stamp}-{slugify(entry or source)}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def record_feedback(cfg, clip, verdict, record):
    """Append a keep/reject judgement next to the scores that produced the clip."""
    entry = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "clip": str(clip),
             "verdict": verdict}
    entry.update({k: v for k, v in (record or {}).items() if k not in ("when", "clip", "verdict")})
    with open(feedback_path(cfg), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def load_feedback(cfg):
    path = feedback_path(cfg)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def signal_split(feedback):
    """Per-signal kept vs rejected means, how far apart they sit, and the sample counts."""
    import numpy as np

    names = set()
    for row in feedback:
        names.update((row.get("features") or {}).keys())
    table = {}
    for name in sorted(names):
        kept = [row["features"][name] for row in feedback
                if row.get("verdict") == "keep" and row.get("features", {}).get(name) is not None]
        dropped = [row["features"][name] for row in feedback
                   if row.get("verdict") == "reject" and row.get("features", {}).get(name) is not None]
        if not kept or not dropped:
            continue
        kept_mean, dropped_mean = float(np.mean(kept)), float(np.mean(dropped))
        if max(kept_mean, dropped_mean) < 1e-6:
            continue
        table[name] = {
            "kept": round(kept_mean, 3),
            "rejected": round(dropped_mean, 3),
            # signed and scale-free: +1 means the signal is pure signal, 0 means it is noise
            "separation": round((kept_mean - dropped_mean) / (kept_mean + dropped_mean + 1e-6), 2),
            "n_kept": len(kept),
            "n_rejected": len(dropped),
        }
    return table


def signal_separation(feedback):
    """Which signal actually separates kept clips from rejected ones."""
    return {name: {"kept": row["kept"], "rejected": row["rejected"],
                   "separation": row["separation"]}
            for name, row in signal_split(feedback).items()}


def learned_multipliers(cfg, min_each=3, strength=0.6, floor=0.5, ceiling=1.5):
    """Scale each signal's baseline weight by how well it separated your judgements.

    Each signal's kept and rejected means are compared on a signed, scale-free scale:
    +1 when only kept clips score on it, 0 when the two are indistinguishable. The weight
    moves by `strength` times that separation, is clamped to [floor, ceiling] so one run
    of lucky clips cannot take over, and stays put until `min_each` clips of each kind
    exist. Signals with no data at all are left on their baseline.
    """
    multipliers = {}
    for name, row in signal_split(load_feedback(cfg)).items():
        if row["n_kept"] < min_each or row["n_rejected"] < min_each:
            continue
        moved = 1.0 + strength * row["separation"]
        multipliers[name] = round(min(max(moved, floor), ceiling), 2)
    return multipliers


def cmd_stats(cfg, args):
    sessions = sorted(sessions_dir(cfg).glob("*.json"))
    feedback = load_feedback(cfg)
    made = 0
    modes = {}
    for path in sessions:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        made += len(data.get("clips") or [])
        modes[data.get("mode", "?")] = modes.get(data.get("mode", "?"), 0) + 1

    print("ClipForge selection report\n")
    print(f"Sessions: {len(sessions)}  ({', '.join(f'{k} {v}' for k, v in modes.items()) or 'none'})")
    print(f"Clips rendered: {made}")

    if not feedback:
        print("\nNo feedback yet - judge a few clips in the app (Keep / Reject buttons) "
              "and this report will show which signals earn their weight.")
        return

    judged = len(feedback)
    kept = sum(1 for row in feedback if row.get("verdict") == "keep")
    print(f"\nFeedback: {judged} clip(s) judged - {kept} kept ({kept / judged * 100:.0f}%)")
    by_rank = {}
    for row in feedback:
        rank = row.get("rank")
        if rank is None:
            continue
        slot = by_rank.setdefault(int(rank), [0, 0])
        slot[1] += 1
        if row.get("verdict") == "keep":
            slot[0] += 1
    for rank in sorted(by_rank)[:args.ranks]:
        good, seen = by_rank[rank]
        print(f"  rank {rank}: kept {good}/{seen} ({good / seen * 100:.0f}%)")

    separation = signal_separation(feedback)
    if separation:
        print("\nSignal separation (mean score of kept vs rejected clips):")
        print(f"  {'signal':<9} {'kept':>7} {'rejected':>9} {'separation':>11}")
        for name, row in sorted(separation.items(), key=lambda kv: -kv[1]["separation"]):
            print(f"  {name:<9} {row['kept']:>7.2f} {row['rejected']:>9.2f} {row['separation']:>11.2f}")

        print("\nSeparation runs from -1 to 1: near 1 the signal picks what you keep and "
              "nothing else, near 0 it cannot tell them apart, and negative means it is "
              "picking clips you reject.")

    multipliers = learned_multipliers(cfg)
    base = dict(cfg.get("weights") or {})
    split = signal_split(feedback)
    print("\nWeights the next run will use:")
    print(f"  {'signal':<9} {'baseline':>9} {'learned':>8} {'effective':>10}  basis")
    for name in sorted(base):
        multiplier = multipliers.get(name, 1.0)
        row = split.get(name) or {}
        basis = (f"{row.get('n_kept', 0)} kept / {row.get('n_rejected', 0)} rejected"
                 if row else "no usable values yet")
        print(f"  {name:<9} {base[name]:>9.2f} {multiplier:>8.2f} {base[name] * multiplier:>10.2f}  {basis}")
    waiting = [name for name in sorted(base) if name not in multipliers]
    if waiting:
        print(f"\n{", ".join(waiting)} still run on the baseline - a signal only moves "
              "once it has 3 kept and 3 rejected clips behind it.")

    if sessions:
        latest = json.loads(sessions[-1].read_text(encoding="utf-8"))
        print(f"\nMost recent session: {sessions[-1].name}")
        print(f"  {latest.get('mode')} • {latest.get('entry')} • "
              f"{len(latest.get('clips') or [])} clip(s)")
        for name, row in (latest.get("signals") or {}).items():
            if isinstance(row, dict):
                print(f"  {name:<9} median {row.get('median')}  max {row.get('max')}  "
                      f"separation {row.get('separation')}")


# ---------------------------------------------------------------- library ops

def free_entry_dir(root, name, signature):
    """Never let a second video with the same filename take over an existing entry."""
    candidate, counter = name, 2
    while True:
        entry = root / candidate
        meta_path = entry / "source.json"
        if not meta_path.exists():
            return entry
        try:
            existing = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return entry
        if (existing.get("path") or existing.get("url")) == signature:
            return entry
        candidate = f"{name} ({counter})"
        counter += 1


def cmd_add(cfg, args):
    source = args.source
    is_url = bool(re.match(r"^https?://", source, re.I))
    root = media_root(cfg) / "library"

    if is_url:
        print(f"reading stream info for {source} ...")
        info = fetch_remote_meta(cfg, source)
        name = args.name or slugify(info["title"])
        entry = free_entry_dir(root, name, source)
        entry.mkdir(parents=True, exist_ok=True)
        meta = {"kind": "remote", "url": source, "title": info["title"],
                "uploader": info.get("uploader", ""), "duration": info["duration"],
                "added": time.strftime("%Y-%m-%d %H:%M")}
    else:
        path = Path(source).expanduser().resolve()
        if not path.exists():
            raise Fail(f"no such file: {path}")
        name = args.name or slugify(path.name)
        entry = free_entry_dir(root, name, str(path))
        entry.mkdir(parents=True, exist_ok=True)
        print(f"probing {path.name} ...")
        info = probe(cfg, path)
        meta = {"kind": "local", "path": str(path), "title": path.stem,
                "duration": info["duration"], "width": info["width"],
                "height": info["height"], "fps": info["fps"],
                "added": time.strftime("%Y-%m-%d %H:%M")}

    save_entry(entry, meta)
    name = entry.name
    print(f"added '{name}': {fmt_ts(meta['duration'])}"
          + (f" {info['width']}x{info['height']}" if not is_url else ""))
    print(f"  next: cf index {name}")


def cmd_ls(cfg, args):
    items = entries(cfg)
    if not items:
        print("library is empty. `cf add <file-or-url>` to start.")
        return
    print(f"{'name':<44} {'length':>9}  {'indexed':<18} {'kind':<7}")
    for entry in items:
        meta = json.loads((entry / "source.json").read_text(encoding="utf-8"))
        if (entry / "transcript.json").exists():
            data = json.loads((entry / "transcript.json").read_text(encoding="utf-8"))
            indexed = f"{len(data['segments'])} segs"
        elif (entry / "highlights.json").exists():
            indexed = "highlights only"
        else:
            indexed = "-"
        print(f"{entry.name[:44]:<44} {fmt_ts(meta.get('duration') or 0):>9}  {indexed:<18} {meta.get('kind',''):<7}")


def cmd_new(cfg, args):
    """Pick up recordings that finished since the last scan and index them."""
    state_path = media_root(cfg) / "seen.json"
    seen = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    extensions = (".mp4", ".mkv", ".flv", ".mov", ".ts", ".webm")
    found = []
    for watch in cfg["watch_dirs"]:
        base = Path(watch)
        if not base.exists():
            continue
        for file in base.rglob("*"):
            if file.suffix.lower() not in extensions or not file.is_file():
                continue
            key = str(file.resolve())
            if key in seen:
                continue
            age = time.time() - file.stat().st_mtime
            if age < (args.min_age or 120):
                continue
            if file.stat().st_size < 20 * 1024 * 1024:
                continue
            found.append(file)
    if not found:
        print("no new recordings found")
        return
    for file in found:
        print(f"\n=== {file.name} ({human_size(file.stat().st_size)})")
        try:
            cmd_add(cfg, argparse.Namespace(source=str(file), name=None))
            entry = find_entry(cfg, slugify(file.name))
            if not args.no_index:
                cmd_index(cfg, argparse.Namespace(name=entry.name, model=None, language=None))
            seen[str(file.resolve())] = time.strftime("%Y-%m-%d %H:%M")
        except Fail as exc:
            print(f"  skipped: {exc}")
    state_path.write_text(json.dumps(seen, indent=2), encoding="utf-8")



# ---------------------------------------------------------------- updates

def parse_version(text):
    """'1.2.3' -> (1, 2, 3), so versions compare as numbers instead of strings."""
    parts = re.findall(r"\d+", text or "")
    return tuple(int(part) for part in parts[:3]) or (0,)


def remote_version(timeout=5.0):
    """The version published on the main branch, or None when the check can't be made.

    The file is fetched at its commit sha rather than at the branch name: GitHub's CDN
    keeps serving the previous build from a branch URL for minutes after a push, which
    would have the check reporting an older build than the repo actually holds.
    """
    import urllib.request

    headers = {"User-Agent": f"clipforge/{VERSION}", "Accept": "application/vnd.github+json"}
    sha = None
    try:
        api = f"https://api.github.com/repos/{REPO}/commits/main"
        with urllib.request.urlopen(
                urllib.request.Request(api, headers=headers), timeout=timeout) as response:
            sha = json.loads(response.read().decode("utf-8")).get("sha")
    except Exception:  # noqa: BLE001 - the raw URL below is a usable fallback
        sha = None

    urls = []
    if sha:
        urls.append(f"https://raw.githubusercontent.com/{REPO}/{sha}/clipforge.py")
    urls.append(f"https://raw.githubusercontent.com/{REPO}/main/clipforge.py")
    for url in urls:
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers), timeout=timeout) as response:
                source = response.read().decode("utf-8", errors="replace")
            break
        except Exception:  # noqa: BLE001 - try the next URL
            continue
    else:
        return None

    match = re.search(r'^VERSION = "([0-9][0-9.]*)"', source, re.MULTILINE)
    return match.group(1) if match else None


def update_status(cfg, max_age=12 * 3600):
    """Compare against the published version, remembering the answer for half a day."""
    import urllib.request  # noqa: F401 - imported so a missing network fails here, not later

    cache_path = media_root(cfg) / "update-check.json"
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if time.time() - float(cached.get("at", 0)) < max_age:
            return cached.get("latest"), cached.get("checked", True)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    try:
        latest = remote_version()
    except Exception:  # noqa: BLE001 - offline is normal, not an error worth showing
        return None, False
    if latest:
        try:
            cache_path.write_text(json.dumps({"at": time.time(), "latest": latest}),
                                  encoding="utf-8")
        except OSError:
            pass
    return latest, latest is not None


def update_available(cfg):
    """The published version when it is newer than this one, otherwise None."""
    latest, checked = update_status(cfg)
    if checked and latest and parse_version(latest) > parse_version(VERSION):
        return latest
    return None


def cmd_update(cfg, args):
    print(f"ClipForge {VERSION}")
    latest, checked = update_status(cfg, max_age=0 if args.fresh else 12 * 3600)
    if not checked:
        print("Couldn't reach GitHub - check the connection and try again.")
        return 1
    if latest and parse_version(latest) > parse_version(VERSION):
        print(f"Version {latest} is out: {REPO_URL}")
        print("Download the zip, extract it, and run Install ClipForge again - it upgrades in place.")
        return 0
    print(f"Up to date (published version is {latest or 'unknown'}).")
    return 0


def cmd_doctor(cfg, args):
    ok = True

    def check(label, fn):
        nonlocal ok
        try:
            print(f"  [ok]   {label}: {fn()}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  [FAIL] {label}: {exc}")

    print("clipforge doctor\n")
    check("ffmpeg", lambda: run([ffmpeg_path(cfg), "-version"]).stdout.splitlines()[0])
    def encoder_report():
        available, usable = working_encoders(cfg)
        if not usable:
            raise Fail("no working h264 encoder")
        chosen = pick_encoder(cfg)
        others = [name for name in usable if name != chosen]
        return f"{chosen} (tested ok; also usable: {', '.join(others) or 'none'})"

    check("encoder", encoder_report)
    check("subtitles font", lambda: pick_font(cfg))
    check("libass subtitles", lambda: "available" if " ass " in
          run([ffmpeg_path(cfg), "-hide_banner", "-filters"]).stdout else "MISSING")
    check("yt-dlp", lambda: run([sys.executable, "-m", "yt_dlp", "--version"]).stdout.strip())
    check("whisper", lambda: (lambda m: f"loaded on {m[1]}")(whisper_model(cfg, cfg["model"])))
    root = media_root(cfg)
    free = shutil.disk_usage(root).free
    print(f"  [ok]   media root: {root} ({human_size(free)} free)")
    watches = [w for w in cfg["watch_dirs"] if Path(w).exists()]
    print(f"  [ok]   watch dirs: {', '.join(watches) if watches else 'none exist'}")
    print(f"\n{'all good' if ok else 'fix the failures above'}")
    return 0 if ok else 1


def latest_session(cfg):
    """The most recent run record on disk, or None."""
    folder = sessions_dir(cfg)
    files = sorted(folder.glob("*.json"))
    return files[-1] if files else None


def review_records(cfg, target=None):
    """Clips worth judging: from one session, a folder of them, or the newest run."""
    if target:
        path = Path(target)
        if not path.exists():
            raise Fail(f"no such session or folder: {path}")
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = [dict(row) for row in payload.get("clips") or []]
        else:
            rows = [{"path": str(item)} for item in sorted(path.glob("*.mp4"))]
    else:
        session = latest_session(cfg)
        if not session:
            raise Fail("no runs recorded yet - make some clips first")
        payload = json.loads(session.read_text(encoding="utf-8"))
        rows = [dict(row) for row in payload.get("clips") or []]
        target = session
    rows = [row for row in located_clips(cfg, rows) if row]
    if not rows:
        raise Fail("no clips left to judge there - the files were moved or deleted")
    return rows, Path(target)


def located_clips(cfg, rows):
    """Rows whose clip still exists, following a file to wherever it was moved."""
    roots = [Path(cfg.get("clip_output") or media_root(cfg) / "clips"), media_root(cfg) / "clips"]
    located = []
    for row in rows:
        path = Path(row.get("path") or "")
        if not path.exists():
            path = next((hit for root in roots if root.is_dir()
                         for hit in root.rglob(path.name)), None)
        if path:
            located.append({**row, "path": str(path)})
    return located



def cmd_review(cfg, args):
    """Open the judging window on a finished run."""
    import clipforge_gui as gui

    rows, target = review_records(cfg, args.target)
    gui.open_review_window(cfg, rows, target)
    return 0


# ---------------------------------------------------------------- cli

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        cfg = load_config()
    except Fail as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    p = argparse.ArgumentParser(prog="cf", description="Turn stream recordings into post-ready vertical clips.")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="register a recording file or a VOD url")
    a.add_argument("source")
    a.add_argument("--name", help="library name (default: from the file/stream name)")

    a = sub.add_parser("index", help="transcribe a recording (cached)")
    a.add_argument("name")
    a.add_argument("--model", help="whisper model, e.g. small.en, medium.en, large-v3")
    a.add_argument("--language")

    a = sub.add_parser("find", help="search the transcript for a moment")
    a.add_argument("name")
    a.add_argument("query", nargs="+")
    a.add_argument("--limit", type=int, default=10)

    a = sub.add_parser("suggest", help="rank candidate moments from every available signal")
    a.add_argument("name")
    a.add_argument("--limit", type=int, default=8)
    a.add_argument("--preset", help="boss kill | commentary | reaction (from config)")

    a = sub.add_parser("make", help="render one clip")
    a.add_argument("name")
    a.add_argument("--from", dest="start", required=True, help="clip start, e.g. 1:23:45")
    a.add_argument("--to", dest="end", help="clip end")
    a.add_argument("--dur", dest="duration", help="clip length, e.g. 45 or 0:45")
    a.add_argument("--pad", type=float, default=0.0, help="seconds of extra tail/head padding")
    a.add_argument("--snap", action="store_true", help="grow the window to whole spoken sentences")
    a.add_argument("--fit", choices=["blur", "crop", "black"], help="vertical framing")
    a.add_argument("--focus", type=float, default=0.5, help="0..1 horizontal focus for --fit crop")
    a.add_argument("--captions", choices=["karaoke", "block", "off"])
    a.add_argument("--caps", action="store_true", help="UPPERCASE captions")
    a.add_argument("--fps", type=int)
    a.add_argument("--open", action="store_true", help="open the output folder when done")

    a = sub.add_parser("auto", help="render the top auto-detected moments in one shot")
    a.add_argument("name")
    a.add_argument("--top", type=int, default=3, help="how many clips to render (default 3)")
    a.add_argument("--preset", help="boss kill | commentary | reaction (from config)")
    a.add_argument("--fit", choices=["blur", "crop", "black"])
    a.add_argument("--focus", type=float, default=0.5)
    a.add_argument("--captions", choices=["karaoke", "block", "off"])
    a.add_argument("--caps", action="store_true")
    a.add_argument("--fps", type=int)
    a.add_argument("--open", action="store_true")

    a = sub.add_parser("batch", help="render many clips from a csv of start,end rows")
    a.add_argument("name")
    a.add_argument("list_file")
    a.add_argument("--fit", choices=["blur", "crop", "black"])
    a.add_argument("--focus", type=float, default=0.5)
    a.add_argument("--captions", choices=["karaoke", "block", "off"])
    a.add_argument("--caps", action="store_true")
    a.add_argument("--fps", type=int)

    sub.add_parser("ls", help="list library recordings")

    a = sub.add_parser("new", help="find newly finished recordings and index them")
    a.add_argument("--min-age", type=float, help="seconds a file must sit untouched (default 120)")
    a.add_argument("--no-index", action="store_true")

    a = sub.add_parser("chat", help="attach Twitch chat replay to a VOD, for hype scoring")
    a.add_argument("name")
    a.add_argument("--file", help="import an existing chat log instead of downloading")

    a = sub.add_parser("stats", help="which signals actually pick clips you keep")
    a.add_argument("--ranks", type=int, default=5, help="how many pick ranks to break out")

    a = sub.add_parser("review", help="judge clips from a run that already finished")
    a.add_argument("target", nargs="?", help="a session json or a folder of clips "
                                            "(default: the newest run)")

    a = sub.add_parser("update", help="compare this copy with the one on GitHub")
    a.add_argument("--fresh", action="store_true", help="ignore the half-day cache")

    sub.add_parser("doctor", help="check that everything is installed")

    args = p.parse_args(argv)
    handlers = {
        "add": cmd_add, "index": cmd_index, "find": cmd_find, "suggest": cmd_suggest,
        "make": cmd_make, "auto": cmd_auto, "batch": cmd_batch, "ls": cmd_ls,
        "chat": cmd_chat, "stats": cmd_stats, "new": cmd_new, "doctor": cmd_doctor,
        "review": cmd_review, "update": cmd_update,
    }
    try:
        return handlers[args.command](cfg, args) or 0
    except Fail as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
