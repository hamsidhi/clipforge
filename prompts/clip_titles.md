You write the on-screen copy for vertical shorts (YouTube Shorts / Instagram Reels) cut from a Hinglish podcast.

Podcast: {podcast_title}
Channel: {channel}

Below are {n} clips with EVERYTHING that is said in each. For each clip write:
- `hook`: a short, punchy on-screen hook title shown in the first seconds (max 8 words), e.g. "Ye galti mat karna!"
- `title`: a curiosity-driven title, max 60 characters, true to what is said.
- `why`: one sentence on why this clip will perform.
Be SPECIFIC: build the hook and title on the concrete claim, number, name or story in the clip
("22 saal mein 5 crore loss", "Sam Altman's biggest fear"), not on vague words like "insights", "journey",
"power" or "secrets". Make the viewer need the answer. Write each clip in the language its speaker uses
(given in the CLIP line): an English clip gets an English hook and title, never Hindi or Hinglish words; a
Hindi clip gets Hinglish in Latin script. No hashtags, no emojis.

{clips}

Respond with ONLY this JSON (no prose, no markdown), one entry per clip id:
{{"clips":[{{"id":1,"hook":"...","title":"...","why":"..."}}]}}
