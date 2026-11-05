# ClipForge — notes for Claude / contributors

## Goal
Free, local, open-source tool that turns a long Hinglish podcast (first target: Raj Shamani's
"Figuring Out", 1–3 h, multi-camera) into 15–25 vertical 1080×1920 shorts with burned-in animated
captions + hook title, plus metadata (titles, captions, ≤5 lowercase hashtags, source credit).

## Hard constraints
1. 100% free: no paid APIs/SaaS, no watermarking libs. LLM = local Ollama (default) or free-tier Gemini/Groq; must work with Ollama only.
2. Zero watermark: nothing added except captions + hook title.
3. Runs on Windows/macOS/Linux, CPU-only OK; CUDA/NVENC auto-detected. Colab notebook as free-GPU fallback.
4. Resumable: every stage caches to `workspace/<video_id>/`; re-runs skip finished stages.
5. Python 3.11+ (dev machine uses 3.12 — mediapipe/ctranslate2 have no 3.14 wheels), type hints, pydantic models, rich logging.

## Architecture
```
cli.py (typer) ─┐
api/ (FastAPI) ─┴─> pipeline.py ──> stages/{download, transcribe, analyze_audio, select_clips,
                                       refine, romanize, scenes, reframe, captions, render, metadata, upload}
                   llm/{ollama,gemini,groq,mock}_provider.py   utils/{ffmpeg,timecode,text,gpu,assets,log}.py
```
- `pipeline.py` is the ONLY orchestrator. CLI and API both call `prepare / select / render / metadata / run_all`.
- `services/{jobs,clips,publishing}.py` (Phase 15) is the ONLY implementation of user actions (create/cancel/retry/rerun/
  delete jobs, clip edits/trim/words/approve/render/undo, queue fixes/schedule/health/stats). Web routes, the Telegram
  bot and the CLI all call it; it raises `services.errors.ServiceError` (an HTTPException subclass, `.detail` = sentence).
- Progress goes through a `ProgressReporter` (utils/log.py): `NullReporter` for CLI, the API's reporter streams SSE.
- Stage cache: `Workspace.stage_done(stage, fingerprint, outputs)`; fingerprints hash the relevant config.
- `candidates.json` is human-editable (start/end/title/hook/enabled/layout/caption_style/word_edits/crop_overrides).
  Hand edits are preserved (select is skipped if the file is newer than the last selection).
- Per clip: `clips/NN_slug.mp4` + `clips/_work/NN/{crop_plan.json, captions.ass, filtergraph.txt, render.json, previous.mp4, thumb.jpg, meta.json}`.

### Design choices (details + reasons in DECISIONS.md)
- **Rendering**: one ffmpeg filtergraph per clip — each camera shot is a `trim` chain framed as track/split/blur/center,
  joined with the `concat` *filter*, then `ass` burns captions once, `loudnorm` (2-pass, linear) on audio. No intermediate
  files → no A/V/caption drift, single encode. Moving crops use `sendcmd` with per-frame `crop x` updates.
- **Scenes** are detected lazily only inside clip ranges (±2 s) — decoding a 2 h video just for cuts is wasteful.
- **Roman Hinglish** transliteration is done lazily only for words inside selected clips (LLM, 1:1 word count check,
  rule-based fallback via indic-transliteration).
- **Faces**: MediaPipe Tasks FaceDetector (BlazeFace) and OpenCV YuNet both run; detections are merged and ranked by
  size × confidence³ (posters/photos on the wall produce low-confidence "faces"). Split-screen needs two similar-size,
  confident faces for ≥60% of a shot that lasts ≥1.5 s.
- **Whisper on CPU**: `large-v3-turbo` int8, greedy (beam 1) ≈ 0.4–0.5× real time; `small` is unusable for Hindi.
  Audio is decoded by us (PyAV 19 breaks faster-whisper 1.2.1). Transcription checkpoints to `transcript.partial.json`.
- **Snapping**: Whisper segment boundaries count as sentence boundaries (Hinglish output has little punctuation).
- **LLM windows**: 8-min windows / 1-min overlap. Local models get the transcript in Latin script (fewer tokens).
- **Selection format** (DECISIONS "Faster selection"): the LLM sees `id| text` lines (id = 1-based Whisper segment in the
  window, no timestamps, fillers stripped from the prompt only) and answers `{"s","e","score","hook","standalone",
  "topic"}`; `select_clips.to_candidate` maps ids → word times. num_ctx is sized once per stage (a different value
  reloads the model). Then `refine.score_pool` → `select_clips.rerank` (top 40, ±20 s context, `select.rerank`) →
  `refine.pick` → `select_clips.write_titles` (title/hook/why for the final clips, 5 per call). Window/title/rerank
  replies are cached in `select_cache/` (key = model, temperature, seed, prompt, options).
- **Measuring selection quality**: `scripts/bench_commit.sh <commit> <label>` (worktree-pinned) runs
  `scripts/bench_select.py` on copies of cached episodes and `scripts/compare_selection.py` against
  `tests/fixtures/select_baseline/` (overlap vs the baseline's own re-run noise floor, fixed judge prompt, boundary %).
  Never time two runs with identical prompts back to back: Ollama's prompt cache makes the second one look 2× faster.

## Commands
```bash
# setup (Windows: .venv\Scripts\activate)
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt && pip install -e .
clipforge doctor --fix
clipforge run "<youtube url or file>" [--clips 20] [--provider ollama|gemini|groq|mock] [--section 10:00-25:00]
clipforge render <video_id> --only 3,7
clipforge preview <video_id>
clipforge ui            # web app on http://localhost:8000  (--host 0.0.0.0 for phone)
pytest                  # unit + API + e2e render tests (~1 min)
node scripts/screenshots.mjs   # with `clipforge ui` running: screenshots of every page -> docs/screenshots, fails on console errors
cd frontend && npm install && npm run build   # build the UI once
```

## Conventions
- ffmpeg via `utils.ffmpeg.run_ffmpeg` (never moviepy). Inside filtergraphs prefer paths relative to ffmpeg's `cwd` (`render._filter_dir`); `escape_filter_path` is the fallback.
- Never swallow errors silently: raise with a message that says what to do, or `log.warning` for cosmetic failures.
- New config keys: add to `config.py` model AND `config.yaml` with a comment.
- Tests in `tests/` for pure logic; use the `mock` LLM provider — no network in tests.

## Web layer notes
- `api/jobs.py`: one worker thread, tasks `pipeline` / `render` with per-task cancel tokens; events in a per-job
  ring buffer → SSE (`snapshot` carries `last_seq` + `boot_id` so clients don't re-fire toasts on replay).
- Clip ids in the API are `<job_id>-<rank>`; clip data lives in candidates.json (edit under `pipeline.candidates_lock`).
- Windows: renames go through `workspace.safe_replace` (retries; browsers keep video files open).
- Frontend: `frontend/src/api/{client,hooks,types}.ts`; pages in `src/pages`; shadcn/ui in `src/components/ui`.
  Built output in `frontend/dist` is served by FastAPI; `npm run dev` proxies `/api` to :8000.

## Publishing (Phase 12)
- `clipforge/publish/`: `timez` (UTC storage, IST display, Pacific quota days), `youtube` (client wrapper + fake hook
  `set_client_factory`), `quota`, `channels` (OAuth, settings), `metadata_opt`, `slots` (pure planning), `queue`
  (DB ops), `runner` (API-mode uploads, status), `reconcile`, `batch`, `analytics`, `worker` (2nd thread + 10-min ticker).
- Routes in `api/publish_routes.py`; CLI in `cli_publish.py`; DB tables Channel/QueueItem/VideoPerf/StatSnapshot/ChannelDaily
  (`db._migrate` adds new columns to old databases).
- Tests: `tests/fake_youtube.py` + `tests/test_publish*.py` — never call Google in tests.
- Demo/screenshots: `python scripts/demo_publish.py`, then run the UI with `CLIPFORGE_CONFIG=workspace_demo/config.yaml`
  and `CLIPFORGE_NO_TICKER=1`; `node scripts/screenshots.mjs http://127.0.0.1:8010 1 1-1 phase12`.
- Safety invariants: uploads default to private + publishAt (other visibilities only when the user picks them per clip);
  unconfirmed or paused channels never upload; a clip key is unique per channel; never exceed
  `publishing.insert_calls_per_day`; no browser automation of YouTube.

### Remote / phone (Phase 14)
- `api/auth.py`: `AuthMiddleware` gates every `/api/*` path except health/auth for non-local requests (local = loopback
  peer + no proxy headers + localhost Host). Cookie session (`AuthSession`, token hash only) + CSRF double-submit
  (`cf_csrf` cookie → `X-CSRF-Token`, sent by `frontend/src/api/client.ts`), scrypt password in `.env`, lockout,
  `AuditEvent` log, security headers. Tests use `TestClient(app, headers={"X-Forwarded-For": …})` to act remote.
- `remote.py` (Tailscale status/serve, QR), `service.py` (watchdog + logon task), `watch.py`, `utils/power.py`
  (keep-awake), `stages/preview.py` (540×960 proxy, `?preview=1`), `notify/telegram.py` + `commands.py` (bot,
  `set_bot()` for tests with `httpx.MockTransport`), `api/remote_routes.py`, `cli_remote.py`.
- PWA: `frontend/public/{manifest.webmanifest, sw.js, icons/}` (icons from `scripts/make_icons.py`); never cache `/api/*`.
- Phone screenshots: `node scripts/screenshots.mjs http://127.0.0.1:8010 1 1-1 phase14` (390×844, fails on console
  errors or horizontal scroll, checks the PWA basics).

### Instagram (Phase 13)
- `publish/instagram.py`: Graph client (`GraphIG`, fake hook `set_client_factory`), Facebook Login (`oauth_url`,
  `finish_oauth` → `pick`), tokens in `workspace/accounts/instagram/<ig id>/token.json` (long-lived user token refreshed
  <10 days before expiry; Page token for API calls), `preflight()` (ffprobe Reels specs), `revoke()`.
- `publish/ig_runner.py` (called from `runner._run_locked` per Instagram account): container + resumable upload at
  slot − `upload_lead_minutes`, poll `status_code`, `media_publish` at the slot after `content_publishing_limit`,
  missed after `max_late_minutes`, insights every 12 h → `VideoPerf` rows with id `ig:<media id>`.
- `publish/ig_meta.py`: caption rules (≤5 lowercase hashtags, ≤2,200 chars). `api/ig_routes.py`: Meta app, OAuth,
  manual accounts, `/queue/{id}/permalink`, `/queue/{id}/video`, `/accounts/{id}/ig-dashboard`, `/publishing/overview`.
- Accounts are `Channel` rows with `platform="instagram"` (id `ig:<id>` / `ig:manual:<username>`); items have
  `QueueItem.platform`. Tests: `tests/fake_instagram.py`, `tests/test_instagram.py`. Never automate instagram.com.

### Publishing repair (2026-11-05): modes, enforced transitions, one wording
- `publish/modes.py`: per-account `mode` (auto | assisted) in the channel settings beats the global default
  (`publishing.mode` / `instagram.mode`); manual Instagram accounts are always assisted. Auto that can't work →
  `failed/auto_unavailable` (never a silent fallback). Use `modes.resolve(ch)`, never `cfg.publishing.mode`, in code.
- `states.TRANSITIONS` is enforced in `queue.save` (`IllegalTransition`); `published` needs a platform media id.
  New stored states `processing`, `cancelled`; `states.CANONICAL` / `canonical()` give the spec names.
- `states.status_text()` (via `services.publishing.status_text`) is the ONLY wording of a state: API `status_text`,
  web badges, Telegram `status_line`. Don't write status words anywhere else.
- Claims are leases (`lease_owner`, `lease_expires_at`); `idempotency_key` = `<platform>:<account>:<clip_key>`.
- `clipforge scheduler --dry-run`; `clipforge publish-test <channel>` = private, no publishAt (the token has no
  `videos.update` scope, so a publishAt can't be removed afterwards).

### Publishing v2 (2026-11-06): duplicates, limits, daily plan
- `publish/dedup.py` (pub_key, unique `ux_queue_pub_key`, `add_or_skip`, `duplicate_of`, `republish`, `backfill`),
  `publish/yt_errors.py` (the ONE Google error table), `publish/limits.py` (UploadLedger, eligibility controller,
  cooldown + probe), `publish/plan.py` (daily plan + adaptation), `publish/llm_budget.py`. New states
  `skipped_duplicate` (final) and `waiting_capacity`. Config sections `youtube:` and `schedule:`.
- Tests that need the old fixed slot lists / unlimited uploads write `schedule: {enabled: false}` and a loose
  `youtube:` section into their temp config (see the env fixture in tests/test_publish.py).

### Queue state machine (Phase 12.5, `publish/states.py`)
```
draft → queued → uploading → scheduled (on YouTube: private + publishAt) → published
                          ↘ failed (retryable)   ↘ locked_private
queued → needs_manual (assisted mode only: upload in Studio, then Sync/Link) | missed (slot passed: Reschedule / Publish now)
uploaded = private/unlisted by the user's choice (never goes public by itself)
```
- Every attention state carries `QueueItem.reason` (code in `states.REASONS` → label + UI actions). Never set a bare
  `needs_manual` without a reason.
- API mode uploads as soon as an item is queued (`upload_ahead_days: 0`). `runner._handle_late` applies
  `publishing.late_policy` (next_slot | publish_now | ask) when a slot is <15 min away or passed.
- Assisted mode: before flagging anything, `reconcile.sync` runs (throttled to 30 min). It matches Studio uploads by
  `fileDetails.fileName` (clip file / batch file), then by title (hashtags stripped). YouTube's publishAt wins over our slot.
- `runner.apply_state` stores the verified YouTube state in `yt_state_json` after every upload/refresh.
- `workspace/scheduler_state.json` = last run (source app/task/daemon) for the Health panel (`/api/publishing/health`).
- Worker ticks every 60 s (first tick 5 s after start = catch-up); YouTube status refresh is throttled to 10 min.
- Task Scheduler: one XML task (`utils/winsched.py`): every 30 min + logon + wake event, StartWhenAvailable, hidden (pythonw).

### Telegram (Phase 15a, `notify/`)
- `telegram.py` = Bot (dispatch, pairing, poll loop, ticker, notification hooks `on_job_status / on_render / on_run_report`).
  `tg_core.py` = Ctx, registries (`@command`, `@callback`, `@step`, `@danger`), roles, PIN, scrub, error ids.
  Handlers: `tg_jobs.py` (link/file → wizard, progress, /jobs…), `tg_clips.py` (cards, edit, trim, words, render, undo),
  `tg_publish.py` (schedule, queue, needs-you, health, stats), `tg_admin.py` (help, menu, devices, /app, /server,
  confirmation chain). `tg_api.py` (client: limiter, 429, split), `tg_store.py` (SQLite state), `tg_media.py` (file
  fallback chain), `webapp.py` (Mini App initData).
- State lives in SQLite tables `tgchat / tgcallback / tgconversation / tgkv / tgfile` — never in memory only.
- callback_data is always `c:<id>` (≤ 64 bytes); dangerous actions go through `ctx.ask_danger` (Yes → type YES → PIN).
- Only paired chats get replies; unknown chats are silent. Token, pairing and PIN are laptop-only (403 remotely).
- One poller per token: `tg_store.acquire_poller` lease + 409 back-off; `deployment.mode: worker` never polls.
- Every API route needs a row in the audit table of `docs/TELEGRAM.md` (`tests/test_telegram_audit.py`).
- Tests: `tests/fake_telegram.py` (MockTransport; `msg()`, `press()`, `button(label)`, `transcript()`), `tests/test_telegram.py`.

### Always online (Phase 15b, `server/`, `deployment.mode: standalone | server | worker`)
- `standalone` (default) = everything as before. `server` = owns DB, logins, publishing, Telegram; leases heavy stages to
  workers. `worker` = laptop: `server/worker.py` loop (or inside `clipforge ui`), never publishes, never polls Telegram.
- `server/leases.py`: WorkerNode + StageTask (queued → leased(fence) → committing → done); claim/heartbeat/complete/
  fail, `reap()` re-queues expired leases, caps attempts (3), resets stuck commits and falls back unclaimed tasks to the
  server (`hold` tasks wait). Results only with the current fence; complete is idempotent and verified
  (`dispatch.stage_output_missing` → 422); `dispatch.repair_stuck_jobs()` resumes jobs with no live task.
- `server/dispatch.py`: `delegate()` is called by the job manager before a pipeline task runs (first undone stage →
  worker if one is online, else local; blocked YouTube download → hold task); `on_task_done()` re-submits the job;
  `publishing_allowed()` guards every publish path; reaper thread + nightly backup/cleanup in server mode.
- `server/artifacts.py`: path allowlist, sha256 manifests (cached), staged chunked uploads, all-or-nothing commit.
  `pipeline.py` exposes per-stage runners/fingerprints (`run_download/transcribe/analyze`, `*_done`, `render_todo`,
  `adopt_renders`) so a worker runs exactly one stage with the job's config.
- `server/{worker_api,handover,backup,housekeeping,status,updater}.py`, CLI `cli_server.py` (`clipforge server …`,
  `clipforge worker`), deploy kit in `deploy/` (systemd, not Docker). `CLIPFORGE_ENV` overrides the .env path.
- Tests: `tests/test_server.py` (two "machines" in one process); real two processes: `scripts/deploy_e2e.py`.
- Spec: docs/WORKER_PROTOCOL.md; setup: deploy/README.md; guide: AUTOMATION_GUIDE Part I.
