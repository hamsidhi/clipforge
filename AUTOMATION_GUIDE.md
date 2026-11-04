# ClipForge automation guide (step by step)

This takes you from "nothing set up" to "Shorts publish on YouTube by themselves on schedule".
It's written for Windows, but the macOS/Linux differences are noted.

**What's automatic and what isn't (honest version):**

| Step | Automatic? |
|---|---|
| Cut 15–25 clips from an episode (transcribe, select, reframe, captions, render) | ✅ one click / one command per episode |
| You review the clips (approve/reject, fix a word) | 👤 your judgement, about 10 minutes, deliberately kept manual |
| Titles, descriptions, tags, hashtags, credit line | ✅ |
| Choosing publish times (best clips at peak hours, topics spread out) | ✅ |
| Uploading to YouTube with a scheduled publish time | ✅ in **API mode**; 👤 in assisted mode (you drag files into Studio) |
| YouTube making them public at the right time | ✅ YouTube does it, even if your laptop is off |
| Pulling views, subscribers and YPP progress | ✅ every 12 h |
| Instagram Reels | ✅ with a Meta app (Part G); the laptop must be on at post time. Without one: an Instagram kit you post yourself |

Plan on about **10 minutes of your time per episode** (approving clips). Everything else runs by itself.

---

## Part A — One-time setup (≈ 45 min, done once)

### A1. Make sure ClipForge itself runs
1. Open **PowerShell** and run:
   ```powershell
   cd E:\youtube\clipforge
   .venv\Scripts\activate
   clipforge doctor
   ```
   Every **required** line should show ✔. If something is red, run the command shown in the "Fix" column.
2. Make sure **Ollama** is running (its icon is in the system tray). If not, start "Ollama" from the Start menu.
3. Start the app:
   ```powershell
   clipforge ui
   ```
   Your browser opens <http://localhost:8000>. Keep this terminal open while you use ClipForge.
   - If the browser shows errors after an update, press **Ctrl+C** in the terminal and run `clipforge ui` again.
     An already-running server keeps using the old code.

### A2. Create your free Google Cloud project (one time, ≈ 15 min)
This is what lets ClipForge talk to YouTube. It's free and there's no credit card.

1. Go to <https://console.cloud.google.com/projectcreate>, sign in with the Google account that owns your
   YouTube channel, name the project **ClipForge**, and click **Create**.
2. At the top of the page, make sure the **ClipForge** project is selected.
3. Turn on two APIs:
   1. Open **☰ menu → APIs & Services → Library**.
   2. Search **YouTube Data API v3** → open it → **Enable**.
   3. Go back to the Library, search **YouTube Analytics API** → **Enable**.
4. Set up the consent screen (APIs & Services → **OAuth consent screen**, sometimes called **Google Auth Platform → Branding**):
   - User type: **External** → Create.
   - App name: `ClipForge`, support email: yours, developer contact: yours → Save and continue.
   - Scopes: skip (ClipForge asks for them itself) → Save and continue.
   - **Test users → Add users →** your Google email → Save.
   - Back on the overview, click **Publish app → Confirm** so the status says **In production**.
     *Why:* in "Testing" mode Google logs you out every 7 days. In production, unverified is fine for your
     own use; you'll see one warning screen when you sign in.
5. Create the OAuth client:
   1. Go to **APIs & Services → Credentials → + Create credentials → OAuth client ID**.
   2. Application type: **Desktop app**. Name: `ClipForge desktop` → **Create**.
   3. Click **Download JSON**. The file is called something like `client_secret_123…json`.
6. In ClipForge, go to **Settings → Publishing (YouTube) → Upload client_secret.json** and pick that file.
   You should see "installed ✔". (The file is stored in your ClipForge folder and is never pushed to GitHub.)

### A3. Connect your channel(s)
Do this **on the laptop itself** at <http://localhost:8000>, not from your phone, because Google sends the login back to localhost.

1. Open **Channels → Add channel**.
2. Google opens. Choose your account and then the **channel** (Brand Account channels appear in the list).
   - If you see "Google hasn't verified this app": click **Advanced → Go to ClipForge (unsafe)**. It's your own app.
   - Tick **all** permission boxes: upload, view your channel, and view YouTube Analytics.
3. Back in ClipForge, the channel appears. Click **Confirm** and check that the avatar and name are right.
   ClipForge refuses to upload to an unconfirmed channel, so you can never post to the wrong one (for example your music channel).
4. Click **Test login**. It should say "Login works".
5. Open **Schedule & metadata settings** under the channel and set:
   - **Publish times (IST):** `12:30, 18:30, 21:00` (the defaults; the Dashboard suggests better ones later).
   - **Peak times:** `18:30, 21:00`. Your strongest clips go here.
   - **Posts / day:** `3`. **Posting days:** all 7 (or skip weekends).
   - **Title language:** Hinglish or English. **Category:** Education (27) is a good default.
   - **Description template:** for example "Follow for daily clips from India's best podcasts 🔔".
   - **Credit line:** keep `🎙 Source: {episode} — {source_channel} {url}`. Only post other creators' clips under an
     official clipping campaign or with their permission.
   - **Campaign:** name, rate per 1,000 views (₹) and fee %, if you're in a Whop/clipping campaign.
   - Click **Save settings**.
6. Repeat for every channel you post to. For channels ClipForge should never touch, flip the switch to **Paused**.

### A4. Choose the publishing mode
Go to **Settings → Publishing → Mode**:

| | **Assisted** (default) | **API** (fully automatic) |
|---|---|---|
| Who uploads | You, in YouTube Studio, from a ready-made folder + checklist | ClipForge |
| Your work per batch | ≈ 10 min of copy-paste | none |
| Laptop needed | when you upload | for a few minutes every day or two (it uploads 5 days ahead) |
| Limit | Studio takes ≈ 15 files at a time | 100 uploads/day for your whole Google project (all channels together) |

Google removed the old "API uploads stay private" restriction on 2 Nov 2026, so **API mode works right away**.

**Recommendation:** do **one week in assisted mode** (Part C), then switch to API mode (Part D) once you trust the
titles and times. Click **Save settings** after changing the mode.

---

## Part B — Every new episode (≈ 10 min of your time)

1. **New job:** go to **Jobs → New job** and paste the YouTube link of the full episode.
   - Clips: 20 · Length 20–59 s · Caption style "Bold Yellow Pop" · Layout Auto · AI: Ollama.
   - Click **Create clips**. On this laptop a 2-hour episode takes about 1¼ h (transcription about 40 min, selection about 26 min,
     render about 8 min). You can close the browser tab; the job keeps running while `clipforge ui` is open.
   - CLI alternative:
     ```powershell
     clipforge run "https://www.youtube.com/watch?v=VIDEO_ID"
     ```
2. **Review (the only manual part):** open the job's **Gallery**.
   - Hover over a clip to preview it. Click ✓ to approve or ✗ to reject. **Approve ≥ score** approves all strong clips at once.
   - If a caption word is wrong or a clip starts badly, click ✎. In the editor, fix the word or drag the trim handles, then
     click **Save & re-render**.
3. **Send to YouTube:** in the Gallery, click **Add approved to schedule**, pick the channel, then **Add**.
   ClipForge writes three title options, a description with the credit line, tags and hashtags in the background
   (about 20–40 s per clip with the local AI).
4. **Schedule:** open **Schedule**, select the channel, and click **Schedule approved clips**.
   - The clips fill the next free slots: best clips at 18:30/21:00, episodes alternate, and the same topic is never
     repeated within 24 h.
   - Click any clip to pick another title option, edit the description or change its time. Drag it to another slot to move it.
   - Anything under **Drafts** with a yellow warning (for example a duplicate title) needs a quick fix, then
     **Schedule approved clips** again.

From here, Part C (assisted) or Part D (API) takes over.

---

## Part C — Assisted mode: uploading a batch (≈ 10 min, every few days)

1. Go to **Schedule → Prepare batch** (or **Batches → Prepare batch**). A checklist page opens, and the files are in
   `workspace\batches\<channel>\<date>\`. Use **Copy folder path** on the Batches page to find them.
2. Open **YouTube Studio** (the button on the checklist). Check the channel avatar at the top right.
3. Go to **Create → Upload videos** and select **all the `.mp4` files** in the batch folder at once.
4. For each video (file names start with `01_`, `02_`… matching the checklist):
   - **Title:** click *Copy* on the checklist, then paste.
   - **Description:** Copy, then paste.
   - **Show more → Tags:** Copy, then paste.
   - **Audience:** "No, it's not made for kids". **Altered content:** No.
   - **Visibility → Schedule:** set the **date and time from the checklist (IST)** → **Schedule**.
   - Tick **Done** on the checklist.
5. Back in ClipForge: **Schedule → Sync with YouTube**. The clips turn purple ("Scheduled on YouTube").
   If one isn't matched (for example because you changed the title in Studio), open it in ClipForge and paste its YouTube link
   into **Link**.
6. Instagram (optional): each card on the checklist has an **Instagram kit** with the caption and hashtags. Post the same mp4 as a Reel.

---

## Part D — API mode: fully automatic uploads

### D1. Switch it on
1. **Settings → Publishing → Mode → API**, then **Save settings**.
2. In **Channels**, make sure your channel is **Confirmed**, **Active** (not paused) and logged in.
3. Go to **Schedule → Upload due now** to test right away. Watch the clip turn yellow ("Uploading"), then purple
   ("Scheduled on YouTube").
4. Check **YouTube Studio → Content**. The Short should be **Private** with **"Scheduled"** and the right date and time.
   YouTube makes it public at that time by itself.
   - If it shows "Locked private" in ClipForge instead, go to Settings → Publishing and turn on
     **"Unaudited uploads get locked"**, then use assisted mode until Google approves your API audit (see D4).

**How API mode uploads (since Phase 12.5):** a clip is uploaded **as soon as it is scheduled** (quota permitting) as
*private + publish time*. YouTube then publishes it at the slot by itself, even if your laptop is off. If the laptop
was off and a slot is less than 15 minutes away (or already passed) when ClipForge gets to it, Settings → Publishing →
*If a slot is <15 min away or passed* decides: **move it to the next free slot** (default), publish right away, or ask you.

### D2. Make it run without you (Windows Task Scheduler)
While `clipforge ui` is open, ClipForge checks every minute (and catches up right after it starts). To also run when the
app is closed:

```powershell
cd E:\youtube\clipforge
.venv\Scripts\activate
clipforge schedule-task install --dry-run   # shows what it will create
clipforge schedule-task install
```
This creates one hidden Windows task, **ClipForge Scheduler**, with three triggers:
- **every 30 minutes**,
- **when you log in**,
- **when the laptop wakes from sleep**.

It also runs a missed run as soon as possible ("start when available"), and runs on battery too.
Each run uploads clips that are waiting, publishes Instagram posts that are due, updates statuses (and links Studio
uploads), pulls stats every 12 h, then exits. Add `--daemon` to also start the always-on scheduler at login. You only
need it for Instagram when the web app isn't open.

- If it says *Access is denied*, open PowerShell **as Administrator** and run the same command.
- Check it: `clipforge schedule-task status`, or **Task Scheduler** → *Task Scheduler Library* → *ClipForge Scheduler*.
  The Dashboard's **Health** panel also shows the last and next run.
- Run it by hand any time:
  ```powershell
  clipforge scheduler --once
  ```
- Remove it:
  ```powershell
  clipforge schedule-task remove
  ```
- macOS/Linux: run `crontab -e` and add:
  ```
  @reboot /path/to/clipforge/.venv/bin/clipforge scheduler --once
  */30 * * * * /path/to/clipforge/.venv/bin/clipforge scheduler --once
  ```

**Why your laptop doesn't need to be on at 9 pm:** clips are uploaded right after you schedule them, with a fixed
publish time, and YouTube's own servers publish them. The laptop only needs to be on once after you schedule.
(Instagram is different: see Part G.)

### D3. Your weekly routine in API mode
1. Once or twice a week, run a new episode (Part B, steps 1–4). About 10 minutes of approving.
2. That's it. Uploads, publishing and stats run on their own.
3. Glance at **Schedule → Needs attention**. Every item says **why** and has one-click fixes (see the next section).

### D4. The YouTube API audit (optional)
Since 2 Nov 2026 the audit is **not needed to publish**. It only raises the quota (100 uploads/day by default) and gives
you an "audited" record. Everything for it is ready in **[AUDIT_KIT.md](AUDIT_KIT.md)**: the form answers, the public
privacy policy and terms pages, and the screenshots + screencast in `docs/audit/`.
1. GitHub repo → **Settings → Pages → Source: GitHub Actions** (one time), so `https://hamsidhi.github.io/clipforge/` goes live.
2. Fill the form with AUDIT_KIT.md (add your Cloud **project number**), attach the files from `docs/audit/`.
3. **Settings → Publishing → Apply for the YouTube API audit → Applied** (with the date). When Google answers, set
   Approved/Rejected. Approved also sets `api_audited: true`.
Approval isn't guaranteed for a personal tool. Assisted mode and API mode both keep working either way.

---

## Why "Needs you" appeared, and what to do now

**What happened (diagnosed on this laptop, 4 Nov 2026):**
- You were in **assisted mode**, where ClipForge never uploads by itself, and no Task Scheduler task existed.
- You uploaded 10 clips in YouTube Studio straight from the `clips` folder, **changed most titles** and picked your own times.
- The old Sync recognised uploads only by title, so it linked just 3 of them. The other clips stayed "queued" in
  ClipForge, and the moment ClipForge's (different) slot passed they turned into a reasonless "Needs you", even though
  YouTube had them correctly scheduled.
- Nothing was wrong on YouTube: all 10 were private + scheduled for 5 Nov, so nothing was public yet.

**What changed:**
- **Sync recognises uploads by their file name**, even with new titles. Then by title with `#shorts` and hashtags ignored.
  Before calling anything "missed", ClipForge runs Sync by itself. Your Studio time wins over ClipForge's slot.
- **Every problem says why and offers a fix.** Examples:

  | Reason shown | One-click fixes |
  |---|---|
  | Assisted mode: upload this batch in YouTube Studio | Open batch · Sync · Link · Reschedule |
  | Uploaded in Studio? Not linked yet | Sync · Link · Reschedule |
  | In a Studio batch, so API mode won't upload it | Upload with API instead · Sync · Link |
  | The slot passed before the upload / laptop was off | Reschedule to next free slot · Publish now |
  | Login expired | Reconnect |
  | Locked private by YouTube | What this means · Open in Studio |
  | Upload quota used up | Nothing: continues tomorrow by itself |
  | Upload failed: *error* | Retry · Reschedule |

- **Schedule → Needs attention** has bulk buttons: *Reschedule all missed to next free slots*, *Retry all failed*.
  The list view adds *Switch to assisted batch* and *Upload with API instead* for selected clips.
- **Dashboard → Health** shows the last and next scheduler run, login status per account, quota left, audit status and
  keep-awake state.

**What to do now:**
1. Restart the app: Ctrl+C in the old `clipforge ui` window, then `clipforge ui`.
2. **Schedule → Sync with YouTube.** (I already ran it once: all 9 of your Studio uploads are now linked and show
   "Scheduled on YouTube" at your Studio times.)
3. Choose a mode:
   - **API mode** (recommended now that the lock is gone): Settings → Publishing → Mode = API. Then run
     `clipforge schedule-task install` once. The remaining clips upload by themselves as private + scheduled.
   - **Stay assisted:** Schedule → *Prepare batch* → upload the files in Studio (from the batch folder or `clips/`;
     changing titles/times is fine) → *Sync*.
4. Optional check that your API project isn't locked: `clipforge publish-test UCT5PqvmHmx8HldPvyZwRRTQ` uploads one
   3-second private test video scheduled 30 days out and reports "Not locked" or "Locked". Delete it in Studio afterwards.

---

## Part E — Tracking growth and money (5 min a week)

1. **Dashboard → Sync stats** (it also runs automatically every 12 h):
   - **YPP progress bars:** subscribers and 90-day Shorts views against the targets, with "at the current pace you'll
     reach it on…". The target automatically switches to **20M Shorts views** for new applicants from **1 Feb 2027**.
   - **Best publish times heatmap:** after 20+ Shorts, ClipForge suggests better times. Click
     **Apply recommended slots** only if you agree; it never changes them by itself.
   - **What's working:** which topics, episodes, clip lengths and title styles get the most views. Use it to choose
     which episodes to clip next.
2. **Campaign earnings (Whop etc.):** the Dashboard lists published Shorts **not yet submitted**. For each one, click
   **Log submission**, paste your submission link and set the status to *Submitted*. Later, update it to
   *Approved / Paid* and fill in **approved views**. Earnings = approved views ÷ 1000 × rate × (1 − fee %).
3. **Export:** use the CSV buttons on Schedule, Dashboard and Tracker for spreadsheets.

---

## Part G — Instagram Reels (automatic, free)

ClipForge posts Reels through Instagram's official API. **Important difference to YouTube:** Instagram's API can't
schedule. ClipForge uploads each Reel ~20 minutes before its time and publishes it **at** the time, so **this laptop
must be on, online, and running ClipForge** at Instagram post times (keep the app open, or run
`clipforge schedule-task install --daemon`). If it was off for more than 90 minutes past a slot, that Reel is
**not** posted late. It shows *Missed slot* with **Reschedule** / **Publish now**.

### G1. Make your Instagram a professional account (2 min)
1. Instagram app → your profile → ☰ → **Settings and activity → Account type and tools → Switch to professional
   account** → *Creator* (or *Business*). It's free and you keep your followers.

### G2. Create / link a Facebook Page (5 min)
1. Create a Facebook Page if you don't have one (facebook.com → Pages → Create). Any name, e.g. your clip brand.
2. Link it: Instagram app → Settings → **Accounts Center** → *Accounts* → add your Facebook account. Then on Facebook:
   Page → Settings → **Linked accounts → Instagram → Connect**.

### G3. Create the free Meta app (10 min)
1. Go to https://developers.facebook.com/apps/creation/ (log in with the Facebook account that manages the Page).
2. Create app → type **Business** → name it "ClipForge" → create.
3. Add the products **Facebook Login for Business** and **Instagram** (API setup with Facebook login).
4. Facebook Login → **Settings → Valid OAuth Redirect URIs** → add `http://localhost:8000/api/instagram/oauth/callback`
   → Save.
5. **App settings → Basic:** copy the **App ID** and **App Secret**.
6. Leave the app in **Development** mode. You don't need App Review for your own accounts, because you are the app's admin.
7. ClipForge: **Settings → Instagram Reels** → paste App ID + App Secret → **Save Meta app**.
   - If Meta shows "Feature unavailable" / asks for a *configuration*: Facebook Login for Business → Configurations →
     create one with the permissions listed in the app. Paste its ID into **Login configuration ID**.

### G4. Add the account in ClipForge (on the laptop)
1. **Accounts → Instagram → Add Instagram account** → log in with Facebook → tick **every** permission and your Page.
2. Pick the Instagram account (username + followers are shown).
3. **Confirm** it (avatar + name check), so ClipForge never posts to the wrong account.
4. Open *Schedule & metadata settings* on the card:
   - post times (default 13:00, 19:00, 21:30 IST), posts per day;
   - **Hours after YouTube** (e.g. 3 = the Reel goes out ≥3 h after the same clip's Short);
   - call-to-action line, *Also show in the main feed*.

### G5. Test post
1. Gallery → approve a clip → **Add to schedule** → tick your Instagram account (and YouTube if you like) → Add.
2. Schedule → pick the account → open the clip:
   - check the caption: max 5 lowercase hashtags, ≤2,200 characters, counters shown;
   - pick a **cover frame** with the slider.
3. Click **Publish now** in the clip's fixes (or schedule it ~30 min ahead and wait). Within ~1–2 minutes it's
   *Published* with a link to the Reel.

### G6. Every week
- Add new clips to **both** platforms in one go (Add to schedule → tick both). Then Schedule → **All accounts** →
  **Schedule approved clips** (YouTube first, then Instagram respecting "hours after YouTube").
- The pink banner on Schedule lists the times the laptop must be on.
- Dashboard → pick the Instagram account: followers, views/reach/likes/shares/saves, top Reels, best times (after 20
  Reels), and *Reels posted but not submitted to a campaign* → **Log submission**.

### G7. No Meta app? Assisted Instagram
**Accounts → Instagram → Add without a Meta app (assisted)** → type your username. Then:
1. Schedule → pick it → **Prepare Instagram kit**: a folder with the files in order, plus a page with the caption, copy
   buttons and the time for each.
2. Post them in the Instagram app, or schedule them all for free in **Meta Business Suite** (Planner → Create Reel →
   Schedule). ClipForge never automates instagram.com: that's against Instagram's rules.
3. Paste each Reel's link into the clip (**Reel link**) to track it.

---

## Part H — Use it from your phone (anywhere, free, private)

ClipForge keeps running on your laptop at home (it has the CPU, Ollama and the files). Your Android phone becomes the
remote control: start jobs, approve clips, schedule, watch stats, get Telegram pings. Nothing is opened to the public
internet. **Tailscale** (free personal plan) links your phone and laptop privately.

### H1. Laptop: password + keep it running (10 min)
1. Set a password (the phone needs it; the laptop itself doesn't):
   ```powershell
   clipforge auth set-password
   ```
2. Start ClipForge at every login, restarting it if it crashes (web app + publishing scheduler):
   ```powershell
   clipforge service install
   ```
   (Then either log out and in, or start it now: `schtasks /Run /TN "ClipForge Service"`. Logs: `workspace\logs\`.)
3. Windows power settings (Settings → System → Power & battery):
   - **Plugged in: Never sleep** (screen can turn off; that's fine).
   - Control Panel → Power Options → *Choose what closing the lid does* → **When plugged in: Do nothing** (your choice:
     only if you leave the laptop closed and charging).
   - ClipForge also keeps the laptop awake by itself while a job runs or an Instagram post is due (Settings → Phone &
     remote → Keep the laptop awake).

### H2. Tailscale on both devices (10 min)
1. Laptop: install from https://tailscale.com/download → log in (Google account is fine). In the tray icon settings
   tick **Run unattended** / start at login.
2. Phone: install **Tailscale** from the Play Store → log in with the **same** account → switch it on.
3. Admin console https://login.tailscale.com/admin/dns: **MagicDNS → Enable** and **HTTPS Certificates → Enable**.
4. Laptop:
   ```powershell
   clipforge remote setup
   ```
   It checks everything above, publishes ClipForge privately at `https://<laptop>.<tailnet>.ts.net` and shows a QR code
   (also in Settings → Phone & remote).

### H3. Phone: open + install the app
1. Scan the QR (or type the address) in **Chrome** on the phone → log in → tick *Remember this device*.
2. Chrome ⋮ → **Install app** (or the *Install app* button at the top). ClipForge now opens like a normal app.
3. **Share links straight in:** in the YouTube app → Share → **ClipForge** → New job opens with the link filled in.
4. On the phone:
   - the bottom bar has Jobs · Gallery · Schedule · Stats · More;
   - in the Gallery, **swipe right = approve, swipe left = reject**, tap = play;
   - the trimmer has ±0.5 s buttons;
   - pull down to refresh.
   - Videos play as small previews (data saver; change it in Settings → Phone & remote).
5. Uploading a video file from the phone resumes by itself if the connection drops.
6. Connecting new YouTube/Instagram **accounts** is done on the laptop (Google/Meta send logins back to `localhost`).
   Everything else works from the phone.

### H4. Telegram bot (5 min, optional but handy)
1. Telegram → **@BotFather** → `/newbot` → pick a name → copy the token.
2. ClipForge → Settings → Phone & remote → Telegram: paste the token → **Save token** → **Pair** → send the shown
   `/pair 123456` to your new bot. Only your Telegram account can use it from now on.
3. You get:
   - **Job finished:** the top 3 clips as videos, with ✅ Approve / ❌ Reject / ✏️ Open buttons;
   - **Job failed / posted / upload failed / Instagram slot missed / account needs re-login / quota used up;**
   - **a daily summary at 22:00 IST.**
   Each one can be switched off.
4. Commands: send a YouTube link (or `/new <link>`), `/status`, `/queue`, `/approve 12 1,3,5`,
   `/schedule 12 both`, `/pause <account>`, `/resume <account>`, `/dashboard`, `/help`. Pause and schedule ask for confirmation.

### H5. A phone-only day
1. Morning: in the YouTube app, **Share → ClipForge** on a new episode → *Create clips*.
2. ~1–2 h later Telegram pings "Job finished" with the top clips → tap ✅ on the good ones (or open the Gallery and swipe).
3. Send `/schedule <job> both` → ✅ Yes. YouTube Shorts upload as private + scheduled. Reels publish at their slots
   while the laptop stays on.
4. Evening: `/dashboard` or the Stats tab; the 22:00 summary arrives by itself.

### H6. Optional alternative: Cloudflare Tunnel (public address)
Only if you can't use Tailscale on a device. A Cloudflare Tunnel + **Cloudflare Access** (email one-time code) can put
ClipForge at e.g. `clipforge.yourdomain.com`.
- **Warning:** the address is public on the internet; ClipForge's password and Cloudflare Access are then all that
  protect your accounts.
- Keep `auth.trust_localhost: true` only if nothing else on the laptop proxies to it. Never run a tunnel without
  Access + a strong password.
- ClipForge doesn't set this up for you on purpose.

### Phone troubleshooting
| Problem | Fix |
|---|---|
| Phone says "Can't reach your laptop" | Laptop on and awake? `clipforge service status`; Tailscale switched on on **both** (tray icon / phone app)? |
| Laptop fell asleep | Power settings in H1.3; Settings → Phone & remote → Keep awake = *Always* if needed |
| Tailscale logged out | Open Tailscale on the laptop, log in, keep *Run unattended* on; `clipforge remote status` |
| Certificate / "not secure" warning | Enable **HTTPS Certificates** in the Tailscale admin DNS page, run `clipforge remote setup` again, use the `https://…ts.net` address |
| "Set a password on the laptop first" | `clipforge auth set-password` on the laptop |
| "Too many wrong passwords" | Wait 15 min (or revoke devices in Settings → Security) |
| Telegram bot silent | Settings → Phone & remote → Telegram: token saved? *Paired*? *Bot enabled* on? ClipForge running? Send `/help` |
| Share menu doesn't show ClipForge | Install the app first (H3.2); Android lists installed web apps as share targets |

---

## Part F — Troubleshooting

| You see | Do this |
|---|---|
| Errors in the terminal after an update | Ctrl+C, then `clipforge ui` again (a running server keeps old code) |
| "config.yaml has invalid values" when starting | The message names the bad line; fix or delete it (defaults are used) |
| Channel shows **needs re-login** | Channels → **Reconnect** (and set the consent screen to *In production*, see A2.4) |
| "Not granted: youtube.upload" on a channel | Reconnect and tick **every** box on Google's permission screen |
| Clip status **Needs you** / **Missed slot** | Schedule → *Needs attention*: the reason + buttons say what to do |
| Clip status **Locked private** | Publish it manually in Studio; Settings → turn on "Unaudited uploads get locked" |
| Health: "Last scheduler run" is hours ago | Open the app, or `clipforge schedule-task install` (30 min / logon / wake) |
| "Upload quota used up for today" | Nothing: it continues automatically the next day (quota resets at midnight US Pacific ≈ 1:30 pm IST) |
| Sync didn't match an upload | The Sync dialog suggests clips for each unmatched video: click **Link to …**, or paste the link in the clip |
| Ollama errors / very slow | Start the Ollama app; or add a free Gemini key in Settings and choose Gemini as the provider |
| Task Scheduler "Access denied" | Run `clipforge schedule-task install` in an Administrator PowerShell |
| Instagram: "Add Instagram account" greyed out | Settings → Instagram Reels → paste the Meta App ID + Secret |
| Instagram: "None of your Pages has a linked Instagram professional account" | Do G1 + G2, and tick the Page on Meta's permission screen |
| Instagram: "URL blocked" / redirect error from Facebook | Add `http://localhost:8000/api/instagram/oauth/callback` to *Valid OAuth Redirect URIs* (G3.4) and open ClipForge at `localhost:8000` |
| Instagram clip **Missed slot** | The laptop/app was off at post time. Click Reschedule (or Publish now); keep the app open or use `schedule-task install --daemon` |
| Instagram "couldn't process the video" | Re-render the clip; check the error (Reels: 3 s–15 min, ≤300 MB, H.264/AAC) and Retry |

Useful commands:
```powershell
clipforge doctor               # health check
clipforge channels list        # connected channels + queue counts
clipforge queue list           # every planned clip with its status and time
clipforge scheduler --once     # do all due publishing work now
clipforge sync --channel UC…   # link Studio uploads + pull stats
```

## Safety rules ClipForge always follows
- Nothing goes public without a time slot you scheduled. Uploads are *private + scheduled* unless you pick another
  visibility for a clip (Public at the slot, Unlisted, Private).
- An unconfirmed or **paused** channel never receives uploads.
- The same clip can't be queued twice on one channel. Clips already on YouTube or in a Studio batch are never uploaded again.
- It never goes over the daily upload quota, and never automates the YouTube website itself (that's against YouTube's rules).
