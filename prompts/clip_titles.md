You write the on-screen copy for vertical shorts (YouTube Shorts / Instagram Reels) cut from a Hinglish podcast.

Podcast: {podcast_title}
Channel: {channel}

Below are {n} clips with EVERYTHING that is said in each. For each clip write:
- `hook`: a short, punchy on-screen hook title shown in the first seconds (max 8 words), e.g. "Ye galti mat karna!"
- `title`: a curiosity-driven title, max 60 characters, true to what is said.
- `why`: one sentence on why this clip will perform.
Use the conversation's language mix (Hinglish in Latin script is great). No hashtags, no emojis.

{clips}

Respond with ONLY this JSON (no prose, no markdown), one entry per clip id:
{{"clips":[{{"id":1,"hook":"...","title":"...","why":"..."}}]}}
