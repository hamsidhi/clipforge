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
