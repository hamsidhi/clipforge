# ClipForge — Master handoff (Phases 12.5, 13, 14)

Built unattended on 4 Nov 2026. Everything is committed and pushed to `main`. Tests: `pytest`: all green (no network,
fake YouTube / Instagram / Telegram clients). The real end-to-end run on
[the Sam Altman episode](https://youtu.be/TjPPIf-M6KE) still works with the new code (`clipforge run …`, 20 clips).
It also feeds the demo, the screenshots and the audit screencast.

## 1. Why "Needs you" happened (plain words)
- You were in **assisted mode**. In this mode ClipForge never uploads by itself, and no Task Scheduler job existed.
- You uploaded 10 clips in YouTube Studio straight from the `clips` folder, **changed most titles** and picked your
  own times (all 5 Nov).
- ClipForge's old Sync recognised your uploads **only by title**, so it linked just 3 of them. The rest stayed
  "queued", and as soon as ClipForge's own (different) time passed they flipped to a "Needs you" with no reason.
- **YouTube itself was fine:** all 10 were private + scheduled, nothing was broken or locked. Nothing was public yet
  only because you'd scheduled everything for 5 Nov.

**What I fixed:**
- **Sync now recognises uploads by their original file name**, then by title, ignoring `#shorts`. It runs on its own
  before anything is called missed. I ran it once on your channel (read-only): **all 9 of your Studio uploads are now
  linked** and show "Scheduled on YouTube" at your Studio times.
- **Every problem now says why and has one-click fixes** (Reschedule, Sync, Link, Open batch, Retry, Reconnect,
  Publish now…). There are bulk buttons too, and a **Health** panel on the Dashboard.
- **API mode uploads as soon as a clip is scheduled** (private + publish time), so YouTube publishes on time even with
  the laptop off. If the laptop was off at a slot, the clip moves to the next free slot (configurable).
- Google removed the "unaudited API projects upload private-only" lock on **2 Nov 2026** (checked in their revision
  history today). **API mode works without the audit.** The audit kit is still ready if you want more quota.

## 2. Status
| Part | | Notes |
|---|---|---|
| 12.5 diagnosis + "Needs you" fix | ✅ | DECISIONS.md → "Phase 12.5 diagnosis"; state machine in `publish/states.py` |
| Upload-ASAP, late policy, verify-after-upload, catch-up | ✅ | tested with the fake client |
| Task Scheduler: 30 min + logon + wake, hidden | ✅ | `clipforge schedule-task install`. Not registered on your PC (that changes system settings): your step |
| Health panel | ✅ | Dashboard |
| Audit kit: pages, AUDIT_KIT.md, RMF upload UI, screenshots + screencast | ✅ | `docs-site/`, `docs/audit/` |
| GitHub Pages live | ⚠️ | needs one click: repo Settings → Pages → Source "GitHub Actions" (`gh` isn't logged in here) |
| Real test upload (`publish-test`) | ⚠️ | not run: "now + 2 h" would make it public, and ClipForge's login can't unschedule or delete. Command ready for you |
| 13 Instagram Reels (API path, resumable upload, publish at slot, rate limit, tokens, insights) | ✅ | fake Graph API tests |
| Instagram assisted kit + manual accounts | ✅ | |
| Instagram with your real account | ⚠️ | needs your Meta app + Professional account (Part G) |
| 14 Auth (scrypt, sessions, CSRF, lockout, audit log, headers) | ✅ | security-reviewed by a subagent |
| Tailscale remote setup + QR | ✅ / ⚠️ | code + tests done; Tailscale isn't installed on this laptop, so it wasn't run for real |
| Service (watchdog at logon), keep-awake | ✅ | `clipforge service install` is your step |
| PWA (install, share target, offline page), phone UI, previews, resumable upload | ✅ | phone screenshots in `docs/screenshots/phase14/`, no console errors, no sideways scroll |
| Telegram bot (pairing, commands, buttons, alerts, daily summary) | ✅ / ⚠️ | fake Bot API tests; needs your bot token |
| `clipforge watch <folder>` | ✅ | off by default |

## 3. Your steps, in order
**YouTube (15 min)**
1. Restart the app: Ctrl+C in any old `clipforge ui` window, then `clipforge ui` (or set up the service, step 14).
2. Schedule → **Sync with YouTube** (links anything new).
3. Choose a mode in Settings → Publishing:
   - **API (recommended):** set the mode, then run `clipforge schedule-task install` once.
     Optional lock check: `clipforge publish-test UCT5PqvmHmx8HldPvyZwRRTQ` (private test video, publishes in 30 days:
     **delete it in Studio afterwards**).
   - **Assisted:** Prepare batch → upload in Studio (titles/times may change) → Sync. About 10 min a week.
4. Optional audit:
   - GitHub → Settings → Pages → Source **GitHub Actions**.
   - Fill the form with [AUDIT_KIT.md](AUDIT_KIT.md); attach `docs/audit/*`.
   - Settings → Publishing → audit status **Applied**.

**Instagram (30 min)**: [AUTOMATION_GUIDE.md → Part G](AUTOMATION_GUIDE.md)
5. Switch Instagram to a **Professional** account; link a **Facebook Page**.
6. Create the free **Meta app** (Business), add Facebook Login for Business + Instagram, and the redirect URI
   `http://localhost:8000/api/instagram/oauth/callback`. Paste the App ID + Secret into Settings → Instagram Reels.
7. On the laptop: Accounts → Instagram → **Add Instagram account** → pick → **Confirm**.
8. Test: approve a clip → Add to schedule (Instagram) → open it → **Publish now** → the Reel appears within ~2 min.

**Phone (30 min)**: [AUTOMATION_GUIDE.md → Part H](AUTOMATION_GUIDE.md)
9. `clipforge auth set-password`
10. Install Tailscale on the laptop + phone (same account). Admin console: MagicDNS + HTTPS Certificates on.
11. `clipforge remote setup` → scan the QR on the phone → log in → **Install app**.
12. `clipforge service install` (starts at login, restarts if it crashes). Power: plugged in → never sleep; lid → do nothing.
13. Telegram: @BotFather → token → Settings → Phone & remote → Save → Pair → send `/pair <code>` to the bot.

## 4. A phone-only day
1. YouTube app → Share → **ClipForge** → *Create clips*.
2. Telegram pings "Job finished" with the top 3 clips → ✅ the good ones (or swipe in the Gallery: right = approve).
3. Send `/schedule <job> both` → ✅ Yes. Shorts upload as private + scheduled, Reels wait for their slots.
4. Check the Stats tab or `/dashboard`; the 22:00 summary arrives by itself.

## 5. Known limits
- **The laptop must be on and online** to make clips, to upload, and to answer your phone.
- **YouTube** publishes by itself once uploaded (laptop can be off).
- **Instagram** has no scheduling in its API: **ClipForge must be running at each Reel's time.** Missed by >90 min →
  "Missed slot" (never posted late).
- **Accounts are connected on the laptop** (Google and Meta send logins back to `localhost`).
- Remote access is private to your tailnet. A public address (Cloudflare Tunnel + Access) is documented as an option
  only, with warnings.
- The audit isn't needed to publish any more and may be refused for a personal tool; assisted mode always works.
- Licensed music can't be picked through Instagram's API.

## 6. Where things are
- `DECISIONS.md`: Phase 12.5 diagnosis and fixes, Phase 13 facts and design, Phase 14 design, and the two review
  rounds (findings and fixes).
- `AUTOMATION_GUIDE.md`: the "Needs you" section, Parts D, G, H and troubleshooting.
- `TODO_ON_LAPTOP.md`: everything that needs your accounts or devices.
- `CLAUDE.md`: architecture notes for the new modules.
- Screenshots: `docs/screenshots/phase13/`, `docs/screenshots/phase14/`; audit files: `docs/audit/`.
