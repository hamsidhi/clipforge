"""Instagram Reel captions: hook line + 1–2 lines of value + CTA + credit line + at most 5 lowercase hashtags.

Hard rules (validated here, used by the API and the scheduler): ≤ 2,200 characters (Instagram's limit), ≤ 5 hashtags
(our rule; Instagram allows 30), all hashtags lowercase. Extras are stripped, never silently posted.
"""

from __future__ import annotations

import re

MAX_CAPTION = 2200
MAX_HASHTAGS = 5
CTA = "Follow for more clips from the best Indian podcasts 🎙"
_TAG = re.compile(r"#[^\s#]+")


def hashtags(text: str) -> list[str]:
    return _TAG.findall(text or "")


def clean_caption(text: str, max_tags: int = MAX_HASHTAGS) -> str:
    """Lowercase every hashtag, keep the first `max_tags` unique ones (dropping the rest), cap at 2,200 chars."""
    seen: list[str] = []

    def fix(m: re.Match) -> str:
        tag = m.group(0).lower()
        if tag in seen or len(seen) >= max_tags:
            return ""
        seen.append(tag)
        return tag

    out = _TAG.sub(fix, (text or "").replace("\r\n", "\n"))
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    if len(out) > MAX_CAPTION:
        out = out[: MAX_CAPTION - 1].rstrip() + "…"
    return out


def problems(caption: str) -> list[str]:
    out = []
    tags = hashtags(caption)
    if len(caption) > MAX_CAPTION:
        out.append(f"Caption is {len(caption)} characters — Instagram allows {MAX_CAPTION}.")
    if len(tags) > MAX_HASHTAGS:
        out.append(f"{len(tags)} hashtags — keep at most {MAX_HASHTAGS}.")
    if any(t != t.lower() for t in tags):
        out.append("Hashtags must be lowercase.")
    if not caption.strip():
        out.append("Caption is empty.")
    return out


def build(hook: str, value: str, credit: str = "", tags: list[str] | None = None, cta: str = CTA) -> str:
    """Compose a caption from parts (the metadata optimizer supplies them)."""
    tag_line = " ".join(("#" + t.lstrip("#")).lower() for t in (tags or [])[:MAX_HASHTAGS])
    parts = [p.strip() for p in (hook, value, cta, credit, tag_line) if p and p.strip()]
    return clean_caption("\n\n".join(parts))
