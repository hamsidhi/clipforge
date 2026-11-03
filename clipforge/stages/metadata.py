"""Stage 8b — titles, descriptions, captions, hashtags + gallery.html for every clip."""

from __future__ import annotations

import csv
import html
import re
from pathlib import Path

from pydantic import BaseModel

from clipforge.llm.base import LLMError, LLMProvider
from clipforge.models import ClipCandidate, ClipMetadata, VideoInfo, Word
from clipforge.utils.log import log
from clipforge.utils.text import normalize_hashtags, truncate
from clipforge.utils.timecode import words_in_range

PROMPT = """You write social-media copy for a vertical short cut from a Hinglish podcast.

Podcast: {podcast} — {channel}
Clip title: {title}
Clip hook: {hook}
What is said in the clip:
\"\"\"{text}\"\"\"

Return JSON:
{{"yt_description": "2-3 sentences for YouTube Shorts (Hinglish ok), no hashtags",
  "ig_caption": "2-3 short lines for Instagram + a call to action (e.g. 'Follow for more'), may use 1-2 emojis, no hashtags",
  "hashtags": ["5 lowercase hashtags without spaces, relevant to the topic, e.g. podcast, business"]}}"""


class MetaOut(BaseModel):
    yt_description: str
    ig_caption: str
    hashtags: list[str]


DEFAULT_TAGS = ["podcast", "hindipodcast", "shorts", "motivation", "business"]


def _strip_tags(text: str) -> str:
    text = re.sub(r"(?<!\w)#\w+", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def credit_line(info: VideoInfo) -> str:
    parts = f"Source: {info.title}"
    if info.channel:
        parts += f" — {info.channel}"
    if info.url:
        parts += f" ({info.url})"
    return parts


def build(clip: ClipCandidate, file: Path | None, info: VideoInfo, words: list[Word], llm: LLMProvider | None,
          script: str) -> tuple[ClipMetadata, bool]:
    """Returns (metadata, written_by_llm). Template fallbacks are not cached by the caller."""
    text = " ".join(w.display(script) for w in words_in_range(words, clip.start, clip.end))
    meta: MetaOut | None = None
    if llm is not None:
        try:
            meta = llm.complete_json(
                PROMPT.format(podcast=info.title, channel=info.channel, title=clip.title, hook=clip.hook,
                              text=truncate(text, 1500)),
                MetaOut, retries=2, task="metadata",
            )
        except LLMError as exc:
            log.warning(f"Metadata LLM failed for clip {clip.rank}; using a template ({exc})")
    used_llm = meta is not None
    if meta is None:
        meta = MetaOut(
            yt_description=f"{clip.hook or clip.title}. Full podcast: {info.title}.",
            ig_caption=f"{clip.hook or clip.title} 👀\nPoora podcast dekho — link in bio.\nFollow for more 🔥",
            hashtags=DEFAULT_TAGS,
        )
    # models often sneak hashtags into the text too; keep only our ≤5 normalised ones at the end
    found = re.findall(r"#(\w+)", meta.yt_description + " " + meta.ig_caption)
    meta.yt_description = _strip_tags(meta.yt_description)
    meta.ig_caption = _strip_tags(meta.ig_caption)
    tags = normalize_hashtags(meta.hashtags + found + DEFAULT_TAGS, 5)
    credit = credit_line(info)
    title = truncate(clip.title, 60)
    return ClipMetadata(
        rank=clip.rank, file=file.name if file else "", start=clip.start, end=clip.end, duration=clip.duration,
        score=clip.score, title=title, hook=clip.hook,
        yt_title=truncate(f"{title} #shorts", 100),
        yt_description=f"{meta.yt_description.strip()}\n\n{credit}\n\n{' '.join(tags)}",
        ig_caption=f"{meta.ig_caption.strip()}\n\n{credit}\n\n{' '.join(tags)}",
        hashtags=tags, credit=credit,
    ), used_llm


def write(metas: list[ClipMetadata], csv_path: Path, json_path: Path) -> None:
    import json

    json_path.write_text(json.dumps([m.model_dump() for m in metas], indent=2, ensure_ascii=False), encoding="utf-8")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel shows Hindi/emoji correctly
        w = csv.writer(f)
        cols = list(ClipMetadata.model_fields)
        w.writerow(cols)
        for m in metas:
            row = m.model_dump()
            row["hashtags"] = " ".join(m.hashtags)
            w.writerow([row[c] for c in cols])


def gallery_html(info: VideoInfo, metas: list[ClipMetadata]) -> str:
    cards = []
    for m in metas:
        cards.append(f"""
<div class="card">
  <video src="clips/{html.escape(m.file)}" controls preload="metadata" playsinline></video>
  <div class="meta"><b>#{m.rank}</b> <span class="score">{m.score:.1f}</span> · {m.duration:.0f}s
  <h3>{html.escape(m.title)}</h3><p>{html.escape(' '.join(m.hashtags))}</p></div>
</div>""")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(info.title)} — ClipForge</title>
<style>
body{{background:#0b0b0f;color:#eee;font-family:system-ui,sans-serif;margin:0;padding:24px}}
h1{{font-size:20px;margin:0 0 20px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:20px}}
.card{{background:#16161d;border-radius:18px;overflow:hidden;border:1px solid #26262f}}
video{{width:100%;aspect-ratio:9/16;background:#000;display:block}}
.meta{{padding:10px 12px;font-size:13px}} .meta h3{{font-size:14px;margin:6px 0}} .meta p{{color:#999;margin:0}}
.score{{background:#ffe600;color:#000;border-radius:8px;padding:1px 6px;font-weight:700}}
</style></head><body>
<h1>{html.escape(info.title)} — {len(metas)} clips</h1>
<div class="grid">{''.join(cards)}</div>
</body></html>"""
