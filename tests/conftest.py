from __future__ import annotations

import os

import pytest

# FastAPI's TestClient connects as peer "testclient" with Host "testserver"; the app treats those as the laptop
# itself only when this flag is set (never in production).
os.environ["CLIPFORGE_TESTING"] = "1"

from clipforge.models import Word


def make_words(text: str, start: float = 0.0, dur: float = 0.4, gap: float = 0.1) -> list[Word]:
    """'a b c. d' -> Words with regular timing."""
    out = []
    t = start
    for tok in text.split():
        out.append(Word(start=round(t, 3), end=round(t + dur, 3), text=tok))
        t += dur + gap
    return out


@pytest.fixture
def words() -> list[Word]:
    # 3 sentences, ~0.5 s per word
    return make_words(
        "So hello there. This is a very important point about money and business. "
        "Third sentence starts here and then it ends right now."
    )
