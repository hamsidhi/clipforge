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
| Instagram Reels | 👤 manual: Instagram's free API needs a public server, so ClipForge gives you copy-ready captions |

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

### D2. Make it run without you (Windows Task Scheduler)
While `clipforge ui` is open, ClipForge checks every 10 minutes. To also run when the app is closed:

```powershell
cd E:\youtube\clipforge
.venv\Scripts\activate
clipforge schedule-task install --dry-run   # shows what it will create
clipforge schedule-task install
```
This creates two Windows tasks:
- **ClipForge Scheduler (logon):** runs once when you log in.
- **ClipForge Scheduler (every 2h):** runs every 2 hours.

Each run uploads any clips due in the next 5 days, updates statuses, and pulls stats every 12 h, then exits.

- If it says *Access is denied* for the logon task, open PowerShell **as Administrator** and run the same command, or
  just keep the every-2-hours task.
- Check it: open **Task Scheduler** from the Start menu → *Task Scheduler Library* → you should see both tasks.
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
  0 */2 * * * /path/to/clipforge/.venv/bin/clipforge scheduler --once
  ```

**Why your laptop doesn't need to be on at 9 pm:** clips are uploaded up to 5 days ahead with a fixed publish time,
and YouTube's own servers publish them. The laptop only needs to be on now and then, at least once every few days,
to upload the next batch.

### D3. Your weekly routine in API mode
1. Once or twice a week, run a new episode (Part B, steps 1–4). About 10 minutes of approving.
2. That's it. Uploads, publishing and stats run on their own.
3. Glance at **Schedule** for any **orange/amber** items ("Needs you"). They're explained in the clip's dialog (for example
   "the slot passed while the laptop was off — pick a new time").

### D4. If you need more than 100 uploads a day
At 3 Shorts/day per channel you'd need 30+ channels to hit this limit, so you'll probably never need it.
If you do: request more quota via the [YouTube API audit & quota form](https://support.google.com/youtube/contact/yt_api_form).
Once approved, tick **"My API project passed Google's audit"** in Settings.

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
| Clip status **Needs you** | Open it; the message says what to do (new time, sync from Studio, fix the title) |
| Clip status **Locked private** | Publish it manually in Studio; Settings → turn on "Unaudited uploads get locked" |
| "Upload quota used up for today" | Nothing: it continues automatically the next day (quota resets at midnight US Pacific ≈ 1:30 pm IST) |
| Sync didn't match an upload | Open the clip → paste its YouTube link → **Link** |
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
- Nothing goes public without a time slot you scheduled. Uploads are always *private + scheduled*.
- An unconfirmed or **paused** channel never receives uploads.
- The same clip can't be queued twice on one channel. Clips already on YouTube or in a Studio batch are never uploaded again.
- It never goes over the daily upload quota, and never automates the YouTube website itself (that's against YouTube's rules).
