"""Per-clip caption language: auto | en | hinglish (stages/caption_lang.py)."""

from __future__ import annotations

import json

from clipforge.models import ClipCandidate, Word
from clipforge.stages import caption_lang


class _LLM:
    calls = 0

    def complete_json(self, prompt, model, **kw):
        _LLM.calls += 1
        ids = [int(line.split("|")[0]) for line in prompt.splitlines() if line[:1].isdigit() and "|" in line]
        return model.model_validate({"lines": [{"id": i, "text": f"translated line {i}"} for i in ids]})


def _words(text: str, start: float = 10.0, step: float = 0.4) -> list[Word]:
    return [Word(start=start + i * step, end=start + i * step + 0.35, text=t) for i, t in enumerate(text.split())]


ENGLISH = _words("so when we started openai. nobody believed us at all. and that was fine with me honestly.")
HINDI = _words("toh dekho yaar business mein sabse badi galti ye hai. log sochte nahi hain kabhi bhi.")


def _clip(lang: str, words) -> ClipCandidate:
    return ClipCandidate(rank=1, start=words[0].start - 0.1, end=words[-1].end + 0.1, title="t", caption_lang=lang)


def test_auto_and_same_language_need_no_translation():
    assert caption_lang.target_for(_clip("auto", ENGLISH), ENGLISH) is None
    assert caption_lang.target_for(_clip("en", ENGLISH), ENGLISH) is None
    assert caption_lang.target_for(_clip("hinglish", HINDI), HINDI) is None
    assert caption_lang.target_for(_clip("hinglish", ENGLISH), ENGLISH) == "hinglish"
    assert caption_lang.target_for(_clip("en", HINDI), HINDI) == "en"


def test_lines_break_at_sentences_and_keep_times():
    c = _clip("hinglish", ENGLISH)
    lines = caption_lang.source_lines(ENGLISH, c)
    assert [t for *_, t in lines][0] == "so when we started openai."
    assert all(0 <= a < b <= c.duration for a, b, _ in lines)


def test_translated_words_stay_in_each_line_and_are_cached(tmp_path):
    c = _clip("en", HINDI)
    _LLM.calls = 0
    cw = caption_lang.caption_words(tmp_path, c, HINDI, lambda: _LLM())
    lines = caption_lang.source_lines(HINDI, c)
    assert cw and " ".join(w.text for w in cw).startswith("translated line 1")
    assert cw[0].start == lines[0][0] and abs(cw[2].end - lines[0][1]) < 0.01   # 3 words fill line 1's time
    assert json.loads((tmp_path / "caption_en.json").read_text(encoding="utf-8"))["texts"][0] == "translated line 1"
    caption_lang.caption_words(tmp_path, c, HINDI, lambda: _LLM())
    assert _LLM.calls == 1                                                      # second render: cache, no LLM


def test_auto_returns_none_so_normal_captions_apply(tmp_path):
    assert caption_lang.caption_words(tmp_path, _clip("auto", HINDI), HINDI, lambda: _LLM()) is None
