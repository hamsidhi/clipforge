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
6. **Unattended:** run `clipforge schedule-task install` once.
   - If Windows refuses the "at logon" task, run it from an *Administrator* terminal.
   - Otherwise keep only the "every 2 h" task.

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
