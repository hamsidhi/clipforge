# ClipForge on Telegram — full remote control

The bot is the quickest way to use ClipForge from your phone: paste a YouTube link, watch the progress, get the clips as
cards, preview / approve / edit / trim / fix words / re-render / schedule — and get told when posts go live or need you.
It runs inside `clipforge ui` (or on the always-on server, see AUTOMATION_GUIDE Part I) and only talks to the
Telegram Bot API **outbound** (long polling). Nothing on your computer is opened to the internet.

* Setup (5 min): Settings → Phone & Telegram → paste the token from [@BotFather](https://t.me/BotFather) → **Pair my
  phone** → send `/pair 123456` to your bot. CLI: `clipforge telegram pair` · `devices` · `unpair` · `set-pin` · `status`.
* `/help` in the bot lists everything with examples, `/menu` shows a keyboard with the main actions.

## Commands

| Command | What it does | Example |
|---|---|---|
| *(paste a link)* / `/new` | start a job → options wizard (clips 10/15/20/25, full video or a part, LLM, layout, caption script) or **▶ Start with my defaults**. Same video again → "Open existing job". | `https://youtu.be/abc123` |
| *(send / forward a video or audio)* | same, from the file (≤ 20 MB on the cloud Bot API, ≤ 2 GB with a local Bot API server) | |
| `/jobs` | all jobs, 6 per page, ◀ ▶ | |
| `/job 12` | progress or result + buttons: Clips · Retry · Re-run a step · Cancel · Delete | |
| `/cancel` · `/cancel 12` | stop the question I'm waiting for · cancel job 12 | |
| `/retry 12` | run again; finished steps are reused | |
| `/rerun 12 select` | redo one step (download, transcribe, analyze, select, render, metadata) and everything after it | |
| `/delete 12` | delete the job and its files (asks; PIN if on) — tells you how much disk was freed | |
| `/clips 12 [approved\|rejected\|pending]` | clip cards, 5 per page | `/clips 12 pending` |
| `/clip 12-3` | one card | |
| `/send [job] all\|approved\|1-3 [preview\|full]` | the videos themselves (preview = 540×960 streamable, full = original as a file) | `/send approved full` |
| `/approve 12 1-5,8` · `/approve 12 >7` · `/reject 12 9` | bulk review | |
| `/edit 3` | edit panel: title, hook, style, layout, script, trim ±1 s / ±0.3 s, words, re-render, undo | |
| `/trim 3 0:12-0:47` | keep seconds 12–47 *of the clip* (what you see in the preview); `@1:02:10-1:02:55` = episode time. Snaps to words and shows the first/last words. | |
| `/words 3` · `/word 3 "galti" "galat"` · `/word 3 #12 galat` | numbered caption words · fix a word (every match / by number) | |
| `/render 3` · `/render changed` · `/render 12 approved` | re-render; the new version is sent here with **↩ Undo** | |
| `/text 3` | copy-ready title, YouTube description, Instagram caption, ≤ 5 lowercase hashtags (long-press a block to copy) | |
| `/meta 12-3` · `/meta 12` | the same publishing text for one clip, or every approved clip of job 12; writes missing metadata first (owner) | |
| (automatic) | every clip video and clip card is followed by a reply with its publishing text (YouTube title, description, Instagram caption, hashtags, tags) — one tap on a block copies it | |
| (automatic) | longer than 4096 characters → split between sections; no metadata yet → "Metadata not ready — /meta 12-3 to generate" | |
| `/schedule` | the next 7 days as a calendar (IST) | |
| `/schedule 12 youtube\|instagram\|both` | approved clips of job 12 → next free slots | |
| 📅 on a card | one clip → platform(s) → account(s) → next free slot or a time you type (`today 21:00`, `tomorrow 9pm`, `5 Nov 18:30`) | |
| `/queue` | next posts grouped by day, ▶️ YouTube / 📸 Instagram | |
| `/needsyou` | every post that needs you, the reason, and the same one-tap fixes as the web app | |
| `/link 41 <youtube link>` | link a post you uploaded yourself | |
| `/batch <account>` | assisted mode: the checklist + the files | |
| `/accounts` · `/pause <account>` · `/resume <account>` | status + pause/resume (resume asks; PIN if on) | |
| `/health` | quota left, last scheduler run, logins, audit status, disk, workers | |
| `/stats 7d\|28d` · `/top` · `/earnings` · `/campaigns` | numbers per platform · top posts · campaign estimate · unsubmitted posts with "Mark submitted" | |
| `/status` · `/dashboard` | what's running · followers / views / YPP | |
| `/server` | machine: uptime, disk, RAM, CPU, load, workers, jobs, last backup | |
| `/devices` | paired chats, Unpair buttons (owner only) | |
| `/app` | open the ClipForge app (trimmer / crop editor); a Mini App if `telegram.mini_app` is on | |
| `/menu` · `/hidemenu` · `/help` | | |

### What happens when a job finishes
1. The live progress message (edited at most every `telegram.progress_every_sec`, only when the text changes) turns
   into the result with **Clips** / **Approve all ≥ 7** buttons.
2. A summary (count, best score, total length), an album of the top thumbnails (`telegram.clips_to_send`, albums take
   2–10 photos and can't carry buttons), then one **card per clip**:
   `▶ Preview · ✅ Approve · ❌ Reject · ✏️ Edit · 📅 Schedule · ⬇ Full quality`. Cards update in place (✅ Approved).
3. Failures say why in plain words (YouTube bot check, disk full, Ollama not running, …) with a Retry button — never a
   stack trace.

## Sending videos — the size fallback chain

| Step | When | What you get |
|---|---|---|
| 1 | preview | the 540×960 proxy with `sendVideo` + `supports_streaming` (a few MB) |
| 2 | full quality fits | the original 1080×1920 file with `sendDocument` (Telegram doesn't re-compress documents) |
| 3 | full quality too big (> 50 MB cloud / > 2 GB local server) | a **Telegram-fit** copy: 2-pass H.264 at the bitrate that fits, still 1080×1920 above ~1.2 Mbps (720×1280 below); the caption says it was re-encoded. Cached in `clips/_work/NN/telegram_fit.mp4`. |
| 4 | can't re-encode to fit | a private Tailscale link (opens only on your tailnet); without remote access a hint how to get it |

Every file sent once is remembered by its Telegram `file_id`, so sending it again is instant and uploads nothing.

### Optional: local Bot API server (2 GB files both ways)
Worth it only if you often send episodes as files (> 20 MB) or want full-quality clips > 50 MB without re-encoding.
It's free and open source ([tdlib/telegram-bot-api](https://github.com/tdlib/telegram-bot-api)) but needs your own
`api_id` / `api_hash` from <https://my.telegram.org> → API development tools.

```bash
# Docker (any OS) — listens on 127.0.0.1:8081 only
docker run -d --name telegram-bot-api --restart unless-stopped -p 127.0.0.1:8081:8081 \
  -e TELEGRAM_API_ID=<api_id> -e TELEGRAM_API_HASH=<api_hash> -e TELEGRAM_LOCAL=1 \
  -v telegram-bot-api-data:/var/lib/telegram-bot-api aiogram/telegram-bot-api:latest
```
On the server, `deploy/install.sh --with-bot-api` sets up the same thing as a systemd service (see deploy/README.md).
Then: once call `https://api.telegram.org/bot<token>/logOut` (moves the bot off the cloud server; it must be done
before a local server can use the token), set `telegram.api_base_url: http://127.0.0.1:8081` and restart ClipForge.
To go back: delete `api_base_url`, call `close` on the local server, wait 10 minutes.

## Security model

* **Only paired chats.** Pairing codes are 6 digits, one-time, valid 10 minutes, burnt after 5 wrong guesses, made on
  the laptop (Settings or `clipforge telegram pair`). Unknown chats get **no reply at all** — one audit entry per hour.
* **Groups** only with a code made for groups (`--group`), and in a group only the person who paired it counts;
  anonymous admins and channel posts are ignored.
* **Roles**: `owner` = everything; `viewer` = read-only (cards, previews, files, stats) — no edit, publish or delete.
* **Buttons** carry only `c:<8 random chars>` (≤ 64 bytes); the action lives in the `tgcallback` table bound to the chat
  (expires after 14 days). Confirmations are bound to the user who asked, expire after **2 minutes** and work **once**.
* **Dangerous actions** (publish now / public, delete a job, resume an account, unpair): Yes/No → for "public" you also
  **type YES** → the **PIN** if enabled (`clipforge telegram set-pin`, 6–8 digits; scrypt hash in `.env`; the bot
  deletes your PIN message right away; 5 wrong → locked 15 minutes, then 30 min, 1 h … up to 24 h, and owners are
  told). PIN actions are refused in groups — confirm them in your private chat with the bot.
* **Laptop-only, never in Telegram**: bot token, pairing, PIN, password, output folders, remote setup, connecting /
  disconnecting accounts and OAuth. Re-login links are never sent. From a phone (web app) only the notification
  switches of the bot can be changed — not where it connects (`api_base_url` must be Telegram or this machine), not
  the PIN rule, not `deployment` / `server` settings.
* **Nothing secret is echoed**: replies are scrubbed of tokens/keys (incl. every value in `.env`), IPs, tailnet names
  and absolute paths; unexpected errors become "error a1b2c3" that you can find in `clipforge.log`.
* **Abuse limits**: `telegram.commands_per_minute` per chat (default 30); outbound ~1 message/s per chat, ≤ 20 edits a
  minute per chat, ≤ 25/s overall; HTTP 429 `retry_after` is honoured by every sender.
* **Audit**: every command, button and confirmation goes to the audit log (Settings → Security); the daily summary
  says what the bot did today.
* **One poller**: only one process may read a bot's updates. A DB lease (renewed every poll) + backing off on HTTP 409;
  the update offset is stored, so restarts never replay or skip messages. A `worker` laptop never polls.
* **Token rotation**: in @BotFather send `/revoke` → pick the bot → copy the new token → Settings → Phone & Telegram →
  Save token (this unpairs every chat; pair again). Old buttons stop working, which is intended.

## Mini App (A6) — findings

* Telegram opens a Mini App URL in its in-app webview **on your phone**; the URL must be HTTPS
  ([docs](https://core.telegram.org/bots/api#webappinfo)). Telegram's servers don't fetch it, so a Tailscale
  `https://<laptop>.<tailnet>.ts.net` URL (real Let's Encrypt certificate) loads when the phone is on your tailnet —
  no public exposure needed.
* So it ships, **off by default** (`telegram.mini_app: true` to use it): `/app` then shows a `web_app` button instead of a
  plain link. Server side, `POST /api/telegram/webapp` checks `initData` (HMAC-SHA256 with the key derived from the bot
  token, ≤ 24 h old) and that the Telegram user is paired. It never logs you in by itself: the normal ClipForge password
  session is still required for every `/api` call.
* Not verified on a real phone in this phase (no device in the build environment). If the webview can't reach the
  tailnet on your phone, keep `mini_app: false` — `/app` then sends a normal link that opens in your browser.

## Audit table — every web / CLI action and where it lives on Telegram

`tests/test_telegram_audit.py` fails if an API route has no row here. **Laptop** = deliberately not on Telegram.

| API route | Web app / CLI | Telegram | Why not (if not) |
|---|---|---|---|
| `GET /api/health` | — | — | liveness probe |
| `GET /api/auth/status` | login page | — | web session only |
| `POST /api/auth/login` | login page | — | web session only |
| `POST /api/auth/logout` | Settings → Security | — | web session only |
| `POST /api/auth/setup` | Settings / `clipforge auth set-password` | Laptop | password never through chat |
| `GET /api/auth/sessions` | Settings → Security | Laptop | security setting |
| `DELETE /api/auth/sessions/{sid}` | Settings → Security | Laptop | security setting |
| `GET /api/auth/audit` | Settings → Security | daily summary line | full log stays in the app |
| `POST /api/jobs` | New job / `clipforge run` | link / file → wizard, `/new` | |
| `GET /api/jobs` | Dashboard / `clipforge list` | `/jobs` | |
| `GET /api/jobs/{job_id}` | Job page | `/job 12` | |
| `DELETE /api/jobs/{job_id}` | Job page | `/delete 12` (confirm + PIN) | |
| `POST /api/jobs/{job_id}/cancel` | Job page | `/cancel 12`, ⏹ button | |
| `POST /api/jobs/{job_id}/retry` | Job page | `/retry 12`, 🔁 button | |
| `POST /api/jobs/{job_id}/rerun/{stage}` | — / `clipforge select --force` etc. | `/rerun 12 select`, ♻ button | |
| `POST /api/jobs/{job_id}/transcript/whisper` | — / `clipforge run … --transcript-source whisper` | 🎙 Use Whisper instead (after a transcript warning) | |
| `GET /api/jobs/{job_id}/events` | live progress (SSE) | live progress message | |
| `GET /api/jobs/{job_id}/thumbnail` | Dashboard | album | |
| `GET /api/jobs/{job_id}/clips` | Clips page | `/clips 12` | |
| `POST /api/jobs/{job_id}/render-all` | Clips page | `/render changed`, `/render 12 approved` | |
| `POST /api/jobs/{job_id}/approve` | Clips page "approve ≥ score" | `/approve 12 >7`, button | |
| `GET /api/jobs/{job_id}/source-waveform` | editor waveform | Mini App / `/app` | a waveform needs a screen, not chat |
| `GET /api/jobs/{job_id}/source-video` | editor | Mini App / `/app` | |
| `GET /api/jobs/{job_id}/transcript` | editor timeline | `/words 3` | |
| `GET /api/jobs/{job_id}/export.zip` | Clips page | `/send approved full` | a zip of 1 GB doesn't fit Telegram; files are sent one by one |
| `GET /api/clips/{clip_id}` | editor | card, `/clip 12-3`, `/edit 3` | |
| `PATCH /api/clips/{clip_id}` | editor | title / hook / style / layout / script / trim / words / approve buttons | crop overrides: Mini App / `/app` |
| `POST /api/clips/{clip_id}/render` | editor | 🔁 Re-render, `/render 3` | |
| `POST /api/clips/{clip_id}/undo` | editor | ↩ Undo | |
| `GET /api/clips/{clip_id}/video` | player / download | ▶ Preview, ⬇ Full quality, `/send` | |
| `GET /api/clips/{clip_id}/thumbnail` | cards | album | |
| `GET /api/clips/{clip_id}/shots` | crop editor | Mini App / `/app` | per-shot crop needs a screen |
| `GET /api/clips/{clip_id}/frame` | crop editor | Mini App / `/app` | |
| `POST /api/uploads` | New job (file) | send / forward a file | |
| `PUT /api/uploads/{upload_id}` | New job (file) | send / forward a file | |
| `GET /api/uploads/{upload_id}` | resumable upload | — | Telegram uploads aren't resumable by the bot |
| `POST /api/uploads/{upload_id}/complete` | New job (file) | send / forward a file | |
| `GET /api/probe` | New job preview | wizard shows the link | |
| `GET /api/fonts/{name}` | caption preview | — | static asset |
| `GET /api/settings` | Settings | Laptop | settings stay on the laptop |
| `PUT /api/settings` | Settings | Laptop | (job options are in the wizard) |
| `GET /api/settings/ollama-models` | Settings | Laptop | |
| `GET /api/doctor` | Settings → Doctor / `clipforge doctor` | `/server`, `/health` (summary) | fixing needs the computer |
| `POST /api/doctor/fix` | `clipforge doctor --fix` | Laptop | downloads models on the computer |
| `GET /api/system` | Settings | `/server` | |
| `GET /api/estimate` | New job | progress ETA | |
| `GET /api/presets` | editor | 🎨 Style buttons | |
| `POST /api/presets` | Settings → caption styles | Laptop | designing a style needs the preview |
| `DELETE /api/presets/{key}` | Settings → caption styles | Laptop | |
| `GET /api/posts` | Tracker | `/campaigns`, `/earnings` | |
| `POST /api/posts` | Tracker | "Mark submitted" button | other fields: app |
| `PATCH /api/posts/{post_id}` | Tracker | "Mark submitted" button | |
| `DELETE /api/posts/{post_id}` | Tracker | Laptop | rare, destructive |
| `GET /api/posts.csv` | Tracker | — | a CSV for a spreadsheet |
| `GET /api/posts/reminders` | Dashboard | `/campaigns` | |
| `GET /api/publishing/status` | Publishing | `/health` | |
| `GET /api/publishing/health` | Dashboard → Health | `/health` | |
| `GET /api/publishing/reach` | Dashboard → Reach | weekly Telegram summary | |
| `GET /api/publishing/overview` | Accounts | `/dashboard`, `/stats` | |
| `POST /api/publishing/run` | Health → Run now | — | runs every minute anyway |
| `POST /api/publishing/audit` | Settings → audit checklist | Laptop | |
| `POST /api/publishing/client-secret` | Settings | Laptop | a secret file |
| `GET /api/channels` | Accounts | `/accounts` | |
| `POST /api/channels/oauth/start` | Accounts → Add | Laptop | OAuth never through chat |
| `GET /api/oauth/callback` | (Google redirect) | Laptop | |
| `GET /api/channels/pick/{pid}` | Accounts → Add | Laptop | |
| `POST /api/channels/pick/{pid}` | Accounts → Add | Laptop | |
| `PATCH /api/channels/{channel_id}` | Accounts (pause, slots, mode) | `/pause`, `/resume` | slots, mode: app |
| `POST /api/channels/{channel_id}/confirm` | Accounts | Laptop | first-upload check shows the avatar + name |
| `POST /api/channels/{channel_id}/check-auth` | Accounts | `/accounts` (read-only token status) | |
| `DELETE /api/channels/{channel_id}` | Accounts → Disconnect | Laptop | disconnect revokes logins |
| `POST /api/channels/{channel_id}/autoschedule` | Schedule | 📅 Next free slot, `/schedule 12 both` | |
| `GET /api/channels/{channel_id}/free-slots` | Schedule (move) | 📅 → Pick a time | |
| `POST /api/channels/{channel_id}/batch` | Schedule (assisted) | `/batch <account>` | |
| `POST /api/channels/{channel_id}/sync` | Schedule → Sync | 🔄 Sync in `/needsyou` | |
| `GET /api/channels/{channel_id}/dashboard` | Dashboard | `/dashboard`, `/top`, `/stats` | heatmap: app |
| `POST /api/channels/{channel_id}/apply-slots` | Dashboard recommendation | Laptop | changes the posting plan |
| `GET /api/channels/{channel_id}/stats.csv` | Dashboard | — | a CSV for a spreadsheet |
| `GET /api/queue` | Schedule | `/queue`, `/schedule` | |
| `POST /api/queue` | Add to queue | 📅 Schedule | |
| `PATCH /api/queue/{item_id}` | queue item dialog | 📅 Pick a time | title/description edits of a queued post: app |
| `DELETE /api/queue/{item_id}` | queue item dialog | — | use the app (rare) |
| `POST /api/queue/{item_id}/regenerate` | queue item dialog | — | use the app (shows variants) |
| `POST /api/queue/{item_id}/unschedule` | queue item dialog | — | use the app |
| `POST /api/queue/{item_id}/reschedule` | Needs you → Reschedule | `/needsyou` button | |
| `POST /api/queue/{item_id}/retry` | Needs you → Retry | `/needsyou` button | |
| `POST /api/queue/{item_id}/unbatch` | Needs you | `/needsyou` button | |
| `POST /api/queue/{item_id}/publish-now` | Needs you | `/needsyou` button (type YES + PIN) | |
| `POST /api/queue/{item_id}/fix` | Needs you (Switch to assisted / auto, Upload via Studio instead) | `/needsyou` buttons (switch to assisted, Studio instead) | switch to auto: app (confirmation) |
| `POST /api/queue/{item_id}/link` | Needs you → Link | `/link 41 <url>` | |
| `POST /api/queue/{item_id}/permalink` | Instagram assisted | `/link 41 <reel url>` | |
| `GET /api/queue/{item_id}/video` | Instagram kit | `/batch <account>` | |
| `POST /api/queue/bulk` | Schedule bulk bar | Reschedule all missed / Retry all failed | other bulk edits: app |
| `GET /api/queue.csv` | Schedule | — | a CSV for a spreadsheet |
| `GET /api/batches` | Schedule (assisted) | `/batch` | |
| `GET /api/batches/files/{path:path}` | batch page | `/batch` sends the files | |
| `GET /api/instagram/app` | Settings → Instagram | Laptop | Meta app secrets |
| `POST /api/instagram/app` | Settings → Instagram | Laptop | |
| `POST /api/instagram/oauth/start` | Accounts → Instagram | Laptop / phone (Tailscale https) | OAuth |
| `GET /api/instagram/oauth/callback` | (Meta redirect) | Laptop | |
| `GET /api/instagram/pick/{pid}` | Accounts → Instagram | Laptop | |
| `POST /api/instagram/pick/{pid}` | Accounts → Instagram | Laptop | |
| `POST /api/instagram/manual` | Accounts → Instagram | Laptop | |
| `POST /api/instagram/token` | Accounts → Instagram (Connect with a token) | Laptop / phone | token exchanged on the laptop |
| `GET /api/instagram/attempts` | Accounts → Instagram | Laptop / phone | |
| `GET /api/accounts/{channel_id}/ig-dashboard` | Accounts → Instagram | `/stats`, `/top` | |
| `GET /api/remote/status` | Settings → Phone | `/app` | |
| `POST /api/remote/setup` | `clipforge remote setup` | Laptop | |
| `GET /api/remote/qr.svg` | Settings → Phone | — | a QR for the phone screen |
| `GET /api/telegram` | Settings → Telegram | `/devices` | |
| `POST /api/telegram/token` | Settings → Telegram | Laptop | the bot token itself |
| `POST /api/telegram/pair` | Settings / `clipforge telegram pair` | Laptop | codes are made on the laptop |
| `POST /api/telegram/unpair` | Settings / `clipforge telegram unpair` | `/devices` → Unpair | |
| `POST /api/telegram/pin` | Settings / `clipforge telegram set-pin` | Laptop | |
| `POST /api/telegram/test` | Settings | — | |
| `POST /api/telegram/webapp` | — | Mini App check | |
| `GET /api/server/status` | Settings → This computer / `clipforge server status` | `/server` | |
| `GET /api/server/workers` | Settings → This computer / `clipforge server workers` | `/server`, `/health` | |
| `POST /api/server/workers` | `clipforge server add-worker` | Laptop | creates a secret key (server itself only) |
| `DELETE /api/server/workers/{worker_id}` | `clipforge server remove-worker` | Laptop | security setting |
| `POST /api/server/fence` | `clipforge worker --standalone-emergency` | — | machine-to-machine (worker key) |
| `GET /api/node/status` | Settings → This computer (laptop) | `/server` | |
| `POST /api/worker/hello` | (worker) | worker joined / left messages | machine-to-machine |
| `POST /api/worker/claim` | (worker) | — | machine-to-machine |
| `POST /api/worker/heartbeat` | (worker) | progress message ("on laptop") | machine-to-machine |
| `POST /api/worker/complete` | (worker) | — | machine-to-machine |
| `POST /api/worker/fail` | (worker) | job failed message | machine-to-machine |
| `POST /api/worker/import` | (worker, offline jobs) | — | machine-to-machine |
| `GET /api/worker/artifacts/{task_id}/{path:path}` | (worker pull) | — | machine-to-machine |
| `PUT /api/worker/artifacts/{task_id}/{path:path}` | (worker push) | — | machine-to-machine |
| `GET /api/worker/staged/{task_id}/{path:path}` | (worker resume) | — | machine-to-machine |

## Example chat (rendered from `tests/test_telegram.py::test_e2e…`)

```
you: look https://youtu.be/vidA?si=xyz
bot: 🎬 New job
     https://youtu.be/vidA?si=xyz
     Clips: 20 · Part: full video
     LLM: ollama · Layout: auto · Captions: roman
     [▶ Start with my defaults] [10] [15] [✓ 20] [25] [✓ Full video] [✂ Part…] [✓ ollama] [✓ auto] [track] [split] [blur] [✓ Roman] [देवनागरी] [✖ Cancel]
you: (taps 15, then ▶ Start)
bot (edits the same message): 🎬 Job 1 · https://youtu.be/vidA?si=xyz
     ▶ Transcribing ▰▰▰▱▱▱▱▱▱▱ 31% · ~10:00 left
     ✅ ▶ · · · · · ·
     📍 on my-laptop
     [⏹ Cancel job]
bot (edits): … ▰▰▰▰▰▱▱▱▱▱ 50% …
bot: 🎉 4 clips ready
     Best score ⭐ 8.0 · total 2:00
     [🎬 All 4 clips] [✅ Approve all ≥ 7] [📅 Schedule approved]
bot [album of 2 thumbnails]
bot: #1 · ⭐ 8.0 · 0:30
     Clip 1 title
     🪝 hook 1
     ⏳ Not reviewed
     [▶ Preview] [✅ Approve] [❌ Reject] [✏️ Edit] [📅 Schedule] [⬇ Full quality]
bot (edits the progress message): ✅ Job 1 done — 4 clips ready.  [🎬 Clips] [✅ Approve all ≥ 7]
you: /approve 1 1-3
bot: ✅ Approved 3 clip(s) of job 1 (1-3).  [📅 Schedule approved]
you: (taps 📅 Schedule approved → ▶️📸 Both)
bot (edits): 📅 Scheduled (private until the slot):
     ▶️ Figuring Clips · Wed 04 Nov 18:30 · Clip 1 title — the real story
     ▶️ Figuring Clips · Wed 04 Nov 21:00 · Clip 2 title — the real story
     ▶️ Figuring Clips · Thu 05 Nov 12:30 · Clip 3 title — the real story
     📸 figuring.reels · Wed 04 Nov 19:00 · Clip 1 title — the real story
     📸 figuring.reels · Wed 04 Nov 21:30 · Clip 2 title — the real story
     📸 figuring.reels · Thu 05 Nov 13:00 · Clip 3 title — the real story
you: /queue
bot: 📅 Next 30 days (IST) — 6 posts
     Wed 04 Nov
     18:30 ▶️ Clip 1 title — the real story (Figuring Clips)
     19:00 📸 Clip 1 title — the real story (figuring.reels)
     …
you: /needsyou
bot: ✅ Nothing needs you.
```

Editing (from `test_edit_rerender_resend_undo`):
```
you: /edit 1   → ✏️ Editing #1 · 0:30 — Starts: “w10 w11 w12 w13 …” Ends: “… w67 w68 w69.”
     [📝 Title] [🪝 Hook] [🎨 Style] [🧱 Layout] [🔤 Script] [Start −1s] [−0.3] [+0.3] [+1s] [End −1s] … [🔡 Words] [📋 Text] [🔁 Re-render] [↩ Undo]
you: (📝 Title) Paisa kaise banta hai
bot: Title of #1: ~~Clip 1 title~~ → Paisa kaise banta hai  [💾 Save] [✖ Cancel]
you: /trim 1 0:02-0:20
bot: ✂ #1 is now 0:18. Starts: “w15 w16 …” Ends: “… w49.”  [🔁 Re-render] [✏️ Fine-tune]
you: /word 1 "galti" "galat"
bot: 🔤 Changed 1 word(s) in #1 → “galat”. Re-render to apply.  [🔁 Re-render]
bot [video]: 🔁 New version of #1 · Paisa kaise banta hai · 0:18  [↩ Undo] [✅ Approve] [📅 Schedule]
```

Limits used (checked against core.telegram.org on 2026-11-04, Bot API 10.3): text 4,096 chars, captions 1,024,
callback data 64 bytes, albums 2–10, cloud uploads 50 MB / downloads 20 MB, local server 2,000 MB uploads,
~1 msg/s per chat, ~30 msg/s overall, 20/min in groups, `retry_after` on 429.
