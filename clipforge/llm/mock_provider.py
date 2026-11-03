"""Deterministic fake LLM for tests and offline pipeline checks (no model needed).

- task="select": parses the "[start-end] text" lines in the prompt and proposes clips made of
  consecutive sentences of ~25-50 s.
- task="transliterate": romanizes Devanagari words with indic-transliteration (1:1 word count).
- task="metadata": returns simple hashtags/caption from the title.
"""

from __future__ import annotations

import json
import re

from clipforge.llm.base import LLMProvider
from clipforge.utils.text import is_devanagari

LINE_RE = re.compile(r"^\[(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\]\s*(.*)$", re.MULTILINE)


def romanize_word(word: str) -> str:
    """Devanagari -> rough Roman Hinglish (lowercase, schwa-dropped). Non-Devanagari unchanged."""
    if not is_devanagari(word):
        return word
    try:
        from indic_transliteration import sanscript
        from indic_transliteration.sanscript import transliterate

        out = transliterate(word, sanscript.DEVANAGARI, sanscript.ITRANS)
    except Exception:
        return word
    out = out.replace("~N", "n").replace(".N", "n").replace(".n", "n").replace("M", "n").replace(".h", "")
    out = out.replace("A", "aa").replace("I", "ee").replace("U", "oo").replace("RRi", "ri").replace("^", "")
    out = re.sub(r"[^A-Za-z0-9.,!?'%₹-]", "", out).lower()
    if len(out) > 3 and out.endswith("a") and not out.endswith("aa"):
        out = out[:-1]  # Hindi schwa deletion: "karta" stays, "kaama" -> "kaam" is approximated
    return out or word


class MockProvider(LLMProvider):
    name = "mock"

    def __init__(self, model: str = "mock", temperature: float = 0.0, timeout: int = 0) -> None:
        super().__init__("mock", temperature, timeout)

    def generate(self, prompt: str, *, system: str | None = None, json_mode: bool = True, task: str = "") -> str:
        if task == "transliterate":
            m = re.search(r"<words>\s*(\[.*?\])\s*</words>", prompt, re.DOTALL)
            words = json.loads(m.group(1)) if m else []
            return json.dumps({"roman": [romanize_word(w) for w in words]}, ensure_ascii=False)
        if task == "metadata":
            return json.dumps({
                "yt_description": "Full episode link in the description. Follow for more!",
                "ig_caption": "Ye baat sabko pata honi chahiye 👇\nPoora podcast dekho — link bio mein.\nFollow for more 🔥",
                "hashtags": ["podcast", "hindipodcast", "business", "motivation", "shorts"],
            })
        return json.dumps(self._select(prompt))

    @staticmethod
    def _select(prompt: str) -> dict:
        lines = [(float(a), float(b), t) for a, b, t in LINE_RE.findall(prompt)]
        cands = []
        i = 0
        n = 0
        while i < len(lines) and n < 6:
            start = lines[i][0]
            j = i
            while j + 1 < len(lines) and lines[j][1] - start < 35:
                j += 1
            end = lines[j][1]
            if end - start >= 15:
                text = " ".join(t for _, _, t in lines[i : j + 1])
                first = " ".join(text.split()[:6])
                k = len(cands)
                cands.append({
                    "start": start, "end": end, "hook": first, "title": f"Moment {int(start // 60)}:{int(start % 60):02d} — {first}"[:60],
                    "topic": f"topic-{int(start // 120)}", "why": "mock selection",
                    "scores": {"hook": 6 + k % 4, "standalone": 7, "emotion": 5 + k % 3, "value": 6, "controversy": 3},
                })
                n += 1
            i = j + 1
        return {"candidates": cands}
