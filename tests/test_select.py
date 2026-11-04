"""Selection stage: compact JSON, sentence-id → word-time mapping, title pass."""

from clipforge.config import Config
from clipforge.llm.base import LLMError, LLMProvider
from clipforge.llm.mock_provider import MockProvider
from clipforge.models import ClipCandidate, RawCandidateList, Segment, Transcript, VideoInfo, Word
from clipforge.stages import select_clips as sc
from clipforge.utils.log import NullReporter
from clipforge.workspace import Workspace


def seg(i: int, start: float, text: str = "ek do teen chaar") -> Segment:
    words = []
    t = start + 0.2
    for w in text.split():
        words.append(Word(start=round(t, 2), end=round(t + 0.3, 2), text=w))
        t += 0.4
    return Segment(id=i, start=start, end=t + 0.5, text=text, words=words)


SEGS = [seg(i, i * 5.0) for i in range(10)]


def test_window_segments_numbering_and_lines():
    segs = sc.window_segments(SEGS + [Segment(id=99, start=60, end=61, text="  ")], 9.0, 31.0)
    assert [s.id for s in segs] == [2, 3, 4, 5, 6]  # overlap with [9, 31) (seg 1 ends at 7.3); blank one ignored
    assert sc.window_lines(segs, latin=True, timestamps=True).splitlines()[0].startswith("1| [10.0-")
    lines = sc.window_lines(segs, latin=True).splitlines()
    assert lines[0] == "1| ek do teen chaar" and lines[-1].startswith("5| ")


def test_lines_drop_fillers_but_keep_ids():
    segs = [seg(0, 0, "haan haan toh suno"), seg(1, 5, "um"), seg(2, 10, "you know, maine 5 crore loss kiya")]
    assert sc.window_lines(segs, latin=True).splitlines() == ["1| haan toh suno", "3| maine 5 crore loss kiya"]
    assert segs[0].words[1].text == "haan"  # the transcript itself is untouched


def test_strip_fillers_keeps_meaning():
    f = sc.strip_fillers
    assert f("like like it was um crazy") == "like it was crazy"
    assert f("Matlab, ye galat hai") == "ye galat hai"
    assert f("iska matlab hai ki paisa") == "iska matlab hai ki paisa"
    assert f("You know what, I lost 5 crore") == "You know what, I lost 5 crore"
    assert f("dheere dheere sab theek hua you know.") == "dheere dheere sab theek hua."
    assert f("the the thing is, you know the rules") == "the thing is, you know the rules"


def test_ids_map_to_word_times_inclusive():
    segs = SEGS[2:8]
    c = sc.to_candidate(sc.CompactCandidate(s=2, e=4, score=7, hook=8, standalone=6, topic="Startup Funding Big Round X"), segs)
    assert c.start == segs[1].words[0].start  # id 2 = second segment of the window (1-based)
    assert c.end == segs[3].words[-1].end    # id 4 inclusive
    assert c.topic == "startup funding big round"
    assert (c.scores.hook, c.scores.standalone, c.scores.value) == (8, 6, 7)


def test_ids_reversed_are_swapped_and_unknown_ids_dropped():
    segs = SEGS[:5]
    c = sc.to_candidate(sc.CompactCandidate(s=4, e=2), segs)
    assert c.start == segs[1].words[0].start and c.end == segs[3].words[-1].end
    assert sc.to_candidate(sc.CompactCandidate(s=0, e=2), segs) is None
    assert sc.to_candidate(sc.CompactCandidate(s=3, e=6), segs) is None


def test_parse_compact_keeps_good_items_and_clamps():
    raw = RawCandidateList.model_validate({"candidates": [{"s": 1, "e": 3, "score": 14, "hook": "7"}, {"s": "x"}, {"e": 2}]})
    good, bad = sc.parse_compact(raw)
    assert bad == 2 and good[0].score == 10 and good[0].hook == 7


def _info() -> VideoInfo:
    return VideoInfo(id="v", title="Pod", channel="Ch", duration=60.0, source_path="x.mp4")


def test_write_titles_batches_and_fallback(tmp_path):
    t = Transcript(language="hi", model="x", duration=60, segments=SEGS)
    cands = [ClipCandidate(start=s.start, end=s.end + 20, title="", topic="money") for s in SEGS[:7]]
    ws = Workspace(tmp_path, "v")
    calls = []

    class Counting(MockProvider):
        def generate(self, prompt, **kw):
            calls.append(kw.get("task"))
            return super().generate(prompt, **kw)

    sc.write_titles(ws, Config(), Counting(), NullReporter(), _info(), t, cands)
    assert calls == ["titles", "titles"]  # 7 clips -> batches of 5 + 2
    assert all(c.title.startswith("Moment") and c.hook for c in cands)

    class Down(LLMProvider):
        name = "down"

        def generate(self, prompt, **kw):
            raise LLMError("boom")

    fresh = [ClipCandidate(start=0, end=25, title="", topic="money")]
    sc.write_titles(ws, Config(), Down("x"), NullReporter(), _info(), t, fresh)
    assert fresh[0].title.startswith("Money: ") and fresh[0].hook


def test_prefilter_skips_only_clear_non_content():
    normal = [seg(i, i * 5.0, "maine apni pehli company mein bahut paisa gawaya aur seekha") for i in range(96)]
    assert sc.skip_reason(normal, 0, 480) is None
    quiet = [seg(0, 0, "music"), seg(1, 200, "haan toh chaliye shuru karte hain")]
    assert "no speech" in sc.skip_reason(quiet, 0, 480)
    promo = [seg(i, i * 5.0, "this episode is sponsored by acme use code FIGURE for a discount") for i in range(8)]
    assert "sponsor" in sc.skip_reason(promo + normal[:3], 0, 60)
    # one subscribe line in a normal window, or a low-energy but talkative window, is never skipped
    assert sc.skip_reason(normal + [seg(99, 470, "please subscribe to our channel")], 0, 480) is None


def test_prefilter_window_not_sent_to_llm(tmp_path):
    talk = [seg(i, i * 5.0, "maine apni pehli company mein bahut paisa gawaya aur seekha") for i in range(96)]
    tail = [seg(200 + i, 960 + i * 5.0, "this episode is sponsored by acme use code FIGURE") for i in range(90)]
    t = Transcript(language="hi", model="x", duration=1440, segments=talk + tail)
    cfg = Config().with_overrides({"llm.window_min": 8, "llm.overlap_min": 0})
    prompts = []

    class Spy(MockProvider):
        def generate(self, prompt, **kw):
            prompts.append(prompt)
            return super().generate(prompt, **kw)

    cands, failed, ctx = sc.run(Workspace(tmp_path, "v"), cfg, Spy(), NullReporter(), _info(), t)
    assert len(prompts) == 1 and cands and failed == 0 and ctx % 1024 == 0
    off = cfg.with_overrides({"select.prefilter": False})
    prompts.clear()
    sc.run(Workspace(tmp_path, "w"), off, Spy(), NullReporter(), _info(), t)
    assert len(prompts) == 2  # the empty middle window is skipped by the old <30-words rule either way


def test_rerank_averages_scores_and_keeps_bonus(tmp_path):
    from clipforge.models import Scores
    from clipforge.stages.refine import weighted_score

    t = Transcript(language="hi", model="x", duration=60, segments=SEGS)
    w = Config().scoring_weights
    cands = []
    for i, s in enumerate(SEGS[:6]):
        sc_ = Scores(hook=8, standalone=8, emotion=8, value=8, controversy=8)
        cands.append(ClipCandidate(start=s.start, end=s.end + 20, title="", topic="t", scores=sc_,
                                   score=round(weighted_score(sc_, w) + (0.4 if i == 0 else 0), 2)))
    prompts = []

    class Spy(MockProvider):
        def generate(self, prompt, **kw):
            prompts.append(prompt)
            return super().generate(prompt, **kw)

    sc.rerank(Workspace(tmp_path, "v"), Config(), Spy(), NullReporter(), _info(), t, cands)
    assert len(prompts) == 2 and "BEFORE:" in prompts[0] and "AFTER:" in prompts[0]
    # mock: clip n of a batch -> score 5 + n % 4, hook 6, standalone 6
    first = cands[0]
    assert first.scores.hook == 7 and first.scores.value == (8 + 6) / 2
    assert abs(first.score - (weighted_score(first.scores, w) + 0.4)) < 0.011


def test_pipeline_select_with_rerank_mock(tmp_path):
    from clipforge.stages import refine
    from clipforge.models import LLMCandidate, Scores

    t = Transcript(language="hi", model="x", duration=60, segments=SEGS)
    raw = [LLMCandidate(start=s.start, end=s.end + 25, title="", topic=f"t{i}", scores=Scores(hook=i, standalone=5))
           for i, s in enumerate(SEGS)]
    cfg = Config().with_overrides({"clips.count": 3, "clips.min_sec": 5, "clips.max_count": 25,
                                   "select.rerank_pool": 5, "clips.max_overlap": 1.0})
    pool = refine.score_pool(raw, t.all_words(), None, cfg, 60, t.boundaries())
    short = refine.pick(pool, cfg, target=cfg.select.rerank_pool)
    assert len(short) == 5
    sc.rerank(Workspace(tmp_path, "v"), cfg, MockProvider(), NullReporter(), _info(), t, short)
    final = refine.pick(short, cfg)
    assert [c.rank for c in final] == [1, 2, 3] and all(c in short for c in final)
