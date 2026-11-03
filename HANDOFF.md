# ClipForge: handoff

Built and tested **on your laptop** (Windows 11, Ryzen 7 255, 31 GB RAM, AMD Radeon 780M, so no CUDA)
between 19:00 and about 23:00 on 2026-11-03. Code: <https://github.com/hamsidhi/clipforge> (branch `main`).

## 1. What was built

| Area | Status |
|---|---|
| CLI: `run`, `transcribe`, `select`, `render --only`, `preview`, `doctor [--fix]`, `ui`, `upload`, `list` | ✅ |
| Download (yt-dlp ≤1080p, H.264 preferred, chapters) or local file; `--section` for part of a video | ✅ |
| Transcription: faster-whisper with word timestamps + VAD, resumable checkpoints | ✅ |
| Roman Hinglish captions (LLM, 1:1 word alignment, rule-based fallback) and Devanagari | ✅ |
| Signals: camera cuts (PySceneDetect), loudness peaks, YouTube chapters | ✅ |
| Clip selection: 8-minute windows, JSON validated per candidate, retries, heuristics | ✅ |
| Refine: snap to word/sentence boundaries, length limits, dedupe, topic diversity, ranking | ✅ |
| Reframe 9:16: face tracking (MediaPipe + YuNet), smoothing, split-screen, blur, per-shot overrides | ✅ |
| Captions: karaoke highlight with pop, keyword colour, safe zones, hook title box with fade, 4 presets + custom | ✅ |
| Render: one ffmpeg graph per clip, H.264/AAC 1080×1920 30 fps, 2-pass loudnorm −14 LUFS, NVENC auto, parallel | ✅ |
| Metadata: titles, YouTube description, Instagram caption, ≤5 lowercase hashtags, source credit (CSV + JSON) + gallery.html | ✅ |
| Web app: all 9 pages, SSE progress, SQLite jobs, waveform trimmer, word fixes, undo, publish kit, ZIP, tracker | ✅ |
| YouTube upload (private/scheduled, quota log), **off by default** | ⚠️ unit-tested with a fake client; real OAuth needs your Google account ([TODO_ON_LAPTOP.md](TODO_ON_LAPTOP.md)) |
| Colab notebook (T4 GPU + Gemini/Groq, ZIP to Drive) | ⚠️ written and validated as JSON, not run in Colab |
| Tests | ✅ 45 pytest tests: unit, API and an end-to-end synthetic render |

### Acceptance criteria
Tested on the full **2 h 0 min** episode *Liquor License, Alcohol Tax & Liquor Business Secrets in India | Mokksh Sani | FO516*
(<https://www.youtube.com/watch?v=sdMHVIcPGwg>), through the web app, with the default settings and the local Ollama `qwen2.5:7b`.

| Criterion | Result |
|---|---|
| `clipforge doctor` passes | ✅ all required checks pass (CUDA/NVENC optional, not present) |
| 15–25 mp4s, 1080×1920, 20–59 s, H.264/AAC | ✅ 20 clips, all 1080×1920 H.264/AAC 30 fps, 20.3–42.8 s |
| No watermark, nothing paid | ✅ only captions and the hook box; local Ollama, free OFL fonts |
| No clip starts or ends mid-word | ✅ boundaries snapped to words and, where possible, to sentence/segment ends |
| Face in frame, no jitter, split-screen on wide shots | ✅ 75 tracked shots + 1 split-screen; false "faces" (wall photos) filtered. ⚠️ No active-speaker detection (see below) |
| Captions in sync, readable, highlight, clear of Reels UI | ✅ word-timed karaoke at 65% height with the right edge kept clear |
| Consistent loudness | ✅ −14.0 ±0.2 LUFS on all 20 clips (measured with ebur128) |
| metadata.csv: title, captions, ≤5 lowercase hashtags, credit | ✅ |
| Re-run skips finished stages | ✅ also verified by restarting the server mid-job; Retry resumed from cache |
| Edit candidates.json + `render --only 3` re-renders just that clip | ✅ (e2e test + manual) |
| Full browser flow: URL → progress → gallery → trim → fix word → re-render → approve → publish kit → ZIP | ✅ done by hand in the in-app browser |
| Jobs survive refresh/restart; interrupted job resumes | ✅ |
| Tracker saves posts and computes earnings | ✅ (12,000 views × ₹50/1k = ₹600) |
| Phone on the same Wi-Fi (`--host 0.0.0.0`) | ⚠️ mobile layouts verified with screenshots; real phone test is yours ([TODO_ON_LAPTOP.md](TODO_ON_LAPTOP.md)) |
| Tests pass; README covers Windows + macOS/Linux | ✅ 45 tests pass |

## 2. Setup on your Windows laptop

**Already done on this machine:** Python 3.12 venv in `.venv`, ffmpeg (WinGet), Ollama + `qwen2.5:7b`,
fonts, face models, the frontend build and the Whisper model download. To use it now:
```powershell
cd E:\youtube\clipforge
.venv\Scripts\activate
clipforge doctor
clipforge ui
```

**From scratch (a new PC):**
1. Install **Python 3.12** from python.org and tick *Add to PATH*. Don't use 3.13+: MediaPipe has no wheels for it.
2. `winget install --id Gyan.FFmpeg -e`, then open a **new** terminal.
3. `winget install OpenJS.NodeJS.LTS` (only needed to build the web UI once).
4. Install **Ollama** from <https://ollama.com/download>, then `ollama pull qwen2.5:7b`. It's a 4.7 GB download and the best free local model for Hinglish here.
5. Get the code and set it up:
   ```powershell
   git clone https://github.com/hamsidhi/clipforge.git
   cd clipforge
   py -3.12 -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   pip install -e . --no-deps
   cd frontend; npm install; npm run build; cd ..
   clipforge doctor --fix
   clipforge ui
   ```
   If `pip install` stalls on `numpy` (it did here: PyPI's metadata endpoint hung), download the wheel from
   <https://pypi.org/simple/numpy/> and run `pip install numpy-2.5.3-cp312-cp312-win_amd64.whl`, then re-run the install.
6. The first transcription downloads Whisper `large-v3-turbo` (about 1.6 GB, once).

## 3. Making clips from a Raj Shamani episode
**Web (easiest):** `clipforge ui` → **New job** → paste the YouTube link → *Create clips*. You can close the tab;
the job keeps running. When it's done: **Gallery** → approve or reject → edit any clip → **Publish kit** → **Export ZIP**.

**CLI:**
```powershell
clipforge run "https://www.youtube.com/watch?v=VIDEO_ID"
clipforge preview VIDEO_ID
```
Clips land in `workspace\VIDEO_ID\clips\`, with copy in `metadata.csv`.

**How long it takes on this laptop (CPU only, measured):**
| Stage (2 h episode) | Time |
|---|---|
| Download (767 MB) | 4 min |
| Transcription (large-v3-turbo, CPU) | 37 min |
| Clip selection (17 windows, qwen2.5:7b) | ~26 min (about 1.5 min per window after the `num_predict`/schema fix) |
| Romanize + reframe + captions + render + metadata, 20 clips | 8 min |
| **Total** | **≈ 1 h 15 min** (this run took 1 h 39 min, including a restart test and the slow windows before the fix) |

**Colab fallback (free GPU, about 4× faster):** open `notebooks/clipforge_colab.ipynb` in Google Colab,
choose *Runtime → T4 GPU*, paste a free Gemini key (<https://aistudio.google.com/apikey>) and the URL, then run all cells.
The ZIP lands in Google Drive → `ClipForge/`.

## 4. Known issues & what to test first
1. **Clip quality depends on the LLM.** `qwen2.5:7b` picks sensible moments but tends toward short (≈20 s) clips,
   and its titles are plain. A free Gemini key (Settings) usually picks better. Edit `prompts/clip_selection.md` to tune it.
2. **No active-speaker detection.** In a shot with two people close together, the crop centres between them; in a
   single-person shot it follows the most prominent face. If a shot is framed wrong, use the Clip editor → Framing → drag the box.
3. **Whisper errors on Hinglish.** Some words are misheard or romanized oddly. Fix them in the editor by clicking the word;
   timing is kept.
4. **Near-duplicate topics** can still slip through (e.g. three "Sanju" clips from the same minute). Reject the extras in the gallery.
5. **On-screen text from the source** (such as the channel's alcohol disclaimer ticker) is part of the video and isn't removed.
6. **This build is CPU-only**: a 2-hour episode takes about 1¼ hours. Use the Colab notebook when you need it faster.
7. Untested here (needs your accounts or hardware): YouTube upload, Gemini/Groq, Colab, NVIDIA GPU, a real phone.

**Test first:** `clipforge ui` → open the finished job ("Liquor License…") → watch a few clips → try the editor on one
→ then run a new episode of your choice.

## 5. Where things are
- [README.md](README.md): install, usage, config, YouTube upload setup, troubleshooting
- [DECISIONS.md](DECISIONS.md): every choice made without asking, and why
- [CLAUDE.md](CLAUDE.md): architecture and conventions, for future work with Claude Code
- [TODO_ON_LAPTOP.md](TODO_ON_LAPTOP.md): the things only you can test
- `docs/screenshots/`: every page, desktop and mobile
