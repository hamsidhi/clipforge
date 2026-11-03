# ClipForge

Turn a long podcast into **15–25 ready-to-post vertical shorts** (Instagram Reels + YouTube Shorts) —
**100% free, runs on your own computer, zero watermark.** Built for Hinglish podcasts like
*Figuring Out with Raj Shamani* (1–3 h, two or more people, several cameras).

For every clip you get:
- a **1080×1920 H.264/AAC mp4**, 20–59 s, cut on word boundaries;
- the speaker kept in frame (face tracking, split-screen on wide two-person shots);
- **burned-in animated captions** (word-by-word highlight, Roman Hinglish or Devanagari) placed clear of the Reels/Shorts buttons;
- a **hook title** for the first 3 seconds;
- audio normalised to **−14 LUFS**;
- **metadata**: titles, YouTube description, Instagram caption, up to 5 lowercase hashtags, source credit line.

Everything is free and open source: `yt-dlp`, `ffmpeg`, `faster-whisper`, PySceneDetect, MediaPipe/OpenCV,
a **local LLM via Ollama** (or the free tiers of Gemini/Groq), FastAPI and React.

---

## 1. Install

### Windows 10/11
1. **Python 3.12**: <https://www.python.org/downloads/> → tick *Add python.exe to PATH*.
   (3.11 also works. Don't use 3.13+ yet: MediaPipe has no wheels for it.)
2. **ffmpeg** (full build with libass) — in PowerShell:
   ```powershell
   winget install --id Gyan.FFmpeg -e
   ```
   Then open a **new** terminal.
3. **Node.js LTS** (only needed once, to build the web app): `winget install OpenJS.NodeJS.LTS`
4. **Ollama** (local AI): <https://ollama.com/download>. Then:
   ```powershell
   ollama pull qwen2.5:7b
   ```
5. **ClipForge**:
   ```powershell
   git clone https://github.com/hamsidhi/clipforge.git
   cd clipforge
   py -3.12 -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   pip install -e . --no-deps
   cd frontend; npm install; npm run build; cd ..
   clipforge doctor --fix
   ```

### macOS / Linux
```bash
# macOS: brew install python@3.12 ffmpeg node ollama
# Ubuntu: sudo apt install python3.12-venv ffmpeg nodejs npm && curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:7b
git clone https://github.com/hamsidhi/clipforge.git && cd clipforge
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e . --no-deps
(cd frontend && npm install && npm run build)
clipforge doctor --fix
```

`clipforge doctor` checks ffmpeg/libass, yt-dlp, Ollama and its models, GPU, fonts, face models and Node,
and prints the exact command to fix anything missing. `--fix` downloads the free fonts and face models.

**GPU:** with an NVIDIA GPU (CUDA 12 + cuDNN 9), Whisper `large-v3` and NVENC encoding are used automatically.
Without one, everything runs on the CPU (Whisper `small`, int8). For a full 2–3 h episode on a slow laptop,
use the [Colab notebook](notebooks/clipforge_colab.ipynb), which runs on a free T4 GPU.

---

## 2. Use it — web app (recommended)

```bash
clipforge ui                    # opens http://localhost:8000
clipforge ui --host 0.0.0.0     # also reachable from your phone on the same Wi-Fi (URL is printed)
```

1. **New job**: paste a YouTube link or drop a video file, then pick the number of clips, caption style, layout and AI provider.
2. **Progress**: a live stepper (Download → Transcribe → Analyze → Select → Reframe → Captions → Render) with ETA and log.
   Clips appear as soon as each one is rendered. You can close the tab: the job keeps running.
3. **Gallery**: 9:16 cards that play on hover. Approve or reject each clip, or bulk-approve everything above a score. Export a ZIP.
4. **Clip editor**:
   - trim on the waveform (handles snap to words);
   - fix caption words (timing is kept);
   - change the hook, caption style, colours and position;
   - change the layout, or drag the crop box per camera shot;
   - re-render just this clip, or undo to the previous render.
5. **Publish kit**: one-click copy of the YouTube title and description, Instagram caption, hashtags and credit line.
6. **Tracker**: log where you posted each clip and its views; it computes estimated earnings at your campaign's rate per 1,000 views.
7. **Settings** / **System check**: everything in `config.yaml`, API keys (stored in `.env`, always masked), and the doctor results.

Jobs are stored in `workspace/clipforge.db` (SQLite), so refreshing the page or restarting the server never loses them.
To continue an interrupted job, click **Retry**: finished stages are reused.

## 3. Use it — command line

```bash
clipforge run "https://www.youtube.com/watch?v=VIDEO_ID"            # full pipeline, 20 clips
clipforge run episode.mp4 --clips 15 --provider gemini --layout split
clipforge run URL --section 10:00-40:00                              # only part of the video
clipforge transcribe URL                                              # stage-only (debugging)
clipforge select VIDEO_ID [--force]
clipforge render VIDEO_ID --only 3,7,12                               # re-render after editing candidates.json
clipforge preview VIDEO_ID                                            # gallery.html with all clips
clipforge list                                                        # processed videos
clipforge doctor [--fix]
clipforge ui [--dev] [--host 0.0.0.0] [--port 8000]
clipforge upload VIDEO_ID --only 1,2                                  # YouTube upload (opt-in, see below)
```

Output for each video is in `workspace/<video_id>/`:

| File | What it is |
|---|---|
| `source.mp4`, `audio.wav`, `info.json` | downloaded video, 16 kHz audio, title/channel/chapters |
| `transcript.json` | words with start/end/probability, plus `roman` Hinglish |
| `scenes.json`, `energy.json` | camera cuts, loudness peaks |
| `candidates.json` | **the clips — edit it** (start/end/title/hook/enabled/layout/caption_style/word_edits) |
| `clips/01_<title>.mp4` … | final videos |
| `metadata.csv` / `metadata.json` | titles, captions, hashtags, credit for each clip |
| `gallery.html` | open in a browser to review every clip |

Re-running any command skips stages that are already finished. To re-render one clip, edit `candidates.json`
and then run `clipforge render <id> --only 3`.

---

## 4. Configuration

All defaults live in [`config.yaml`](config.yaml), and the web app's Settings page edits the same file. The most useful settings:

```yaml
clips:    { count: 20, min_sec: 20, max_sec: 59, max_overlap: 0.3 }
whisper:  { model: auto, language: hi }          # auto = large-v3 on GPU, small on CPU
llm:      { provider: ollama, model: auto }      # ollama | gemini | groq | mock
reframe:  { layout: auto }                        # auto | track | split | blur
captions: { script: roman, preset: bold-yellow-pop, position_pct: 0.65, hook_title: true }
render:   { crf: 20, preset: veryfast, loudness_lufs: -14 }
```

The selection prompt is in [`prompts/clip_selection.md`](prompts/clip_selection.md); edit it to change
what the AI looks for, with no code changes. Optional free API keys go in `.env` (see `.env.example`):

- Gemini: <https://aistudio.google.com/apikey> → `GEMINI_API_KEY=…`
- Groq: <https://console.groq.com/keys> → `GROQ_API_KEY=…`

Caption presets: **Bold Yellow Pop** (default), **Clean White**, **Green Highlight** and **Hindi Devanagari**.
You can also create your own in Settings.

---

## 5. YouTube upload (optional, off by default)

Clips upload as **private** (or scheduled), using the free YouTube Data API quota (about 6 uploads per day).
Instagram posting stays manual.

1. <https://console.cloud.google.com> → create a project → **Enable** "YouTube Data API v3".
2. **OAuth consent screen** → External → add your Google account as a **test user**.
3. **Credentials → Create credentials → OAuth client ID → Desktop app** → download the JSON →
   save it as `client_secret.json` in the ClipForge folder (it is git-ignored).
4. In `config.yaml` set `upload: { enabled: true, privacy: private, schedule_hours_apart: 24 }`.
5. Run `clipforge upload <video_id> --only 1`. The first time, a browser asks you to sign in; the token is saved to `youtube_token.json`.

Quota use is logged to `workspace/youtube_quota.json`.

---

## 6. How it works

```
URL/file ─▶ yt-dlp ─▶ ffmpeg (16 kHz wav) ─▶ faster-whisper (word timestamps, VAD)
        ─▶ loudness peaks ─▶ LLM picks moments in 8-min windows (JSON, validated, retried)
        ─▶ snap to words · dedupe · rank with topic diversity ─▶ candidates.json
        ─▶ per clip: camera cuts (PySceneDetect) ─▶ faces (MediaPipe + YuNet) ─▶ crop plan
        ─▶ Roman Hinglish (LLM, 1:1 words) ─▶ .ass karaoke captions + hook
        ─▶ one ffmpeg filtergraph: per-shot crop/split/blur → concat → captions → loudnorm → H.264
```

For design decisions and the reasons behind them, see [DECISIONS.md](DECISIONS.md) and [CLAUDE.md](CLAUDE.md).

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| `ffmpeg not found` | Install it (see above), then open a **new** terminal. `clipforge doctor` finds WinGet installs even when PATH isn't refreshed. |
| `yt-dlp could not download` | Run `pip install -U yt-dlp` (YouTube changes often), or download the video yourself and pass the file. |
| `Ollama is not running` | Start the Ollama app (Windows/macOS) or run `ollama serve`. |
| Transcription is slow | On CPU a 2 h episode takes a while. Use `--whisper-model base`, `--section`, or the Colab notebook. |
| Captions show boxes instead of letters | Run `clipforge doctor --fix` to download the fonts. |
| A clip starts or ends badly | Trim it in the editor (snaps to words), or edit `candidates.json` and run `clipforge render <id> --only N`. |
| The face is cropped wrong | Clip editor → Framing → drag the crop box for that shot → Re-render. |

## Tests

```bash
pytest          # unit tests + API tests + an end-to-end render of a synthetic video (needs ffmpeg)
```

License: MIT. Fonts: SIL Open Font License (Montserrat, Poppins, Noto Sans Devanagari).
