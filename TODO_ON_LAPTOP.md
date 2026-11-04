# Things only you can test (need your accounts, devices or hardware)

Everything else was run on this laptop during the build. These steps need your accounts, your phone,
another network device, or hardware that isn't here.

## 1. YouTube publishing (Phase 12): needs your Google account
Everything is unit-tested with a fake YouTube client. The real OAuth flow, uploads and Analytics calls need you:
1. **Google Cloud setup:** follow README → 5.1, then upload `client_secret.json` in Settings → Publishing.
2. **Channels → Add channel:** sign in, pick the channel, then **Confirm** it (check the avatar and name).
3. **Test assisted mode first:**
   - Gallery → approve 2 clips → *Add approved to schedule* → Schedule → *Schedule approved clips* → *Prepare batch*.
   - Upload the two files in Studio with *Schedule* visibility.
   - Back in ClipForge: *Sync with YouTube*. Both clips should show "Scheduled on YouTube".
4. **Optional, API mode:**
   - Settings → Publishing → Mode = API → Schedule → *Upload due now*.
   - Check YouTube Studio: the clip is **Private, Scheduled** at the slot time.
   - If it shows "Locked private", turn on *Unaudited uploads get locked*.
5. **Dashboard:** *Sync stats* fills subscribers, Shorts views (needs the YouTube Analytics API enabled in your project) and YPP bars.
6. **Unattended:** run `clipforge schedule-task install` once (one hidden task: every 30 min + at logon + on wake).
   - If Windows refuses it, run it from an *Administrator* terminal. `clipforge schedule-task status` shows last/next run.
7. **Phase 12.5 (Needs-you fix):**
   - Restart `clipforge ui` (the old server process keeps old code).
   - Optional lock check: `clipforge publish-test UCT5PqvmHmx8HldPvyZwRRTQ`. It uploads ONE 3-second *private* test video,
     publishAt +30 days, and prints "Not locked" / "Locked". **Delete it in Studio afterwards.** I didn't run it for you:
     a publish time of now + 2 h would make it public, and ClipForge's login can't cancel a schedule or delete videos.
8. **Audit kit:** GitHub → Settings → Pages → Source **GitHub Actions**, then Actions → *Publish docs-site* → Run.
   Check https://hamsidhi.github.io/clipforge/privacy.html. Then follow AUDIT_KIT.md.

## 1b. Instagram Reels (Phase 13): needs your Instagram + Facebook
Everything is tested with a fake Graph API. Real setup (AUTOMATION_GUIDE.md → Part G):
1. Instagram → switch to a **Professional** account; link a **Facebook Page**.
2. Create the free **Meta app** (Business type), add *Facebook Login for Business* + *Instagram*, and add the redirect URI
   `http://localhost:8000/api/instagram/oauth/callback`. Copy the App ID + Secret into Settings → Instagram Reels.
3. On the laptop: Accounts → Instagram → **Add Instagram account** → pick it → **Confirm**.
4. Test: approve one clip → Add to schedule (Instagram) → Schedule → open it → **Publish now**. Check that it appears
   on your profile within ~2 minutes and the clip shows the Reel link.
5. If Meta's login says you need a *configuration*, create one (Facebook Login for Business → Configurations) and paste
   its ID in Settings. Report what Meta asked for, so the guide can be updated.

## 1c. Phone remote control (Phase 14): needs Tailscale on both devices + your Telegram
Tested with fakes; the real setup is AUTOMATION_GUIDE.md → Part H:
1. `clipforge auth set-password`, then `clipforge service install`, then log out and back in. Check that
   http://localhost:8000 opens without you starting anything.
2. Install Tailscale on the laptop + phone (same account). In the admin console: MagicDNS + HTTPS Certificates on.
   Then run `clipforge remote setup`. **Tailscale isn't installed on this laptop yet**, so this couldn't be tested here.
3. Phone: open the printed `https://…ts.net` address → log in → Install app. Try: Share a YouTube link from the YouTube
   app → ClipForge; swipe in the Gallery; a 1-minute file upload over mobile data.
4. Telegram: @BotFather → token → Settings → Phone & remote → Save → Pair → `/pair <code>`; then `/help` and `/status`.
5. Power: plugged in → never sleep; lid close → do nothing (if you want it closed). Tailscale → Run unattended.
6. If anything in the remote setup differs from the guide (Tailscale CLI output, Chrome install prompt), note it.

## 1d. Telegram full remote control (Phase 15a): needs your phone + Telegram (~15 min)

Your old pairing from Phase 14 is migrated automatically (you become the owner). After pulling, restart `clipforge ui`.
1. **(1 min)** In the bot send `/help` — you should see the full command list; `/menu` shows quick-action buttons.
2. **(2 min)** Set a PIN: `clipforge telegram set-pin` (or Settings → Phone & remote → PIN). Then `/resume <account>` on a
   paused account → Yes → it asks the PIN and deletes your message.
3. **(5 min)** Paste a short YouTube link → change "Clips" to 10 → **Start** → watch the progress message update →
   clip cards arrive → **▶ Preview**, **✅ Approve**, **✏️ Edit** → **+0.3** (end) → **🔁 Re-render** → **↩ Undo**.
4. **(3 min)** **📅 Schedule** on an approved card → Both → **Pick a time** → `tomorrow 9pm` → **Schedule**. Check
   `/queue`, then in YouTube Studio that the upload is **private + scheduled** (never public before the slot).
5. **(2 min)** Optional: `clipforge telegram pair --role viewer` → pair a second Telegram account → it can preview and
   see stats but every edit button is missing / refused.
6. **(optional, 15 min)** Local Bot API server for 2 GB files: docs/TELEGRAM.md → "Optional: local Bot API server".
7. **(optional)** Mini App: set `telegram.mini_app: true`, send `/app` → **Open app** inside Telegram (phone on
   Tailscale). Not tested on a real phone yet — tell me if the webview can't open the `ts.net` address.

## 2. Phone over Wi-Fi
```powershell
clipforge ui --host 0.0.0.0
```
- The first time, Windows Defender Firewall asks whether to allow Python on **Private networks**. Click **Allow**.
- Open the printed `http://192.168.x.x:8000` address on your phone. It must be on the same Wi-Fi.
- If it doesn't load, set the network to Private: Settings → Network → your Wi-Fi → *Private network*.

## 3. Gemini / Groq free tiers: need your free API keys
Both providers are implemented (REST, rate-limit back-off), but neither was called without a key.
Add a key in **Settings** (or `.env`), then run a short test:
```powershell
clipforge run "<url>" --section 10:00-20:00 --provider gemini --clips 5
```

## 4. Colab notebook
`notebooks/clipforge_colab.ipynb` clones this repo, installs the pinned requirements and runs on a T4.
Open it in Colab (File → Upload notebook, or open it from GitHub), choose *Runtime → T4 GPU*, and run the cells in order.
If a pinned package fails to install on Colab's Python, run `!pip install -U <that package>` and run the cell again.

## 5. NVIDIA GPU path
This laptop has an AMD iGPU, so the CUDA (Whisper `large-v3` float16) and NVENC code paths were never run here.
On an NVIDIA machine, `clipforge doctor` should show both green. If CUDA Whisper fails to load, ClipForge
falls back to the CPU automatically and logs a warning.
