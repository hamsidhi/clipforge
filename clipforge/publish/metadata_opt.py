"""YouTube metadata per clip per channel: 3 title variants (scored), description, tags, hashtags.

The LLM writes candidates; a deterministic rubric picks the title so the choice is explainable and testable.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from clipforge.llm.base import LLMError, LLMProvider, strict_schema
from clipforge.models import ClipCandidate, VideoInfo, Word
from clipforge.utils.text import normalize_hashtags, truncate
from clipforge.utils.timecode import words_in_range

TITLE_MAX = 70
TAGS_MAX_CHARS = 500
CLICKBAIT = {"shocking", "you won't believe", "gone wrong", "100% real", "not clickbait", "must watch", "omg"}
CURIOSITY = {"?", "why", "how", "secret", "truth", "kyun", "kaise", "kya", "sach", "galti", "mistake", "never", "real"}

PROMPT = """You write YouTube Shorts metadata for a clip cut from an Indian podcast.

Podcast episode: {episode}
Clip's working title: {title}
Clip hook: {hook}
Topic: {topic}
Language for titles: {language}
What is said in the clip:
\"\"\"{text}\"\"\"

Write:
- "titles": exactly 3 different titles, each max 70 characters. Hook-first: the most interesting idea in the
  first 3-4 words, main keyword early, curiosity WITHOUT lying or exaggerating, no ALL CAPS words, no emojis,
  {language_rule}
- "summary": 1-2 short lines that tease the clip's payoff (no hashtags).
- "tags": 8-12 search tags (lowercase, no #), mixing the topic, the guest's field and generic podcast tags.
- "hashtags": 3 lowercase hashtags (with #) about the topic, e.g. "#business".

Return JSON only: {{"titles": ["...","...","..."], "summary": "...", "tags": ["..."], "hashtags": ["#..."]}}"""


class MetaReply(BaseModel):
    titles: list[str]
    summary: str
    tags: list[str]
    hashtags: list[str]


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9ऀ-ॿ]+", " ", t.lower()).strip()


def is_duplicate(title: str, existing: list[str]) -> bool:
    """Same title after ignoring case, punctuation and spacing ("Why X?" == "why x!")."""
    n = _norm(title)
    return bool(n) and any(n == _norm(e) for e in existing if e)


def score_title(title: str, topic: str = "") -> tuple[float, list[str]]:
    """0–10 rubric. Returns (score, reasons) so the UI can explain the pick."""
    t = title.strip()
    reasons: list[str] = []
    if not t:
        return 0.0, ["empty"]
    s = 5.0
    if 25 <= len(t) <= 60:
        s += 1.5
        reasons.append("good length")
    elif len(t) > TITLE_MAX:
        s -= 3
        reasons.append("too long")
    elif len(t) < 15:
        s -= 1.5
        reasons.append("too short")
    first = _norm(t)[:35]
    kw = [w for w in _norm(topic).split() if len(w) > 2]
    if kw and any(w in first for w in kw):
        s += 1.5
        reasons.append("keyword early")
    if re.search(r"\d", t):
        s += 1
        reasons.append("has a number")
    low = t.lower()
    if any(c in low for c in CURIOSITY):
        s += 1
        reasons.append("curiosity")
    caps = [w for w in re.findall(r"[A-Za-z]{3,}", t) if w.isupper()]
    if len(caps) > 1:
        s -= 2
        reasons.append("ALL CAPS")
    if any(c in low for c in CLICKBAIT):
        s -= 3
        reasons.append("clickbait phrase")
    if t.count("!") > 1:
        s -= 1
        reasons.append("too many !")
    return max(0.0, min(10.0, s)), reasons


def tags_within_limit(tags: list[str], limit: int = TAGS_MAX_CHARS) -> list[str]:
    """YouTube counts tag characters + commas, and quotes around tags containing spaces."""
    out: list[str] = []
    total = 0
    for t in tags:
        t = re.sub(r"[<>]", "", t.strip().lstrip("#").lower())[:100]
        if not t or t in out:
            continue
        cost = len(t) + (2 if " " in t else 0) + (1 if out else 0)
        if total + cost > limit:
            break
        out.append(t)
        total += cost
    return out


def build_description(summary: str, credit: str, template: str, hashtags: list[str]) -> str:
    parts = [summary.strip()]
    if credit:
        parts.append(credit.strip())
    if template.strip():
        parts.append(template.strip())
    parts.append(" ".join(hashtags))
    return "\n\n".join(p for p in parts if p)[:4900]


def credit_line(template: str, info: VideoInfo) -> str:
    if not info.url and not info.channel:
        return ""  # your own upload (local file without source info): no credit needed
    return template.format(episode=info.title, source_channel=info.channel or "", url=info.url or "").strip()


def final_hashtags(llm_tags: list[str], niche: str) -> list[str]:
    """3–5 lowercase hashtags, always #shorts + the channel's niche tag."""
    return normalize_hashtags(["#shorts", niche, *llm_tags, "#podcast"], 5)


def generate(clip: ClipCandidate, info: VideoInfo, words: list[Word], settings: dict[str, Any],
             llm: LLMProvider | None, existing_titles: list[str]) -> dict[str, Any]:
    """Returns fields for a QueueItem: title, description, tags, variants, ig_caption, category_id."""
    text = " ".join(w.display("roman") for w in words_in_range(words, clip.start, clip.end))
    language = settings.get("title_language", "hinglish")
    reply: MetaReply | None = None
    if llm is not None:
        try:
            reply = llm.complete_json(
                PROMPT.format(episode=info.title, title=clip.title, hook=clip.hook, topic=clip.topic,
                              text=truncate(text, 1500), language=language,
                              language_rule="Hinglish in Latin script is welcome." if language == "hinglish"
                              else "English only."),
                MetaReply, retries=2, task="ytmeta", json_schema=strict_schema(MetaReply))
        except LLMError:
            reply = None
    titles = [t.strip().strip('"') for t in (reply.titles if reply else [])][:3]
    # always keep the clip's own title/hook as fallback candidates
    for extra in (clip.title, clip.hook, f"{clip.hook} | {clip.topic}" if clip.hook and clip.topic else ""):
        if extra and len(titles) < 3 + 2:
            titles.append(extra)
    variants = []
    for t in dict.fromkeys(titles):  # unique, keep order
        t = truncate(t, TITLE_MAX)
        sc, why = score_title(t, clip.topic)
        dup = is_duplicate(t, existing_titles)
        if dup:
            sc, why = 0.0, ["duplicate of an earlier upload"]
        variants.append({"title": t, "score": round(sc, 1), "reasons": why, "duplicate": dup})
    variants.sort(key=lambda v: v["score"], reverse=True)
    best = next((v["title"] for v in variants if not v["duplicate"] and v["title"]), "")
    summary = (reply.summary if reply else clip.hook or clip.title).strip()
    hashtags = final_hashtags(reply.hashtags if reply else [], settings.get("niche_tag", "#podcast"))
    credit = credit_line(settings.get("credit_template", ""), info)
    tags = tags_within_limit([*(reply.tags if reply else []), clip.topic, *[h.lstrip("#") for h in hashtags],
                              "podcast", "hindi podcast", "shorts"])
    ig = f"{summary}\n\n{credit}\n\n{' '.join(hashtags)}".strip()
    return {
        "title": best, "description": build_description(summary, credit, settings.get("description_template", ""), hashtags),
        "tags": tags, "variants": variants[:5], "ig_caption": ig, "category_id": str(settings.get("category_id", "27")),
        "used_llm": reply is not None,
    }


def validate(title: str, description: str, tags: list[str], existing_titles: list[str]) -> list[str]:
    """Problems that block scheduling (shown in the UI)."""
    problems = []
    if not title.strip():
        problems.append("Title is empty.")
    if len(title) > 100:
        problems.append("Title is longer than 100 characters (YouTube's limit).")
    if is_duplicate(title, existing_titles):
        problems.append("Same title as one of your last 100 uploads.")
    if len(description) > 5000:
        problems.append("Description is longer than 5000 characters.")
    if sum(len(t) + 1 for t in tags) > TAGS_MAX_CHARS + 1:
        problems.append("Tags exceed 500 characters.")
    if "<" in title or ">" in title:
        problems.append("Titles can't contain < or >.")
    return problems
