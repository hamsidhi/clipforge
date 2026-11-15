# Live gaming mode: handoff (2026-11-15)

ClipForge can now make highlight clips **while you stream** on this laptop, and post them while you're still live.
Code: `clipforge/live/`. Decisions: DECISIONS.md → "Live gaming". Tests: `tests/test_live.py` (18).

## 1. What works now
- **Footage from OBS:** the Replay Buffer is saved the moment a highlight happens. There is no second recording and no
  extra encode. Without OBS, ClipForge reads your stream link (YouTube/Twitch) at ≤720p.
- **Highlights are detected by:**
  - your marks: `/clip [note]` on Telegram, **CLIP NOW** on the Live page, the `Ctrl+Alt+C` hotkey, and `!clip` in
    chat (only from you, your mods, or names you allow);
  - loud reactions on your mic and big bursts in game audio;
  - chat spikes (Twitch is free; YouTube chat is optional because it costs API quota);
  - game events read from small HUD screenshots: BGMI/PUBG kills and Chicken Dinner, Valorant kills/ACE/clutch/round
    win, CS2 kills/round win, Free Fire kills/Booyah, GTA "WASTED"/mission passed/busted, Minecraft death.
- **Clips:**
  - vertical 1080×1920, with your **facecam on top** (position read from OBS) and **gameplay below**;
  - "gameplay only" when there's no facecam;
  - captions and a hook title;
  - titles and hashtags made from per-game templates (no AI while you play).
- **Telegram:** each clip arrives with **why** it was clipped and four buttons: ✅ Post now · 🕒 Queue · ✏️ Title ·
  ❌ Discard. Commands: `/live`, `/live pause|resume`, `/live auto on|off`, `/livestats`.
- **Posting:**
  - only to accounts you mark as **Gaming**, never to the podcast accounts;
  - while you're live, the caption says "🔴 LIVE now on YouTube/Twitch → link";
  - after the stream, posts not yet uploaded switch to "Full stream: …";
  - at most 1 post per platform every 25 minutes while live; the rest wait in the queue.
- **When the stream ends:** you get a recap and a 🎞 **Make long highlight video** button (a 5–10 min best-of).
- **Safety:**
  - ClipForge never starts or stops your stream and never touches OBS encoder/bitrate/scenes (enforced in code);
  - it runs at low priority on 2 CPU cores;
  - it pauses itself above 75% CPU and stops completely if OBS starts dropping frames.

**Time from moment to clip on Telegram** (measured in simulation, captions on): render + captions take **7–11 s**
after the moment's 10 s "after" window, so a clip arrives about **20–40 s after the moment**, plus upload time to
Telegram. If the CPU is busy, the work waits until it's free.

## 2. Measured cost (this laptop)
| 40 s clip, 6 busy "game" processes | Render time | ffmpeg CPU | Game slowdown |
|---|---|---|---|
| **AMD AMF + iGPU decode** (default) | **5.6 s** (7.2× real time) | ~1.6 cores for 5.6 s | ~10 % for those 5.6 s only |
| x264 ultrafast (fallback) | 26.6 s | 2 cores for 26.6 s | ~15 % for 26.6 s |

- AMD AMF works on this laptop (h264_amf probed OK). Decoding on the iGPU cut ffmpeg's CPU work by 2.4×.
- The game benchmark used CPU burners: there's no real game on this PC yet. The game also uses the 780M. AMF and the
  decoder run on the iGPU's separate video engine (VCN), not on the part that draws the game, but **check FPS in a real
  15-min test stream**.
- Detectors cost almost nothing: volume meters come for free from OBS, and one 640 px screenshot every 1.5 s.

## 3. Your setup steps (once)
1. **OBS Studio 30+** → Tools → **WebSocket Server Settings**:
   - tick Enable, port 4455, Generate/Set a password;
   - add the password to `D:\clipforge\.env` as `OBS_WEBSOCKET_PASSWORD=yourpassword`.
2. OBS → Settings → Output → **Replay Buffer**:
   - Enable;
   - Maximum Replay Time **120 s**;
   - encoder **"(Use stream encoder)"**, so it costs nothing extra;
   - ClipForge starts the buffer with your stream.
3. Rename your webcam source so its name contains **facecam** or **webcam**.
4. Create a **gaming YouTube channel** and a **gaming Instagram** account, then connect them:
   - Accounts page → Add (for YouTube from another device, use the login-link trick from before);
   - then **Live page → Gaming accounts** → switch them on.
5. **Live page → Settings:**
   - Live mode **ON**;
   - your stream links (`https://www.youtube.com/@you`, `https://www.twitch.tv/you`);
   - your Twitch name for chat;
   - **Save**. The app starts the live agent within 15 s.
6. **Test without streaming:**
   ```
   clipforge live sample
   clipforge live simulate workspace\samples\bgmi_sample.mp4 --facecam sample
   ```
   Or use a recording of yours (add `--facecam x,y,w,h` and `--game bgmi_pubg`).
7. **Real 15-min test stream** (set it to private/unlisted):
   - play;
   - type `/clip` once;
   - check `/live` for CPU and clips;
   - watch the stream for dropped frames (OBS → Stats).

## 4. Known limits
- **Game HUD boxes are first guesses.**
  - They need tuning on your real footage: run `clipforge live simulate <your recording>` and tell me what it missed.
  - Installing OCR (`pip install rapidocr-onnxruntime`) makes "WINNER / ACE / WASTED" detection more exact.
  - BGMI is a phone game; on an emulator (BlueStacks/GameLoop) the HUD layout may differ.
- **YouTube:**
  - Uploads made through an unaudited API project may be **locked private**. Live clips then show as such; use
    assisted/Studio posting until the audit is approved.
  - Reading YouTube live chat costs quota (budget: 1,500 units per stream).
- **GTA 6:** a placeholder pack (death screen only), to be updated after launch.
- **Live clips aren't re-renderable from the Gallery** (there's no long source file). Edit the title before posting.
- **Hotkey:** `Ctrl+Alt+C` works only while the app runs in your Windows session (it does, via the logon task).
