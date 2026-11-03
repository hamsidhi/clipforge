from clipforge.config import Config
from clipforge.models import ClipCandidate, EnergyData, LLMCandidate, Scores
from clipforge.stages.refine import refine, similar, weighted_score
from tests.conftest import make_words


def _cfg(**clips) -> Config:
    c = Config()
    return c.with_overrides({f"clips.{k}": v for k, v in clips.items()})


def _long_words():
    # 300 short sentences of 2 words -> 0.5 s per word -> a sentence every second
    return make_words(" ".join(f"word{i} end{i}." for i in range(300)))


def test_weighted_score_normalised():
    s = Scores(hook=10, standalone=10, emotion=10, value=10, controversy=10)
    assert abs(weighted_score(s, Config().scoring_weights) - 10) < 1e-9


def test_scores_are_clamped():
    assert Scores(hook=15, value=-3).hook == 10
    assert Scores(hook=15, value=-3).value == 0


def test_dedupe_overlap_keeps_best():
    words = _long_words()
    cands = [
        LLMCandidate(start=10, end=45, title="Alpha money story", topic="money", scores=Scores(hook=9, standalone=9)),
        LLMCandidate(start=15, end=50, title="Beta different take", topic="career", scores=Scores(hook=3, standalone=3)),
        LLMCandidate(start=100, end=140, title="Gamma totally new", topic="health", scores=Scores(hook=5, standalone=5)),
    ]
    out = refine(cands, words, None, _cfg(count=10, min_sec=20, max_sec=59), duration=300)
    titles = [c.title for c in out]
    assert "Alpha money story" in titles
    assert "Beta different take" not in titles  # >30 % overlap with a better clip
    assert "Gamma totally new" in titles
    assert [c.rank for c in out] == [1, 2]
    for c in out:
        assert 20 <= c.duration <= 59.5


def test_energy_peak_boost_changes_rank():
    words = _long_words()
    cands = [
        LLMCandidate(start=10, end=40, title="One", topic="a", scores=Scores(hook=5, standalone=5)),
        LLMCandidate(start=100, end=130, title="Two", topic="b", scores=Scores(hook=5, standalone=5)),
    ]
    energy = EnergyData(rms_db=[], peaks=[110.0, 120.0])
    out = refine(cands, words, energy, _cfg(count=5), duration=300)
    assert out[0].title == "Two"


def test_similar_titles_deduped():
    a = ClipCandidate(start=0, end=30, title="Why most startups fail in India", topic="startups")
    b = ClipCandidate(start=200, end=230, title="Why most startups fail in India today", topic="failure")
    assert similar(a, b)


def test_too_short_candidates_are_extended_or_dropped():
    words = _long_words()
    cands = [LLMCandidate(start=10, end=14, title="short", topic="x", scores=Scores(hook=8))]
    out = refine(cands, words, None, _cfg(count=5, min_sec=20, max_sec=59), duration=300)
    assert all(c.duration >= 18 for c in out)
