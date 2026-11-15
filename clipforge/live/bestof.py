"""After the stream: a 5–10 min horizontal "best of" from the stream's moments (their original 16:9 footage), in
chronological order, rendered at normal priority (the stream is over). Output: workspace/live_<…>/best_of.mp4.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from clipforge.config import Config
from clipforge.live import store
from clipforge.utils.ffmpeg import ffmpeg_bin


def pick(clips: list[dict], max_sec: float = 600) -> list[dict]:
    good = [c for c in clips if c.get("status") != "discarded" and c.get("source") and Path(c["source"]).exists()]
    good.sort(key=lambda c: -c.get("score", 0))
    out, total = [], 0.0
    for c in good:
        if total + c.get("duration", 0) > max_sec and out:
            continue
        out.append(c)
        total += c.get("duration", 0)
    return sorted(out, key=lambda c: c.get("trigger", 0))


def make_best_of(cfg: Config, sid: str, max_sec: float = 600) -> Path:
    sess = store.load_session(cfg, sid)
    chosen = pick(sess.get("clips", []), max_sec)
    if not chosen:
        raise ValueError("No clip footage left for this stream (replay files deleted?)")
    out = cfg.workspace / sid / "best_of.mp4"
    args = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    for c in chosen:
        args += ["-ss", f"{c.get('source_start', 0):.3f}", "-t", f"{c['duration']:.3f}", "-i", c["source"]]
    n = len(chosen)
    chains = [f"[{i}:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
              f"fps=30,setsar=1,format=yuv420p[v{i}];[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]"
              for i in range(n)]
    graph = ";".join(chains) + ";" + "".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
    args += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium", "-crf", "21",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"best-of render failed: {r.stderr[-500:]}")
    sess = store.load_session(cfg, sid)
    sess["best_of"] = out.name
    store.save_session(cfg, sid, sess)
    return out
