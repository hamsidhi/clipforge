"""Per-stream numbers for /livestats and the Live page: clips made, posted, views of the posted ones."""

from __future__ import annotations

from sqlmodel import select

from clipforge.config import Config
from clipforge.live import store


def session_stats(cfg: Config, n: int = 5) -> list[dict]:
    from clipforge.api import db

    out = []
    for s in store.sessions(cfg, n):
        if s.get("simulated"):
            continue
        ids = [int(i) for c in s.get("clips", []) for i in (c.get("posted") or {}).values()]
        views = 0
        if ids:
            with db.session() as ses:
                perf = ses.exec(select(db.VideoPerf).where(db.VideoPerf.queue_id.in_(ids))).all()  # type: ignore[attr-defined]
            views = sum(p.views or 0 for p in perf)
        out.append({"id": s["id"], "title": f"{s.get('game_label', 'Stream')} · {s.get('started_at', '')[:16].replace('T', ' ')}",
                    "clips": len(s.get("clips", [])), "posted": sum(1 for c in s.get("clips", []) if c.get("posted")),
                    "views": views, "live": bool(s.get("live"))})
    return out
