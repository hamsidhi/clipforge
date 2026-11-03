"""Text helpers: slugs, Devanagari detection, keyword picking, hashtag cleanup, JSON extraction."""

from __future__ import annotations

import json
import re
import unicodedata

DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")

# Words that make a caption line "pop" when coloured (Hinglish + English).
STRONG_WORDS = {
    "money", "paisa", "paise", "crore", "crores", "lakh", "lakhs", "million", "billion", "rich", "poor",
    "success", "fail", "failure", "secret", "never", "always", "biggest", "best", "worst", "truth", "sach",
    "galat", "sahi", "zero", "free", "business", "startup", "dream", "fear", "darr", "power", "game",
    "mistake", "galti", "life", "zindagi", "love", "pyaar", "dhokha", "khatam", "important", "problem",
    "solution", "hate", "salary", "profit", "loss", "invest", "investment", "wealth", "job", "naukri",
}

NUMBER_RE = re.compile(r"(₹|\$|rs\.?|%)|\d")


def slugify(text: str, max_len: int = 40) -> str:
    """'Kya Baat Hai! ₹100 Crore' -> 'kya-baat-hai-100-crore'. Falls back to 'clip'."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text[:max_len].rstrip("-")) or "clip"


def is_devanagari(text: str) -> bool:
    return bool(DEVANAGARI_RE.search(text))


def clean_word(text: str) -> str:
    return re.sub(r"[^\w₹$%]+", "", text.lower(), flags=re.UNICODE)


def keyword_score(word: str) -> int:
    """How 'highlight-worthy' a word is. 0 = not a keyword."""
    w = clean_word(word)
    if not w:
        return 0
    if NUMBER_RE.search(word.lower()):
        return 3
    if w in STRONG_WORDS:
        return 2
    return 0


def normalize_hashtags(tags: list[str] | str, limit: int = 5) -> list[str]:
    """Lowercase, '#'-prefixed, alnum only, deduped, max `limit`."""
    if isinstance(tags, str):
        tags = re.split(r"[\s,]+", tags)
    out: list[str] = []
    for t in tags:
        t = re.sub(r"[^a-z0-9_]", "", t.lower().lstrip("#"))
        if t and f"#{t}" not in out:
            out.append(f"#{t}")
        if len(out) >= limit:
            break
    return out


def extract_json(text: str) -> dict | list:
    """Parse JSON from an LLM reply, tolerating ```json fences and prose around it."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # find the outermost {...} or [...]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        s = text.find(open_c)
        e = text.rfind(close_c)
        if s != -1 and e > s:
            try:
                return json.loads(text[s : e + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("Response did not contain valid JSON")


def truncate(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"
