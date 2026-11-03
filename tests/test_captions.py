from clipforge.config import CaptionsConfig
from clipforge.models import ClipCandidate, Word
from clipforge.stages.captions import CapWord, ass_color, build_ass, chunk_words, clip_words


def _cw(text: str) -> list[CapWord]:
    out, t = [], 0.0
    for tok in text.split():
        out.append(CapWord(t, t + 0.3, tok))
        t += 0.35
    return out


def test_ass_color():
    assert ass_color("#FFE600") == "&H0000E6FF"
    assert ass_color("#000000", 0x80) == "&H80000000"
    assert ass_color("garbage") == "&H00FFFFFF"


def test_chunking_limits():
    lines = chunk_words(_cw("one two three four five six seven eight"), per_line=3, max_chars=18)
    assert all(len(line) <= 3 for line in lines)
    assert all(len(" ".join(w.text for w in line)) <= 18 for line in lines)
    assert sum(len(line) for line in lines) == 8


def test_chunking_breaks_on_long_word_and_sentence():
    lines = chunk_words(_cw("hi. supercalifragilistic ok"), per_line=3, max_chars=18)
    assert [w.text for w in lines[0]] == ["hi."]


def test_chunking_breaks_on_pause():
    ws = [CapWord(0, 0.3, "a"), CapWord(0.35, 0.6, "b"), CapWord(2.0, 2.3, "c")]
    lines = chunk_words(ws, per_line=3, max_chars=18)
    assert len(lines) == 2


def test_clip_words_rebased_and_edited():
    words = [Word(start=10 + i, end=10.5 + i, text=f"w{i}", roman=f"r{i}") for i in range(10)]
    clip = ClipCandidate(start=12, end=16, title="t", word_edits={"1": "FIXED", "2": ""})
    cw = clip_words(words, clip, "roman")
    assert cw[0].text == "r2"[:0] + "r2" or cw[0].start >= 0
    assert cw[0].start == 0.0 and cw[0].text == "r2"
    texts = [w.text for w in cw]
    assert "FIXED" in texts and "r4" not in texts  # index 2 deleted


def test_build_ass_has_events_and_hook():
    clip = ClipCandidate(start=0, end=5, title="Title here", hook="Ye galti mat karna!")
    ass = build_ass(_cw("paisa kamana easy nahi hai 100 crore"), clip, CaptionsConfig())
    assert "[Events]" in ass
    assert ass.count("Dialogue: 2,") == 7  # one event per active word
    assert "YE GALTI" not in ass and "Ye galti mat karna!" in ass  # hook keeps its case
    assert "\\p1" in ass  # rounded box drawing
    assert "PlayResY: 1920" in ass
    assert "&H0000E6FF" in ass  # yellow highlight


def test_build_ass_without_hook():
    clip = ClipCandidate(start=0, end=5, title="T", hook="x")
    ass = build_ass(_cw("a b c"), clip, CaptionsConfig(hook_title=False))
    assert "\\p1" not in ass
