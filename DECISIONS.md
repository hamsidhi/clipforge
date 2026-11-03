# Decisions log

Every choice made without asking, with the reason. Newest at the bottom of each section.

## Environment (detected 2026-11-03)
| Item | Found | Decision |
|---|---|---|
| Machine | The build ran on the target laptop itself (Windows 11 Pro, Ryzen 7 255, 16 threads, 31 GB RAM), not a cloud sandbox | Everything is tested on the real target |
| GPU | AMD Radeon 780M (integrated) — no CUDA, no NVENC | CPU paths: Whisper `small` int8, libx264. CUDA/NVENC code paths kept and auto-detected |
| Python | 3.14 (default) + 3.12 | venv on **3.12**: mediapipe 1.2 / ctranslate2 publish no 3.14 wheels |
| ffmpeg | missing | Installed `Gyan.FFmpeg` 9.0.2 full build via winget (includes libass). `utils/ffmpeg.py` also finds it in the WinGet packages folder when PATH isn't refreshed yet |
| yt-dlp | missing | pip dependency (2026.10.26) |
| Ollama | installed, only `qwen2.5-coder:7b` (reached via `127.0.0.1`, not `localhost`: on Windows, IPv6-first resolution of localhost made 5 s health checks time out) | Pulled `qwen2.5:7b` (general instruct model, better Hinglish + JSON). `llm.model: auto` prefers general models over coder models |
| Node | 24.21 | used to build the frontend |
| PyPI | `pip install numpy` hung on PyPI's JSON metadata endpoint (other packages fine) | numpy 2.5.3 wheel downloaded directly from files.pythonhosted.org, then everything else installed normally |

## Libraries
- **mediapipe 1.2.0** removed the legacy `mp.solutions` API → use the Tasks `FaceDetector` with the BlazeFace short-range model (auto-downloaded to `assets/models/`).
- **OpenCV YuNet** (`cv2.FaceDetectorYN`, model from opencv_zoo) is used as fallback when BlazeFace finds no face: BlazeFace short-range is tuned for selfie-distance faces and misses the small faces of wide 2-shot podcast frames.
- **Fonts**: Montserrat ExtraBold static TTF from the JulietaUla/Montserrat repo (google/fonts only ships a variable font, which libass renders at the default weight); Poppins Bold from google/fonts; Noto Sans Devanagari Bold static from notofonts. All OFL.
- **LLM HTTP**: plain `httpx` calls to Ollama / Gemini REST / Groq (OpenAI-compatible) — no vendor SDKs needed.
- **ffmpeg ≥7**: `-filter_complex_script` is deprecated → `-/filter_complex <file>`.

## Pipeline
- **yt-dlp format**: as specified (`bv*[height<=1080]+ba/b[height<=1080]`) plus `format_sort` preferring H.264/m4a: fastest to decode on CPU and plays in every browser (needed for the web editor).
- **`--section START-END`** option (CLI + API) to process only part of a video — used for quick tests and useful for very long episodes.
- **Whisper on CPU = `large-v3-turbo`, greedy (beam 1)** instead of `small`/`medium`. Measured on this laptop on Hinglish
  audio: `small` int8 produced unusable gibberish Devanagari (and still ran ~1.5× real time with beam 5), while
  `large-v3-turbo` int8 + greedy produced correct Hinglish at ≈0.5× real time (120 s of audio in 58 s, 8 threads).
  GPU keeps `large-v3` + beam 5. `whisper.model` / `whisper.beam_size` override both.
- **Whisper threads** = physical cores (logical/2): CTranslate2 is slower with hyper-threads.
- **Audio is decoded by us** (wave → numpy) and passed to faster-whisper as an array: faster-whisper 1.2.1 calls
  `av.open(..., metadata_errors=...)`, which PyAV 19 (pulled in by pip) rejects.
- **Whisper**: `condition_on_previous_text=False` to avoid hallucination/repetition loops on long audio; VAD min silence 500 ms.
- **Roman Hinglish is lazy**: only words inside the selected clips are transliterated (≈2–3k words instead of ≈20k for 2 h). On CPU with a 7B model, transliterating a full transcript would take over an hour. Results are cached in `transcript.json` (`roman` field per word).
- **Transliteration fallback**: if the LLM's word count doesn't match, that batch uses rule-based romanization (indic-transliteration ITRANS + Hinglish clean-up) instead of falling back to Devanagari — mixed scripts inside one caption look broken.
- **Selection prompt in Latin script for local models**: Devanagari costs ~3× more tokens on Qwen/Llama; the window transcript is fed romanized (rule-based) to Ollama, original script to Gemini/Groq.
- **Scene detection is lazy**: PySceneDetect only runs on clip ranges ±2 s (cached in `scenes.json` with covered ranges). Full-video detection on a 2 h 1080p file would take 15–25 min on CPU and is only needed for reframing.
- **Heuristic scoring**: +0.4 per audio-energy peak inside the clip (max 2), −0.8 if the start could not be snapped to a sentence start.
- **Too-long candidates** are cut at the nearest sentence end within `max_sec` (deterministic, no extra LLM call).
- **Explicit small `--clips N`** (below `min_count`) is respected — used for tests.
- **Hand edits win**: if `candidates.json` is newer than the last selection, `select` keeps it unless `--force`.
- **Re-selection** deletes old renders in `clips/` (ranks would no longer match).

## Reframe
- 0 faces → `blur` layout in `auto` mode (shows full frame: B-roll, slides, graphics are never cropped away).
- 2+ faces → split-screen if faces are ≥ 35 % of the width apart or don't both fit inside a 9:16 crop; else one crop centred between them.
- `blur` layout = every shot blurred-background (predictable when the user explicitly picks it).
- Tracking: EMA (0.85) + deadzone (4 % width); crop position resets at every camera cut; per-frame `sendcmd` with linear interpolation between 5 fps samples.

## Captions
- One ASS event per active word; line stays put, active word recoloured + pops (`\t` scale 118 % → 109 %).
- Positioned at 65 % height, centred slightly left of the frame centre with 140 px kept clear on the right (Reels/Shorts buttons); long lines are shrunk to fit.
- Hook title: white rounded box (ASS vector drawing) with black text at the top, fades out after 3 s.
- Uppercase for the "Bold Yellow Pop" / "Green Highlight" presets (common viral style); Devanagari never uppercased.

## Render
- 2-pass loudnorm per clip (measure, then `linear=true`) → consistent −14 LUFS.
- Workers: `min(4, cores/4)` parallel libx264 encodes (each x264 is itself multi-threaded); 1 with NVENC.
- One previous render kept in `_work/NN/previous.mp4` for undo in the web editor.

## Code review fixes (after Phase 8)
A review subagent audited the engine; fixed:
- **Snapping**: Whisper segment boundaries now count as sentence boundaries (Hinglish transcripts have very little
  punctuation). End selection prefers sentence ends within the length limits before any word end; a clip whose
  start *or* end isn't on a boundary is penalised. Off-boundary snaps on random spans: ~45 % → ~15 %.
- **LLM selection**: candidates are validated one by one (one bad item no longer discards an 8-minute window); a bare
  JSON list is accepted; windows that fail after retries are retried on the next run (stage not marked done).
- **Caching**: download fingerprint uses the video id (youtu.be vs watch?v= no longer re-transcribes); transcribe/analyze
  fingerprints use the audio file instead of timestamps; selection is checked before contacting the LLM and is keyed on
  the configured (not resolved) model; re-selection only deletes renders whose rank now holds a different clip.
- **Windows paths**: ffmpeg runs inside the clip's `_work/NN/` folder with relative `ass`/`sendcmd`/`fontsdir` paths,
  so user names with `'` or spaces work. Temp renders live in `_work/`, never next to finished clips.
- **Fallbacks aren't cached forever**: template metadata and batch-failed rule-based romanization are retried when the LLM is back.
- **Long episodes**: transcription checkpoints every 2 min (`transcript.partial.json`) and resumes after a crash/cancel;
  audio scaling in place and chunked RMS keep memory down; failed scene detection / frame decoding is reported and retried.

## Code review fixes (web layer, after Phase 9)
- **Job queue**: each task has its own id; cancelling a re-render reports `render_failed` and never marks a
  finished job "cancelled"; a later task for the same job is unaffected by an earlier cancel; the worker thread
  catches everything (a broken config.yaml can no longer kill it). Job title/duration appear right after download.
- **candidates.json**: per-video lock for every web edit; atomic writes use unique temp names; renames retry
  (`safe_replace`) because Windows can't rename a file a browser is still streaming. The editor also releases
  its `<video>` before re-render/undo.
- **Approve toggle**: `approved: null` returns a clip to "to review".
- **Uploads**: ids validated (`^[0-9a-f]{12}$`), a retried chunk truncates to its offset (resumable after a
  dropped request), settings are validated before the upload is consumed.
- **Export ZIP** is written to disk and streamed with `FileResponse` (no 1 GB in RAM), deleted afterwards.
- **SSE**: snapshot carries `last_seq` + `boot_id`; replayed history rebuilds the UI but doesn't re-fire toasts;
  a server restart resets the client's de-dup set.
- **Clip editor**: remounts per clip (Prev/Next); refetches never wipe unsaved edits; the server's snapped
  times are adopted after Save.
- **Settings save keeps config.yaml comments** (in-place value update); multi-line API keys are rejected.
- Frame endpoint validates rank/time; "Render changed" only re-renders stale clips.
