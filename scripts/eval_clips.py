"""Evaluate a job's clips (Clip quality, 2026-11-09). Read-only; no LLM.

    python scripts/eval_clips.py <workspace/video_id> [--candidates other.json] [--quiet] [--json out.json]

Per clip: rubric scores (arc engine), first and last line, the 15 s after the end. Totals:
- cut mid-answer = the clip's last segment is not a thought end (no . ? ! and no pause ≥ 1 s and the next segment
  is not a question): the speaker keeps going;
- gate pass % (completeness ≥ 7, standalone ≥ 7, not continuing), average completeness/value, average length;
- garbled clips (> 15 % low-confidence words);
- label agreement: clips you approved (👍) / rejected (👎) in the gallery or on Telegram vs score ≥ min_quality.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clipforge.models import CandidateFile, Transcript, VideoInfo  # noqa: E402
from clipforge.stages import arcs  # noqa: E402


def evaluate(ws: Path, cand_path: Path | None = None, min_quality: float = 7.0) -> dict:
    tr = Transcript.model_validate_json((ws / "transcript.json").read_text(encoding="utf-8"))
    info = VideoInfo.model_validate_json((ws / "info.json").read_text(encoding="utf-8"))
    cands = CandidateFile.model_validate_json((cand_path or ws / "candidates.json").read_text(encoding="utf-8")).candidates
    st = arcs.build_structure(tr, info)
    rows = []
    for c in cands:
        inside = [i for i in range(len(st.segs)) if st.start(i) >= c.start - 0.5 and st.end(i) <= c.end + 0.5]
        inside = inside or [i for i in range(len(st.segs)) if st.end(i) > c.start and st.start(i) < c.end]
        if not inside:
            continue
        a, b = inside[0], inside[-1]
        after = " ".join(arcs.seg_text(st.segs[i]) for i in range(b + 1, min(len(st.segs), b + 12))
                         if st.start(i) < st.end(b) + 15)
        r = c.rubric or {}
        mid = not st.ends_thought(b)
        gates = (r.get("completeness", 0) >= 7 and r.get("standalone", 0) >= 7 and not r.get("continues", mid)) if r else None
        rows.append({"rank": c.rank, "title": c.title, "len": round(c.end - c.start, 1), "score": c.score,
                     "rubric": {k: r.get(k) for k in ("hook", "completeness", "standalone", "value", "flow", "trend")} if r else {},
                     "first": arcs.seg_text(st.segs[a])[:140], "last": arcs.seg_text(st.segs[b])[:140],
                     "after_15s": after[:240], "cut_mid_answer": mid, "gates": gates,
                     "garbled": st.garbled_ratio(a, b) > 0.15, "approved": c.approved})
    judged = [x for x in rows if x["rubric"]]
    labelled = [x for x in rows if x["approved"] is not None]
    agree = [x for x in labelled if (x["score"] >= min_quality) == bool(x["approved"])]
    return {
        "clips": len(rows),
        "cut_mid_answer": sum(x["cut_mid_answer"] for x in rows),
        "garbled": sum(x["garbled"] for x in rows),
        "avg_len": round(mean(x["len"] for x in rows), 1) if rows else 0,
        "gate_pass_pct": round(100 * sum(1 for x in judged if x["gates"]) / len(judged)) if judged else None,
        "avg_completeness": round(mean(x["rubric"]["completeness"] for x in judged), 2) if judged else None,
        "avg_value": round(mean(x["rubric"]["value"] for x in judged), 2) if judged else None,
        "labels": len(labelled), "label_agreement_pct": round(100 * len(agree) / len(labelled)) if labelled else None,
        "rows": rows,
    }


if __name__ == "__main__":
    ws = Path(sys.argv[1])
    cp = Path(sys.argv[sys.argv.index("--candidates") + 1]) if "--candidates" in sys.argv else None
    res = evaluate(ws, cp)
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    if "--quiet" not in sys.argv:
        for x in res["rows"]:
            flag = "CUT" if x["cut_mid_answer"] else "ok "
            print(f"#{x['rank']:>2} {flag} {x['len']:>5}s score {x['score']:<5} {x['title'][:50]}  {x['rubric'] or ''}")
            print(f"     first: {x['first']}\n     last:  {x['last']}\n     next:  {x['after_15s'][:160]}")
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}))
