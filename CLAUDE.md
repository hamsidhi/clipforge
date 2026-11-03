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
