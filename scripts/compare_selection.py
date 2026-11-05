"""Quality guardrail for the select stage: compare a new selection run against the saved baseline.

    python scripts/compare_selection.py --new workspace/_bench/<label> [--baseline tests/fixtures/select_baseline]
                                        [--rerank] [--no-judge] [--md report.md]

Both sides are JSON files written by scripts/bench_select.py (one per video id). Per episode and pooled:

* top-20 overlap   — share of baseline clips that have a new clip covering the same moment
                     (> 50 % time overlap, intersection / shorter clip — the same maths refine uses)
* avg score        — the pipeline's own score of the final clips (informational: its scale depends on the prompt)
* judge score      — every final clip's transcript text re-scored by ONE fixed judge prompt (local Ollama,
                     temperature 0, fixed seed, cached), mean of hook / standalone / payoff. This is the score
                     used by the pass rule, because it is comparable across prompt formats.
* boundary %       — clips that start on a sentence boundary (punctuation, ≥0.6 s pause or a Whisper segment
                     start — the definition refine snaps to) AND last 20–59 s (±0.5 s padding)
* new / dropped clips (title + timestamps) to watch.

Pass rule: overlap ≥ 80 % AND judge score not lower AND boundary % not lower (pooled over all episodes).
With --rerank the overlap condition is skipped (a re-rank pass is expected to change the picks).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import BaseModel, Field  # noqa: E402

from clipforge.llm.mock_provider import romanize_word  # noqa: E402
from clipforge.models import Transcript, Word  # noqa: E402
from clipforge.utils.timecode import _starts_sentence, fmt_ts, overlap_ratio  # noqa: E402

SAME_MOMENT = 0.5
JUDGE_VERSION = "judge-v1"
JUDGE_SEED = 7
JUDGE_PROMPT = """You judge clips cut from a Hinglish podcast as vertical shorts (YouTube Shorts / Reels).
For EACH clip below, rate 0-10 (integers, be strict, 9-10 only for truly viral moments):
- hook: do the first 1-3 seconds grab attention (bold claim, question, number, story start)?
- standalone: does it make complete sense with zero context?
- payoff: does it end on an insight, answer, punchline or conclusion (not mid-thought)?

{clips}

Respond with ONLY JSON: {{"ratings":[{{"id":1,"hook":7,"standalone":6,"payoff":8}}, ...]}} — one entry per clip id."""


class Rating(BaseModel):
    id: int
    hook: float = Field(ge=0, le=10)
    standalone: float = Field(ge=0, le=10)
    payoff: float = Field(ge=0, le=10)


class Ratings(BaseModel):
    ratings: list[Rating]


def load_words(video_id: str, workspace: Path) -> tuple[list[Word], set[float]]:
    t = Transcript.model_validate_json((workspace / video_id / "transcript.json").read_text(encoding="utf-8"))
    words = sorted(t.all_words(), key=lambda w: w.start)
    return words, t.boundaries()[0]


def clip_text(words: list[Word], start: float, end: float) -> str:
    return " ".join((w.roman or romanize_word(w.text)) for w in words if start <= (w.start + w.end) / 2 <= end)


def on_boundary(c: dict, words: list[Word], seg_starts: set[float]) -> bool:
    dur = c["end"] - c["start"]
    if not 19.5 <= dur <= 59.5:
        return False
    idx = next((i for i, w in enumerate(words) if (w.start + w.end) / 2 >= c["start"]), None)
    if idx is None:
        return False
    return _starts_sentence(words, idx) or words[idx].start in seg_starts


def judge(clips: list[dict], words: list[Word], video_id: str, cache_path: Path, model: str) -> list[float]:
    """Judge score (0-10) per clip; cached so the baseline is judged once."""
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    keys = [hashlib.sha1(f"{JUDGE_VERSION}|{model}|{video_id}|{c['start']:.2f}|{c['end']:.2f}".encode()).hexdigest()[:16]
            for c in clips]
    todo = [i for i, k in enumerate(keys) if k not in cache]
    if todo:
        from clipforge.llm.ollama_provider import OllamaProvider

        llm = OllamaProvider(model, temperature=0.0, max_tokens=400)
        llm.seed = JUDGE_SEED  # honoured by the provider once Ollama tuning lands; harmless before
        for b in range(0, len(todo), 5):
            batch = todo[b:b + 5]
            body = "\n\n".join(f"CLIP {n + 1} ({clips[i]['end'] - clips[i]['start']:.0f} s):\n\"\"\"{clip_text(words, clips[i]['start'], clips[i]['end'])}\"\"\""
                               for n, i in enumerate(batch))
            res = llm.complete_json(JUDGE_PROMPT.format(clips=body), Ratings, retries=3, task="judge")
            by_id = {r.id: r for r in res.ratings}
            for n, i in enumerate(batch):
                r = by_id.get(n + 1)
                if r is None:
                    raise RuntimeError(f"judge skipped clip {n + 1} of a batch; re-run")
                cache[keys[i]] = round((r.hook + r.standalone + r.payoff) / 3, 3)
            cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    return [cache[k] for k in keys]


def match(a: list[dict], b: list[dict]) -> list[bool]:
    """For each clip in `a`: is there a clip in `b` covering the same moment?"""
    return [any(overlap_ratio((x["start"], x["end"]), (y["start"], y["end"])) > SAME_MOMENT for y in b) for x in a]


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--new", required=True, help="dir with <video_id>.json from bench_select.py (or one file)")
    ap.add_argument("--baseline", default=str(ROOT / "tests" / "fixtures" / "select_baseline"))
    ap.add_argument("--workspace", default=str(ROOT / "workspace"))
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--rerank", action="store_true", help="skip the overlap condition (re-rank pass)")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--judge-model", default="qwen2.5:7b")
    ap.add_argument("--md", default="")
    ap.add_argument("--noise", default=str(ROOT / "workspace" / "_bench" / "noise"),
                    help="a 2nd run of the baseline code (same settings): its agreement with the baseline is the noise "
                         "floor, and the reference scores are the mean of both runs ('' = off)")
    ap.add_argument("--strict-overlap", action="store_true",
                    help="require 80%% overlap even if the baseline's own re-run agrees less (prompt-identical changes)")
    args = ap.parse_args()

    new_path = Path(args.new)
    new_files = [new_path] if new_path.is_file() else sorted(new_path.glob("*.json"))
    ws = Path(args.workspace)
    cache_path = ws / "_bench" / "judge_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    pooled = {"base_match": [], "base_score": [], "new_score": [], "base_judge": [], "new_judge": [],
              "base_bound": [], "new_bound": [], "base_sec": 0.0, "new_sec": 0.0,
              "noise_match": [], "new_match2": [], "ref_judge": [], "ref_bound": []}
    noise_dir = Path(args.noise) if args.noise else None
    if noise_dir and (not noise_dir.is_dir() or noise_dir.resolve() == new_path.resolve()):
        noise_dir = None
    for nf in new_files:
        new = json.loads(nf.read_text(encoding="utf-8"))
        vid = new["video_id"]
        bf = Path(args.baseline) / f"{vid}.json"
        if not bf.exists():
            print(f"(no baseline for {vid}, skipped)")
            continue
        base = json.loads(bf.read_text(encoding="utf-8"))
        words, seg_starts = load_words(vid, ws)
        bc, nc = base["clips"][:args.top], new["clips"][:args.top]
        kept = match(bc, nc)
        added = [c for c, m in zip(nc, match(nc, bc)) if not m]
        bb = [on_boundary(c, words, seg_starts) for c in bc]
        nb = [on_boundary(c, words, seg_starts) for c in nc]
        bj = nj = []
        if not args.no_judge:
            bj = judge(bc, words, vid, cache_path, args.judge_model)
            nj = judge(nc, words, vid, cache_path, args.judge_model)
        pooled["base_match"] += kept
        pooled["base_score"] += [c["score"] for c in bc]
        pooled["new_score"] += [c["score"] for c in nc]
        pooled["base_judge"] += bj
        pooled["new_judge"] += nj
        pooled["base_bound"] += bb
        pooled["new_bound"] += nb
        nz_path = noise_dir / f"{vid}.json" if noise_dir else None
        if nz_path and nz_path.exists():
            zc = json.loads(nz_path.read_text(encoding="utf-8"))["clips"][:args.top]
            nz_kept = match(bc, zc)
            new2 = match(zc, nc)
            zj = judge(zc, words, vid, cache_path, args.judge_model) if not args.no_judge else []
            zb = [on_boundary(c, words, seg_starts) for c in zc]
            pooled["noise_match"] += nz_kept
            pooled["new_match2"] += kept + new2
            pooled["ref_judge"] += bj + zj
            pooled["ref_bound"] += bb + zb
            out_noise = (f"- noise floor (baseline re-run vs baseline): overlap {mean(nz_kept) * 100:.0f}%"
                         + ("" if args.no_judge else f", judge {mean(zj):.2f}")
                         + f"; new vs both runs: overlap {mean(kept + new2) * 100:.0f}%")
        else:
            out_noise = ""
            pooled["new_match2"] += kept
            pooled["ref_judge"] += bj
            pooled["ref_bound"] += bb
        pooled["base_sec"] += base["select_sec"]
        pooled["new_sec"] += new["select_sec"]
        out.append(f"### {vid} — {new['label']} vs {base['label']}")
        out.append(f"- select time: {base['select_sec']:.0f} s → {new['select_sec']:.0f} s "
                   f"(tokens in {base['prompt_tokens']} → {new['prompt_tokens']}, out {base['output_tokens']} → {new['output_tokens']})")
        out.append(f"- top-{args.top} overlap: {mean(kept) * 100:.0f}% ({sum(kept)}/{len(bc)})")
        out.append(f"- avg pipeline score: {mean([c['score'] for c in bc]):.2f} → {mean([c['score'] for c in nc]):.2f}")
        if not args.no_judge:
            out.append(f"- avg judge score: {mean(bj):.2f} → {mean(nj):.2f}")
        out.append(f"- boundary + 20–59 s: {mean(bb) * 100:.0f}% → {mean(nb) * 100:.0f}%")
        if out_noise:
            out.append(out_noise)
        if added:
            out.append("- NEW clips:")
            out += [f"  - {fmt_ts(c['start'])}–{fmt_ts(c['end'])} ({c['end'] - c['start']:.0f} s) {c['title']}" for c in added]
        dropped = [c for c, m in zip(bc, kept) if not m]
        if dropped:
            out.append("- DROPPED clips:")
            out += [f"  - {fmt_ts(c['start'])}–{fmt_ts(c['end'])} ({c['end'] - c['start']:.0f} s) {c['title']}" for c in dropped]
        out.append("")

    ov = mean(pooled["new_match2"])  # vs both baseline runs when the noise run exists
    floor = mean(pooled["noise_match"]) if pooled["noise_match"] else 1.0
    bar = 0.8 if args.strict_overlap else min(0.8, floor)
    bj, nj = mean(pooled["ref_judge"]), mean(pooled["new_judge"])
    bbp, nbp = mean(pooled["ref_bound"]), mean(pooled["new_bound"])
    conds = {
        f"overlap ≥ {bar * 100:.0f}%": True if args.rerank else ov >= bar - 1e-9,
        "judge score not lower": True if args.no_judge else nj >= bj - 1e-9,
        "boundary % not lower": nbp >= bbp - 1e-9,
    }
    verdict = "PASS" if all(conds.values()) else "FAIL"
    out.append("### Pooled")
    out.append(f"- select time: {pooled['base_sec']:.0f} s → {pooled['new_sec']:.0f} s")
    if pooled["noise_match"]:
        out.append(f"- noise floor: the baseline code re-run agrees on {floor * 100:.0f}% of its own top-{args.top}; "
                   "reference scores = mean of both baseline runs")
    out.append(f"- overlap {ov * 100:.0f}%{' (not required: re-rank)' if args.rerank else ''}; "
               f"pipeline score {mean(pooled['base_score']):.2f} → {mean(pooled['new_score']):.2f}; "
               + ("" if args.no_judge else f"judge {bj:.2f} → {nj:.2f}; ")
               + f"boundary {bbp * 100:.0f}% → {nbp * 100:.0f}%")
    out.append(f"- **{verdict}** — " + ", ".join(f"{k}: {'ok' if v else 'NO'}" for k, v in conds.items()))
    text = "\n".join(out)
    print(text)
    if args.md:
        Path(args.md).write_text(text + "\n", encoding="utf-8")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
