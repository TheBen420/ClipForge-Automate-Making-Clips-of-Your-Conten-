# ClipForge reference

The detail behind the [README](../README.md): every command, what a render produces, how
moments are scored and how the weighting learns, the measurements behind the defaults, and
what the app assumes about a machine.

## Version numbers

`VERSION` at the top of `clipforge.py` is the build number, and every build increments the last
digit: 0.0.5, 0.0.6 ... 0.0.9, then 0.1.0 rather than 0.0.10, because the digits carry like a
decimal rather than a counter. The app compares that against the copy on `main` on every launch
and offers an update when the published one is higher, so a build is only ever new by that one
line. Nothing else in the code should hard-code a version.

## Command line

Plain `cf` commands from any terminal. Everything renders to `D:\ClipForge\ready\<stream>\<start>-<end>\`.

## The loop

```
cf new                                    # grab freshly finished recordings and transcribe them
cf suggest my-stream                     # ranked moments, ready to copy-paste
cf find my-stream "when I called the boss ugly"
cf make my-stream --from 1:23:45 --dur 45 --snap --caps
cf auto my-stream --top 3                # or let it pick and render three on its own
```

## Commands

| command | what it does |
| --- | --- |
| `cf add <file-or-url> [--name x]` | register a recording (local file, Twitch/YouTube VOD link) |
| `cf index <name>` | transcribe with word timings (cached; ~40x realtime on the 4080) |
| `cf ls` | what's in the library, and whether it's transcribed |
| `cf find <name> "words"` | search the transcript, prints timestamps and a ready `make` line |
| `cf suggest <name>` | ranks moments by audio energy + speech density, prints `make` lines |
| | if the audio track is too quiet to rank, it ranks by on-screen motion instead |
| `cf make <name> --from 1:23:45 --dur 45` | render one clip |
| `cf auto <name> --top 3` | render the three best auto-detected moments, no timestamps needed |
| `cf batch <name> clips.csv` | render many from a csv of `start,end` (or `start,duration`) rows |
| `cf new` | scan the watch folders, add + transcribe anything new |
| `cf review [session-or-folder]` | judge clips from a run that already finished |
| `cf doctor` | verify ffmpeg, NVENC, whisper, yt-dlp, disk |

`--from`/`--to`/`--dur` accept `5025`, `83:45`, `1:23:45`, or `1h2m3s`.

## What `make` produces

Per clip, named after the source footage it came from plus the window it covers — `Moon Lord 2m31s-2m57s.mp4`:

- `.mp4` - 1080x1920, H.264 (NVENC), 60fps when the source is 60fps, AAC 192k loudness-normalised to -14 LUFS, `+faststart` so it uploads instantly
- `-thumb.jpg` - 1080x1920 cover frame
- `.srt` - sidecar subtitles, if you'd rather upload those than burn them in
- `.txt` - the hook line, the full in-clip transcript, and hashtags, so posting is copy-paste

Framing defaults to `blur`: the full 1920x1080 frame shrunk to fit with a blurred fill above and below, captions underneath, nothing cropped away. `--fit crop` instead punches a 9:16 window out of the frame (`--focus 0..1` slides it left/right), which fills the screen but throws away 68% of the width. `--fit black` is the same as blur with a near-black backdrop.

Captions default to `karaoke`: three or four words on screen, the word being spoken highlighted, burning in via libass. `--captions block` shows whole phrases instead, `--captions off` for none. `--caps` for ALL CAPS.

## How moments get picked

Five signals are scored over sliding windows, each normalised against its own spread so they can be added:

| signal | what it notices |
| --- | --- |
| `energy` | loudness swings: shouting, hits, music stings |
| `speech` | words per second, for commentary-heavy footage |
| `motion` | on-screen motion and brightness change, for silent gameplay |
| `chat` | Twitch chat message rate, with hype words and ALL-CAPS counted double |
| `phrase` | your `boost_phrases` list matched against the transcript |

Weights live in `config.json` under `weights`. Three presets ship with it — `boss kill`, `commentary`, `reaction` — which also set framing, captions and clip length. Pick one in the **Content type** box, or leave it on `auto` and use the global weights. **Clip length** in the window - `length` in `config.json` - is the scoring window. Snap on speech starts no more than three seconds before the spoken sentence holding the peak and ends on the nearest word boundary within `max(1.4 x length, length + 4s)`, so a 30-second request lands at 30 seconds and runs a few seconds over only to avoid cutting mid-sentence. A preset's own length wins over the box when one is chosen. The first and last tenth of a recording (capped at a minute) is skipped so intros and outros don't win.

For a Twitch VOD, tick **Use Twitch chat hype** and it pulls the chat replay first — chat spikes are the most reliable "something happened" signal on a quiet stream. `cf chat <name> --file chat.json` imports a chat log you already have.

Running the same footage again skips windows already clipped, so a second run hands back new moments instead of the same clip. That has a limit worth knowing: once a recording's good stretches are taken, what's left scores lower, and the run says so — "the strongest moment still untouched scores 2.99 while one already clipped here scores 4.29: this recording's best moments are used up" — rather than quietly handing you filler. A fresh recording, or a longer one, is the answer at that point.

The replay is read a hundred messages at a time, each request asking Twitch for the next hundred after a timestamp. Their cursor API now fails an integrity check for anonymous clients, so the offset form is the one that works; a busy three-hour VOD is about 11,000 messages in well under a minute. Twitch only hands back what it kept, though: a VOD whose chat never got going has a handful of messages, so anything under five is refused with the count and the run ranks on the other signals instead.

## Judging clips, and what the numbers mean

<img src="docs/judge-window.png" alt="The judging window" width="620">

After a run the app opens the judging window with the clips it made. **Keep**, **Maybe**, or a reason for rejecting — *boring*, *wrong moment*, *bad edit*, *late start*, *cut short* — with keys `k`, `m`, and `b`/`w`/`e`/`l`/`c`. The two boundary reasons exist because "the moment was wrong" and "the moment was right but the clip started after it did" are different mistakes with different fixes: the first is the ranking, the second is where the window opens and closes. Every judgement goes into `D:\ClipForge\feedback.jsonl` alongside the scores that produced that clip, and the list selects the next row after each one so you can walk a batch in order. `cf stats` reads it back:

```
Feedback: 24 clip(s) judged - 15 kept (63%)
  rank 1: kept 9/12 (75%)
  rank 2: kept 4/12 (33%)

Signal separation (mean score of kept vs rejected clips):
  signal       kept  rejected  separation
  chat         1.84      0.71        0.44
  motion       0.92      0.88        0.02
```

A separation near 0 means that signal isn't telling your kept and rejected clips apart, and its weight will drift down as you judge more; near 1 it is earning every bit of its weight. Each run also writes `D:\ClipForge\sessions\<timestamp>-<recording>.json` with every candidate's features, the per-signal spread across the recording, and what was rendered — that's the raw data behind the numbers above.

Judging doesn't need the app that made the clips. **Judge last run** on the review tab opens the newest session in its own window, and `cf review` does the same from a terminal: `cf review` takes the latest run, `cf review <session.json>` or `cf review <folder>` takes a specific one. It follows clips that have since been moved, since it matches on filename. Space plays the selected clip, `k` keeps it, `r` rejects it, and the list moves on to the next unjudged clip by itself.

Ask for more clips than the recording can support without overlap and you'll get fewer than you asked for; a 90-second file with one standout moment yields one clip.

### How the weighting learns

`weights` in `config.json` is the baseline: energy 1.0, speech 0.5, motion 0.7, chat 1.3, phrase 1.2. Those numbers are a starting opinion, not a tuning - they came from watching what separates a good moment from a boring one across the footage this was built on.

Every judgement you make moves them. Each signal is compared on the clips you kept against the clips you rejected, on a signed scale where 1 means only kept clips scored on it and 0 means the two are indistinguishable:

```
  signal       kept  rejected  separation
  phrase       0.09      0.00        1.00
  energy       1.52      1.49        0.01
  speech       0.35      0.43       -0.10
```

The multiplier applied to that signal's baseline weight is `1 + 0.6 x separation`, clamped to 0.5-1.5, and only once a signal has at least 3 kept and 3 rejected clips behind it. A flat signal is left alone; a signal that scores just as high on clips you reject is pulled down; one that only fires on clips you keep is pushed up. So the effective weight of `phrase` above is 1.2 x 1.5 = 1.8 while `energy` stays at 1.0.

Two properties matter here. The multipliers are derived from `feedback.jsonl` on every run rather than saved into `config.json`, so the config stays the readable baseline and the feedback stays the state - delete judgements and the weighting returns to baseline. And the effective weights are written into each session file next to the candidates they ranked, so any past run can be re-read knowing exactly what weighting produced it. `cf stats` prints the table above plus the baseline, learned and effective weight for every signal.

## Speed

Measured on this machine, one 22-second clip each time, mean CPU sampled once a second for the whole run (a VOD clip includes downloading its section, a local clip does not):

| setup | wall time | mean CPU |
| --- | --- | --- |
| one render at a time, 60fps, VOD clip | 24.0s | 31% |
| two renders at a time, 60fps, VOD clip | 25.8s | 92% |
| one render at a time, 30fps, VOD clip | 21.1s | 38% |
| one local recording, no captions | 6.9s | 6% |

Two renders at once is the only real CPU sink: three times the cores for 4% of the wall clock. That is why one job is the default, and why the machine stays responsive while it works instead of sitting at 100% for an hour. 30fps finishes sooner but spends the same total CPU spread thinner; it is a smoothness choice, not a savings one, so `auto` leaves the frame rate matching the source.

The render is CPU-bound on decode and filtering while NVENC handles the encode; decoding on the GPU instead (NVDEC) measured *slower* once the filter chain was cheapened, so decoding stays on the CPU.

What actually helps:

- **One job at a time** is the default for exactly that reason. Two is possible by hand-editing `config.json` (see [Config](#config)), and it overlaps one clip's analysis with another's render, but it spends triple the CPU to save a second per clip.
- **Cached motion analysis.** The pass that decodes a recording to score on-screen motion writes `motion.json` beside `transcript.json`, keyed to the file's size and timestamp. The first run on a recording costs 10-26s; every run after is instant, so re-clipping something you already indexed costs nothing extra.
- **The filter chain** blurs the background at quarter resolution and scales with bicubic instead of lanczos. Visually identical, about a third faster per render.

## Config

`config.json` next to this file. The useful knobs:

```json
{
  "media_root": "D:/ClipForge",
  "watch_dirs": ["D:/Vod Temp", "C:/Users/Ben/Videos/Recordings", "D:/Downloaded"],
  "model": "small.en",
  "fit": "blur",
  "captions": "karaoke",
  "font": "Arial Black",
  "font_size": 88,
  "caption_margin_bottom": 330,
  "hashtags": ["#gaming", "#twitch", "#clips", "#streamer", "#twitchclips"],
  "ytdlp_cookies_from_browser": ""
}
```

`model` is the whisper model: `small.en` is the default, `large-v3` gives the best transcript on the 4080. Raise `caption_margin_bottom` to lift captions higher up the frame, lower it to drop them toward the bottom edge. `ytdlp_cookies_from_browser` takes `chrome` or `firefox` for login-walled VODs.

### Parallel rendering, if you edit the file

One clip is rendered at a time, and that is not a setting the window offers at all: two renders at once measured 92% CPU against 31% for one, to save 4% of the wall clock. The capability still exists for anyone who wants it by hand, and it lives only in the config file:

```json
{
  "workers": 2,
  "render_workers": 2
}
```

Both keys mean the same thing — how many clips or recordings are worked on at once — and both are read only from `config.json`, so changing `2` to `1` or `1` to `2` there and restarting is the whole procedure. Nothing in the interface writes these values, so a hand edit survives every later save of the other settings.

**The cap at 2 is a failsafe, not a suggestion.** This is a young program with an unproven scheduler, and a render is already using every core of the machine; three or four at once on a stock cooler is how you turn a fan curve into a thermal shutdown, and on a laptop it is how you turn one batch into a throttled, corrupted-length render. So a value above 2 is held at 2, with a line in the log naming what you asked for. Numbers below 1 are read as 1, decimals are rounded down. A value that isn't a number at all — `"a few"`, `null` — stops the program with a message naming the line to fix, rather than guessing at a number on your behalf. You cannot configure this into harming the machine.

## Captions: accuracy and timing

Two things were wrong early on, and both are fixed.

**Timing.** Caption events were being written at their absolute position in the recording rather than their position in the clip, so a clip taken from 10:36 had its captions scheduled ten minutes into a thirty-second video — in practice, no captions at all unless the clip started near zero. They are now shifted by the clip's own start offset. Word-level karaoke also used to fade in on every word, which blanked the phrase for a frame at each transition; the fade now happens once per phrase. Measured against the expected caption events, burn-in timing agrees on 159 of 160 sampled frames.

**Accuracy.** The default model was `small.en`, which misheard game vocabulary ("chess piece" for "chest piece"). `medium.en` is now the default, with `large-v3` available in the **Transcription** box for the best pass at the cost of speed.

The bigger accuracy lever is the **Vocabulary** field. Whisper can't know your game's proper nouns, so it guesses. List the names it keeps getting wrong, comma separated, and they are fed to the decoder as a prompt:

```
Preludes 13, armor attunement, Steel Singer Stitchy Wicks, Palworld, courage
```

On the same audio this turned "chess piece … the steel singer stitchy wicks" into "chest piece … the Steel Singer Stitchy Wicks", and it restored punctuation and capitalisation as a side effect.

Changing the model re-transcribes automatically the next time a recording is used in the app, since a cache from a weaker model is worse than a fresh pass.

## Running on other hardware

Nothing here assumes an RTX card. At first use clipforge tests which H.264 encoder actually works on the machine and remembers it for the session, in this order: NVENC (NVIDIA), QSV (Intel), AMF (AMD), VideoToolbox (Apple), then libx264 on the CPU. Each candidate is proven with a two-frame test encode, because an encoder can be listed by ffmpeg and still fail without the hardware behind it. If a chosen encoder fails mid-render it falls back to libx264 and says so. `cf doctor` prints what it picked and what else was usable.

Transcription walks down a chain as well: CUDA float16, CUDA int8, CPU int8, CPU int8-float32, CPU float32. A machine with no CUDA lands on CPU int8, and a CPU too old for int8 lands on float32. Set `CLIPFORGE_DEVICE=cpu` to force the CPU path.

Captions ask for `Arial Black` but check what's installed (fontconfig, or Tk's font list) and pick the boldest available substitute — Impact, Segoe UI Black, Arial, Liberation Sans, DejaVu Sans. `cf doctor` shows the font it settled on.

Paths and shell behaviour adapt too: the default media root is `D:\ClipForge` only if that drive exists *and* is writable, otherwise `C:\ClipForge`, otherwise `~/ClipForge`. Opening the output folder uses explorer, `open` or `xdg-open` depending on the OS, and child processes avoid console windows only where that flag exists.

Still Windows-shaped: the desktop shortcut script (`.lnk` files via PowerShell) and `cf.cmd`. On macOS or Linux the app runs with `python3 clipforge_gui.py`; nothing else needs changing.

## Notes

- Twitch VODs: paste the `twitch.tv/videos/...` URL. Only the requested section downloads, so a 4-hour VOD costs you seconds of bandwidth.
- Local files are referenced, never copied or moved. Delete the original and the library entry complains rather than guessing.
- Transcripts are cached in `library/<name>/transcript.json`. Deleting it forces a re-transcribe with a different `--model`.
- Recordings with a very quiet audio track still work: highlighting falls back to on-screen motion, and the audio is lifted to -14 LUFS on the way out. Audio below -45 dB peak is treated as noise floor and dropped rather than amplified.
- Rendered folder names get a `-crop`/`-nocap` suffix when you pass a `--fit` or `--captions` value that differs from the config, so variants of the same moment don't overwrite each other.
- `cf new` and the desktop app skip files modified in the last minute and anything under 20MB (`cf new` only), so a recording mid-save isn't grabbed.
