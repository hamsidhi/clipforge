"""Stage 3b — camera-cut detection with PySceneDetect (ContentDetector).

Cuts are only needed to reframe clips, so we detect them inside clip ranges (plus a margin)
instead of decoding a whole 2-hour video. Covered ranges are cached in scenes.json and only
new ranges are analysed when a clip is moved in the editor.
"""

from __future__ import annotations

import json

from clipforge.models import Scene
from clipforge.utils.log import log
from clipforge.workspace import Workspace

MARGIN = 2.0


def _merge(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _subtract(ranges: list[tuple[float, float]], covered: list[tuple[float, float]]) -> list[tuple[float, float]]:
    todo: list[tuple[float, float]] = []
    for a, b in ranges:
        segs = [(a, b)]
        for ca, cb in covered:
            nxt = []
            for sa, sb in segs:
                if cb <= sa or ca >= sb:
                    nxt.append((sa, sb))
                else:
                    if sa < ca:
                        nxt.append((sa, ca))
                    if cb < sb:
                        nxt.append((cb, sb))
            segs = nxt
        todo += [(sa, sb) for sa, sb in segs if sb - sa > 0.5]
    return todo


def _load(ws: Workspace) -> dict:
    if ws.scenes.exists():
        return json.loads(ws.scenes.read_text(encoding="utf-8"))
    return {"covered": [], "cuts": []}


def detect_cuts(ws: Workspace, start: float, end: float, threshold: float = 27.0) -> list[float]:
    from scenedetect import ContentDetector, detect

    scenes = detect(str(ws.source), ContentDetector(threshold=threshold, min_scene_len=15),
                    start_time=float(start), end_time=float(end))
    cuts = [s[0].seconds for s in scenes[1:]]  # first scene starts at `start`, not a cut
    return [round(float(c), 3) for c in cuts]


def ensure(ws: Workspace, ranges: list[tuple[float, float]], duration: float) -> list[float]:
    """Make sure cuts are known for all `ranges`; return all known cut times."""
    data = _load(ws)
    covered = [tuple(r) for r in data["covered"]]
    want = _merge([(max(0.0, a - MARGIN), min(duration, b + MARGIN)) for a, b in ranges])
    todo = _subtract(want, covered)  # type: ignore[arg-type]
    if todo:
        log.info(f"Detecting camera cuts in {len(todo)} range(s) ({sum(b - a for a, b in todo):.0f}s of video)")
    cuts = set(data["cuts"])
    for a, b in todo:
        try:
            cuts.update(detect_cuts(ws, a, b))
        except Exception as exc:
            log.warning(f"Scene detection failed for {a:.0f}-{b:.0f}s ({exc}); treating it as one shot")
        covered.append((a, b))
    data = {"covered": _merge(covered), "cuts": sorted(cuts)}  # type: ignore[arg-type]
    ws.write_json(ws.scenes, data)
    return data["cuts"]


def shots_for(cuts: list[float], start: float, end: float, min_len: float = 0.4) -> list[Scene]:
    """Split [start, end] at cuts -> shots in CLIP-RELATIVE time. Tiny shots merge into neighbours."""
    inner = [c - start for c in cuts if start + min_len < c < end - min_len]
    bounds = [0.0, *inner, end - start]
    shots = [Scene(start=round(a, 3), end=round(b, 3)) for a, b in zip(bounds, bounds[1:]) if b - a > 0]
    merged: list[Scene] = []
    for s in shots:
        if merged and (s.end - s.start) < min_len:
            merged[-1].end = s.end
        else:
            merged.append(s)
    return merged
