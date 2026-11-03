"""Roman Hinglish captions: transliterate Devanagari words 1:1 so word timings stay aligned.

Done lazily, only for words inside selected clips (a 2-hour transcript has ~20k words; the clips
need ~2-3k) — this keeps a local 7B model on CPU fast. Results are cached in transcript.json.
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from clipforge.llm.base import LLMError, LLMProvider
from clipforge.llm.mock_provider import romanize_word
from clipforge.models import Transcript, Word
from clipforge.utils.log import ProgressReporter, check_cancel, log
from clipforge.utils.text import is_devanagari

BATCH = 60

PROMPT = """You transliterate Hindi/Hinglish podcast words from Devanagari into natural Roman Hinglish,
the way Indians type on WhatsApp/Instagram (e.g. "क्या बात है" -> "kya baat hai", "बिज़नेस" -> "business",
"पैसे" -> "paise", "ज़िंदगी" -> "zindagi"). NOT formal ISO/IAST transliteration — no diacritics.
English words written in Devanagari must become the normal English spelling.
Words already in Latin script stay exactly as they are. Keep punctuation attached to the word.

Return JSON: {{"roman": [...]}} with EXACTLY {n} strings — one per input word, same order.
Never merge or split words.

<words>
{words}
</words>"""


class RomanOut(BaseModel):
    roman: list[str]


def _fallback(words: list[Word]) -> None:
    for w in words:
        w.roman = romanize_word(w.text)


def romanize_words(words: list[Word], llm: LLMProvider | None, reporter: ProgressReporter, retries: int = 2) -> int:
    """Fill `roman` for the given words in place. Returns how many words were changed."""
    todo = [w for w in words if w.roman is None]
    for w in todo:
        if not is_devanagari(w.text):
            w.roman = w.text
    todo = [w for w in todo if w.roman is None]
    if not todo:
        return 0
    for i in range(0, len(todo), BATCH):
        check_cancel(reporter)
        batch = todo[i : i + BATCH]
        reporter.progress(i / len(todo), None, f"romanizing words {i}/{len(todo)}")
        if llm is None:
            _fallback(batch)
            continue
        texts = [w.text for w in batch]
        try:
            out = llm.complete_json(
                PROMPT.format(n=len(texts), words=json.dumps(texts, ensure_ascii=False)),
                RomanOut, retries=retries, task="transliterate",
            )
            if len(out.roman) != len(texts):
                raise LLMError(f"word count mismatch ({len(out.roman)} != {len(texts)})")
            for w, r in zip(batch, out.roman):
                r = r.strip()
                # guard against the model dropping a word or answering in Devanagari again
                w.roman = r if r and not is_devanagari(r) else romanize_word(w.text)
        except LLMError as exc:
            log.warning(f"Transliteration batch fell back to rule-based romanization: {exc}")
            _fallback(batch)
    return len(todo)


def romanize_ranges(transcript: Transcript, ranges: list[tuple[float, float]], llm: LLMProvider | None,
                    reporter: ProgressReporter) -> int:
    words = [w for w in transcript.all_words() if any(a - 1 <= w.start <= b + 1 for a, b in ranges)]
    n = romanize_words(words, llm, reporter)
    if n:
        transcript.romanized = True
    return n
