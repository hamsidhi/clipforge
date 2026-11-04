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
    m = re.match(r"^(.*?)([.,!?]*)$", out)
    core, punct = (m.group(1), m.group(2)) if m else (out, "")
    # Hinglish spelling habits: final schwa dropped (eka->ek, baata->baat), long final vowels shortened
    # (kyaa->kya, kee->ki, hoo->hu), "aa"/"ee"/"oo" kept inside words (baat, deep, door).
    if len(core) >= 3 and core.endswith("a") and not core.endswith("aa"):
        core = core[:-1]
    if core.endswith("aa") and len(core) > 2:
        core = core[:-1]
    elif core.endswith("ee") and len(core) > 2:
        core = core[:-2] + "i"
    elif core.endswith("oo") and len(core) > 2:
        core = core[:-2] + "u"
    return (core + punct) or word


class MockProvider(LLMProvider):
    name = "mock"

    def __init__(self, model: str = "mock", temperature: float = 0.0, timeout: int = 0) -> None:
        super().__init__("mock", temperature, timeout)

    def generate(self, prompt: str, *, system: str | None = None, json_mode: bool = True, task: str = "",
                 json_schema: dict | None = None, options: dict | None = None) -> str:
        if task == "transliterate":
            pairs = re.findall(r"^(\d+): (.+)$", prompt, re.MULTILINE)
            return json.dumps({k: romanize_word(w.strip()) for k, w in pairs}, ensure_ascii=False)
        if task == "ytmeta":
            m = re.search(r"Clip's working title: (.*)", prompt)
            base = (m.group(1).strip() if m else "Podcast moment")[:50]
            return json.dumps({
                "titles": [f"{base} — the real story", f"Why {base.lower()}?", f"{base}: 3 lessons"],
                "summary": f"{base}. Watch till the end.",
                "tags": ["podcast", "business", "india", "startup", "money"],
                "hashtags": ["#business", "#startup", "#india"],
            })
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
