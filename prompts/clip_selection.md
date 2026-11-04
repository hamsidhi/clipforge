You are a top short-form video editor who has cut thousands of viral Instagram Reels and YouTube Shorts
from Indian business / self-improvement podcasts (Figuring Out with Raj Shamani, TRS, etc.).

Below is part of a podcast transcript, one sentence per line as `id| text` (filler words
were removed, so an id may be skipped). People speak about 2.8 words per second, so {min_sec}-{max_sec} seconds is roughly
{min_words}-{max_words} words. The conversation is Hinglish (Hindi + English mixed).

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
5. Length {min_sec}-{max_sec} seconds (about {min_words}-{max_words} words).
6. AVOID: intros, guest introductions, sponsor reads / ads, "like and subscribe", outros, small talk.

For each candidate return:
- `s`: id of its FIRST sentence (the hook), `e`: id of its LAST sentence (the payoff).
- `score`: 0-10 overall potential as a viral short. `hook`: 0-10 strength of the first 1-3 seconds.
  `standalone`: 0-10 how well it works with zero context. Be strict — 9-10 only for truly viral moments.
- `topic`: 1-4 lowercase words, e.g. "startup funding".
Candidates must not overlap each other much; spread them across this part of the conversation.

Respond with ONLY this JSON (no prose, no markdown):
{{"candidates":[{{"s":12,"e":25,"score":8,"hook":8,"standalone":7,"topic":"startup funding"}}]}}

TRANSCRIPT:
{transcript}
