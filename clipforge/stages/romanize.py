"""Roman Hinglish captions: transliterate Devanagari words 1:1 so word timings stay aligned.

Done lazily, only for words inside selected clips (a 2-hour transcript has ~20k words; the clips
need ~2-3k) — this keeps a local 7B model on CPU fast. Results are cached in transcript.json.

The LLM answers with an index-keyed object ({"0": "kya", "1": "baat", …}) rather than a list, so one
dropped or merged word only affects that word (it falls back to rule-based romanization), not the batch.
"""

from __future__ import annotations

from pydantic import BaseModel

from clipforge.llm.base import LLMError, LLMProvider
from clipforge.llm.mock_provider import romanize_word
from clipforge.models import Transcript, Word
from clipforge.utils.log import ProgressReporter, check_cancel, log
from clipforge.utils.text import is_devanagari

BATCH = 40

PROMPT = """Transliterate these Hindi/Hinglish podcast words from Devanagari into natural Roman Hinglish,
the way Indians type on WhatsApp/Instagram: "क्या"->"kya", "बात"->"baat", "है"->"hai", "एक"->"ek",
"की"->"ki", "पैसे"->"paise", "ज़िंदगी"->"zindagi", "करना"->"karna". NOT formal ISO/IAST — no diacritics.
English words written in Devanagari become normal English spelling: "बिज़नेस"->"business", "वाइन"->"wine".
Keep punctuation attached to the word.

Words (index: word), shown in sentence order for context:
{words}

Return ONLY a JSON object mapping EVERY index to its romanized word, e.g. {{"0": "kya", "1": "baat"}}.
One word in, one word out — never merge or split words."""


class RomanOut(BaseModel):
    model_config = {"extra": "allow"}


def _fallback(words: list[Word], src: str = "rule-failed") -> None:
    """Rule-based romanization. "rule-failed" marks words to retry with the LLM on the next render."""
    for w in words:
        w.roman = romanize_word(w.text)
        w.roman_src = src


def romanize_words(words: list[Word], llm: LLMProvider | None, reporter: ProgressReporter, retries: int = 2) -> int:
    """Fill `roman` for the given words in place. Returns how many words were changed."""
    todo = [w for w in words if w.roman is None or (llm is not None and w.roman_src == "rule-failed")]
    for w in todo:
        w.roman = None if is_devanagari(w.text) else w.text
    todo = [w for w in todo if w.roman is None]
    if not todo:
        return 0
    misses = 0
    for i in range(0, len(todo), BATCH):
        check_cancel(reporter)
        batch = todo[i : i + BATCH]
        reporter.progress(i / len(todo), None, f"romanizing words {i}/{len(todo)}")
        if llm is None:
            _fallback(batch)
            continue
        listing = "\n".join(f"{k}: {w.text}" for k, w in enumerate(batch))
        try:
            out = llm.complete_json(PROMPT.format(words=listing), RomanOut, retries=retries, task="transliterate")
            data = out.model_dump()
            if isinstance(data.get("roman"), dict):  # tolerate {"roman": {...}}
                data = data["roman"]
            elif isinstance(data.get("roman"), list):  # tolerate the list form if the count matches
                data = {str(k): v for k, v in enumerate(data["roman"])} if len(data["roman"]) == len(batch) else {}
        except LLMError as exc:
            log.warning(f"Transliteration batch fell back to rule-based romanization: {exc}")
            _fallback(batch)
            continue
        for k, w in enumerate(batch):
            r = str(data.get(str(k), "")).strip()
            # guard: missing, still Devanagari, or obviously merged words -> rule-based for this word only
            if not r or is_devanagari(r) or len(r.split()) > 2:
                w.roman, w.roman_src = romanize_word(w.text), "rule"
                misses += 1
            else:
                w.roman, w.roman_src = r, "llm"
    if misses:
        log.info(f"Romanization: {misses}/{len(todo)} words used the rule-based fallback")
    return len(todo)


def romanize_ranges(transcript: Transcript, ranges: list[tuple[float, float]], llm: LLMProvider | None,
                    reporter: ProgressReporter) -> int:
    words = [w for w in transcript.all_words() if any(a - 1 <= w.start <= b + 1 for a, b in ranges)]
    n = romanize_words(words, llm, reporter)
    if n:
        transcript.romanized = True
    return n
