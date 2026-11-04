"""Benchmark the select stage on an already-processed episode WITHOUT touching its workspace.

    python scripts/bench_select.py <video_id> --label baseline [--out tests/fixtures/select_baseline/<id>.json]

Copies info/transcript/energy into workspace/_bench/<label>/<video_id>/ (so the real candidates.json and the
rendered clips are never touched and no window cache is reused), runs `pipeline.select(force=True)` with the
configured LLM (real Ollama by default) and writes: wall time, Ollama's own per-call counters (prompt/output
tokens and seconds) and the final clips. Compare two results with scripts/compare_selection.py.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clipforge import pipeline  # noqa: E402
from clipforge.config import load_config  # noqa: E402
from clipforge.workspace import Workspace  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video_id")
    ap.add_argument("--label", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--model", default="qwen2.5:7b")
    ap.add_argument("--ollama-url", default="", help="another Ollama server, e.g. a Vulkan one on :11435")
    ap.add_argument("--workspace", default=str(ROOT / "workspace"))
    args = ap.parse_args()

    src = Path(args.workspace) / args.video_id
    bench_root = Path(args.workspace) / "_bench" / args.label
    dst = bench_root / args.video_id
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for name in ("info.json", "transcript.json", "energy.json", "state.json"):
        if (src / name).exists():
            shutil.copy2(src / name, dst / name)
    state = json.loads((dst / "state.json").read_text(encoding="utf-8")) if (dst / "state.json").exists() else {}
    state.pop("select", None)
    (dst / "state.json").write_text(json.dumps(state), encoding="utf-8")

    cfg = load_config()
    cfg.llm.provider = "ollama"
    cfg.llm.model = args.model
    if args.ollama_url:
        cfg.llm.ollama_url = args.ollama_url
    holder: dict = {}
    real_get_llm = pipeline.get_llm

    def get_llm(c, required: bool = True):
        holder["llm"] = real_get_llm(c, required)
        return holder["llm"]

    pipeline.get_llm = get_llm
    ws = Workspace(bench_root, args.video_id)
    t0 = time.perf_counter()
    cands = pipeline.select(ws, cfg, force=True)
    took = time.perf_counter() - t0
    calls = getattr(holder.get("llm"), "calls", [])
    raw = json.loads(ws.llm_candidates.read_text(encoding="utf-8")) if ws.llm_candidates.exists() else []
    out = {
        "video_id": args.video_id,
        "label": args.label,
        "model": args.model,
        "created": time.strftime("%Y-%m-%d %H:%M"),
        "select_sec": round(took, 1),
        "llm_sec": round(sum(c["total_sec"] for c in calls), 1),
        "prompt_tokens": sum(c["prompt_tokens"] for c in calls),
        "output_tokens": sum(c["output_tokens"] for c in calls),
        "raw_candidates": len(raw),
        "config": {"llm": cfg.llm.model_dump(), **({"select": cfg.select.model_dump()} if hasattr(cfg, "select") else {})},
        "calls": calls,
        "clips": [{"rank": c.rank, "start": c.start, "end": c.end, "score": c.score, "topic": c.topic,
                   "title": c.title, "hook": c.hook} for c in cands],
    }
    path = Path(args.out) if args.out else bench_root / f"{args.video_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{args.video_id} [{args.label}]: select {took:.0f}s, {len(calls)} LLM calls, "
          f"{out['prompt_tokens']} prompt + {out['output_tokens']} output tokens, {len(cands)} clips -> {path}")


if __name__ == "__main__":
    main()
