# Handoff — Phase 15: Full Telegram control + always online

Date: 2026-11-04 · repo `hamsidhi/clipforge`, branch `main`.

## What was built

**Part A — Telegram as a full remote control** (`clipforge/notify/`, docs: [docs/TELEGRAM.md](docs/TELEGRAM.md))
* One service layer (`clipforge/services/`) used by the web app, Telegram and the CLI — no second implementation.
* Start jobs by pasting a link / forwarding a video / sending a file → options wizard (clips, part of the video, LLM,
  layout, caption script, or one tap "Start with my defaults"), duplicate check, one live progress message per job.
* Clips arrive as an album + one card per clip: Preview · Approve · Reject · Edit · Schedule · Full quality. Files go
  through a size fallback chain (cloud 50 MB → re-encoded "Telegram-fit" copy → private Tailscale link), with a
  file_id cache and an optional local Bot API server for 2 GB files.
* Edit from chat: title/hook (with a preview before saving), caption style, layout, script, trim buttons and
  `/trim`, caption word fixes (`/words`, `/word`), bulk approve/reject, re-render with the new version sent back and Undo.
* Publishing from chat: schedule to YouTube and/or Instagram (next free slot or a typed IST time), `/queue`,
  `/schedule` calendar, `/needsyou` with the web app's one-tap fixes, assisted batches, `/health`, `/stats`, `/top`,
  `/earnings`, `/campaigns`, pause/resume, `/server`.
* Security: only paired chats (unknown chats get silence), owner/viewer roles, groups only with a group code, short
  opaque button ids, 2-minute one-shot confirmations bound to you, "type YES" for public, optional hashed PIN (message
  deleted, lockout), scrubbed errors with error ids, rate limits, audit log + "what the bot did today", one poller.
* Mini App support (off by default; Tailscale HTTPS, initData checked, password session still required).

**Part B — Always online** (`clipforge/server/`, `deploy/`, docs: [deploy/README.md](deploy/README.md),
[docs/WORKER_PROTOCOL.md](docs/WORKER_PROTOCOL.md), AUTOMATION_GUIDE Part I)
* `deployment.mode: standalone | server | worker` — **standalone stays the default; your laptop works exactly as before.**
* Server = database, logins, publishing, Telegram, web app (Tailscale only). Laptop = worker for download / transcribe /
  select / render, pulling and pushing workspace files with sha256 checks; leases + heartbeats + fencing tokens;
  re-queue on worker loss; server fallback after 10 min; "hold" tasks when YouTube blocks the server's download.
* Exactly one publisher (never a worker, never a fenced server). Emergency standalone switch.
* One-command install (systemd, not Docker): Tailscale with your auth key, non-root user, firewall (tailnet only),
  swap, automatic security updates, time sync, Ollama + model, `clipforge doctor`.
* `clipforge server status | logs | update (auto-rollback) | backup | restore | cleanup | add-worker | workers |
  handover --dry-run/--run/--rollback | import-token | unfence`, `clipforge worker [install-task]`.
* Nightly encrypted backups (7 daily + 4 weekly, key stays with you, optional rclone crypt to your Drive), disk
  housekeeping that never deletes anything still waiting to be published, Telegram disk warnings, `/server`.

## What was tested, and how
* **All automated tests pass** (`pytest`, ~4 min; Phase 15 added ~70): `tests/test_telegram.py` (19: pairing, roles, groups,
  unknown-chat silence, confirmations bound/expiring/one-shot, PIN with message deletion + lockout, "type YES", rate
  limiter + 429 retry_after, 4096/1024 splitting incl. code blocks, callback data ≤ 64 bytes, conversation timeout +
  /cancel, file-size fallback chain + file_id cache, big/small uploads, edit → re-render → resend → undo, IST time
  parsing, single-poller lease + 409 + durable offset, worker never polls, Mini App initData, and a full E2E: link →
  wizard → progress edits → clips delivered → approve 3 → schedule both platforms → /queue → /needsyou empty),
  `tests/test_telegram_audit.py` (every API route has a row in the audit table; every command is documented),
  `tests/test_server.py` (14: leases/heartbeat/requeue/fencing/idempotency, fallback vs hold, cancel + server restart,
  in-process worker round trips for transcribe and render, stale results dropped, checksum failure + resume, path
  traversal over HTTP, worker auth + mode, offline-job import, publishing guard worker/fenced, unique upload claims,
  bot-check detection + capped backoff + routing to the laptop, cleanup rules, handover dry-run/run/rollback,
  backup/restore round trip + wrong key + truncation + retention).
* **Two-process deploy test** (`scripts/deploy_e2e.py`, log in [docs/DEPLOY_TEST.md](docs/DEPLOY_TEST.md)): a real
  server process and a real worker process over HTTP — transcribe (Whisper tiny), select (mock LLM) and render all
  ran on the worker, came back sha256-verified, the job finished on the server, then the server's scheduler uploaded
  both clips to a fake YouTube as private + publishAt and they went public only at their slot. PASS in 67 s.
* Web UI rebuilt; Settings → Phone & Telegram (devices, viewer pairing, PIN) and "This computer" checked in the
  browser, no console errors.
* **Security review** (separate agent): 13 findings, all fixed with regression tests (`tests/test_phase15_security.py`).
  The serious ones: a logged-in phone could change Telegram/deployment/backup settings (now laptop-only, and the
  Telegram API address is validated so the bot token can't be sent elsewhere); a worker key could overwrite another
  job via `/api/worker/import`; error texts to Telegram weren't fully scrubbed. Details: DECISIONS.md.
* **Reliability drills** (separate agent, real processes + fakes): 8 drills — kill the worker mid-render, restart the
  server mid-job, disk full, YouTube blocked, expired tokens, Telegram 429s + network drops, two pollers, laptop
  sleeping through its lease. 5 passed; the 3 failures (a job looping forever after a lost hand-in, no retry of a lost
  hand-in, Telegram updates handled twice when a batch outlived the lease) and 5 risks are fixed, each with a
  regression test (`tests/test_reliability.py`).

## One incident to know about
From **16:00 to 17:01 IST today** your running `clipforge ui` couldn't read `config.yaml` (I had added the new
Phase 15 keys to it while the old code was running; the old code rejects unknown keys). Its scheduler failed during
that hour; I fixed it at 17:01 by removing those keys from your working copy (they all have defaults). **No post was
missed**: today's posts were already on YouTube as private + scheduled, and your queued uploads are days ahead.
Your own settings (slots, 10 posts/day, 10 days ahead, Telegram on) were never changed. Details: DECISIONS.md.

## What was NOT verified (be careful)
* **Nothing against the real Telegram servers.** All bot behaviour was tested on a fake Bot API. Real-world quirks
  (formatting of long messages, `deleteMessage` of your PIN, album look) need your first real use — TODO 1d.
* **The Mini App on a real phone** — off by default; `/app` sends a normal link unless you turn it on.
* **No Linux / Arm machine was available** (no Docker, no WSL distro here). `deploy/install.sh` passed `bash -n` but has
  never run on a real Oracle VM; the two-process test ran on Windows. The `ubuntu-24.04-arm` GitHub workflow exists
  (manual: Actions → Deploy test → Run workflow) — the repo is private and `gh` isn't logged in, so I couldn't see a run.
* **Performance numbers for the Oracle box are estimates** (scaled from laptop numbers), not measurements.
* **Oracle sign-up, capacity in Hyderabad/Mumbai, the budget alert, Tailscale serve on the server, the SSH handover
  over a real network, the local Bot API server build, rclone to Google Drive** — all need your accounts.
* Windows sleep/hibernate can't be caught reliably; a sleeping laptop's lease simply expires (≤ 2 min) and the job
  moves on — tested in code, not on a real lid-close.

## Acceptance scenarios (run these yourself after the steps below)
| Scenario | Expected | Status |
|---|---|---|
| **Phone only, laptop ON**: share a link → wizard → job runs on the laptop → clips arrive → trim + word fix → re-render → approve → schedule both platforms → posts go out on time | progress says "📍 on <laptop>"; new version arrives with Undo; YouTube upload is private + scheduled; Instagram at its slot | ⚠️ simulated end to end (fake Telegram + two real processes); not on your phone yet |
| **Phone only, laptop OFF**: same flow | job runs on the server ("📍 on clipforge-server (server)", slower); a blocked download says so in Telegram and waits for the laptop / your file; posts go out on time | ⚠️ server path tested with the server doing every stage in unit tests; real Oracle box not tried |
| **Laptop comes back mid-job** | the stage already running on the server finishes there; the next stages / jobs go to the laptop; nothing duplicated (one live task per stage, fencing) | ⚠️ delegation + fencing tested in unit tests and reliability drills; not with real hardware |
| Standalone (today's setup) keeps working | everything as before; Telegram now has the full command set | ✅ default mode; all old tests pass |

## Your steps, in order
1. **Restart ClipForge on the laptop (1 min)** so it runs the new code: close `clipforge ui` (or `clipforge service`
   restarts it) and start it again. Then in Telegram send `/help`.
2. **Pair + PIN (5 min)**: you're already paired (migrated as owner). `clipforge telegram set-pin`. Try `/menu`,
   paste a short YouTube link, approve a clip, `/needsyou`. (TODO_ON_LAPTOP 1d.)
3. **Optional: local Bot API server (15–40 min)** only if you send big files often — docs/TELEGRAM.md.
4. **Oracle account + ₹1 budget alert (10 min)** — deploy/README.md step 1. Home region Hyderabad or Mumbai (permanent).
5. **Create the A1 VM (5 min + capacity retries)** — 2 OCPU / 12 GB, Ubuntu 24.04, 100 GB boot; "out of host capacity"
   → another availability domain, or retry later (night IST works best).
6. **Install (5 min + ~20 min waiting)**: `sudo TS_AUTHKEY=tskey-auth-… bash install.sh` on the VM; then delete the
   port-22 ingress rule in the Oracle security list (exact clicks in deploy/README.md step 5).
7. **On the server (5 min)**: `clipforge auth set-password`; `clipforge server backup-keygen` → public key into
   `server.backup_recipient`, secret key into your password manager, delete it from the server.
8. **Handover (10 min + copy time)**, on the laptop: `clipforge server handover --to clipforge-server.<tailnet>.ts.net --dry-run`,
   read it, then `--run` (`--skip-sources` makes it much faster; sources are re-downloaded only if you re-edit old jobs).
   This creates the worker key on the server and saves it on the laptop for you (`server add-worker` is only needed for
   a second laptop). Connect new accounts later with `clipforge server import-token`.
9. **Laptop worker (2 min)**: `clipforge worker status`, then `clipforge worker install-task`.
10. **Run the 3 acceptance scenarios above (30–60 min)** and note anything odd.

## Rollback
* **Telegram**: everything is additive; `clipforge telegram clear-pin`, `clipforge telegram unpair`.
* **Server/worker**: `clipforge server handover --rollback --to clipforge-server.<tailnet>.ts.net` stops the server's
  service first (so two machines never publish), restores this laptop's config + logins (mode `standalone`) and
  re-enables the scheduler task. Then restart `clipforge ui`. If the server is unreachable: stop it in the Oracle
  console (Instance → Stop) before using the laptop.
* **Server gone, laptop must take over now**: `clipforge worker --standalone-emergency` (fences the server first).
* **Code**: `git log` → Phase 15a `279431e`, 15b `15cc284`; the previous version is `69e65df`.
