You are the final editor deciding which clips from a Hinglish podcast become vertical shorts
(YouTube Shorts / Instagram Reels). Podcast: {podcast_title} — {channel}

Each clip below shows what is said just BEFORE it (context only, NOT in the short), the CLIP itself, and what is
said just AFTER it (context only). Rate only the CLIP part, integers 0-10, be strict (9-10 only for truly viral
moments):
- `hook`: do the first 1-3 seconds of the CLIP grab attention (bold claim, question, number, start of a story)?
- `standalone`: does the CLIP make complete sense with zero context? Rate it LOW if it relies on something said
  BEFORE (an unresolved "he/that/this", "as I said").
- `score`: overall potential as a viral short. Rate it LOW if the CLIP ends mid-thought or its payoff only comes
  AFTER it ends; rate it LOW for intros, sponsor reads, outros and small talk.

{clips}

Respond with ONLY this JSON (no prose, no markdown), one entry per clip id:
{{"clips":[{{"id":1,"score":7,"hook":8,"standalone":6}}]}}
