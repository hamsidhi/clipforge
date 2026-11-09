"""Arc selection engine (stages/arcs.py) + content profiles + the publish-queue guard. Mock LLM, no network."""

from __future__ import annotations

import json

import numpy as np
import pytest

from clipforge.config import Config
from clipforge.llm.mock_provider import MockProvider
from clipforge.models import CandidateFile, ClipCandidate, Segment, Transcript, VideoInfo, Word
from clipforge.stages import arcs, profiles
from clipforge.utils.log import NullReporter
from clipforge.workspace import Workspace


def _seg(i, t0, text, dur=None, prob=0.95):
    words = text.split()
    dur = dur or len(words) / 2.6
    step = dur / max(1, len(words))
    ws = [Word(start=round(t0 + k * step, 2), end=round(t0 + (k + 1) * step - 0.02, 2), text=w, prob=prob)
          for k, w in enumerate(words)]
    return Segment(id=i, start=t0, end=t0 + dur, text=text, words=ws)


def _talk(parts: list[tuple[str, float]], gap: float = 0.3, prob: float = 0.95) -> Transcript:
    segs, t = [], 0.0
    for i, (text, dur) in enumerate(parts):
        segs.append(_seg(i, t, text, dur, prob))
        t += dur + gap
    return Transcript(language="en", model="test", duration=t, segments=segs)


INFO = VideoInfo(id="v", title="Sam Altman on AI | Podcast FO583", channel="Raj Shamani", duration=600)

# question → a long answer told in unpunctuated Whisper segments, then the next question
QA = [
    ("What do people not get about building AI?", 4),
    ("I think it's just very outside of the norm of why people assume", 6),
    ("we would do this and the reason is that it is the most important and useful thing", 7),
    ("i can imagine doing and it's incredibly", 5),
    ("interesting and incredibly exciting to work on every day.", 6),
    ("So the whole point is compounding progress for everyone.", 6),
    ("Why is India more optimistic than the US?", 4),
    ("People here are building with it in incredible ways and the energy is bigger.", 8),
    ("A kid used it to raise money for his school and it worked.", 7),
    ("That is why I am so optimistic about India.", 5),
]


def test_structure_questions_blocks_and_thought_ends():
    st = arcs.build_structure(_talk(QA), INFO)
    assert st.questions[0] and st.questions[6] and not st.questions[3]
    assert [a for a, _ in st.blocks] == [0, 6]
    assert not st.ends_thought(3)          # "...it's incredibly" — Whisper segment end, not a thought end
    assert st.ends_thought(5) and st.ends_thought(9)


def test_qa_unit_is_the_full_answer_not_the_first_line():
    st = arcs.build_structure(_talk(QA), INFO)
    units = arcs.qa_units(st, 20, 90, 30)
    first = next(u for u in units if u.a == 0)
    assert first.b == 5                    # question → end of the answer, before the next question
    assert all(st.ends_thought(u.b) for u in units)


def test_long_answer_splits_into_complete_sub_arcs():
    long = [("How did you build the company?", 4)] + [(f"Part {i} of the story goes on and on and on", 9) for i in range(20)]
    long[7] = ("and that was the first big lesson.", 6)
    long[14] = ("which is the second lesson I learned.", 6)
    st = arcs.build_structure(_talk(long), INFO)
    units = arcs.qa_units(st, 20, 90, 30)
    assert units and all(st.span(u.a, u.b) <= 90 and st.ends_thought(u.b) for u in units)
    assert units[0].a == 0 and units[0].b in (7, 14)


def test_length_policy_never_cuts_mid_answer():
    # one 120 s answer with no thought end inside: no clip is better than a clip cut mid-answer
    parts = [("Why did it fail?", 3)] + [("and then it kept going and going without a pause", 8)] * 15
    st = arcs.build_structure(_talk(parts, gap=0.2), INFO)
    assert arcs.qa_units(st, 20, 90, 30) == []
    pick = arcs.expand_pick(st, __import__("clipforge.models", fromlist=["x"]).LLMCandidate(start=5, end=15, title="t"), 20, 90)
    assert pick is None


def test_pick_expands_to_its_question_and_a_thought_end():
    from clipforge.models import LLMCandidate

    st = arcs.build_structure(_talk(QA), INFO)
    x = arcs.expand_pick(st, LLMCandidate(start=st.start(1), end=st.end(2), title="punchy line"), 20, 90)
    assert x.a == 0 and st.ends_thought(x.b) and x.b >= 4


def test_garbled_segments_are_detected():
    t = _talk(QA)
    for w in t.segments[8].words + t.segments[9].words:
        w.prob = 0.2
    st = arcs.build_structure(t, INFO)
    assert st.garbled[8] and st.garbled_ratio(6, 9) > 0.15 and st.garbled_ratio(0, 5) == 0


def _ws(tmp_path):
    ws = Workspace(tmp_path, "v")
    ws.select_cache.mkdir(parents=True, exist_ok=True)
    return ws


def test_judge_extends_the_end_when_the_answer_continues(tmp_path):
    cfg = Config().with_overrides({"clips.count": 5})
    st = arcs.build_structure(_talk(QA), INFO)
    x = arcs.Arc(0, 3, "pick")                       # ends on "...it's incredibly"
    j = arcs.judge(_ws(tmp_path), st, x, "B0 — q", INFO, MockProvider(), cfg)
    a, b, cont = arcs.apply_judgement(st, x, j, 10, 90)
    assert (a, b) == (0, 4) and cont                   # moved to the thought end; the judge said "continues"
    j2 = arcs.judge(_ws(tmp_path), st, arcs.Arc(a, b, "pick"), "B0 — q", INFO, MockProvider(), cfg)
    assert arcs.apply_judgement(st, arcs.Arc(a, b, "pick"), j2, 10, 90) == (0, 4, False)  # judged again: complete


def test_engine_end_to_end_with_gates_and_no_padding(tmp_path):
    cfg = Config().with_overrides({"clips.count": 10, "arcs.min_quality": 7.0})
    tr = _talk(QA + [("smalltalk smalltalk weather nice today right?", 4)] +
               [("smalltalk about nothing at all really.", 22)])
    prof = profiles.PROFILES["podcast"]
    cands, stats = arcs.select(_ws(tmp_path), cfg, MockProvider(), MockProvider(), NullReporter(), INFO, tr, [], [],
                               prof)
    assert cands and stats["judged"] >= 2
    assert all(c.rubric["completeness"] >= 7 and not c.rubric["continues"] for c in cands)
    assert all("smalltalk" not in " ".join(s.text for s in tr.segments if c.start <= s.start < c.end) for c in cands)
    assert len(cands) < 10                             # never padded to the requested count
    assert [c.rank for c in cands] == list(range(1, len(cands) + 1)) and all(c.title == "" for c in cands)


def test_profiles_auto_detection():
    tr = _talk(QA)
    assert profiles.resolve("auto", INFO, tr)[0].name == "podcast"
    g = VideoInfo(id="g", title="GTA 6 gameplay first mission", channel="x")
    assert profiles.resolve("auto", g)[0].name == "gaming"
    c = VideoInfo(id="c", title="Joker Ki Haveli ka bhoot", channel="x")
    assert profiles.resolve("auto", c)[0].name == "horror_comedy"
    m = VideoInfo(id="m", title="Last To Leave Wins ₹10 Lakh", channel="x")
    assert profiles.resolve("auto", m)[0].name == "vlog_challenge"
    quiet = VideoInfo(id="q", title="Untitled", channel="x")
    sparse = Transcript(language="en", model="t", duration=600, segments=[_seg(0, 0, "nice shot")])
    assert profiles.resolve("auto", quiet, sparse)[0].name == "gaming"
    assert profiles.resolve("comedy", quiet)[0].name == "comedy"


def test_gaming_peaks_on_synthetic_audio_become_moments():
    from clipforge.stages.analyze_audio import compute_energy

    sr = 8000
    rng = np.random.default_rng(1)
    audio = (rng.normal(0, 300, sr * 180)).astype(np.int16)          # 3 min of quiet commentary/background
    for t in (60, 130):                                                # two explosions
        audio[t * sr:(t + 2) * sr] = (rng.normal(0, 12000, 2 * sr)).clip(-32000, 32000).astype(np.int16)
    peaks = compute_energy(audio, sr).peaks
    assert any(abs(p - 60) <= 2 for p in peaks) and any(abs(p - 130) <= 2 for p in peaks)
    tr = _talk([(f"gameplay commentary line {i} here.", 9) for i in range(20)], gap=0.5)
    st = arcs.build_structure(tr, VideoInfo(id="g", title="GTA 6 gameplay"))
    g = profiles.PROFILES["gaming"]
    moments = arcs.peak_moments(st, peaks, g.build_sec, g.react_sec, g.min_sec, g.max_sec)
    assert moments and all(st.start(m.a) <= 60 + 2 or st.start(m.a) <= 130 for m in moments)
    assert all(g.min_sec <= st.span(m.a, m.b) <= g.max_sec for m in moments)


# ----------------------------------------------------------------------------- publish-queue guard
def test_keep_locked_keeps_queued_clips_and_appends_new_ones():
    from clipforge.pipeline import keep_locked

    old = [ClipCandidate(rank=1, start=0, end=30, title="queued one"), ClipCandidate(rank=2, start=40, end=70, title="free")]
    new = [ClipCandidate(rank=1, start=5, end=40, title="overlaps queued"), ClipCandidate(rank=2, start=100, end=150, title="new")]
    out = keep_locked(old, new, {1})
    assert [(c.rank, c.title) for c in out] == [(1, "queued one"), (3, "new")]


def test_select_never_reselects_a_job_with_queued_clips(tmp_path, monkeypatch):
    from clipforge import pipeline

    cfg = Config().with_overrides({"output.dir": str(tmp_path), "llm.provider": "mock"})
    ws = Workspace(tmp_path, "v")
    ws.dir.mkdir(parents=True, exist_ok=True)
    ws.write_model(ws.candidates, CandidateFile(video_id="v", candidates=[ClipCandidate(rank=1, start=0, end=30, title="q")]))
    monkeypatch.setattr(pipeline, "queued_ranks", lambda vid: {1})
    monkeypatch.setattr(pipeline, "get_llm", lambda cfg: pytest.fail("must not re-select"))
    got = pipeline.select(ws, cfg)
    assert [c.title for c in got] == ["q"]
    assert json.loads(ws.candidates.read_text(encoding="utf-8"))["candidates"][0]["title"] == "q"


def test_normalize_starts_on_a_sentence_and_never_ends_on_the_next_question():
    st = arcs.build_structure(_talk(QA), INFO)
    assert arcs.normalize(st, 2, 6) == (0, 5)        # backs up to its question, drops the next question (6)
    assert arcs.normalize(st, 0, 5) == (0, 5)


def test_outro_and_sponsor_lines_never_become_clips():
    parts = QA + [("Thank you so much for watching this episode, please subscribe and comment below.", 12)]
    st = arcs.build_structure(_talk(parts), INFO)
    assert st.garbled[-1]


def test_judge_prompt_has_no_example_numbers_to_copy():
    assert '"hook":7' not in arcs.JUDGE_PROMPT and "<0-10>" in arcs.JUDGE_PROMPT
