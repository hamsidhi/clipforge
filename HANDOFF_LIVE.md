# Live clipping: handoff (2026-11-15)

Paste **any streamer's live link** (Twitch, Kick, YouTube live, or anything yt-dlp can read). ClipForge watches the
stream and drafts vertical highlight clips **while it's live**. You don't stream yourself, and there's no OBS.
Code: `clipforge/live/`. Decisions: DECISIONS.md → "Live clipping". Tests: `tests/test_live.py` (20).

## 1. How to use it
- **Telegram:**
  - paste a Twitch or Kick channel link, then tap **🔴 Clip this live stream**;
  - for a YouTube link that is live right now, you also get a "Clip it live instead" button;
  - or send `/live https://www.twitch.tv/name`.
- **App → Live:** paste the link and press **Start clipping**.
- **Up to 3 streams at once** (`live.max_streams`).
- **Auto-start:** App → Live → Settings → "Auto-start when these channels go live", one link per line.
- **Each clip arrives on Telegram as a draft:**
  - shows **why** it was clipped and the streamer's name;
  - buttons: ✅ Post now · 🕒 Queue · ✏️ Title · ❌ Discard.
- **Commands:**
  - `/live`: the streams being watched, with 🎬 Clip now · ⏹ Stop;
  - `/clip [note]`: clip what just happened;
  - `/live stop`, `/live pause|resume`, `/live auto on|off`, `/livestats`.
- **When a stream ends:** you get a recap and 🎞 **Make long highlight video**.

## 2. What makes a clip
- **Loud moments:** sudden spikes in the stream's audio (screams, laughs, explosions) compared to that stream's normal level.
- **Chat spikes:** Twitch chat is free and on. YouTube chat uses API quota and stops before eating your upload quota.
  Kick chat is **off**: Kick changed its chat key (see limits).
- **Game events** read from the screen:
  - BGMI/PUBG: kills, Chicken Dinner;
  - Valorant: kills, ACE, clutch, round win;
  - CS2: kills, round win;
  - Free Fire: kills, Booyah;
  - GTA: WASTED, mission passed, busted;
  - Minecraft: death.
  - The game is detected from the stream title.
- **Your marks:** `/clip` on Telegram or CLIP NOW in the app.
- **Each stream is judged against itself:** a moment must beat that stream's own normal level (a loud streamer isn't
  clipped every minute). Limits: 90 s cooldown, max 6 automatic clips per hour per stream.

## 3. What the clip looks like
- 1080×1920.
- **Streamer's facecam on top** (found automatically: the face that stays in one place), **gameplay below**.
- IRL / just-chatting streams: a full-height crop that follows the face.
- No facecam: gameplay only.
- Captions burned in, plus a hook title.
- **Titles are about the streamer**, e.g. "xQc lost it here 💀" or "Mortal got the Chicken Dinner 😭🔥". Never "I/my".
- **Captions credit and link the streamer:**
  - while live: "🔴 LIVE now: xQc on Twitch → link" plus "🎥 Credit: xQc";
  - after the stream: "Full stream: …".

## 4. Tested for real (this laptop, 15 Nov)
- **Watched xQc live on Twitch and on Kick at the same time:**
  - CPU while watching: **2–10 %**;
  - the facecam was found on both;
  - Twitch chat was read (~15–25 msgs / 10 s).
- **A `/clip` mark → finished clip in ~80 s**, of which ~10 s is rendering on the iGPU (AMD AMF):
  - the clip had xQc's cam on top, the game below and captions;
  - the rest of that time is waiting for the stream's 10-s pieces to finish, plus the 10 s after the moment.
- **Automatic clips** (simulation, BGMI test stream): 3/3 highlights caught (a kill streak, Chicken Dinner and a mark),
  and an ordinary loud moment ignored.
- **xQc's real stream:** no automatic clip in 13 minutes of a calm Zelda session.
  - The bar is now a little lower (3.5), so one very strong reaction or chat spike is enough.
  - Watch the first real sessions and tell me if it clips too much or too little.

## 5. Your setup steps
1. **Accounts for live clips:** decide which account(s) these clips post to. App → Live → **Accounts for live clips**
   → switch them on. Until you do, clips stay drafts.
2. **Paste a live link and watch the first clips arrive on Telegram.** Nothing is posted until you tap Post now or
   Queue, or turn on Auto-post.
3. **Optional:** add your favourite streamers to "Auto-start when these channels go live".

## 6. Known limits
- **Copyright:**
  - clips of other creators can get Content ID claims or strikes;
  - captions credit and link the streamer, but **ask streamers for permission** where you can;
  - don't post a channel's clips if it forbids it.
- **Kick chat is off:**
  - Kick changed its public chat key and the old one is refused;
  - Kick streams still clip from audio, game events and `/clip`;
  - if you find the new key, set `live.chat.kick_pusher_key` and `kick: true`.
- **YouTube chat** costs API quota: up to 1,500 units per stream, and it stops when fewer than 4,000 units are left
  for the day, so uploads always have quota.
- **Game-event screen areas are first guesses:**
  - run `clipforge live simulate <recording>` on a saved gameplay video to see what fires;
  - installing OCR (`pip install rapidocr-onnxruntime`) makes WINNER / ACE / WASTED detection more exact.
- **YouTube uploads** from an unaudited API project may be locked private (same as before).
- **Live clips can't be re-rendered from the Gallery** (only the stream pieces around each moment are kept, 5 min).
  Edit the title before posting.
- **The laptop must be on.** The live agent restarts with the app and resumes the streams it was watching.
