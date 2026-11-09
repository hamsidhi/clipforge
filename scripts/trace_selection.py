"""Trace how each final clip's boundaries came to be (Clip quality root causes, 2026-11-09).

    python scripts/trace_selection.py <workspace/video_id> [--json out.json]

For every final clip in candidates.json: the raw LLM span it came from (largest overlap in llm_candidates.json),
start/end shifts made by snapping + the length policy, the length, how the clip ends (question? last words) and the
next 30 s of transcript. Also summarises the raw LLM spans (lengths, how many fell under min_sec / over max_sec).
Read-only: no LLM, no writes to the workspace.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from statistics import median

QWORDS = re.compile(r"\?|\b(kya|kyun|kyon|kaise|kaun|kab|kahan|kitna|kitne|what|why|how|when|who|which|do you|did you|"
                    r"are you|is it|can you|would you|should)\b", re.I)


def load(ws: Path):
    tr = json.loads((ws / "transcript.json").read_text(encoding="utf-8"))
    raw = json.loads((ws / "llm_candidates.json").read_text(encoding="utf-8"))
    raw = raw["candidates"] if isinstance(raw, dict) else raw
    fin = json.loads((ws / "candidates.json").read_text(encoding="utf-8"))["candidates"]
    return tr["segments"], raw, fin


def text_between(segs, a: float, b: float) -> str:
    out = []
    for s in segs:
        if s["end"] > a and s["start"] < b:
            out.append(s["text"].strip())
    return " ".join(out)


def overlap(a, b) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def trace(ws: Path, max_sec: float = 59.0, min_sec: float = 20.0) -> dict:
    segs, raw, fin = load(ws)
    lens = [c["end"] - c["start"] for c in raw]
    summary = {"raw_candidates": len(raw), "raw_len_median": round(median(lens), 1) if lens else None,
               "raw_under_min": sum(1 for x in lens if x < min_sec), "raw_over_max": sum(1 for x in lens if x > max_sec),
               "raw_under_10s": sum(1 for x in lens if x < 10)}
    rows = []
    for c in fin:
        src = max(raw, key=lambda r: overlap((r["start"], r["end"]), (c["start"], c["end"])), default=None)
        ov = overlap((src["start"], src["end"]), (c["start"], c["end"])) if src else 0
        last_seg = next((s for s in reversed(segs) if s["start"] < c["end"] - 0.3), None)
        last_q = bool(last_seg and QWORDS.search(last_seg["text"]) and last_seg["text"].strip().endswith("?"))
        after = text_between(segs, c["end"], c["end"] + 30)
        nxt = next((s for s in segs if s["start"] >= c["end"] - 0.2), None)
        rows.append({
            "rank": c["rank"], "title": c["title"], "final": [round(c["start"], 1), round(c["end"], 1)],
            "len": round(c["end"] - c["start"], 1),
            "raw": [round(src["start"], 1), round(src["end"], 1)] if src and ov > 0 else None,
            "raw_len": round(src["end"] - src["start"], 1) if src and ov > 0 else None,
            "start_shift": round(c["start"] - src["start"], 1) if src and ov > 0 else None,
            "end_shift": round(c["end"] - src["end"], 1) if src and ov > 0 else None,
            "first": text_between(segs, c["start"], c["start"] + 6)[:160],
            "last": (last_seg["text"].strip() if last_seg else "")[:160],
            "ends_on_question": last_q,
            "next_starts_question": bool(nxt and nxt["text"].strip().endswith("?")),
            "after_30s": after[:400],
        })
    return {"summary": summary, "clips": rows}


if __name__ == "__main__":
    ws = Path(sys.argv[1])
    res = trace(ws)
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(res["summary"]))
    for r in res["clips"]:
        print(f"\n#{r['rank']} {r['title']}  final {r['final']} ({r['len']}s)  raw {r['raw']} ({r['raw_len']}s)  "
              f"shift {r['start_shift']}/{r['end_shift']}  ends_on_q={r['ends_on_question']}")
        print(f"  FIRST: {r['first']}")
        print(f"  LAST:  {r['last']}")
        print(f"  NEXT30: {r['after_30s'][:300]}")
