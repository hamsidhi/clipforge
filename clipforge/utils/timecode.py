"""Timestamp helpers: formatting, overlap maths and word-boundary snapping."""

from __future__ import annotations

from typing import Sequence

from clipforge.models import Word

SENTENCE_END = (".", "?", "!", "।", "॥", "…")


def fmt_ts(seconds: float) -> str:
    """123.4 -> '02:03.4' ; 3723 -> '1:02:03.0'."""
    seconds = max(0.0, seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h >= 1:
        return f"{int(h)}:{int(m):02d}:{s:04.1f}"
    return f"{int(m):02d}:{s:04.1f}"


def ass_ts(seconds: float) -> str:
    """ASS timestamp H:MM:SS.cc (centiseconds)."""
    seconds = max(0.0, seconds)
    cs = int(round(seconds * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    """Length of the intersection of two intervals (0 if disjoint)."""
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def overlap_ratio(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Intersection divided by the SHORTER interval (so a clip inside another counts as 1.0)."""
    inter = overlap(a[0], a[1], b[0], b[1])
    shorter = min(a[1] - a[0], b[1] - b[0])
    return inter / shorter if shorter > 0 else 0.0


def is_sentence_end(word: Word) -> bool:
    return word.text.strip().endswith(SENTENCE_END)


def _starts_sentence(words: Sequence[Word], i: int, gap: float = 0.6) -> bool:
    """A word starts a sentence if the previous word ended one, or after a long pause."""
    if i == 0:
        return True
    prev = words[i - 1]
    return is_sentence_end(prev) or (words[i].start - prev.end) >= gap


def _ends_sentence(words: Sequence[Word], i: int, gap: float = 0.6) -> bool:
    if i == len(words) - 1:
        return True
    return is_sentence_end(words[i]) or (words[i + 1].start - words[i].end) >= gap


def snap_to_words(
    words: Sequence[Word],
    start: float,
    end: float,
    *,
    pad_start: float = 0.15,
    pad_end: float = 0.3,
    search: float = 4.0,
    min_len: float = 0.0,
    max_len: float = 1e9,
) -> tuple[float, float, bool]:
    """Snap [start, end] to word boundaries so no word is ever cut in half.

    Start moves to the nearest sentence-starting word within `search` seconds (else nearest word
    start); end moves to the nearest sentence-ending word (else nearest word end). Then padding is
    applied without spilling into the neighbouring words.

    Returns (new_start, new_end, started_mid_sentence).
    """
    if not words:
        return start, end, False
    words = sorted(words, key=lambda w: w.start)

    # --- start
    in_range = [i for i, w in enumerate(words) if abs(w.start - start) <= search]
    sent_starts = [i for i in in_range if _starts_sentence(words, i)]
    if sent_starts:
        si = min(sent_starts, key=lambda i: abs(words[i].start - start))
        mid = False
    else:
        si = min(range(len(words)), key=lambda i: abs(words[i].start - start))
        mid = not _starts_sentence(words, si)

    # --- end (must be after start word)
    cand = [i for i in range(si, len(words)) if abs(words[i].end - end) <= search]
    sent_ends = [i for i in cand if _ends_sentence(words, i)]
    pool = sent_ends or cand or list(range(si, len(words)))

    def _len_ok(i: int) -> bool:
        return min_len <= words[i].end - words[si].start <= max_len

    ok = [i for i in pool if _len_ok(i)]
    if not ok:  # relax: any word end that satisfies the length limits
        ok = [i for i in range(si, len(words)) if _len_ok(i)] or pool
    ei = min(ok, key=lambda i: abs(words[i].end - end))

    new_start = words[si].start - pad_start
    if si > 0:
        new_start = max(new_start, words[si - 1].end)
    new_end = words[ei].end + pad_end
    if ei + 1 < len(words):
        new_end = min(new_end, words[ei + 1].start)
    return round(max(0.0, new_start), 3), round(new_end, 3), mid


def words_in_range(words: Sequence[Word], start: float, end: float) -> list[Word]:
    """Words whose midpoint lies inside [start, end]."""
    return [w for w in words if start <= (w.start + w.end) / 2 <= end]
