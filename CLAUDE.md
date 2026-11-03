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
- **Faces**: MediaPipe Tasks FaceDetector (BlazeFace short-range) + OpenCV YuNet fallback for small faces in wide shots.
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
pytest                  # unit tests
cd frontend && npm install && npm run build   # build the UI once
```

## Conventions
- ffmpeg via `utils.ffmpeg.run_ffmpeg` (never moviepy). Paths inside filtergraphs: `escape_filter_path`.
- Never swallow errors silently: raise with a message that says what to do, or `log.warning` for cosmetic failures.
- New config keys: add to `config.py` model AND `config.yaml` with a comment.
- Tests in `tests/` for pure logic; use the `mock` LLM provider — no network in tests.
