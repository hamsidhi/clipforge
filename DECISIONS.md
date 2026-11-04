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

## Phase 12 — YouTube publishing (2026-11-04)

### Platform facts, verified against Google's docs on 2026-11-04 (the brief's assumptions changed)
- **Unverified-project lock is gone.** YouTube Data API revision history, 2 Nov 2026: videos uploaded via `videos.insert`
  from unverified projects "are no longer restricted to private viewing mode" (reverses the 28 Jul 2020 policy).
  → API mode is fully usable without the audit. `publishing.unverified_lock: false` by default; if your uploads ever come
  back locked, set it to `true` and API mode falls back to a single test upload (the brief's original behaviour).
  Lock detection (`videos.list` after upload → `locked_private`) still runs on every upload.
- **Quota (since 1 Jun 2026, granular buckets):** `videos.insert` has its own bucket — 100 calls/day by default;
  `search.list` its own 100; everything else shares 10,000 units/day: videos.list 1, playlistItems.list 1,
  channels.list 1, thumbnails.set 50, playlistItems.insert 50, videos.update 50. Quotas reset at midnight
  Pacific time — the local ledger (`workspace/youtube_quota.json`) uses Pacific days. Audit/quota form:
  https://support.google.com/youtube/contact/yt_api_form
- **YPP** (YouTube announcement 10 Aug 2026, via several outlets — no primary help page found): today 1,000 subs +
  10M Shorts views/90 d; fan funding 500 subs + 3M Shorts views/90 d + 3 uploads/90 d; from 1 Feb 2027 new
  applicants need 20M Shorts views/90 d (also existing partners must keep 10M/90 d for Shorts revenue). Values live in
  `config.yaml → ypp:`; the dashboard switches targets automatically on `new_rule_date`.
- **Studio's 15-files-per-upload batch limit** is only documented by third parties → `publishing.batch_max: 15`, editable.
- OAuth "Testing" refresh tokens expire after 7 days → `invalid_grant` marks the channel `needs_reauth` (Reconnect
  button); docs recommend setting the consent screen to "In production" (unverified is fine for personal use).
- Browser automation of YouTube uploads is NOT built (ToS). Playwright is only used for ClipForge's own screenshots.

### Design
- **Default mode stays `assisted`** as requested (nothing is uploaded by ClipForge until you switch to `api`), even
  though the lock is gone; the Schedule page explains that API mode now works without the audit.
- **Second worker thread for publishing** (`publish/worker.py`): uploads are network-bound and must not wait behind a
  1-hour transcription in the main job queue. A ticker runs `run_once()` every 10 min while the app is open;
  `clipforge scheduler --once` + Task Scheduler cover the app-closed case.
- **Uploads go up `upload_ahead_days` (5) in advance** as `private` + `publishAt` — YouTube publishes at the slot even
  with the laptop off. A queued clip whose slot passes before upload becomes `needs_manual` (never auto-shifted:
  nothing gets a slot you didn't approve).
- **Default min gap is 2.5 h, not 3 h:** the requested default slots 18:30 and 21:00 are 2.5 h apart; a 3 h gap would
  silently drop the 21:00 slot every day.
- **Smart fill:** best clips get peak slots (`peak_slots`, default 18:30/21:00); off-peak slots (12:30) take a
  below-median clip when one exists; episodes interleave; same topic ≥24 h apart; posts/day and (API mode) the shared
  daily upload quota cap each day — overflow rolls to the next day.
- **Duplicate titles** = identical after ignoring case/punctuation (fuzzy matching flagged "Clip 1"/"Clip 2" titles).
  Checked against the channel's last 100 planned/uploaded titles (local DB + synced YouTube titles).
- **Title choice is deterministic:** the LLM writes 3 variants, a rubric (length, keyword early, number, curiosity,
  no ALL-CAPS/clickbait) scores them; all variants + reasons are shown in the editor.
- **Reconcile** = greedy best match on normalised title similarity ≥0.8 AND duration within 2.5 s, read-only API calls.
- **Best-time learner** needs ≥20 Shorts with 24-h views, ≥4 per hour, and a ≥25% lead over the median hour; only
  suggests, never applies without a click. 24 h/72 h views come from the Analytics API (Pacific days), falling back to
  local view snapshots.
- **Earnings** = (approved views or views) ÷ 1000 × rate × (1 − fee %); rejected submissions count as ₹0.
- Old single-channel `clipforge upload` + `upload:` config were replaced by the multi-channel system.
- `CLIPFORGE_CONFIG` (alternative config.yaml) and `CLIPFORGE_NO_TICKER=1` exist for demo/testing;
  `scripts/demo_publish.py` builds a demo workspace with a fake YouTube client (used for the screenshots).

### Phase 12 code-review fixes
A review subagent confirmed 16 issues with scratch tests; all fixed:
- **Runner works on live data:** each upload re-reads the item + channel and claims it atomically
  (`UPDATE … WHERE status='queued' AND slot=? AND no video id AND no batch`), so pausing, unscheduling or moving
  during a long run is respected.
- **Never upload twice:** items with a YouTube id or in a Studio batch are never uploaded; `move` refuses linked
  items; batches are blocked in API mode; an upload interrupted by a crash becomes "Needs you" (check Studio + Sync)
  instead of being re-uploaded blindly.
- **Process lock** holds pid + token + heartbeat (refreshed per upload); taken over only if the holder is dead
  (Windows check via OpenProcess — `os.kill(pid, 0)` would kill the process there) or silent for 6 h; only the owner deletes it.
- **Per-channel isolation:** any error (token refresh, HTTP, network) is caught per channel; auth problems
  (`RefreshError`, invalid_grant, insufficient scopes) mark the channel "needs re-login".
- **Missed slots** are flagged in both modes; Studio batches skip past slots; batches are written fully before items
  are marked, into unique folders.
- Linked `needs_manual` items refresh to scheduled/published; Link checks channel ownership and double-linking;
  the item's own upload is not a duplicate title; Regenerate is blocked for clips on YouTube or in a batch; per-video
  analytics wait 5 days and retry incomplete data; unticked OAuth permissions are reported on the channel.
- Batch file route validates the path before touching the filesystem (a `//host/share` path would make Windows
  open an SMB connection and leak an NTLM hash).

## Phase 12.5 — "Needs you" bug (2026-11-04)

### Phase 12.5 diagnosis (checked on this laptop before changing code)
Evidence:
- `config.yaml`: `publishing.mode: assisted`, `api_audited: false`, `unverified_lock: false`, `posts_per_day: 6`.
- `workspace/youtube_quota.json`: 0 `videos.insert` calls ever. ClipForge has **never uploaded anything**.
- Task Scheduler: **no ClipForge tasks** (`schtasks /query` has no match). The scheduler only ran while `clipforge ui` was open.
- DB: 1 channel (`figuring clips`, token OK, confirmed). 19 queue items: 3 `scheduled` (linked by Sync),
  16 `queued`, 15 of them in a Studio batch (`batches/figuring-clips/2026-11-04_050921`).
- YouTube (read-only API, token works): 10 uploads on the channel, all **processed, private + publishAt = 5 Nov**,
  i.e. scheduled correctly by Studio and not locked. Nothing is public yet because every upload is set for tomorrow.
  `videos.list(part=fileDetails)` shows they were uploaded from `workspace/<id>/clips/NN_slug.mp4`
  (not from the batch folder) and **most titles were edited in Studio**.

Cause by cause:
- a. **TRUE: assisted mode.** By design nothing uploads automatically. `runner._flag_missed` turns every `queued`
  item into `needs_manual` as soon as its ClipForge slot is 15 min away, with a generic message.
- b. Not the cause (mode is assisted), but true too: no Task Scheduler task. The 10-min ticker runs only while the
  web app is open, and in API mode the upload window was only `upload_ahead_days`, so a closed app meant missed slots.
- c. Not true. Google lifted the private-only lock on 2 Nov 2026 (revision history; checked again today), and
  the 10 Studio uploads are scheduled, not locked.
- d. Not true. The token refreshes fine. The scopes granted are upload, readonly and analytics.
- e. **TRUE: reconcile bug + state-machine bug.**
  - Sync matched clips only by title (≥0.8 similarity). Titles edited in Studio and the `#shorts` suffix broke it, so
    **7 of 10 Studio uploads were never linked**. Those clips stay `queued` and flip to "Needs you" when ClipForge's slot
    passes, although YouTube has them scheduled (at a different time the user chose in Studio).
  - The batch time and the Studio time disagree, so the "missed" check uses the wrong time.
  - Also, in API mode, batched items are never uploaded (to avoid duplicates), so they always end up as "Needs you".
  - A passed slot always became a reasonless "Needs you", even in API mode.

### Fixes
- **Reconcile** matches by the original upload file name first (`fileDetails.fileName`, owner-only, 1 unit), against
  the clip file (`NN_slug.mp4`) and the batch file. Then by title with hashtags / `#shorts` stripped and word overlap.
  Each pass runs only when the match is unambiguous. Unmatched videos come back with suggestions for one-click Link.
- **Sync runs automatically** before anything is called missed (assisted mode, at most every 30 min, read-only).
- **Explicit state machine** (`publish/states.py`):
  - `draft → queued → uploading → scheduled → published`
  - `failed` (retryable), `locked_private`, `needs_manual` (assisted only), `missed`
  - Every non-happy state carries a `reason` code + message. The UI turns that into one-click fixes.
- **API mode uploads as soon as an item is queued** (quota permitting) as private + publishAt. If the slot is <15 min
  away or passed, `publishing.late_policy` decides: `next_slot` (default, moves it to the next free slot and tells you),
  `publish_now`, or `ask` (state `missed` with both buttons).
- **Verification** after each upload: `videos.list(part=status,processingDetails)`. The real state is stored in
  `yt_state_json` (uploadStatus, privacy, publishAt, processing, failure/rejection reason).
- **Scheduler robustness:** catch-up run on app start; Task Scheduler every 30 min + at logon + on wake from sleep;
  last run / next run / result written to `workspace/scheduler_state.json` for the Health panel.

## Phase 13 — Instagram Reels (2026-11-04)
Facts checked against developers.facebook.com today (differences from the brief noted):
- **Path:** "Instagram API with Facebook Login" (graph.facebook.com + rupload.facebook.com). Only this path supports
  `upload_type=resumable`, which uploads a local file. The Instagram-Login path needs a public `video_url`.
- **Permissions:** the docs list `instagram_basic`, `instagram_content_publish`, `pages_read_engagement` (+ `ads_*` only
  with a Business Manager role on the Page). We also ask `pages_show_list` (list Pages), `business_management` (Pages
  owned through a Business portfolio) and `instagram_manage_insights` (stats). No App Review is needed for your own
  accounts: the app stays in Development mode and your Facebook user is its admin.
- **Business apps** may require "Facebook Login for Business" with a `config_id`. Optional `META_LOGIN_CONFIG_ID` in
  `.env`; without it the classic `scope=` dialog is used.
- **Reels flow:**
  1. `POST /{ig-id}/media` with `media_type=REELS, upload_type=resumable, caption, share_to_feed, thumb_offset`.
  2. `POST rupload.facebook.com/ig-api-upload/<ver>/<container>` with headers `Authorization: OAuth`, `offset`,
     `file_size`, and the file bytes as body. Resume: `GET` the same URL returns the received `offset`.
  3. Poll `status_code` (`IN_PROGRESS` / `FINISHED` / `ERROR` / `EXPIRED` / `PUBLISHED`). Docs: about once a minute, ≤5 min.
  4. `POST /{ig-id}/media_publish creation_id=…`. Containers expire after 24 h.
- **Rate limit:** 100 API-published posts per moving 24 h (one docs section still says 50 for carousels). We read
  `content_publishing_limit` before every publish and keep a margin of 10 (`instagram.publish_limit_margin`).
- **Reels specs:**
  - MP4/MOV with moov first; H.264/HEVC, 4:2:0, closed GOP; 23–60 fps; width ≤1920 px; VBR ≤25 Mbps.
  - AAC ≤48 kHz, mono or stereo.
  - **3 s – 15 min, ≤300 MB.** Captions ≤2,200 chars and ≤30 hashtags (our own rule: ≤5, lowercase).
  - `instagram.preflight()` checks all of this with ffprobe plus a moov/mdat atom scan. Our 1080×1920 H.264/AAC faststart clips pass.
- **Insights:** `views` is the current metric (`plays` / `ig_reels_aggregated_all_plays_count` were deprecated in v22).
  We read `views, reach, likes, comments, shares, saved`. Followers come from `GET /{ig-id}?fields=followers_count`.
- **Graph version** `v26.0` (`instagram.graph_version`, used in the docs' examples).

Design:
- **One queue for both platforms.** `QueueItem.platform`; Instagram accounts are `Channel` rows (`platform=instagram`,
  id `ig:<ig user id>`, or `ig:manual:<username>` for assisted accounts without a Meta app). The same scheduler,
  duplicate guard, states, reasons, fixes, bulk actions and dashboard apply.
- **Timing:** container + upload `upload_lead_minutes` (20) before the slot. Poll each tick; publish at the first tick
  ≥ slot. The ticker runs every 60 s in the app/daemon, so posts go out within ~1 min. More than `max_late_minutes` (90)
  late → `missed` (never posted late) with Reschedule / Publish now. EXPIRED → new container; ERROR → failed + Retry.
- **Tokens:**
  - Code → short token → long-lived user token (~60 days). The Page token derived from it doesn't expire and is used
    for Instagram calls.
  - The user token is refreshed when <10 days are left. Tokens live in `workspace/accounts/instagram/<id>/token.json`
    and never reach the frontend.
  - Error 190/102/10/200 → account "needs re-login" (Reconnect); the scheduler never crashes.
- **Captions:** `ig_meta.build` = hook (if it adds something) + value line + CTA + credit + ≤5 lowercase hashtags (#reels
  replaces #shorts). Editing in the UI is re-cleaned on save (extras stripped, never silently posted). Cover = `thumb_offset` (ms).
- **"Post to Instagram X hours after YouTube":** after autoscheduling an Instagram account, a Reel whose clip is also
  planned on YouTube moves to the first free slot ≥ YouTube slot + `hours_after_youtube` (per account; 0 = never before
  YouTube). "Schedule approved clips" on *All accounts* schedules YouTube first.
- **Assisted Instagram** (manual account, no Meta app, or `instagram.mode: assisted`): the batch becomes an
  *Instagram kit* (files in order + caption copy buttons + times + Meta Business Suite hint). After the slot the clip
  shows "post it yourself". Pasting the Reel link marks it published (and finds its media id for insights when connected).
- **No browser automation of instagram.com** (against Instagram's terms). Licensed music can't be chosen through the API; ignored.
- **OAuth while remote:** both Google and Meta redirect to `localhost`. The Accounts page says "connect on the laptop"
  when opened from another host. The OAuth start endpoints refuse non-localhost hosts.

### Phase 12.5 / 13 review fixes
A review subagent reproduced 8 of 10 findings with scratch tests; all fixed, with regression tests in `tests/test_review_fixes.py`:
1. **"Reschedule all missed" only takes clips that are certainly not uploaded.** Status `missed`; never the reasons
   `interrupted` / `batched_in_api` / `assisted_unlinked` / `not_scheduled_in_studio`, which could re-upload a clip
   already on YouTube. Those keep their per-clip buttons ("check Studio first").
2. **Lost `media_publish` answer:** a container already `PUBLISHED` is looked up with `recent_media` (caption + time)
   and recorded. It is never published again.
3. **Instagram "Publish now" / Retry clear the old container**, so a fresh one is made.
4. **Quota:**
   - the local quota is checked before claiming (a claim counts an attempt), and `QuotaExceeded` doesn't count as an attempt;
   - after YouTube says "quota exceeded", uploads pause until the next Pacific midnight instead of retrying every minute;
   - every waiting clip says "quota".
5. **Instagram never waits behind YouTube uploads:** it runs first, between YouTube uploads and at the end of every
   run, with the clock advanced by the real time spent.
6. **A crash mid-upload is resumed** next run from the offset Meta reports.
7. **Disconnecting one Instagram account doesn't revoke a Facebook login shared with another connected account.**
8. **Disconnect also removes** `missed` / `ready` items.
9. **Keep-awake only for Instagram posts −5…+60 min away** on active, logged-in, API-mode accounts.
10. **Old bare `needs_manual` rows get a reason** (`backfill_reasons`).
11. **Minor:**
    - the Reel link must be `https://(www.)instagram.com/…` (no `javascript:` links);
    - the Facebook redirect always uses `localhost:<port>`, matching the Meta app setting.

## Phase 14 — Use ClipForge from the phone (2026-11-04)
Checked today: Tailscale Serve CLI (tailscale.com/kb/1242): `tailscale serve --bg --https=443 127.0.0.1:8000`, off with
`… off`, `tailscale serve status --json`. It is tailnet-only; Funnel (public) is never used. HTTPS needs MagicDNS + HTTPS
certificates enabled in the admin console (`clipforge remote setup` checks `tailscale status --json` and prints the steps).
- **Who must log in:** anything not coming straight from the laptop. "Local" = loopback peer **and** no proxy headers
  (`X-Forwarded-For`, `Tailscale-User-*`, `Forwarded`, …) **and** a localhost `Host`. Tailscale Serve connects from
  127.0.0.1, so the header/Host checks are what tell a phone apart; a DNS-rebinding page with a foreign Host also counts
  as remote. `auth.trust_localhost: false` makes the laptop log in too.
- **Password:** stdlib `hashlib.scrypt` (n=2^15, r=8, p=1, 16-byte salt). No new dependency (argon2/bcrypt need native
  wheels), same strength class. Only the hash is stored, in `.env` (`CLIPFORGE_PASSWORD_HASH`). Minimum 10 characters.
  Changing it logs every device out. `clipforge auth set-password`, or Settings on the laptop. Remote changes need the
  current password.
- **Sessions:**
  - random 32-byte token in an `HttpOnly; SameSite=Strict` cookie (`Secure` when HTTPS, incl. `X-Forwarded-Proto`
    from loopback); only its SHA-256 is stored in `AuthSession`;
  - "remember this device" = 30 days (`auth.session_days`), otherwise 12 h; devices listed and revocable in
    Settings → Security.
- **CSRF:** double-submit token (readable `cf_csrf` cookie + `X-CSRF-Token` header) on every state-changing request of a session.
- **Rate limit:** 5 wrong passwords per address in 15 min → 15-min lockout (plus a global 15/15 min). Audit-logged.
- **Hardening:**
  - `/api/health` is public and returns only ok + version;
  - `/docs` and `/openapi.json` are laptop-only;
  - CSP `default-src 'self'`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, `nosniff`;
  - `--host 0.0.0.0` (LAN) refuses to start without a password.
- **Service:**
  - one hidden Task Scheduler task at logon runs `pythonw -m clipforge service run`;
  - a watchdog restarts `clipforge ui` with backoff 5 s → 5 min (reset after 5 min healthy) and logs to
    `workspace/logs/service.log`;
  - the UI logs to `clipforge.log` (rotating 5 × 2 MB).
- **Keep-awake:** `SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED)` held by one thread while a job runs or an
  Instagram post is due within 60 min (`power.keep_awake: when_busy`). It doesn't stop lid-close sleep: that's a Windows
  power setting (README).
- **PWA:**
  - manifest (standalone, maskable icon, `share_target` GET /share → New job with the link);
  - service worker caching the app shell only (network-first navigation, cache-first hashed assets, never `/api/*`),
    plus an offline page "can't reach your laptop".
  - Needs HTTPS → Tailscale.
- **Phone UI:**
  - bottom bar Jobs / Gallery (latest episode) / Schedule / Stats / More;
  - swipe right/left on a clip = approve/reject, tap = play;
  - ±0.5 s trim nudges that snap to words, bigger trim handles on touch;
  - pull-to-refresh;
  - resumable uploads (upload id remembered per file, `GET /api/uploads/{id}` tells the offset; 4 MB chunks remotely).
- **Preview proxy:** 540×960, ~0.9 Mbps H.264 + 64 kbps AAC, faststart, in `clips/_work/NN/preview.mp4`.
  - Made after each render and lazily for old clips.
  - Used automatically when the page isn't on localhost ("data saver", toggle per device); the full file is only for
    download.
  - Clip videos get `Cache-Control: private, max-age=86400` (URLs carry `?v=<mtime>`).
- **Telegram:**
  - long polling (`getUpdates`, 50 s) from the laptop, so no public server;
  - pairing with a 6-digit one-time code (10 min); the paired user id is stored in `.env` (`TELEGRAM_CHAT_ID`), and
    every other user is ignored and audit-logged (once an hour);
  - pause and schedule ask for confirmation with inline buttons; job-finished messages carry the top 3 clips as preview
    videos (≤50 MB) with ✅ / ❌ buttons;
  - alerts are de-duplicated; daily summary at 22:00 IST;
  - backoff 1 → 60 s on network errors;
  - plain httpx, tested with `httpx.MockTransport`.
- **OAuth while remote:** both Google (Desktop client) and Meta redirect to `localhost`, so accounts are connected on
  the laptop. The UI says so and the OAuth start endpoints refuse other hosts.
- **`clipforge watch <folder>`:** picks up video files whose size has been stable for 30 s, hands them to the running
  web app (or runs them itself), and remembers them in `.clipforge-seen.json`. Off by default.
- **Optional alternative, not built in: Cloudflare Tunnel + Access.** Public hostname protected by Cloudflare Access
  (email one-time code). Documented with warnings in AUTOMATION_GUIDE Part H; never auto-enabled.

### Phase 14 security review fixes
A security-review subagent found 9 issues (6 confirmed with scratch tests). All are fixed, with regressions in `tests/test_remote.py`:
1. **Other websites could POST to the trusted localhost** (e.g. *Publish now*, overwrite `client_secret.json`).
   - Every `/api/*` request with `Sec-Fetch-Site` other than same-origin/none is now refused.
   - So is any state-changing request whose `Origin` isn't this host (or the Vite dev server).
   - Only the Google/Meta OAuth callbacks (and health) accept cross-site navigations.
2. **`--dev --host 0.0.0.0` let LAN phones in without a password:** the Vite proxy rewrote Host and sent no X-Forwarded-For.
   It now uses `xfwd: true` (no `changeOrigin`). Only an all-loopback X-Forwarded-For still counts as the laptop.
3. **Open redirect on `/login?next=`:** `next` is parsed with `URL` and must be same-origin.
4. **Global lockout DoS:** someone rotating addresses could lock the owner out. The global counter now only slows logins (2 s);
   only the guessing address is locked.
5. **Telegram pairing guesses:** the code is cancelled after 5 wrong attempts, and the attempts are audit-logged.
6. **Test-only hosts (`testclient` / `testserver` / empty Host) were trusted in production:** they now count as local only
   with `CLIPFORGE_TESTING=1` (set in `tests/conftest.py`).
7. **Plain-HTTP LAN mode:** a loud startup warning; the login page warns when not HTTPS. Tailscale is the recommended path.
8. **Hardening:**
   - uploads can't exceed their declared size;
   - remote sessions can't start jobs on arbitrary laptop file paths;
   - the `auth` / `output` / `remote` settings can only be changed on the laptop.

## Phase 15 — Full Telegram control + always online (2026-11-04)

### Facts checked against the official docs on 2026-11-04 (differences from the brief)
- **Telegram Bot API 10.3** (24 Aug 2026): text 1–4,096 chars, captions 0–1,024, `callback_data` 1–64 bytes,
  `sendMediaGroup` 2–10 items, `sendVideo`/`sendDocument` ≤ 50 MB, `getFile` ≤ 20 MB, local `telegram-bot-api` server:
  uploads ≤ 2,000 MB, downloads unlimited. FAQ: ~1 msg/s per chat, 20/min in groups, ~30/s overall (paid broadcasts
  raise it — not used, it costs Stars). 429 carries `parameters.retry_after`. All as in the brief.
- **Mini Apps**: `WebAppInfo.url` must be HTTPS; the docs don't require public reachability — the phone's webview loads
  it. initData: secret = HMAC_SHA256(key "WebAppData", msg bot token); there's also an Ed25519 `signature` for third
  parties (not needed: we have the token).
- **Oracle Cloud Always Free changed**: the official page now says Ampere A1 = **1,500 OCPU-hours + 9,000 GB-hours a
  month ≈ 2 OCPU / 12 GB** (the brief said 4 OCPU / 24 GB — that was the old allowance; community reports say accounts
  above the new cap had A1 instances disabled in Aug 2026). 200 GB block storage, home region only. Idle reclaim:
  95th-percentile CPU < 20 %, network < 20 % and (A1) memory < 20 % over 7 days. Everything in Part B is sized for
  2 OCPU / 12 GB.
- **yt-dlp** (since 2025.11) wants a JavaScript runtime (Deno recommended) + the yt-dlp-ejs component for YouTube; the
  installer adds Deno.

### Part A — Telegram as a full remote control
- **Service layer first** (`clipforge/services/{jobs,clips,publishing}.py`): the bodies of the job / clip / queue routes
  moved there; routes, Telegram and the CLI call the same functions. `ServiceError` subclasses `HTTPException`, so the
  web layer needed no translation and Telegram shows `.detail` (always a sentence).
- **State in SQLite, not memory** (`tgchat`, `tgcallback`, `tgconversation`, `tgkv`, `tgfile`): pairing codes made by
  the CLI work in the running bot, buttons survive restarts, the update offset is durable. The Phase 14 single
  `TELEGRAM_CHAT_ID` is migrated to an owner row automatically.
- **callback_data = `c:<8 random chars>`** → the real action + payload in `tgcallback` (bound to the chat; confirmations
  also bound to the user, 2-minute expiry, one-shot via an atomic `UPDATE … WHERE used_at IS NULL`).
- **Wizard state and progress messages in `tgkv`** (`wiz:<id>`, `prog:<job>`, `render:<clip>`), so a restart mid-job
  keeps editing the same message. Progress is a 2-second ticker reading the job row (no event plumbing into the
  pipeline thread); edits only when the text changed and at most every `progress_every_sec` (default 8 s; the
  per-chat edit bucket allows 20/min).
- **Outbound limiter** in the API client (token buckets: 1/s per chat with burst 3, 25/s overall, 20 edits/min per
  chat); a 429 pauses every sender for `retry_after` (capped at 60 s, 3 retries). Simpler than a separate outbox thread
  and enough for one owner + a few viewers.
- **Splitting**: paragraph → line → word boundaries; a `<pre>` block that must be split is closed and reopened, never
  cut inside a tag. Captions > 1,024 chars go on the media cut cleanly + follow-up messages.
- **`/schedule` without arguments is the 7-day calendar** (the brief's A5); `/schedule <job> [platform]` keeps the Phase 14
  meaning (approved clips → next free slots). Both kept, no conflict.
- **`/trim` times are relative to the clip** (what you see in the preview); `@` prefix = episode time. **Nudge buttons**
  move to the next word boundary in that direction (never a no-op that snaps back), padded like the refine step.
- **Caption script** = the `hindi-devanagari` preset (that's how the existing code decides the script per clip).
- **Bot token, pairing, PIN are laptop-only** in the web API too (403 from a phone), plus `clipforge telegram …` for a
  headless server over SSH. Unpairing a chat is also possible from `/devices` (owner).
- **PIN** = scrypt hash in `.env` (`TELEGRAM_PIN_HASH`), 4–8 digits, 5 wrong → 15-minute lockout, the PIN message is
  deleted via `deleteMessage` right after reading. Required for: publish now/public, delete job, resume account.
  "Unpair" only asks Yes/No (it can only reduce access).
- **Mini App ships off by default** (`telegram.mini_app`): no public URL is needed (Tailscale `*.ts.net` has a real
  certificate and only the phone loads it), initData is checked server-side, and the password session is still
  required. Not tried on a real phone in this environment — documented as unverified.
- **Unknown chats are silent**, also for button presses (we don't even answer the callback query).
- **One poller**: DB lease (`tgkv['poller']`, `BEGIN IMMEDIATE`, 120 s, renewed every poll) + HTTP 409 → release and
  back off 60 s with one warning per hour. Across machines the DB isn't shared, so the 409 back-off and
  "a worker never polls" are what prevent two pollers.
- A pre-existing test (`test_e2e_queue_schedule_upload_batch_reconcile_dashboard`) failed on HEAD because it mixed a
  fixed clock (09:30 IST today) with the real clock in `batch.prepare`; it now passes its fixed `now`.

### Part B — Always online (server + laptop worker)

**Free always-on host, compared (2026-11-04):**

| Option | Cost | RAM / CPU | Runs the pipeline? | Catches | Verdict |
|---|---|---|---|---|---|
| Oracle Cloud Always Free, Ampere A1 | free (card for identity check only) | **2 OCPU / 12 GB** (was 4/24 until 2026), 200 GB disk | yes, slowly (overnight) | home region fixed; "out of host capacity"; idle reclaim after 7 quiet days; datacenter IP → YouTube bot checks | **primary** |
| Google Cloud e2-micro (Always Free, US regions) | free | 1 GB / 0.25–2 vCPU burst | no — bot + queue only | 30 GB disk, US latency | fallback "mini server" |
| Old Android phone (Termux) / Raspberry Pi you own | free if owned | 2–8 GB | no — bot + queue only | home power/Wi-Fi, ffmpeg on Termux | fallback |
| Free PaaS (Render, Fly, Railway…) | sleep / trial / card-billed | tiny | no | sleep kills long polling & the 60 s ticker; trials turn into bills | rejected |

Fallback design: `deployment.prefer_worker_for` = all stages + `worker_grace_minutes: 1440` makes any small box a
"bot + queue only" server whose jobs wait for the laptop.

- **systemd installer, not Docker** (`deploy/install.sh`): 12 GB RAM is tight for Whisper + a 7B LLM + ffmpeg, and Docker
  adds a daemon plus image layers for nothing; Ubuntu's ffmpeg has libass; Ollama has a native arm64 installer with its
  own systemd unit; updates are `git pull` + pip (`clipforge server update`, with rollback). Re-runnable (every step
  checks first).
- **Server binds 127.0.0.1:8000; `tailscale serve` publishes HTTPS to the tailnet only**; ufw denies all inbound except
  `tailscale0` (SSH too, once Tailscale is up); the user also deletes the Oracle security-list rule for port 22
  (documented clicks). Workers reach `https://<server>.<tailnet>.ts.net`.
- **Tailscale auth key**: read from the environment, written to a 0600 temp file for `tailscale up --auth-key=file:…`,
  deleted; `set +x` so it never reaches a log. cloud-init variant documented with a one-off key.
- **Granularity = pipeline stages, not whole jobs**: download / transcribe / select / render are separate leased tasks.
  The server's job thread never blocks on a worker: it creates the task and returns; `on_task_done` submits the
  pipeline again, the normal workspace cache skips finished stages, and the next stage is delegated. This reuses the
  stage cache unchanged (the brief asked for that) and lets the laptop leave mid-job without losing finished stages.
- **Same fingerprints on both machines**: the claim carries the job's full config; the worker only swaps in its own
  `output.dir` and `llm.ollama_url` (both excluded from fingerprints). Stage state (`state.json` entries) travels with
  the results; render.json fingerprints are re-stamped by the server (`adopt_renders`) so render settings that differ
  per machine (NVENC on the laptop) never cause a second render on the server.
- **Fencing tokens** on every claim, results only with the current fence, idempotent complete, all-or-nothing commit
  after sha256 checks, staging per task+fence. Path allowlist (`artifacts.ALLOWED`) + resolved-parent check against
  traversal.
- **Exactly one publisher**: `publishing_allowed()` (never in worker mode; never on a fenced server) checked by the
  scheduler, publish-now and publish-test; queue claims were already unique per item.
- **Download routing**: a YouTube link goes to an online worker first (home IP). Without a worker the server tries;
  `DownloadBlocked` (bot check / 429, 3 tries, 60 s → 120 s backoff, never faster) parks a `hold` task for the laptop
  and Telegram lists the options. Cookies file: optional, server-only, documented with the account risk.
- **Ollama on 12 GB**: `llm.keep_alive` (new, excluded from the select fingerprint so existing selections stay valid)
  set to `2m` on the server so the model leaves RAM before Whisper / ffmpeg need it; default model `qwen2.5:7b` Q4
  (~4.7 GB), `--model qwen2.5:3b` for speed. Whisper int8 uses one thread per core (Ampere has no SMT).
- **Backups**: built-in X25519 + HKDF + ChaCha20-Poly1305 (chunked, last-chunk flag against truncation) because `age`
  isn't on Windows by default and `cryptography` is already installed; real `age` is used when the recipient is an
  `age1…` key and the tool exists. No key → no backup (they contain logins). 7 daily + 4 weekly. Restore tested.
- **Housekeeping and the 90 % job pause only run in server mode** — standalone keeps exactly today's behaviour.
- **Versioned DB migrations**: `PRAGMA user_version` = 15; an older database is copied to `clipforge.db.bak-v<n>`
  before the additive migration runs. `server update` also copies it before pulling.
- **`CLIPFORGE_ENV`** env var overrides the `.env` path (a second instance on one machine: deploy test, demo).
- **Handover** streams `tar` into `ssh host tar -x` (Windows 10+ ships ssh.exe; no rsync needed), verifies with
  `sha256sum -c` on the server, switches the laptop to worker mode *before* the DB snapshot (nothing can change after
  it), moves token files off the laptop into the handover backup (for `--rollback`). Never run by ClipForge itself.
- **Bugs found while building**: `save_config` could corrupt a YAML file it had itself written with block-style lists
  (fallback path) on the next save — lists are now written in flow style and the in-place updater skips old `- item`
  lines. `with sqlite3.connect()` doesn't close the file on Windows → `contextlib.closing` everywhere a copy is made.
- **Arm64 wheels checked on PyPI**: mediapipe 1.2.0, ctranslate2 4.8.5 (cp312), opencv-contrib 5.0.0.93 all have
  `manylinux aarch64` wheels; faster-whisper is pure Python. The installer still falls back to YuNet-only faces if
  mediapipe ever fails to install.
- **No Linux machine here** (no Docker, no WSL distro): the deploy end-to-end ran as two real processes on Windows
  (`scripts/deploy_e2e.py`, log in docs/DEPLOY_TEST.md); `.github/workflows/deploy-test.yml` runs the same test on
  GitHub's free `ubuntu-24.04-arm` runner. Server benchmarks on Ampere are estimates (scaled), labelled as such.
