"""Does posting TIME predict how well a post does, on this install's real results? (read-only)

Leave-one-out per account: hide one post, build the time curve from the others, predict the hidden post's
relative score (1.0 = the account's typical post) from its posting time; compare with "every post is typical".
  python scripts/eval_timing.py            # all accounts
Prints mean absolute error (lower = better) and rank correlation (Spearman; 0 = no signal, 1 = perfect).
"""

from __future__ import annotations

import datetime as dt
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clipforge.api import db  # noqa: E402
from clipforge.publish import channels as chs, smart  # noqa: E402


def spearman(a: list[float], b: list[float]) -> float:
    def ranks(x: list[float]) -> list[float]:
        order = sorted(range(len(x)), key=lambda i: x[i])
        r = [0.0] * len(x)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    ra, rb = ranks(a), ranks(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mb) ** 2 for y in rb))
    return cov / (va * vb) if va and vb else 0.0


def evaluate(pts: list[dict], others: list[dict] | None = None, kind: str = "podcast") -> dict:
    """pts: this account's scored posts; others: sibling accounts' posts (already weighted)."""
    if len(pts) < 6:
        return {"n": len(pts), "note": "too few posts"}
    pred, flat, truth = [], [], []
    for i, p in enumerate(pts):
        train = [x for j, x in enumerate(pts) if j != i] + list(others or [])
        c = smart.curve(train, kind) if "kind" in smart.curve.__code__.co_varnames else smart.curve(train)
        b = (p["minute"] % 1440) // smart.BIN
        pm = c["perf"][b]
        w = c["data_weight"][b] / (c["data_weight"][b] + 3.0)
        pred.append((1 - w) * 1.0 + w * (pm if pm is not None else 1.0))  # shrunk toward "typical"
        flat.append(1.0)
        truth.append(p["rel"])
    mae = sum(abs(a - b) for a, b in zip(pred, truth)) / len(truth)
    mae0 = sum(abs(a - b) for a, b in zip(flat, truth)) / len(truth)
    return {"n": len(pts), "mae_model": round(mae, 3), "mae_flat": round(mae0, 3),
            "gain_pct": round(100 * (mae0 - mae) / mae0, 1) if mae0 else 0.0, "spearman": round(spearman(pred, truth), 3)}


def main() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    accounts = chs.all_channels()
    for ch in accounts:
        pts = smart.scored_posts(ch.id, now, days=3650)
        sib = smart.sibling_posts(ch, now, days=3650) if hasattr(smart, "sibling_posts") else []
        kind = smart.content_kind(ch) if hasattr(smart, "content_kind") else "podcast"
        print(f"{ch.platform:9} {ch.title[:22]:22} own only : {evaluate(pts, None, kind)}")
        if sib:
            print(f"{'':9} {'':22} + siblings: {evaluate(pts, sib, kind)}  ({len(sib)} sibling posts)")


if __name__ == "__main__":
    main()
