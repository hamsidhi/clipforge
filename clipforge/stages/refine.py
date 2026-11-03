"""Stage 5 — snap boundaries to words, enforce length, dedupe, rank with topic diversity."""

from __future__ import annotations

import re

from clipforge.config import Config, ScoringWeights
from clipforge.models import ClipCandidate, EnergyData, LLMCandidate, Scores, Word
from clipforge.utils.log import log
from clipforge.utils.timecode import overlap_ratio, snap_to_words

PEAK_BOOST = 0.4       # per energy peak inside the clip (max 2)
MID_SENTENCE_PENALTY = 0.8  # start or end could not be put on a sentence boundary


def weighted_score(s: Scores, w: ScoringWeights) -> float:
    total_w = w.hook + w.standalone + w.emotion + w.value + w.controversy or 1.0
    raw = s.hook * w.hook + s.standalone * w.standalone + s.emotion * w.emotion + s.value * w.value + s.controversy * w.controversy
    return raw / total_w


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"\w+", text.lower()) if len(t) > 2}


def similar(a: ClipCandidate, b: ClipCandidate) -> bool:
    """Near-identical topic: same normalised topic, or titles sharing most words."""
    if a.topic and a.topic.strip().lower() == b.topic.strip().lower() and overlap_ratio((a.start, a.end), (b.start, b.end)) > 0:
        return True
    ta, tb = _tokens(a.title), _tokens(b.title)
    if ta and tb and len(ta & tb) / len(ta | tb) >= 0.6:
        return True
    return False


def snap_candidate(c: LLMCandidate, words: list[Word], cfg: Config, duration: float,
                   boundaries: tuple[set[float], set[float]] | None = None) -> tuple[float, float, bool] | None:
    start = max(0.0, min(c.start, duration))
    end = max(0.0, min(c.end, duration))
    if end - start < 3:
        return None
    starts, ends = boundaries or (set(), set())
    s, e, mid = snap_to_words(words, start, end, min_len=cfg.clips.min_sec, max_len=cfg.clips.max_sec - 0.4,
                              seg_starts=starts, seg_ends=ends)
    if e - s < cfg.clips.min_sec * 0.9 or e - s > cfg.clips.max_sec + 0.5:
        return None
    return s, min(e, duration), mid


def refine(
    cands: list[LLMCandidate], words: list[Word], energy: EnergyData | None, cfg: Config, duration: float,
    boundaries: tuple[set[float], set[float]] | None = None,
) -> list[ClipCandidate]:
    words = sorted(words, key=lambda w: w.start)
    peaks = energy.peaks if energy else []
    scored: list[ClipCandidate] = []
    for c in cands:
        snapped = snap_candidate(c, words, cfg, duration, boundaries)
        if not snapped:
            continue
        s, e, mid = snapped
        score = weighted_score(c.scores, cfg.scoring_weights)
        n_peaks = sum(1 for p in peaks if s <= p <= e)
        score += PEAK_BOOST * min(2, n_peaks)
        if mid:
            score -= MID_SENTENCE_PENALTY
        scored.append(ClipCandidate(start=s, end=e, title=c.title.strip()[:60], hook=c.hook.strip(), topic=c.topic.strip(),
                                    why=c.why.strip(), scores=c.scores, score=round(score, 2)))
    scored.sort(key=lambda x: x.score, reverse=True)

    # dedupe: time overlap with a better clip, or near-identical topic/title
    kept: list[ClipCandidate] = []
    for c in scored:
        if any(overlap_ratio((c.start, c.end), (k.start, k.end)) > cfg.clips.max_overlap for k in kept):
            continue
        if any(similar(c, k) for k in kept):
            continue
        kept.append(c)

    # greedy pick with a diversity penalty for repeated topics
    target = min(cfg.clips.count, cfg.clips.max_count)  # an explicit small count (e.g. a test) is respected
    chosen: list[ClipCandidate] = []
    pool = kept[:]
    while pool and len(chosen) < target:
        topic_counts: dict[str, int] = {}
        for ch in chosen:
            topic_counts[ch.topic.lower()] = topic_counts.get(ch.topic.lower(), 0) + 1
        best = max(pool, key=lambda c: c.score - 0.75 * topic_counts.get(c.topic.lower(), 0))
        chosen.append(best)
        pool.remove(best)
    if len(chosen) < min(target, cfg.clips.min_count):
        log.warning(f"Only {len(chosen)} good clips found (wanted {target}). "
                    "The video may be short, or try another LLM provider / lower clips.min_sec.")
    chosen.sort(key=lambda c: c.score, reverse=True)
    for i, c in enumerate(chosen, 1):
        c.rank = i
    return chosen
