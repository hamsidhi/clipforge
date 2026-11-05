# Publishing repair — report (5 Nov 2026)

Unattended run. Every decision is in [DECISIONS.md → "Publishing repair"](DECISIONS.md). No secret, token or part of
one appears in this file, the logs, the commits or the UI.

## A. Root cause (with evidence)
1. **Every YouTube clip went to Studio because the global default is `publishing.mode: assisted`**
   (`config.yaml`, committed default since Phase 12) and there was no per-account mode. In `runner._run_locked` the
   upload budget was 0 unless the global mode was `api`; `_handle_late` then turned passed slots into
   `needs_manual/assisted_unlinked` or `missed/assisted_no_batch` ("upload in Studio and link it").
   Evidence: `workspace/youtube_quota.json` had **0 `videos.insert` calls ever** before today. DB: 54 YouTube items
   `queued` (15 of them in a Studio batch), 9 `published` (all Studio uploads linked by Sync, with real ids and
   `privacyStatus: public` read back from `videos.list`), 1 `needs_manual/assisted_unlinked`.
2. **Instagram could only become "post it yourself"**: the only Instagram account is a *manual* account
   (`ig:manual:figuringclips.in`, added without Facebook Login): no IG user id, no Page, no token. The runner also
   silently treated an API account as assisted when the Meta app was missing or `instagram.mode` was assisted.
3. The scheduler does run and does call the upload function, but only inside the web app (ticker every 60 s,
   `scheduler_state.json` source `app`); no Windows task is registered.
4. Honesty bugs: a fresh YouTube upload was written as `scheduled` before YouTube confirmed anything; an Instagram
   container reported `PUBLISHED` whose Reel couldn't be found was marked `published` **without a media id**; Graph
   `error_subcode` was dropped.
5. Not causes (verified): the YouTube token refreshes and owns the channel; Sync queries the channel's own uploads
   playlist; the Instagram flow (container → resumable `rupload` of the local file → poll `status_code` →
   `media_publish` once) never sends `video_url`.
6. **The brief's "unaudited projects are locked private" is outdated.** Google's revision history, 2 Nov 2026:
   unverified projects "are no longer restricted to private viewing mode" (the `videos.insert` page agrees).
   Locked-private detection is kept anyway as its own state.

## B. Credentials and environment (no values)
| Item | Status | Reason |
|---|---|---|
| Google `client_secret.json` | FOUND | installed-app client, id + secret present |
| YouTube token, figuring clips (UCT5…RTQ) | FOUND | refresh with the saved refresh token works; `channels.list(mine)` returns this channel |
| — scopes | FOUND | `youtube.upload`, `youtube.readonly`, `yt-analytics.readonly`; **no** `youtube`/`force-ssl` (so no `videos.update`: can't unschedule/delete) |
| Meta App ID + Secret | FOUND | `client_credentials` grant accepted by graph.facebook.com |
| Meta login config id | FOUND | in `.env` |
| Instagram account token | MISSING | no account connected with Facebook Login (`workspace/accounts/instagram/` empty) |
| Instagram account | FOUND (manual) | `figuringclips.in`, assisted only: no IG user id, no Page id |
| IG user id / linked Page id | MISSING | exist only after Facebook Login |
| `instagram.test_account_id` | MISSING | empty (so no live Reel may be published) |
| Telegram bot token | FOUND | `.env` |
| Ollama :11434 | FOUND | 2 models (left untouched) |
| ffmpeg / ffprobe | FOUND | ffmpeg 9.0.2 |
| Windows Task Scheduler task | MISSING | not registered |
| Scheduler last run | FOUND | in-app ticker, every minute while `clipforge ui` runs |

## C. Files changed, migration, backup
- New: `clipforge/publish/modes.py`, `tests/test_publishing_repair.py`, this report.
- Changed: `publish/{states,queue,runner,ig_runner,instagram,batch,reconcile}.py`, `api/{db,publish_routes}.py`,
  `services/publishing.py`, `notify/tg_publish.py`, `utils/power.py`, `cli_publish.py`, `config.py`, `config.yaml`
  (2 new keys only), `docs/TELEGRAM.md`, frontend (`api/publish*.ts`, `FixActions`, `HealthPanel`, `QueueItemDialog`,
  `Channels`, `Schedule`), tests (`fake_youtube` processes uploads like YouTube; updated expectations).
- **Migration v15 → v16** (additive): columns `last_attempt_at`, `lease_owner`, `lease_expires_at`,
  `idempotency_key`; unique indexes (clip_key, platform, channel_id), (platform, youtube_video_id) and
  (platform, ig_media_id) where set. No status was renamed or dropped; all 77 rows kept their status.
- **Backup:** `workspace/backups/clipforge-20261105-162429-v15.db` (plus `workspace/clipforge.db.bak-v15`).

## D. YouTube
- Auth: refresh OK (existing refresh token), channel ownership OK.
- Upload path: resumable `MediaFileUpload` (8 MB chunks; the client library retries 5xx/network and resumes the
  same session), body = snippet (title ≤100, description ≤5,000, tags, categoryId, default(Audio)Language) + status
  (private + `publishAt` RFC 3339 UTC, `selfDeclaredMadeForKids: false`, `containsSyntheticMedia` from config).
  After upload the item is `processing` until `videos.list(status, processingDetails)` says otherwise.
- **LIVE private test** (`clipforge publish-test UCT5PqvmHmx8HldPvyZwRRTQ`, 5 Nov 2026 16:24 IST):
  - real video id **`Cx093Vl1V7M`**;
  - read back: `uploadStatus: processed`, `privacyStatus: private`, `processingStatus: succeeded`,
    `selfDeclaredMadeForKids: false`;
  - classified as `uploaded` (private by choice); saved to `workspace/logs/publish_test_Cx093Vl1V7M.json`.
  - **Not tested live: `publishAt` / the lock.** Removing `publishAt` afterwards needs `videos.update`, which the
    token's scopes don't allow, so a `publishAt` test could have gone public. Per Google's docs the lock no longer
    exists; the code still detects it (tested with the fake).
- Quota used: 1 `videos.insert` + about 5 units today (ledger: 1 insert, 15 units for the day).
- Error mapping (tested): `invalid_grant` → channel needs re-login + REQUIRES_REAUTHENTICATION;
  `quotaExceeded` → waits for the next Pacific day; `uploadLimitExceeded` → FAILED with YouTube's message;
  other 4xx → FAILED at once; 5xx/network → RETRYING with backoff.

## E. Instagram
- Meta app: valid. Account: **only a manual account**, so no token, IG user id or Page id. Permissions, token
  expiry and `content_publishing_limit` can't be read until you connect an account with Facebook Login.
- File check: `ig.preflight()` (ffprobe + moov/mdat scan) on a real rendered clip (`01_fame-can-destroy-performers.mp4`,
  18.3 MB, 1080×1920 H.264/AAC): **no problems**.
- Container / upload / processing / publish: **not run live** (no connected account, no `test_account_id`).
  Status: **BLOCKED BY MISSING CREDENTIAL**; not "ready, not published", because no container could be made.
- Code fixes (tested with the fake):
  - after the upload the item is `processing`;
  - a `PUBLISHED` container is only marked published once the Reel's media id is found;
  - Graph code/subcode/user title are shown, and format subcodes fail at once (`ig_format`);
  - tokens are redacted from every error.

## F. Scheduler
- `test_scheduler_tick_calls_the_platform_client`: the exact function the ticker and `scheduler --once` run
  (`worker.run_scheduler`) uploads a due item through the client. The stored state is read back from `videos.list`,
  the lease is taken and released, and a second run doesn't upload again (no duplicate).
- Retries: `test_upload_error_mapping` (5xx → RETRYING + `next_attempt_at`), Instagram `publish` result lost →
  recovered from `recent_media`, never re-published (`test_ig_published_container_without_media_is_not_marked_published`).
- Leases: the claim sets owner `<host>:<pid>` + expiry; a second claimer loses (`test_lease_is_taken_by_the_claim`).
- IST↔UTC boundary tested (01:15 IST 6 Nov = 19:45 UTC 5 Nov; YouTube quota day = Pacific).
- `clipforge scheduler --dry-run` (new) on the real data: YouTube *figuring clips*: mode **assisted (default)**,
  54 waiting, 39 would upload in auto mode; Instagram *figuringclips.in*: assisted (manual account).
- Heartbeat (last run in IST, source, items changed, next run, Windows task yes/no) is on the Dashboard health panel
  and in Telegram `/health`.

## G. UI and Telegram
- Accounts: a **Mode** selector per account (Default / Auto / Assisted, with a confirmation for Auto) and a mode line
  that shows why auto can't work.
- Schedule: the header shows the selected account's own mode.
- Every queue item now carries the server's `status_text` (state, label, detail, actions) from `states.status_text()`.
  The web app's badges, the clip dialog and Telegram (`/needsyou`, `/link`, the calendar, fix results) all use it.
- "Published" appears only for PUBLISHED with a platform id. Other examples: "Scheduled, not uploaded yet",
  "Uploaded, YouTube is processing it", "Uploaded (private), goes public at the slot", "Uploaded but locked private".
- New buttons: **Switch account to assisted / auto**, **Upload via Studio instead** (keeps the locked video's id in the history).
- Health panel / `/health`: per account mode (+ problem), audit status, last confirmed publication, next post.
- Manual Link accepts every YouTube URL form and checks that the video belongs to the channel.

## H. Commands
```powershell
clipforge ui                               # web app + in-app scheduler (every minute)
clipforge scheduler --once                 # one run (what Task Scheduler runs)
clipforge scheduler --daemon               # always-on loop without the web app
clipforge scheduler --dry-run              # what the next run would do, per account (no changes)
clipforge schedule-task install            # Windows task: every 30 min + logon + wake (hidden)
clipforge schedule-task status
clipforge publish-test <channel id>        # one PRIVATE test upload (never public; --days N adds publishAt)
```

## I. Status per feature
| Feature | Status |
|---|---|
| Per-account mode (auto/assisted), no silent downgrade | IMPLEMENTED AND TESTED |
| State machine with enforced transitions, canonical names | IMPLEMENTED AND TESTED |
| Migration + backup + unique indexes | IMPLEMENTED AND TESTED (run on the real DB) |
| YouTube resumable upload + read-back of the real state | IMPLEMENTED AND TESTED (live private upload `Cx093Vl1V7M`) |
| YouTube private + publishAt scheduling | IMPLEMENTED, NOT LIVE-VERIFIED (needs videos.update scope to undo safely) |
| Locked-private detection | IMPLEMENTED AND TESTED (fake); Google says the lock is gone |
| YouTube error mapping, quota rollover | IMPLEMENTED AND TESTED (fake) |
| Instagram container → resumable upload → poll → publish once | IMPLEMENTED AND TESTED (fake), NOT LIVE-VERIFIED |
| Instagram live | BLOCKED BY MISSING CREDENTIAL (no account connected via Facebook Login) |
| Scheduler tick → platform client, leases, retries, IST/UTC | IMPLEMENTED AND TESTED |
| Windows task | IMPLEMENTED, NOT REGISTERED (your step) |
| Assisted mode, Sync, Link (URL forms, ownership) | IMPLEMENTED AND TESTED |
| Honest statuses in web app + Telegram (one shared function) | IMPLEMENTED AND TESTED |
| Security pass (no tokens in logs/bundle/git, redaction, workspace-only upload paths) | IMPLEMENTED AND TESTED |

Checks run: `pytest` 267 passed; `npm run build` OK; `npm run lint` (only old warnings); `clipforge doctor` all
required checks passed; API smoke test; screenshots of Accounts / Dashboard / Schedule on the demo instance (no
console errors).

## J. Only you can do this
1. **(1 min) Restart the app** so it runs the new code: Ctrl+C in the `clipforge ui --host 0.0.0.0` window, then start
   it again. The running process still has the old code.
2. **(5 min) Clean up the Studio batch before going auto.** Schedule → *Sync with YouTube*. For clips still marked
   "In a Studio batch" that you did **not** upload: *Upload with API instead*.
3. **(1 min) Turn on auto for YouTube:** Accounts → *figuring clips* → Mode → **Auto** → confirm. The next run
   uploads the scheduled clips as private with `publishAt` = their slots. Check one in Studio: Private, Scheduled.
4. **(1 min, optional) Delete the test video** `Cx093Vl1V7M` in Studio. It is private and stays private.
5. **(2 min) Keep publishing when the app is closed:** `clipforge schedule-task install` (from an Administrator
   terminal if Windows refuses).
6. **(10 min) Instagram auto:**
   - Accounts → Instagram → **Add Instagram account** (Facebook Login, on the laptop) → pick → Confirm → Mode Auto.
   - Then remove the manual `figuringclips.in` account, or keep it in assisted mode.
   - Note: the laptop must be on at Reel times.
7. **(optional) Live Instagram test:** put the connected account's IG user id in `instagram.test_account_id`; one
   test Reel can then be posted.
8. **(optional, 2 min) Re-consent with the `youtube.force-ssl` scope** if you want ClipForge to change a schedule or
   make a video public after upload. Not needed for normal scheduled publishing.
