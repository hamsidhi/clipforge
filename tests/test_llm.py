import pytest

from clipforge.llm.base import LLMError, LLMProvider
from clipforge.llm.mock_provider import MockProvider, romanize_word
from clipforge.llm.ollama_provider import pick_model
from clipforge.models import LLMCandidateList
from clipforge.stages.select_clips import make_windows
from clipforge.utils.text import extract_json, normalize_hashtags, slugify


class FlakyProvider(LLMProvider):
    name = "flaky"

    def __init__(self, replies):
        super().__init__("x")
        self.replies = list(replies)
        self.prompts = []

    def generate(self, prompt, *, system=None, json_mode=True, task="", json_schema=None):
        self.prompts.append(prompt)
        return self.replies.pop(0)


GOOD = '{"candidates":[{"start":1,"end":30,"title":"t","scores":{"hook":5}}]}'


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here it is: {"a": [1,2]} hope it helps') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_retry_then_success_includes_error():
    p = FlakyProvider(["not json", '{"wrong": 1}', GOOD])
    res = p.complete_json("PROMPT", LLMCandidateList, retries=3)
    assert len(res.candidates) == 1
    assert "invalid" in p.prompts[1].lower() and "PROMPT" in p.prompts[2]


def test_retry_exhausted_raises():
    p = FlakyProvider(["x", "y", "z"])
    with pytest.raises(LLMError):
        p.complete_json("P", LLMCandidateList, retries=3)


def test_mock_select_parses_lines():
    from clipforge.stages.select_clips import CompactList

    lines = "\n".join(f"{i + 1}| [{i * 5}.0-{i * 5 + 4.5}] sentence number {i} here" for i in range(30))
    res = MockProvider().complete_json(f"TRANSCRIPT:\n{lines}", CompactList, task="select")
    assert res.candidates
    for c in res.candidates:
        assert 1 <= c.s < c.e <= 30
        assert (c.e - c.s + 1) * 5 >= 15


def test_romanize_word():
    assert romanize_word("hello") == "hello"
    assert romanize_word("बात") == "baat"
    assert romanize_word("क्या") == "kya"
    assert romanize_word("एक") == "ek"
    assert romanize_word("की") == "ki"
    assert romanize_word("है.") == "hai."


def test_pick_model_prefers_general():
    assert pick_model(["qwen2.5-coder:7b", "qwen2.5:7b"]) == "qwen2.5:7b"
    assert pick_model(["qwen2.5-coder:7b"]) == "qwen2.5-coder:7b"
    assert pick_model(["nomic-embed-text", "llama3.1:8b"]) == "llama3.1:8b"
    assert pick_model([]) is None


def test_windows_cover_duration_with_overlap():
    w = make_windows(3600, 8, 1)
    assert w[0] == (0.0, 480)
    assert w[1][0] == 420
    assert w[-1][1] == 3600
    assert make_windows(300, 8, 1) == [(0.0, 300)]


def test_hashtags_and_slug():
    # a multi-word tag in a list stays one tag; a single string is split on spaces/commas
    assert normalize_hashtags(["#Money", "Startup India", "money", "a", "b", "c", "d"]) == \
        ["#money", "#startupindia", "#a", "#b", "#c"]
    assert normalize_hashtags("#Money, startup India") == ["#money", "#startup", "#india"]
    assert slugify("Kya Baat Hai! ₹100 Crore?") == "kya-baat-hai-100-crore"
    assert slugify("क्या") == "clip"


def test_romanize_keyed_output_tolerates_missing_words():
    from clipforge.models import Word
    from clipforge.stages.romanize import romanize_words
    from clipforge.utils.log import NullReporter

    class Partial(LLMProvider):
        name = "partial"

        def generate(self, prompt, *, system=None, json_mode=True, task="", json_schema=None):
            return '{"0": "kya", "2": "hai"}'  # index 1 missing

    ws = [Word(start=0, end=1, text="क्या"), Word(start=1, end=2, text="बात"), Word(start=2, end=3, text="है"),
          Word(start=3, end=4, text="okay")]
    romanize_words(ws, Partial("x"), NullReporter())
    assert [w.roman for w in ws] == ["kya", "baat", "hai", "okay"]


def test_raw_candidates_keep_good_items():
    from clipforge.models import RawCandidateList

    raw = RawCandidateList.model_validate([{"start": 1, "end": 30, "title": "ok"}, {"start": 5, "end": 2, "title": "bad"}, {"x": 1}])
    assert [c.title for c in raw.valid()] == ["ok"]
