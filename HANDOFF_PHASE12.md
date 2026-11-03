# Phase 12: YouTube publishing, smart scheduling & growth tracking (handoff)

Built on your laptop on 2026-11-04, pushed to <https://github.com/hamsidhi/clipforge> (`main`).

## ⚠️ First: Google changed the rules two days ago
I checked every "critical platform fact" in the brief against Google's own docs. Two had changed:

1. **The private-only lock for unaudited API projects was lifted on 2 Nov 2026** (YouTube Data API revision history).
   API mode works now without the audit.
   - **Assisted mode is still the default**, as you asked. Switch to API mode in Settings whenever you like.
   - The lock check still runs after every upload, and there's a `publishing.unverified_lock` switch in case Google brings the lock back.
2. **Quota:** uploads now have their **own bucket of 100 `videos.insert` calls/day**, instead of costing about 1,600 of the
   10,000 units. Everything else ClipForge calls costs 1 unit, so quota will practically never limit you.

Details and sources are in [DECISIONS.md → Phase 12](DECISIONS.md).

## What was built
| Area | Status |
|---|---|
| 12A Multi-channel accounts: OAuth from the UI (localhost callback, PKCE), Brand-Account picker, per-channel tokens & settings, `invalid_grant` → "Reconnect", client_secret upload with steps | ✅ (real Google sign-in needs your account; see below) |
| 12B Metadata optimizer: 3 title variants + rubric pick, description with credit line + template + 3–5 hashtags (`#shorts` + niche), tags ≤500 chars, category/language/made-for-kids/synthetic-media flags, duplicate-title guard, regenerate, editor with counters | ✅ |
| 12C Smart scheduler: slot plan per channel (IST), best-first + episode interleave + 24 h topic gap + posts/day + quota rollover + weaker clips off-peak; week calendar (drag to move) + list (bulk); background runner (uploads 5 days ahead as private + publishAt, retries with backoff, survives restarts); `clipforge scheduler --once`; `schedule-task install/remove` | ✅ |
| 12C Best-time learner: heatmap by IST weekday/hour from 24 h views, recommendation with an explicit evidence rule, "Apply" only on click | ✅ (needs 20+ published Shorts to recommend) |
| 12D Assisted Studio mode: batch folders (`01_<date_time>_<title>.mp4`) + `batch.html` checklist (copy buttons, Done ticks, Studio link, Instagram kit); Sync with YouTube (fuzzy title + duration) + manual linking | ✅ |
| 12E Dashboard: subscribers, Shorts views 28/90 d, top 10, what's working (topic/episode/length/title style), YPP bars with ETA (date-aware 2027 targets), campaign earnings, "published but not submitted" reminders, CSV exports | ✅ (real numbers after your first sync) |
| 12E Tracker: campaign fee %, submission status/link, approved views; earnings = approved views × rate × (1 − fee) | ✅ |
| 12F API (OpenAPI at `/docs`, tag "publishing"), pages Channels / Schedule / Batches / Dashboard, Settings → Publishing, "Add to schedule" in Gallery + Clip editor, CLI commands | ✅ |
| 12G Safety: always private + publishAt; first-upload confirmation with avatar; per-channel pause; one clip once per channel; never over quota; copyright note next to the credit line; no browser automation of YouTube | ✅ |
| Tests: 64 pass (fake YouTube, no network); e2e: 6 clips → metadata → 2-day schedule → API upload → publish → stats, plus assisted batch → Studio upload → reconcile | ✅ |
| Screenshots of every new page, desktop + mobile, no console errors | ✅ `docs/screenshots/phase12/` |

## What YOU must do with your Google account (one time, ~15 min)
1. Go to <https://console.cloud.google.com/projectcreate> and create a project, e.g. "ClipForge".
2. Go to **APIs & Services → Library** and enable **YouTube Data API v3** and **YouTube Analytics API**.
3. Set up the **OAuth consent screen**:
   - Choose External. Set the app name ("ClipForge") and your email.
   - Scopes: you can skip this page; ClipForge requests them itself.
   - Test users: add your Google account.
   - **Publish app → In production.** It stays unverified, which is fine for your own use, and stops the weekly
     re-login. Google shows an "unverified app" warning once; click *Advanced → Go to ClipForge*.
4. Go to **Credentials → Create credentials → OAuth client ID**. Application type: **Desktop app**. Click **Download JSON**.
5. Run `clipforge ui`, then go to **Settings → Publishing → Upload client_secret.json**.
6. Go to **Channels → Add channel**. Sign in and choose the channel (Brand Accounts appear in Google's chooser). Then:
   - Click **Confirm** and check the avatar and name.
   - Open *Schedule & metadata settings*. Check the times (12:30 / 18:30 / 21:00 IST), posts/day and the description template, then **Save**.
   - Repeat for each channel. **Pause** channels you don't want touched, such as your music channel.

## Your first week of Shorts (assisted mode)
1. **Make clips:** New job → paste the episode → wait → **Gallery**. Approve 15–21 clips (7 days × 3).
2. **Add approved to schedule** → pick the channel. Wait about 20–40 s per clip while the local LLM writes the metadata;
   you'll see "Writing metadata…" in Schedule → Drafts.
3. **Schedule → Schedule approved clips** fills the coming days. Check the week view. Drag clips to swap slots, or click a clip
   to pick another title variant or fix the description.
4. **Prepare batch.** This opens the checklist (up to 15 clips). In **YouTube Studio**, select all the files at once, and for each one:
   - paste the title, description and tags;
   - set *Not made for kids*;
   - set Visibility → **Schedule** at the date and time shown;
   - tick Done.
5. Back in ClipForge: **Sync with YouTube**. The clips turn purple ("Scheduled on YouTube"). Anything not matched is listed;
   open it and paste the YouTube link to link it by hand.
6. Every few days: **Dashboard → Sync stats**. When a Short is published, log it in **Tracker** for your campaign.
   The Dashboard reminds you of any you haven't submitted.

Want it fully hands-off instead? Switch to **API mode** in Settings, keep the 3 slots, and run `clipforge schedule-task install`
once. ClipForge then uploads each clip about 5 days ahead as private + scheduled, and YouTube publishes it on time.

## Known limits
- **Quota:** 100 uploads/day for the whole Google project, shared by all your channels. Plenty for 3 a day per channel.
- **The learner needs data:** heatmap recommendations need 20+ published Shorts with 24-hour views.
- **Analytics lag:** the Analytics API lags 1–2 days, so new Shorts show 24 h/72 h views only after that. YPP "Shorts views (90 d)"
  needs the YouTube Analytics API enabled; without it ClipForge sums per-video views instead (approximate).
- **Reconcile is fuzzy:** it matches on title (≥80% similar) and length (±2.5 s). If you heavily rename a clip in Studio, link it
  by hand.
- **Slot passed before upload:** in API mode, if the laptop was off past a clip's slot before it was uploaded, the clip becomes
  "Needs you". Pick a new time; ClipForge never shifts it by itself.
- **Custom thumbnails** (`thumbnails.set` now supports Shorts) aren't used. YouTube picks a frame.
- **YPP targets** come from news coverage of YouTube's 10 Aug 2026 announcement. Check YouTube's help page and edit `config.yaml → ypp`
  if needed.
- **Adding channels from your phone doesn't work:** Google sends desktop-app logins back to localhost, so add them on the laptop.
- **Instagram stays manual:** the batch page has an Instagram kit, and the Publish Kit copies captions.

## Files
- Code: `clipforge/publish/`, `clipforge/api/publish_routes.py`, `clipforge/cli_publish.py`; pages in `frontend/src/pages/{Channels,Schedule,Batches,PublishDashboard}.tsx`.
- Docs: README §5, [DECISIONS.md](DECISIONS.md) (Phase 12), [TODO_ON_LAPTOP.md](TODO_ON_LAPTOP.md) §1, [CLAUDE.md](CLAUDE.md).
- Try the UI with fake data: `python scripts/demo_publish.py`, then start the UI with
  `CLIPFORGE_CONFIG=workspace_demo\config.yaml` and `CLIPFORGE_NO_TICKER=1`.
