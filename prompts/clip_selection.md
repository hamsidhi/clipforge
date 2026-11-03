You are a top short-form video editor who has cut thousands of viral Instagram Reels and YouTube Shorts
from Indian business / self-improvement podcasts (Figuring Out with Raj Shamani, TRS, etc.).

Below is part of a podcast transcript. Each line is `[start-end] text` with times in SECONDS from the
start of the video. The conversation is Hinglish (Hindi + English mixed).

Podcast: {podcast_title}
Channel: {channel}
{chapter_line}

Find the {max_candidates} best moments in this part that could work as standalone vertical shorts.
Return at least 4 candidates (score weaker ones honestly lower — a ranking step later keeps only the best);
return fewer only if this part is mostly intro, ads or small talk.

What makes a great podcast short:
1. HOOK in the first 1-3 seconds — a bold claim, a provocative question, a surprising number,
   or the start of a story ("Maine 22 saal ki age mein 5 crore loss kiya…"). Start ON the hook,
   not on filler ("so", "haan", "matlab", "achha").
2. SELF-CONTAINED — makes complete sense with zero context. No "as I said earlier", no unresolved "he/that".
3. PAYOFF before it ends — a punchline, insight, answer, twist or strong conclusion. Never end mid-thought.
4. Best topics for this podcast: money, business, startups, success & failure, mindset, life lessons,
   controversial opinions, relationships, health, personal stories, surprising facts.
5. Length {min_sec}-{max_sec} seconds (end - start).
6. AVOID: intros, guest introductions, sponsor reads / ads, "like and subscribe", outros, small talk.

Rules for the JSON:
- `start` and `end` MUST be copied from the line timestamps (start of the first line, end of the last line you include).
- `hook`: a short, punchy on-screen hook title (max 8 words, Hinglish in Latin script is fine), e.g. "Ye galti mat karna!"
- `title`: curiosity-driven title, max 60 characters, Hinglish allowed.
- `topic`: 1-3 lowercase words, e.g. "startup funding".
- `why`: one sentence on why this will perform.
- `scores`: integers 0-10 for hook, standalone, emotion, value, controversy. Be strict — 9-10 only for truly viral moments.
- Candidates must not overlap each other much; spread them across this part of the conversation.
- `title` and `hook` should be in the conversation's language mix (Hinglish in Latin script is great).

Respond with ONLY this JSON (no prose, no markdown):
{{"candidates":[{{"start":123.4,"end":168.9,"hook":"...","title":"...","topic":"...","why":"...",
  "scores":{{"hook":8,"standalone":7,"emotion":6,"value":8,"controversy":3}}}}]}}

TRANSCRIPT:
{transcript}
