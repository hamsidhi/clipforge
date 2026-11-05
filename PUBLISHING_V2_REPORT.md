# Publishing v2 — report (5–6 Nov 2026)

Every decision is in [DECISIONS.md → "Publishing v2"](DECISIONS.md). No secret or token appears in this file, the
logs, the commits or the UI. The publisher runs by itself (deterministic checks, DB queries and API calls); the LLM is
only used to write missing metadata, and that metadata is never regenerated to retry a publish.

## 1. Instagram: root cause, auth method, redirect URI
- **Root cause:** `figuringclips.in` was added with `channels.add_manual_instagram`, i.e. "add manually" (id
  `ig:manual:figuringclips.in`, `extra.manual = true`). It has no IG user id, no Page and no token, and
  `workspace/accounts/instagram/` doesn't exist. `modes.resolve` therefore always returned *assisted*.
  Facebook Login was never completed on this laptop.
- **Auth method now:** one flow only, Facebook Login (for Business, using your `config_id`):
  1. `state`;
  2. code → user token → long-lived token;
  3. `me/accounts` → the Page with `instagram_business_account`;
  4. store the IG id, Page id and tokens (server-side, git-ignored);
  5. `me/permissions` + `debug_token`;
  6. mode **auto only if every check passes**.
- Your manual account is **converted in place** (same row, so its history stays attached) when you log in with
  the same username.
- **Exact redirect URI** for Meta → Facebook Login → Valid OAuth Redirect URIs:
  **`http://localhost:8000/api/instagram/oauth/callback`** (no trailing slash; Strict Mode = exact match).
  Meta's docs don't document a localhost exemption, so add it explicitly. If Meta refuses the `http` URI,
  "Enforce HTTPS" is the reason (TODO_ON_LAPTOP 0b).

## 2. Instagram permissions / eligibility / live test
- **Required permissions** (docs, 5 Nov): `instagram_basic`, `instagram_content_publish`, `pages_read_engagement`,
  plus `pages_show_list` to list your Pages. `business_management` is optional.
- **Development mode:** fine. People with a role on the app get Standard Access, so no App Review is needed.
- **Health per account** checks: Meta app / professional account / Page linked / IG id / token valid + expiry /
  publish permissions / publishing limit → **auto-publish ready**. "Connected" ≠ "ready" in the UI and Telegram.
- **Publish flow:**
  1. ffprobe pre-check;
  2. `REELS` container with `upload_type=resumable` (container id stored before the upload);
  3. resumable `rupload` from the reported offset;
  4. poll `status_code`;
  5. `media_publish` once;
  6. **PUBLISHED only after `GET /{media-id}` returns it.**
  If the outcome is unknown, the account's recent media are checked first, so nothing is published twice.
- **Live test: NOT RUN — MISSING CREDENTIAL.** No Instagram account is connected through Facebook Login, and
  `instagram.test_account_id` is empty. The Meta App ID/Secret are FOUND and valid.

## 3. Duplicates
- **Identity:** `pub_key = sha256(video_id | rank | clip file sha256 | platform | account | intent)`. The file
  hash is computed once per render and cached.
- **Atomic:** one transaction plus a unique index `ux_queue_pub_key`.
- **Logical twins count too:** same episode + rank + account (e.g. after a re-render, or rows from before v2).
- **When checked:** at add time, and again as the first check of every upload job.
- **Never deleted:** duplicates become `skipped_duplicate`. A republish (`dedup.republish`) gets a new intent,
  so a new key.
- **Live backfill:** all 47 rows keyed, 0 duplicates found.

## 4. YouTube limit controller + ledger
- **One error table** (`publish/yt_errors.py`), using the real `reason`:

  | Type | Examples |
  |---|---|
  | project_quota | `quotaExceeded` |
  | channel_limit | `uploadLimitExceeded` |
  | auth | `invalid_grant`, 401, scope |
  | temporary | 5xx, `rateLimitExceeded`, network |
  | permanent | everything else |

- **`UploadLedger` table:** every attempt on both platforms. Only successful inserts count as uploads.
- **Before each upload, in this order:** cooldown → project quota (inserts + units < 90%) → successful uploads
  in 24 h < **8** → 1 active upload → **120 s** gap → **2** per run. `youtube.override_until` forces a retry.
- **On `uploadLimitExceeded`:**
  - one refused call, then the channel stops;
  - its clips become **WAITING_FOR_CHANNEL_CAPACITY** with their slots kept;
  - cooldown = first successful upload of the 24-h window + 24 h;
  - then **one probe** upload; the batch continues only if it succeeds;
  - clips whose slot passed move to the next free slot, and you're told on Telegram.
- **Live:** the ledger was backfilled from the real uploads of 5 Nov (the private test + 9 Shorts = 10 successful
  inserts) and the refusal. **Cooldown until Fri 6 Nov 16:24 IST.**

## 5. The 9 uploaded Shorts and the 15 waiting clips
**9 Shorts** (`videos.list(part=status)`, read-only, 5 Nov 19:04 IST):
- All are `processed`, `private`, with `publishAt` intact. **None is locked private.**
- YouTube publishes them itself at 07:00, 10:00, 12:30, 18:30, 21:00 IST on 6 Nov, and 01:15 (7 Nov), 07:00,
  10:00, 12:30 IST on 7 Nov.
- They were not touched or re-uploaded.

**15 waiting clips:** all `WAITING_FOR_CHANNEL_CAPACITY` (YouTube's upload limit), slots kept:

| # | Clip | Current slot (IST) |
|---|---|---|
| 11 | Scaling Insights: Big Gains | Thu 05 Nov 21:00 |
| 12 | Nerd & Cool: Sam Altman's Childhood | Fri 06 Nov 01:15 |
| 13 | Innovation: Why Herd Mentality | Fri 06 Nov 07:00 |
| 14 | AI Fears \| Father & Altman | Fri 06 Nov 10:00 |
| 19 | Compounding Curve: Career Growth | Fri 06 Nov 12:30 |
| 17 | AI in Science: Discovering New Math | Fri 06 Nov 18:30 |
| 16 | Future Hope: Optimism Amidst Challenges | Fri 06 Nov 21:00 |
| 37 | Speed Training Key For Bryson | Sat 07 Nov 01:15 |
| 38 | Top pe fall kar sakte hain | Sat 07 Nov 07:00 |
| 35 | Gaming Shaped Bryson's Mindset | Sat 07 Nov 10:00 |
| 36 | Tiger's Intensity | Sat 07 Nov 12:30 |
| 21 | Single-Length Clubs: Golf Innovation | Sat 07 Nov 18:30 |
| 22 | 24 Pounds in 30 Days: My Diet | Sat 07 Nov 21:00 |
| 34 | Perfection Obsession | Sun 08 Nov 01:15 |
| 20 | Tech Leader's Journey: Sam Altman | Sun 08 Nov 10:00 |

- After 16:24 IST on 6 Nov, one probe upload runs. If it succeeds, the rest follow (max 8 per 24 h, 2 minutes apart).
- The five clips whose slot has passed by then (#11, #12, #13, #14, #19) move to the next free slots of the new
  YouTube plan. Telegram tells you each new time.

Also: the 29 clips refused on 5 Nov were deleted from the queue by hand (17:34–17:39 IST). The one Instagram
"post it yourself" item due earlier (`ig_assisted`) was deleted from the queue after 19:46 IST. Nothing in the code
deletes queue items.

## 6. Generated schedule (today + tomorrow, IST)
**YouTube** (5/day; 6 when the last 7 days' median 24-h views ≥ the previous 7 days'; capped at 8):
- Thu 05 Nov and Fri 06 Nov: 12:30, 15:00, 18:30, 20:30, 22:00.
- The 6th slot (23:30) unlocks once both weeks have ≥3 measured Shorts.

**Instagram** (7/day = 3 morning + 4 evening; 8–9 when approved clips cover today + 1 day; slots after midnight
count for the evening before):
- Thu 05 Nov and Fri 06 Nov: 08:00, 09:30, 11:00, 18:00, 19:30, 21:00, 22:15 (+23:30, 00:45 at 8/9).
- Your account is still manual, so these are the times the posting kit uses until you connect it.

**Other rules:**
- Slots adapt weekly from 24-h views once there are 20+ posts (±30/60 min inside the windows, plus 1 exploration
  slot a day). Shown as a suggestion only if `schedule.auto_adapt: false`.
- New clips use this plan. Already scheduled clips keep their times.
- Instagram needs the laptop and app on until 01:30 IST. At 17:45 IST, Telegram warns if only the Windows task is
  running.

## 7. Migrations + backup
- Schema v16 → **v17** (additive):
  - `QueueItem.pub_key`, `QueueItem.intent`;
  - new `UploadLedger` table;
  - unique index `ux_queue_pub_key` replaces `ux_queue_clip_platform_account`.
- New states: `skipped_duplicate`, `waiting_capacity`.
- **Backup:** `workspace/backups/clipforge-20261105-191235-v16.db` (taken automatically before migrating).

## 8. Files changed
- **New:**
  - `clipforge/publish/{dedup,limits,yt_errors,plan,llm_budget}.py`
  - `tests/test_publishing_v2.py`
  - this report
- **Changed:**
  - `publish/{runner,ig_runner,instagram,channels,modes,queue,slots,states}.py`
  - `api/{db,ig_routes,publish_routes}.py`
  - `services/publishing.py`
  - `notify/{telegram,tg_publish}.py`
  - `config.py`
  - `config.yaml` (only the new keys were committed: `youtube:`, `schedule:`, `llm.max_calls_per_day/max_concurrent`)
  - frontend: types, labels, health panel
  - tests: legacy envs pin `schedule.enabled: false`
  - `DECISIONS.md`, `CLAUDE.md`, `TODO_ON_LAPTOP.md`

## 9. Tests
- **`pytest`: 319 passed.**
- **New tests (51, 4 of them from the review):** duplicates (every state, concurrency, republish, backfill), error classes, cooldown + probe,
  safety limit, private + publishAt counted as uploaded, Instagram login → Page → id, in-place conversion,
  publish-once with read-back, auth failure, independent queues, schedule rollover/counts/adaptation, LLM cache and
  budget, `/needsyou`, `/health`.
- **Other checks:** `npm run build` OK; `scheduler --dry-run` OK; the app was restarted on the new code with no
  errors since.

## 9b. Review (one review agent, after the commit) — 6 findings, all fixed with regression tests
1. A Reel already on Instagram whose read-back failed could be marked "missed" after 90 min → it's now confirmed instead.
2. Reconnecting Instagram overwrote a mode you chose (assisted → auto) → only new/converted accounts get auto.
3. Re-adding a failed/missed clip could reuse a slot another clip had taken → it returns as a draft without a slot.
4. A converted manual account kept the "pasted link is enough" exception → now only real manual accounts.
5. Duplicate identity matched by rank → now by the clip itself (episode + start/end).
6. YouTube exploration slot could break the planner's gap → same gap in both places.

## 10. Your steps
1. **(3 min)** Meta app → Facebook Login → Valid OAuth Redirect URIs: add
   `http://localhost:8000/api/instagram/oauth/callback`.
2. **(3 min)** On the laptop: Accounts → Instagram → **Add Instagram account** → allow everything → pick
   figuringclips.in. It becomes Auto only if `/health` says "auto-publish ready".
3. **(2 min, optional)** `clipforge schedule-task install`, so YouTube still uploads while the app is closed.
   Instagram always needs the app running at post time.
4. **(nothing to do)** The 15 YouTube clips resume by themselves after 6 Nov 16:24 IST.

| Feature | Status |
|---|---|
| Duplicate guard (pub_key, atomic, backfill) | LIVE-VERIFIED (backfill on real DB) + LOCALLY TESTED |
| YouTube error table, ledger, cooldown, probe | LOCALLY TESTED; live cooldown active (probe after 16:24 IST, 6 Nov) |
| 9 Shorts status check | LIVE-VERIFIED (not locked) |
| Daily schedule + adaptation | LOCALLY TESTED; live plan generated |
| Instagram login checks, in-place conversion, eligibility, publish-once | LOCALLY TESTED |
| Instagram live publish | MISSING CREDENTIAL (Facebook Login not done) |
| /health, /needsyou (API + Telegram) | LOCALLY TESTED; live /health OK |
| LLM budget + no regeneration | LOCALLY TESTED |
