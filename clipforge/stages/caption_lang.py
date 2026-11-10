"""Caption language per clip (2026-11-10): `ClipCandidate.caption_lang` = auto | en | hinglish.

auto (default) = the language the speakers use (English speech → English, Hindi speech → Hinglish in Latin script).
Picking the OTHER language translates the captions: the clip's words are grouped into short lines (pauses /
sentence ends), the local LLM translates every line (same number of lines back), and each translated line keeps the
original line's start/end — its words are spread evenly across that time, so the captions stay in sync with the
speech. Translations are cached per clip in `clips/_work/NN/caption_<lang>.json` (key = the source lines), so a
re-render never calls the LLM twice. Hand word edits apply to the spoken-language captions only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel

from clipforge.models import ClipCandidate, Word
from clipforge.utils.log import log
from clipforge.utils.text import spoken_language
from clipforge.utils.timecode import words_in_range

LINE_WORDS = 24      # max words per source line: whole sentences translate well (short fragments get merged/skipped)
LINE_GAP = 1.2       # a pause this long ends a line

PROMPT = """Translate these {n} caption lines from a podcast clip into {target}.
{rule}
Keep the meaning, the tone and any names/numbers exactly; keep each line short and natural to read on screen.
Translate EVERY line on its own — never merge lines, never skip one, never leave a line untranslated.
Return exactly one translated line per input line, same ids.

{lines}

Respond with ONLY this JSON, with all {n} lines (ids 1 to {n}):
{{"lines":[{{"id":1,"text":"<line 1 in {target}>"}},{{"id":2,"text":"<line 2 in {target}>"}}, ...]}}"""

RULES = {
    "en": "Write plain, simple English (like subtitles). No Hindi or Hinglish words.",
    "hinglish": ("Write Hinglish in Latin script the way Indians text each other (e.g. \"Ye sabse badi galti hai\"): "
                 "Hindi grammar with common English words kept in English. Never Devanagari."),
}
NAMES = {"en": "English", "hinglish": "Hinglish (Hindi written in Roman letters)"}


class _Line(BaseModel):
    id: int
    text: str


class _Out(BaseModel):
    lines: list[_Line]


def target_for(clip: ClipCandidate, words: list[Word]) -> str | None:
    """'en' / 'hinglish' when the captions must be translated, None when they stay as spoken."""
    want = getattr(clip, "caption_lang", "auto") or "auto"
    if want == "auto":
        return None
    spoken = spoken_language([w.text for w in words_in_range(words, clip.start, clip.end)])
    if want == "en" and spoken == "en":
        return None
    if want == "hinglish" and spoken == "hi":
        return None  # Hindi speech is already shown as Hinglish (romanized)
    return want


def source_lines(words: list[Word], clip: ClipCandidate) -> list[tuple[float, float, str]]:
    """Clip words → short lines (start, end in clip time, text in Latin script), breaking at pauses and sentence ends."""
    inside = words_in_range(words, clip.start, clip.end)
    lines: list[tuple[float, float, str]] = []
    cur: list[Word] = []

    def flush() -> None:
        if cur:
            a = max(0.0, cur[0].start - clip.start)
            b = min(clip.duration, cur[-1].end - clip.start)
            lines.append((round(a, 3), round(max(b, a + 0.2), 3), " ".join(w.display("roman") for w in cur)))
            cur.clear()

    for w in inside:
        if cur and (w.start - cur[-1].end > LINE_GAP or len(cur) >= LINE_WORDS):
            flush()
        cur.append(w)
        if w.text.rstrip().endswith((".", "?", "!", "।")):
            flush()
    flush()
    merged: list[tuple[float, float, str]] = []  # a 1-2 word fragment ("Do") joins the next line
    for ln in lines:
        if merged and len(merged[-1][2].split()) <= 2:
            a, _, t = merged.pop()
            ln = (a, ln[1], f"{t} {ln[2]}")
        merged.append(ln)
    return merged


def translate(lines: list[tuple[float, float, str]], target: str, llm) -> list[str]:
    """One translated text per line. Lines the model skips keep their original text (logged)."""
    body = "\n".join(f"{i}| {t}" for i, (_, _, t) in enumerate(lines, 1))
    out = llm.complete_json(PROMPT.format(n=len(lines), target=NAMES[target], rule=RULES[target], lines=body), _Out, retries=2,
                            task="caption_translate", options={"num_predict": 40 * len(lines) + 64})
    got = {ln.id: ln.text.strip() for ln in out.lines if ln.text.strip()}
    missing = [i for i in range(1, len(lines) + 1) if i not in got]
    if missing:
        log.warning(f"[captions] translation skipped {len(missing)} line(s); they stay in the spoken language")
    return [got.get(i, t) for i, (_, _, t) in enumerate(lines, 1)]


def spread(lines: list[tuple[float, float, str]], texts: list[str]):
    """Translated lines → caption words, each line's words evenly spread over the original line's time."""
    from clipforge.stages.captions import CapWord

    out = []
    for (a, b, _), text in zip(lines, texts):
        toks = text.split()
        if not toks:
            continue
        step = (b - a) / len(toks)
        out += [CapWord(round(a + i * step, 3), round(a + (i + 1) * step, 3), tok) for i, tok in enumerate(toks)]
    return out


def caption_words(work_dir: Path, clip: ClipCandidate, words: list[Word], llm_factory):
    """Caption words in the chosen language, or None when the normal (spoken-language) captions apply.
    `llm_factory()` is only called on a cache miss."""
    target = target_for(clip, words)
    if not target:
        return None
    lines = source_lines(words, clip)
    if not lines:
        return None
    key = hashlib.sha1(json.dumps([target, lines], ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    cache = work_dir / f"caption_{target}.json"
    texts = None
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            texts = data["texts"] if data.get("key") == key else None
        except (ValueError, KeyError):
            texts = None
    if texts is None:
        llm = llm_factory()
        if llm is None:
            log.warning(f"[captions] clip {clip.rank}: no LLM available to translate captions — kept as spoken")
            return None
        try:
            texts = translate(lines, target, llm)
        except Exception as exc:  # the render must not fail because of a translation: keep the spoken captions
            log.warning(f"[captions] clip {clip.rank}: translating captions to {NAMES[target]} failed ({exc}) — kept as spoken")
            return None
        work_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"key": key, "texts": texts}, ensure_ascii=False), encoding="utf-8")
    return spread(lines, texts)
