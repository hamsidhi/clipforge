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

### Phase 15 security review fixes (review agent, 13 findings — all fixed, regression tests in tests/test_phase15_security.py)
1. **High — phone could change security settings**: `PUT /api/settings` from a remote session may no longer touch
   `deployment`, `server` or any `telegram` key outside the notification/summary allow-list (a fake `api_base_url`
   would have leaked the bot token and let a fake server impersonate the owner). `telegram.api_base_url` is validated
   (Telegram or loopback only) and `server.backup_recipient` must decode to a real key.
2. **Worker import could overwrite any job**: video ids `.`/`..`/leading dots refused, existing videos → 409,
   `complete()` never takes the workspace from the worker's own result.
3. **Scrubbing**: bot-token URLs, `access_token=`-style pairs, Google/Meta/Instagram/Groq/worker keys, any absolute
   path (Windows, `E:/`, macOS, Linux), IPv6 and `*.ts.net` names, plus every loaded secret value replaced literally;
   scheduler failure and backup-failure notifications are scrubbed too.
4. **PIN in groups**: PIN-protected actions are refused in groups ("use your private chat"); a failed delete of the PIN
   message tells you to delete it.
5. **File modes on the server**: `.env` written 0600, imported tokens/secrets 0600, service `UMask=0077`, `/opt/clipforge` 0750.
6. **Handover**: strict host pattern + `--` before the host (no `-oProxyCommand`), `shlex.quote` for remote names,
   checksum step fails closed, staging extracted by the `clipforge` user with umask 077.
7. **Fencing vs commit race**: `leased → committing` atomically with the fence before files are hashed/moved; the
   reaper ignores committing tasks; a bad upload goes back to `leased`.
8. Worker chunk uploads are streamed with a 16 MB cap (413 beyond).
9. PIN: 6–8 digits, lockouts escalate 15 min → 24 h, owners are told.
10. Messages from anonymous group admins / channel posts (`GroupAnonymousBot`, `sender_chat`) are ignored.
11. Installer: the Tailscale key lives in a non-exported variable (children never inherit it); public SSH is only
    closed when the install session already comes over Tailscale; cloud-init metadata caveat documented.
12. Artifact path allowlist uses `fullmatch`/`\Z` (a trailing newline matched before).
13. `server update`: a narrow sudoers rule lets the service user restart only `clipforge`; a failed restart rolls the
    code back; health must answer with the NEW git revision (`/api/health` shows `rev` to local callers only).
Also: the private GitHub repo needs a read-only deploy key on the server (documented in deploy/README.md step 4).

### Phase 15 reliability drills (review agent ran 8 drills with real processes + fakes; 5 passed, fixes below)
Drills: kill the worker mid-render (✅ server took over / restarted worker re-claimed at fence 2), restart the server
mid-job (❌ → F1/F2/F3), disk full (✅ 409 on new jobs), YouTube blocked with no worker (✅ hold task survives restarts),
expired tokens (✅ login_expired, no uploads), Telegram 429s + network drops (✅ nothing lost or repeated), two pollers
(❌ duplicates when a batch outlived the lease → F4), laptop sleeping through its lease (✅ results dropped, re-claimed).
- **F1 (high) endless loop after a lost hand-in**: a re-claimed task on a laptop that already had the result sent
  nothing. The worker now always offers the stage's outputs that differ from the server's copy (plus the stage
  state), and the server checks the stage is really done before accepting (`stage_output_missing` → 422). `delegate`
  stops after 4 tasks for one stage and does it on the server.
- **F2 (high) hand-in never retried**: `complete` (idempotent) is retried 5 s → 5 min through server restarts before
  the worker takes new work; 422 hands the task back.
- **F3 stuck "running" jobs**: `repair_stuck_jobs()` in the reaper resumes any running/queued job with no live task and
  nothing queued; server start resumes interrupted jobs instead of failing them (standalone keeps "Interrupted → Retry");
  `on_task_done` errors are logged and repaired.
- **F4 Telegram updates handled twice**: the poller lease is renewed before every update (300 s) and the offset only
  moves forward (`advance_offset`, atomic) — an old poller can never re-handle updates.
- **F5 poison tasks**: after 3 vanished workers a task goes to the server (`fallback`), or fails if it's a `hold` task.
- **F6 fence failed open**: if the fence can't be read, a server doesn't publish.
- **F7 interrupted commits**: `committing` tasks are reset on server start and after 10 min; they're cancellable.
- **F8**: imports respect the disk pause; worker uploads can't push the disk past `disk_pause_pct`.
Regression tests: tests/test_reliability.py (each drill), plus the earlier unit tests.
Not fixed by design: after `worker --standalone-emergency --server-is-down` the two databases diverge; keeping the
old server from publishing when it comes back is on you (stop it; the fence is set the moment it's reachable).

### Incident during Phase 15 (2026-11-04, 16:00–17:01 IST) and the config fixes it led to
- While building Phase 15 the new keys were added to the working `config.yaml`. The **already running** `clipforge ui`
  (old code, `extra="forbid"`) re-reads config.yaml on every request and rejected the whole file: its scheduler and
  API failed for an hour. No post was missed (the posts due that hour were already on YouTube as private + publishAt,
  and queued uploads were ten days ahead). Fixed at 17:01 by taking the new keys out of the working copy — every new
  key has a default, so old and new code both read it; the committed `config.yaml` documents them.
- Follow-up: `save_config` used to rewrite the whole file (losing comments) when a key was missing from it. It now
  inserts missing keys at the end of their section (or appends the section), so an older config.yaml keeps its
  comments and your own values. Restarting `clipforge ui` after pulling is still required for new code.

## Transcript input + clip-only Whisper (2026-11-05)
- Full-video Whisper is the slowest stage (~0.45× real time on CPU: ~54 min for 2 h). A pasted transcript or YouTube
  captions replace it (`transcript.source: auto` = pasted > captions with word offsets (json3 auto) > captions without
  > Whisper). Lines without word timing get their words spread evenly — fine for the LLM and for snapping.
- After refine, faster-whisper runs only on each chosen clip ± `clip_pad_sec` (ffmpeg cut of audio.wav), the exact
  words are spliced into transcript.json (`exact_ranges`), boundaries are snapped again (only onto exact words) and
  captions use them. Cached per range in `clip_whisper/`, so re-renders and trims inside a range never re-run it.
- The transcribe fingerprint only changes when a pasted transcript or a non-auto source is set, so jobs that Whisper
  already transcribed keep their cache. Sanity checks (end time >10% off the duration, wrong script) only warn; the
  job keeps going and "Use Whisper instead" (web / Telegram) switches the job's source and re-runs from transcribe.

## Faster selection (2026-11-05)
Goal: a much faster `select` stage with local models only, without lower clip quality. Measured on two cached Figuring
Out episodes — TjPPIf-M6KE (Sam Altman, 68 min, 10 windows) and 67AUqNeRf6U (Bryson DeChambeau, 150 min, 22
windows) — with qwen2.5:7b (Q4_K_M) on the laptop CPU (AMD Ryzen 7 255, 8 cores / 16 threads, Radeon 780M iGPU), Ollama 0.40.13.

### Method
- `scripts/bench_select.py` runs `pipeline.select(force=True)` on a COPY of an episode (info/transcript/energy only,
  in `workspace/_bench/<label>/`) — real candidates.json and rendered clips are never touched, no window cache is
  reused. It records wall time, Ollama's own counters per call (prompt/output tokens + seconds) and the final clips.
- `scripts/bench_commit.sh <commit> <label>` runs that from a throw-away git worktree pinned to a commit, so editing
  the main checkout during a 40-minute run can't change the code under test; then runs the quality check.
- `scripts/compare_selection.py` (quality check): top-20 overlap (same moment = >50 % of the shorter clip), average
  pipeline score, a **judge score**, boundary % (starts on a sentence boundary — punctuation, ≥0.6 s pause or Whisper
  segment start, the definition refine snaps to — and lasts 20–59 s ±0.5 s), new/dropped clips.
- **Judge score instead of the pipeline score for the pass rule**: the pipeline score is the selection LLM grading its
  own picks, and its scale changes with the prompt (A replaces five sub-scores by three). The judge is one fixed
  prompt (hook / standalone / payoff of each final clip's transcript text, qwen2.5:7b, temperature 0, cached), so
  baseline and new runs are rated the same way. Caveat: same model family as the selector, so it is a consistency
  check, not ground truth — watch the new/dropped clips.
- **Noise floor**: the baseline code was run twice. The two runs agree on only **50 %** of their top-20 (65 % / 35 %
  per episode; temperature 0.3, no seed), judge 5.91 vs 5.89. So the requested "overlap ≥ 80 %" is above what the old
  pipeline achieves against itself. Decision: for prompt-changing steps the overlap bar is the noise floor (50 %),
  the reference judge/boundary values are the mean of both baseline runs, and a change that changes the picks more
  than that is only kept when the judge score rises on both episodes (A); prompt-identical steps (D, E) keep the
  strict 80 %. Every new/dropped clip is listed for a human to watch.
- **Ollama prompt cache**: the second baseline run took half the time with identical token counts — Ollama's
  llama.cpp runner served the identical prompts from its prompt cache (~30 000 tok/s "prompt eval"). Its timings are
  discarded (its picks are valid). Every timed run below used prompts Ollama had not seen. Baseline TjPP window 0 was
  also cache-warm from an aborted first attempt (≈50 s understated — in the old code's favour).

### Baseline (old code, 694178b)
| episode | windows | select | prompt tok | output tok | per window |
|---|---|---|---|---|---|
| TjPPIf-M6KE | 10 | 759 s | 37 644 | 2 893 | ~76 s |
| 67AUqNeRf6U | 22 | 2 122 s | 90 668 | 8 837 | ~96 s |
CPU speed: ~90 tok/s prompt eval, ~8.3 tok/s generation → output was ~45 % of the time although it is <10 % of the
tokens. Fixtures: `tests/fixtures/select_baseline/<id>.json`.

### Steps (each benchmarked on both episodes, cumulative, vs the baseline)
| step | select (both) | vs baseline | overlap | judge (ref 5.90) | boundary | decision |
|---|---|---|---|---|---|---|
| A compact JSON + title pass | 2 259 s | −22 % | 30 % | 6.24 | 100 % | kept |
| B numbered lines, fillers, num_ctx | 1 417 s | −51 % | 20 % | 5.92 | 100 % | kept |
| C num_thread / seed / temp 0.2 | 1 387 s | −52 % | 30 % | 5.87 | 100 % | reverted |

**A — compact output.** Per candidate only `{"s","e","score","hook","standalone","topic"}` (sentence ids), with
`num_predict = 56 × max_candidates + 64` (a candidate is ~35 tokens). Output tokens −62 %. Titles / hooks / `why`
are written after refine for the final clips only (`write_titles`, batches of 5, each clip's full text, prompt
`prompts/clip_titles.md`); a failed batch falls back to a template title (logged), selection never fails on copy.
YouTube/Instagram descriptions stay in the metadata stage (already final-clips-only and keyed on title/hook, so a
hand-edited title regenerates them). The overall `score` stands in for emotion/value/controversy in `Scores`, so
refine's weighted score keeps its 0–10 scale. Prompt tokens rose 10 % (ids added to the timestamped lines; B removes
the timestamps). Kept although the overlap (30 %) is below the noise floor: the judge score rose on BOTH episodes
(6.10→6.52, 5.72→5.97), also among clips <35 s only (6.29 vs 5.83/5.71), so it is not just a preference for
longer clips (A's clips average 36 s vs 26 s). Title quality was comparable but generic → the titles prompt now asks
for the concrete claim/number/name (8221b6c; doesn't change which clips are picked).

**B — smaller input.** Lines are `12| text` (no timestamps; ids map back to the first word of the first sentence and
the last word of the last one, both inclusive). Fillers are removed in the LLM input only (`strip_fillers`:
um/uh/hmm; immediate repeats of a list of function/filler words — "haan haan", "like like", "the the" — but not real
Hindi reduplication like "dheere dheere"/"kabhi kabhi"; "you know" only as a filler, "you know what…" stays;
"matlab" only at a line start/after punctuation/before a comma, "iska matlab hai" stays). A line left empty is omitted
without renumbering. The prompt tells the model ~2.8 words/s (measured 2.9–3.05 on these episodes) so it can still
aim for 20–59 s; raw candidates in 20–59 s: 58 % (A: 53 %). `num_ctx` = largest window prompt / 2.5 chars per token
(an over-estimate; measured 3.55–3.62) + reply, rounded up to 1024 (here 5120 / 6144 for a largest prompt of 2 856 / 3 180 tokens) — ONE value for the whole stage incl. the
title/re-rank calls, because a different num_ctx makes Ollama reload the model. No prompt was truncated (Ollama log:
`truncated = 0` throughout). Prompt tokens −30 %; generation got faster too (8.3 → 10.2 tok/s with the smaller
context). Judge 5.92 vs 5.90 → passes; it is 0.3 below A's single sample, C (same input format) scored 5.87, so the
B-format picks are as good as the baseline's; A's higher score was likely partly luck.

**C — Ollama tuning: reverted.** num_thread = physical cores (8) gave the same speed (97 / 10.2 tok/s) because
Ollama's default already is one thread per physical core; temperature 0.2 + seed 42 changed the picks and the judge
score was 0.03 lower than the reference (inside the noise, but the rule is "not lower" and C bought no speed).
`llm.num_thread` / `llm.seed` stay as options (default: Ollama's thread count, random seed). keep_alive (30m) was
already sent on every request.

**E — prefilter (`select.prefilter: true`).** Skips a window only when it has <0.5 words/s (silence/music) or when
≥60 % of its words are in intro/sponsor/outro sentences (keyword list) — never for low energy or a dull topic. With
8-minute windows this never triggers on real episodes: 0 of 10 / 22 / 24 windows on the three cached episodes, so
the prompts are byte-identical to B's, the picks unchanged and the time saved 0 s. Kept because it is free and only
fires on genuinely empty or ad-only windows (e.g. a silent stream start).

**F — re-rank (`select.rerank: true`).** refine was split into `score_pool` (snap/score/dedupe) and `pick` (greedy
topic-diversity pick). The best `rerank_pool` (40, never fewer than clips.count) are re-scored by the same 7B model,
5 per call, each with 20 s of what is said BEFORE and AFTER it (`prompts/clip_rerank.md`: rate standalone low if it
leans on earlier context, the score low if the payoff only comes after the cut). New sub-scores = mean of the window
pass and the re-rank (two independent looks are steadier than one); refine's energy-peak / mid-sentence adjustment is
kept; then the usual diversity pick. Cost +285 s on the two episodes; judge 5.94 vs 5.87 for C right before it →
passes (overlap not required for this step). The measured gain is small and within run-to-run noise.

**D — Radeon 780M (Vulkan).** Current Ollama ships the Vulkan backend on Windows and enables it, but its startup log
says `dropping integrated GPU; to enable, set OLLAMA_IGPU_ENABLE=1` (ROCm has no gfx1103 kernels). Benchmarked
WITHOUT touching the user's settings: a second `ollama serve` on 127.0.0.1:11435 with OLLAMA_IGPU_ENABLE=1 set only
for that process (stopped afterwards). Same window prompt, 3 runs each: prompt eval 85 → 285 tok/s (3.3×),
generation 10.0 → 15.2 tok/s (1.5×), one window 42 s → 16.7 s; model 100 % in GPU memory (16 GB shared). Full run of
the same code (8d4fdd3) on both episodes: 1 736 s (CPU) → 732 s (iGPU) = 2.4× faster, 53 calls, no errors in the
server log. Outputs are not bit-identical (different float maths): top-20 overlap with the CPU run 42 %, judge 5.82
vs 5.53 for the CPU run, boundary 100 % → passes. NOT switched on automatically (it is a user environment variable):
README "Speed" + a `clipforge doctor` line ("Ollama GPU") explain it. Undo: delete OLLAMA_IGPU_ENABLE (or set
OLLAMA_VULKAN=0) and restart Ollama.

### Final runs (all kept changes, step 4)
| run | code | select both episodes | judge | boundary |
|---|---|---|---|---|
| baseline (+ re-run) | 694178b | 2 881 s | 5.91 / 5.89 | 100 % |
| final2, CPU | d621059 (after review fixes) | 1 726 s (−40 %) — TjPP 558 s, 67AU 1 168 s | 5.91 | 100 % |
| final, CPU | 8d4fdd3 (before review fixes) | 1 736 s | 5.53 | 100 % |
| D, iGPU | 8d4fdd3 | 732 s (−75 %) — TjPP 245 s, 67AU 486 s | 5.82 | 100 % |
Without the re-rank pass the selection is ~1 400 s (−51 %); the re-rank spends ~300 s of the saving on quality.

**Honest read of the judge numbers.** The judge varies ~±0.2 between runs of the same code (final 5.53 vs D 5.82,
same commit). All runs with the B input format (B, C, F, final, final2, D, and "Bts" = today's code with the
timestamps kept) average 5.85 — the same as the baseline (5.90). The two A runs (A: 6.24; A2 on the iGPU: 6.04)
average 6.14, ~2 standard errors higher. Bts (5.94) shows the timestamps are not the cause; filler stripping only
removes 0.5–0.7 % of the text (the ~20 % input saving is the timestamps). Not resolved further: separating a 0.3
difference from ±0.2 noise needs several seeded runs per variant. The kept configuration meets the bar (not lower
than the baseline); it may have given back A's small gain.

**Total pipeline time** (stage timestamps of the original runs; only select changed): TjPP 2 214 s transcribe +
841 s select + ~195 s render ≈ 54 min → ≈ 49 min (CPU) / ≈ 44 min (iGPU); 67AU 5 327 s transcribe + 2 187 s select
+ ~288 s render + ~414 s metadata ≈ 2 h 17 min → ≈ 2 h 00 min (CPU) / ≈ 1 h 49 min (iGPU). Whisper still dominates
when it runs on the whole video; with pasted/YouTube transcripts (previous change) select is the main LLM cost.

### Review (subagent) findings — fixed in d621059, regression tests in tests/test_select.py
- A reply in the wrong shape (e.g. old start/end items) passed the lenient wrapper, was cached and silently gave the
  window 0 candidates → `CompactRawList` rejects a reply with no usable item, so `complete_json` retries it.
- `rerank_pool` < clips.count cut the number of clips → the shortlist is at least the final count.
- `strip_fillers` removed "you know" used as a verb ("Do you know?" → "Do?") → only removed when set off by
  punctuation and not after do/what/if/everything/kya….
- Cache keys lacked temperature/seed → added; cache writes are atomic and an unreadable cache file is a miss.
- num_ctx is estimated from UTF-8 bytes (safe for Devanagari); the schema caps candidates at max_candidates_per_window.
- Not changed (noted): refine's title-based dedupe no longer applies (titles are written after refine; topic+time
  overlap dedupe still does); a failed re-rank batch keeps first-pass scores.
- The full test suite once failed in `test_telegram.py::test_e2e_link_wizard_progress_clips_approve_schedule_queue`
  in 2 of 4 full-suite runs (passes alone, ×3, and as a file) — it uses seeded fake clips and time-throttled progress
  edits and never runs select: a load/timing flake, not caused by this change.


## Publishing repair (2026-11-05)

Brief: make automatic publishing (YouTube Shorts + Instagram Reels) work end to end and make every status honest.
Runs unattended; every decision is logged here. Final write-up: `PUBLISHING_REPAIR_REPORT.md`.

### Facts checked first (differences from the brief)
- **The unaudited private-only lock is gone.** The brief assumed unaudited projects are locked private. Google's
  YouTube Data API revision history, entry **2 Nov 2026**: videos uploaded via `videos.insert` from unverified projects
  "are no longer restricted to private viewing mode"; the `videos.insert` page now says the same (checked today).
  → Auto mode works without the audit. Locked-private detection stays (own state `locked_private`, not a failure).
- **`videos.update` needs the `youtube` / `youtube.force-ssl` scope.** The channel's token has `youtube.upload`,
  `youtube.readonly`, `yt-analytics.readonly` only. So ClipForge can't remove a `publishAt` or delete a video after the
  fact. Re-consenting would change the OAuth grant (not allowed by the brief) → the live test is a plain **private**
  upload without `publishAt` (can never go public), instead of "publishAt +2 h, then clear it".
- Quota: `videos.insert` = its own bucket (100 calls/day, 1 unit per call on today's page); other calls share 10,000
  units/day (videos.list 1, videos.update 50, …). Unchanged from Phase 12; the local ledger already uses Pacific days.

### Root cause (evidence from this laptop, read-only)
1. **YouTube → Studio for every clip: `config.yaml` `publishing.mode: assisted`** (the committed default since
   Phase 12, `config.py:137`). There was no per-account mode. `runner._run_locked` computed `budget = 0` unless
   `cfg.publishing.mode == "api"` and `_upload_due` only ran in api mode; `_handle_late`'s assisted branch then flipped
   every passed slot to `needs_manual/assisted_unlinked` or `missed/assisted_no_batch`. Evidence: `youtube_quota.json`
   shows **0 `videos.insert` calls ever**; DB: 54 YouTube items `queued` (15 in a Studio batch), 9 `published` (all
   Studio uploads linked by Sync, real ids, `privacyStatus: public` read from `videos.list`), 1 `needs_manual`.
2. **Instagram → "post it yourself": the only Instagram account is a manual one** (`ig:manual:figuringclips.in`,
   added without Facebook Login): no IG user id, no Page, no token file. `ig_runner._assisted` returned True for
   manual accounts **and silently also when the Meta app was missing or `instagram.mode` was assisted**, so the 13
   Instagram items could only ever become `needs_manual/ig_assisted`. The Meta App ID/Secret in `.env` are valid
   (a `client_credentials` grant succeeds) but no account was ever connected with them.
3. **The scheduler runs and does call the platform client** — but only inside the web app (in-app ticker every 60 s;
   `scheduler_state.json` shows source `app` only). No Windows Task Scheduler task is registered.
4. YouTube upload code (resumable `MediaFileUpload`, 8 MB chunks, `next_chunk(num_retries=5)`) was correct but never
   exercised; a successful upload was immediately written as `scheduled` before reading the platform state.
5. Instagram code path (container → `rupload` → poll `status_code` → `media_publish` once) was correct; two honesty
   bugs: a container reported `PUBLISHED` whose Reel couldn't be found was still marked `published` **without a media
   id**, and Graph `error_subcode` / `error_user_title` were dropped (no way to tell "bad format" from "try later").
6. Sync/reconcile queries the right channel (its uploads playlist + `videos.list` with `fileDetails`); no change.

### Decisions
- **Extend `publish/states.py`, don't rename stored statuses.** The running `clipforge ui` process (old code) shares
  the DB; renaming `queued`→`SCHEDULED` etc. would break it mid-session. Stored values stay lowercase; new ones:
  `processing` (AWAITING_PLATFORM_PROCESSING) and `cancelled`. `states.CANONICAL` maps stored values to the brief's
  names; RETRYING = `queued` + `next_attempt_at`, REQUIRES_REAUTHENTICATION = `queued` + reason `login_expired`.
- **One transition table** (`states.TRANSITIONS`), enforced in `queue.save` (the single write path): an illegal move
  raises `IllegalTransition` and is logged. `published` is final and requires a platform media id (exception: a Reel
  link pasted for a manual account, labelled "Posted by you", since no API id can exist there).
- **Per-account mode** (`publish/modes.py`): the account's own `mode` (auto | assisted) beats the global default;
  a manual Instagram account is always assisted. Auto is never downgraded: if it can't work (no token, no
  `youtube.upload` scope, no Meta app) the due clips become `failed/auto_unavailable` with Reconnect + "Switch to
  assisted", and go back to the queue by themselves once it works.
- **I did not switch the user's channel to auto.** That would make the running app upload ~40 scheduled clips at once
  (more than the brief's "at most 1 live upload" and with titles the user may still edit). It is one click
  (Accounts → Mode → Auto) and listed as the user's step.
- After an upload the item is `processing` until `videos.list` says otherwise (never assumed `scheduled`).
- YouTube errors: `invalid_grant`/revoked → channel `needs_reauth`, item `login_expired`; `quotaExceeded` → waits for
  the next Pacific day; `uploadLimitExceeded` → `failed/upload_limit` with YouTube's message; other 4xx → failed at
  once with the real message; 5xx/network → `retrying` with exponential backoff (the client library already retries
  chunks within the same resumable session).
- "Publish now" (public) needs auto mode, an explicit confirmation (web: confirm dialog; Telegram: YES + PIN), and
  is refused only when `unverified_lock` is on and the project isn't audited (the brief's "only after api_audited"
  was based on the lock, which Google removed).
- Instagram: after the bytes are received the item is `processing`; a `PUBLISHED` container without a findable
  Reel stays `processing/ig_find_media` (keeps looking, offers Link); Graph errors carry code/subcode/user title;
  format subcodes (2207026 …) fail at once as `ig_format` (retrying the same file won't help); tokens are redacted
  from every error text.
- New columns (additive, defaults, old rows untouched): `last_attempt_at`, `lease_owner`, `lease_expires_at`,
  `idempotency_key` (`<platform>:<account>:<clip_key>`). Unique indexes: (clip_key, platform, channel_id),
  (platform, youtube_video_id) and (platform, ig_media_id) where set. Before migrating, the DB is copied to
  `workspace/backups/clipforge-<timestamp>-v<old>.db` (plus the existing `clipforge.db.bak-v<old>`).
- The claim (`queued → uploading`) is also a lease (owner `<host>:<pid>`, 2 h); the process lock stays the first
  guard. `recover_stuck` clears leases and never re-uploads blindly.
- `states.status_text()` is the one wording of a state; the API sends it as `status_text`, the web app shows it and
  Telegram (`tg_publish.status_line`, `/health`) uses the same function.
- New config keys: `publishing.contains_synthetic_media` (false), `instagram.test_account_id` ('').
- Not done on purpose: no Windows task registered (persistent system change → user's step); no OAuth re-consent;
  no browser automation; no hosting/tunnels for Instagram (resumable upload of the local file needs none).

### Results (live, read-only except one private upload)
- Live YouTube check: `clipforge publish-test` → real id `Cx093Vl1V7M`, read back `uploadStatus: processed`,
  `privacyStatus: private`, `processingStatus: succeeded`. Private without publishAt (never public). Quota: 1 insert.
- Migration ran on the real DB (v15 → v16) after the backup `workspace/backups/clipforge-20261105-162429-v15.db`;
  every row kept its status; the three unique indexes exist (no duplicates were present).
- Instagram live: not possible (no account connected via Facebook Login, no `instagram.test_account_id`). The Reels
  preflight passes on a real rendered clip.
- Also fixed on the way: manual Link accepted only `…/<id>` URLs (`watch?v=` became "watch") → `reconcile.parse_video_id`;
  upload paths are refused outside `workspace/`; the Schedule header showed the global mode instead of the account's.
- Sync matching unchanged (file name, then title + duration). Upload time isn't used as a signal: Studio times are
  user-edited, so it would add false matches.


### Follow-up after going live (5 Nov 2026, 17:20–17:45 IST)
- **Crash:** `/api/queue`, autoschedule and the channel's scheduler run failed with "can't compare offset-naive and
  offset-aware datetimes". DB datetimes load UTC-aware; the private test video (no `publishedAt`) fell back to a
  naive `datetime.min` in `queue.existing_titles`. Fixed (aware epoch + `as_utc`), regression test, app restarted.
- **YouTube's per-channel upload limit is real:** after ~10 uploads in a day (the test + 9 scheduled clips, all
  private + publishAt and verified), `videos.insert` answered `400 uploadLimitExceeded` ("The user has exceeded the
  number of videos they may upload"). The first version marked each clip FAILED and kept calling (29 calls in
  19 s). Now the first refusal pauses that channel's uploads for 6 h (`runner.UPLOAD_LIMIT_PAUSE`), every waiting
  clip says so with its next try, attempts aren't counted, and the label clears once an upload works.
  → With `posts_per_day: 10` this channel's limit is roughly one day of posts per day; uploads catch up daily.
- The 29 refused clips were deleted from the queue by a user action (Delete) between 17:34 and 17:39 IST (no code
  path deletes queue items by itself); not re-added.
- The 15 clips still in an old Studio batch were moved to auto upload (`unbatch`) after Sync confirmed none of them
  is on YouTube (the only unlinked channel video is the private test). They wait for the upload limit like the rest.

## Publishing v2 (2026-11-05/06)
Brief: Instagram auto-publish, no duplicates, YouTube limit controller, daily schedule, independent pipelines,
honest /health and /needsyou — with no Claude/agent needed to run the queue. Report: `PUBLISHING_V2_REPORT.md`.

### Facts checked first (docs, 2026-11-05)
- Instagram publishing (Facebook Login path) needs `instagram_basic`, `instagram_content_publish`,
  `pages_read_engagement`; `pages_show_list` to list the Pages. `business_management` is not listed as required
  (kept as optional for Pages owned through a Business portfolio). Development-mode apps get Standard Access for
  people with a role on the app: no App Review for your own account.
- Redirect URIs: Strict Mode is required (exact match: scheme, host, port, path). Meta's security / manual-flow pages
  document no localhost exemption, so the URI must be added explicitly:
  `http://localhost:8000/api/instagram/oauth/callback` (no trailing slash). "Enforce HTTPS" is on by default for new
  apps; if Meta refuses the http URI, that setting is why (TODO_ON_LAPTOP 0b).
- Reels insights: `views` is current (`plays` gone), saves = `saved`. YouTube Analytics has `creatorContentType=SHORTS`.
- `videos.insert` errors: 400 `uploadLimitExceeded` ("The user has exceeded the number of videos they may upload"),
  403 `quotaExceeded` (general table), 403 `forbidden`, 400 invalid* metadata reasons.

### The 9 uploaded Shorts (read-only `videos.list(part=status)`, 5 Nov 19:04 IST)
All 9: `uploadStatus: processed`, `privacyStatus: private`, `publishAt` = their slot (6 Nov 01:30Z … 7 Nov 07:00Z).
None is locked private (a lock drops publishAt); Google lifted the unaudited lock on 2 Nov 2026. Untouched.

### Instagram root cause
- `figuringclips.in` was created by `channels.add_manual_instagram` (Accounts → add manually): id
  `ig:manual:figuringclips.in`, `extra.manual = true`, no IG user id, no Page, no token
  (`workspace/accounts/instagram/` doesn't exist); `modes.resolve` → assisted (`manual_account`). Facebook Login was
  never completed on this laptop, so no code path could make it auto.
- Fixes: `finish_oauth` runs `login_checks` (me/permissions + debug_token with the app token); `pick` →
  `register_instagram` converts a manual account with the same username **in place** (same row id: queue items and
  tracked Reels stay attached) and sets mode `auto` only when every publish permission is granted and the token is
  valid (else `assisted`). `ig.eligibility()` reports professional / Page / IG id / token + expiry / permissions /
  publishing limit / auto-ready; "Connected" ≠ "Auto-publish ready" in the UI and Telegram.
- `media_publish` returns a media id; the item becomes PUBLISHED only after `GET /{media-id}` returns it.
- No Instagram `video_url` path exists (the only `video_url` is the web app's own clip preview URL).

### Duplicates
- `pub_key = sha256(video_id | rank | clip file sha256 | platform | account | intent)`, unique index
  `ux_queue_pub_key` (replaces v16's (clip_key, platform, account) index so an explicit republish can coexist).
  The file hash is cached per render in `clips/_work/sha256.json` (size + mtime).
- A "logical twin" (same episode + rank + platform + account + intent: after a re-render changed the hash, or a pre-v2
  row) also counts: the same clip is never posted twice on one account without an explicit republish.
- Checked when adding (transaction + unique index; IntegrityError = duplicate) and again as the first check of an
  upload job. Duplicates become `skipped_duplicate` (final, kept), never deleted. `dedup.backfill` keys old rows once
  per process.

### YouTube limits
- One error table: `publish/yt_errors.py` (`REASON_TYPE` on the real `reason`, then HTTP status, then network).
- Ledger `UploadLedger`: every attempt (both platforms); only `succeeded_at` rows count as uploads.
- `publish/limits.py` before each upload: cooldown → project quota (inserts, units ≥ 90%) → successful uploads in
  24 h < `youtube.daily_safety_limit` (8) → ≤1 active upload → `min_gap_sec` (120 s, also inside one run, so a run
  uploads one clip and the next minute's run the next) → `max_per_batch` (2). `override_until` skips cooldown +
  safety limit.
- `uploadLimitExceeded`: the channel's waiting clips become `waiting_capacity` (slots kept); cooldown = first
  successful upload of the 24-h window + 24 h (≥30 min from now). Then ONE probe upload: success clears the cooldown
  and the batch continues; a refusal starts a new cooldown. Clips whose slot passed move to the next free slot and
  you're told on Telegram.
- Deviation: during a cooldown the read-only status refresh (`videos.list`) still runs, so already scheduled Shorts
  still flip to "published"; no uploads, no metadata/LLM work for that channel.

### Daily schedule
- `publish/plan.py` + `schedule:` config; `slots.Plan.day_plan` (per-day lists; slots before `day_cutoff` 05:00
  count for the evening before). Used when an account has no explicit `slots` in its own settings.
- Instagram 7/day (3 morning + 4 evening), 8–9 when approved clips cover today + 1 day. YouTube 5/day, 6 when the last
  7 days' median 24-h views ≥ the 7 before (≥3 Shorts each), capped by the safety limit.
- Adaptation: deterministic, `VideoPerf.views_24h` (70% last 14 days), ±30/60 min, ≥10% better, inside windows
  (IG 07:00–12:00 / 18:00–01:30, YT 11:30–00:30), weekly; one exploration slot a day once active.
  `schedule.auto_adapt: false` = suggestion only (`extra.adapt_suggestion`). Existing heatmap on the Dashboard.
- 17:45–18:15 IST: if Instagram posts are due tonight and only a Task Scheduler run is alive, Telegram warns. With the
  laptop off nothing can warn (documented limit).

### LLM
- The publisher only calls the LLM to write metadata for an item without it; `generate_metadata` returns stored
  metadata unchanged (Regenerate sets `pending` first). `llm.max_calls_per_day` (200) / `max_concurrent` (1) wrap the
  provider (`publish/llm_budget.py`); at the cap only metadata waits.

### /health, /needsyou
- /health (API + Telegram): heartbeat, DB check, per account auto-ready, uploads in 24 h (ledger), cooldown, last
  failures (reason only), next post, today's + tomorrow's plan, e.g. "YT 5 / IG 7".
- /needsyou: account actions (reconnect, missing Page/permissions, auto impossible, audit rejected) + items whose reason
  needs a person. Self-healing reasons (retrying, quota, channel_limit, safety_limit, duplicate, ig_limit,
  ig_find_media) are never listed.

### Publishing v2 review fixes (review agent, 6 findings, regression tests in tests/test_publishing_v2.py)
- Instagram item with a media id is never marked missed (confirm via read-back instead).
- `register_instagram`: a reconnect never upgrades a chosen assisted mode to auto; only new/converted rows get auto.
- `dedup.add_or_skip` "resumed": back to draft without the old slot (it may belong to another clip now).
- `states._has_media`: pasted-link exemption only for accounts with `extra.manual` (converted ones keep their old id).
- Logical twins match `clip_key` (episode + start/end), not rank (ranks change when selection is re-run).
- `plan.explore` uses the planner's own gap (`min_gap_hours`), so an explored YouTube slot is never dropped.
