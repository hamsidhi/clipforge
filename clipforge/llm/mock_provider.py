"""Deterministic fake LLM for tests and offline pipeline checks (no model needed).

- task="select": parses the numbered "id| [start-end] text" (or "id| text") lines in the prompt and proposes
  compact candidates ({"s","e","score","hook","standalone","topic"}) of consecutive sentences of ~25-50 s
  (word count when the lines carry no times).
- task="titles": a hook/title/why per "CLIP n" block.
- task="transliterate": romanizes Devanagari words with indic-transliteration (1:1 word count).
- task="metadata": returns simple hashtags/caption from the title.
"""

from __future__ import annotations

import json
import re

from clipforge.llm.base import LLMProvider
from clipforge.utils.text import is_devanagari

LINE_RE = re.compile(r"^(\d+)\|\s*(?:\[(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\]\s*)?(.*)$", re.MULTILINE)
CLIP_RE = re.compile(r'^CLIP (\d+)[^\n]*:\n"""(.*?)"""', re.MULTILINE | re.DOTALL)
WORDS_PER_SEC = 2.8


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
        if task == "rerank":
            ids = [int(n) for n in re.findall(r"^CLIP (\d+) \(", prompt, re.MULTILINE)]
            return json.dumps({"clips": [{"id": n, "score": 5 + n % 4, "hook": 6, "standalone": 6} for n in ids]})
        if task == "titles":
            return json.dumps(self._titles(prompt), ensure_ascii=False)
        return json.dumps(self._select(prompt))

    @staticmethod
    def _titles(prompt: str) -> dict:
        out = []
        for n, text in CLIP_RE.findall(prompt):
            first = " ".join(text.split()[:6])
            out.append({"id": int(n), "hook": first, "title": f"Moment — {first}"[:60], "why": "mock title"})
        return {"clips": out}

    @staticmethod
    def _select(prompt: str) -> dict:
        lines = []
        t = 0.0
        for sid, a, b, text in LINE_RE.findall(prompt):
            if a:
                start, end = float(a), float(b)
            else:  # no times in the prompt: estimate from the word count
                start, end = t, t + max(1.0, len(text.split()) / WORDS_PER_SEC)
            t = end
            lines.append((int(sid), start, end, text))
        cands = []
        i = 0
        while i < len(lines) and len(cands) < 6:
            start = lines[i][1]
            j = i
            while j + 1 < len(lines) and lines[j][2] - start < 35:
                j += 1
            if lines[j][2] - start >= 15:
                k = len(cands)
                cands.append({"s": lines[i][0], "e": lines[j][0], "score": 6 + k % 3, "hook": 6 + k % 4,
                              "standalone": 7, "topic": f"topic-{int(start // 120)}"})
            i = j + 1
        return {"candidates": cands}
