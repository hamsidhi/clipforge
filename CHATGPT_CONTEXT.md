You are helping me with my project **ClipForge**. Read all of this first, then answer my questions as an expert on this codebase. If something isn't covered here, ask me instead of guessing.

## What it is
A free, local, open-source tool that turns long Hinglish podcasts (e.g. Raj Shamani's "Figuring Out", 1–3 h, multi-camera) into 15–25 vertical 1080×1920 Shorts/Reels with burned-in animated captions and a hook title, then schedules and publishes them to YouTube Shorts and Instagram Reels automatically. It is controlled from a web app (phone-friendly PWA) and a Telegram bot.

## Hard rules
1. 100% free: no paid APIs/SaaS. LLM = local Ollama (default) or the free tiers of Gemini/Groq. It must work with Ollama alone.
2. No watermarks: nothing is added except captions, the hook title and a short opening transition.
3. Runs on Windows/macOS/Linux on CPU; CUDA/NVENC are auto-detected.
4. Resumable: every stage caches to `workspace/<video_id>/`, and re-runs skip finished stages (config fingerprints).
5. Python 3.12, type hints, pydantic models, rich logging. ffmpeg only (no moviepy).

## Tech stack
- **Backend:** Python 3.12, Typer CLI, FastAPI + SSE, SQLModel/SQLite, faster-whisper (large-v3-turbo int8), MediaPipe + OpenCV YuNet (face detection), PySceneDetect, ffmpeg, Ollama.
- **Frontend:** React + TypeScript + Vite + Tailwind + shadcn/ui + TanStack Query, built into `frontend/dist` and served by FastAPI. It is a PWA.
- **Integrations:** YouTube Data API v3 (OAuth, private + publishAt uploads), Instagram Graph API (Facebook Login, Reels containers), Telegram Bot API.
- **Hosting:** runs on my Windows laptop at home. I reach it remotely over Tailscale (tailnet-only HTTPS via `tailscale serve`). It is kept alive by a Windows Task Scheduler task running `clipforge service` (a watchdog).

## Architecture
```
cli.py (Typer) ─┐
api/ (FastAPI) ─┼─> services/{jobs,clips,publishing}.py  (the ONLY implementation of user actions)
Telegram bot ───┘            │
                             └─> pipeline.py (the ONLY orchestrator: prepare / select / render / metadata / run_all)
                                   stages/: download → transcribe → analyze_audio → select (arcs engine) → refine
                                            → romanize → scenes → reframe → captions → render → metadata
                                   llm/: ollama | gemini | groq | mock providers
publish/: youtube, instagram, queue, states, runner, worker, plan, smart, limits, dedup, reconcile, history, ...
notify/: Telegram bot (commands, clip cards, publishing controls)
server/: optional "always online" mode (a server owns DB/publishing; the laptop acts as a GPU/CPU worker via leases)
```

### Clip pipeline
1. **Download:** yt-dlp, or a local file.
2. **Transcribe:** faster-whisper on CPU (about 0.45× real time), word timestamps, checkpointed. The language is detected per episode (`smart`: en | hi).
3. **Select (arc engine, `stages/arcs.py`):**
   - Finds the structure: questions, topic blocks, garbled segments.
   - An LLM writes an outline, and candidates come from it: Q&A units, sub-arcs, window picks, audio-peak moments.
   - An LLM judge scores each candidate against a rubric, with ±30 s of context.
   - Gates and min quality decide what's kept: one clip per topic block, a preference for 20–40 s, and openers like "so/matlab" are trimmed.
   - Titles, hooks and the "why" come from a separate LLM pass. LLM replies are cached.
   - The result is `candidates.json`, which I can edit by hand (start/end/title/hook/enabled/layout/caption style/word edits/crop overrides).
4. **Romanize:** Hindi words become Roman Hinglish, only for the selected clips (LLM, rule-based fallback).
5. **Scenes:** detected lazily, only inside clip ranges.
6. **Reframe:** the faces of each camera shot decide the framing: track, split-screen (two speakers), blur background or center.
7. **Captions:** ASS subtitles with an animated word highlight. Caption language per clip: as spoken / English / Hinglish (LLM translation that keeps the timing).
8. **Render:**
   - ONE ffmpeg filtergraph per clip: a trim chain per shot, concat, a split-screen "opening reveal", then the burned-in captions and 2-pass loudnorm. That's a single encode, with no drift.
   - Outputs go to `clips/NN_slug.mp4` and `clips/_work/NN/...`.
9. **Metadata:** LLM titles, descriptions, ≤5 lowercase hashtags, source credit and a pinned-comment question.

### Publishing
- Queue states: `draft → queued → uploading → scheduled (private + publishAt on YouTube) → published`, plus `failed / missed / needs_manual / waiting_capacity / skipped_duplicate / cancelled`. Transitions are enforced, and every attention state has a reason code.
- Each account is in **auto** (API uploads) or **assisted** (I post by hand and the app links it) mode.
- YouTube:
  - Uploads go up early as private, with publishAt.
  - Quota and upload-limit controller: daily safety limit, cooldowns.
  - Duplicate guard.
  - Sync with YouTube Studio: deleted videos leave the queue.
- Instagram: Graph API Reels containers, published at the slot. A long-lived token that refreshes itself.
- **Smart daily plan** (`publish/smart.py`): learns posting times from my own results. Currently 2 YouTube + 2 Instagram posts a day, in the evening (IST), because posting more made the platforms treat the account as a repost page.
- **History/audit log** (`HistoryEvent` table): every add, move, upload, publish or delete is kept forever. History page + Telegram `/history`.
- A worker thread ticks every 60 s, and YouTube status refreshes every 10 min.

### Web app and Telegram
- Pages: Jobs, Gallery, clip editor (trim, words, style, layout), Schedule, Stats, History, Accounts, Settings, Health.
- Remote access: password login (scrypt), cookie session + CSRF, lockout, audit log. Local requests skip the login.
- Telegram bot:
  - Send it a link to start a job, get clip cards, edit/render/approve clips, view the schedule/queue/health/stats.
  - Dangerous actions need YES + a PIN.
  - Only paired chats get replies.

### Data and files
- `workspace/<video_id>/`: transcript, candidates.json, clips, caches.
- SQLite DB tables: Channel, QueueItem, VideoPerf, UploadLedger, HistoryEvent, AuditEvent, Telegram tables.
- `config.yaml` holds every setting (whisper, arcs, render, publishing, schedule, youtube, instagram, telegram, deployment).
- Secrets live in `.env`, `client_secret.json` and token files, and are never shared.

## Current status (Nov 2026)
- Everything above is built and running. The tests (pytest, with fake YouTube/Instagram/Telegram clients) pass.
- Instagram: works, around 100–160 views per Reel, with one Reel at about 4.9k.
- YouTube Shorts: 0–5 views each.
- The account is new and posts reposted podcast clips, so reach is the main problem.
- The YouTube OAuth app was in "Testing" mode, so its login expired every 7 days. I'm moving it to "In production".
- The laptop restarted for a Windows Update and ClipForge stayed off until I logged in, so one post went out late.

## How I want help
Short, simple answers with clear steps. I'm not a social media expert. Focus on practical fixes and on growing reach without breaking the rules above.
