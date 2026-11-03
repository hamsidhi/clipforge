from clipforge.utils.timecode import ass_ts, fmt_ts, overlap_ratio, snap_to_words, words_in_range
from tests.conftest import make_words


def test_fmt_and_ass_ts():
    assert fmt_ts(123.4) == "02:03.4"
    assert fmt_ts(3723) == "1:02:03.0"
    assert ass_ts(0) == "0:00:00.00"
    assert ass_ts(61.239) == "0:01:01.24"
    assert ass_ts(3600.5) == "1:00:00.50"


def test_overlap_ratio_uses_shorter_interval():
    assert overlap_ratio((0, 10), (5, 15)) == 0.5
    assert overlap_ratio((0, 100), (10, 20)) == 1.0  # fully contained
    assert overlap_ratio((0, 10), (10, 20)) == 0.0


def test_snap_never_cuts_words(words):
    # start in the middle of "This" (index 3) and end in the middle of "money"
    s, e, mid = snap_to_words(words, words[3].start + 0.2, words[11].start + 0.2)
    starts = {w.start for w in words}
    ends = {w.end for w in words}
    # snapped start lies in the gap before a word start (padding never enters the previous word)
    assert any(abs((st - 0.15) - s) < 1e-6 or (s >= prev_end and s <= st) for st, prev_end in
               zip([w.start for w in words[1:]], [w.end for w in words[:-1]]))
    # no word is split: every word is fully inside or fully outside
    for w in words:
        inside_start = s <= w.start
        inside_end = w.end <= e
        assert inside_start == inside_end or not (w.start < s < w.end) and not (w.start < e < w.end)
    assert not mid
    assert e > s


def test_snap_prefers_sentence_boundaries(words):
    # "This" starts sentence 2 (index 3); asking for start near "is" should snap back to "This"
    s, e, mid = snap_to_words(words, words[4].start, words[-1].end)
    assert abs(s - (words[3].start - 0.1)) < 0.2  # padded, but not into the previous word
    assert s >= words[2].end
    assert not mid
    # ends after a sentence-ending word
    assert any(abs(e - (w.end + 0.3)) < 0.31 for w in words if w.text.endswith("."))


def test_snap_respects_max_len():
    ws = make_words(" ".join(f"w{i}." for i in range(200)), dur=0.4, gap=0.1)  # every word ends a sentence
    s, e, _ = snap_to_words(ws, 0, 90, min_len=20, max_len=59)
    assert 20 <= e - s <= 59.5


def test_words_in_range(words):
    inside = words_in_range(words, words[2].start, words[5].end)
    assert [w.text for w in inside] == [w.text for w in words[2:6]]


def test_snap_uses_segment_boundaries_when_no_punctuation():
    # no punctuation, no pauses: only Whisper segment ends mark sentences
    ws = make_words(" ".join(f"w{i}" for i in range(120)), dur=0.4, gap=0.1)  # 0.5 s per word
    seg_ends = {ws[i].end for i in range(9, 120, 10)}  # every 10th word ends a segment (every 5 s)
    seg_starts = {ws[i].start for i in range(0, 120, 10)}
    s, e, mid = snap_to_words(ws, 11.2, 38.3, min_len=20, max_len=59, seg_starts=seg_starts, seg_ends=seg_ends)
    assert not mid
    assert any(0 <= e - x <= 0.31 for x in seg_ends)  # padding stops at the next word
    assert any(0 <= x - s <= 0.16 for x in seg_starts)
