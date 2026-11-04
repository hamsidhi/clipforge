"""What the Telegram bot does (pure-ish functions over the same pipeline/publish code the web app uses)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlmodel import select

from clipforge import pipeline
from clipforge.api import db
from clipforge.config import load_config
from clipforge.publish import channels as chs
from clipforge.publish import queue as q
from clipforge.publish.timez import as_utc, now_utc, utc_to_local


def app_url(path: str) -> str | None:
    """The phone URL (Tailscale) if remote access is set up — Telegram buttons need http(s) links."""
    try:
        from clipforge import remote

        st = remote.status()
        return f"{st.url}{path}" if st.serving and st.url else None
    except Exception:
        return None


def new_job(url: str) -> str:
    from clipforge.api.routes import JobCreate, create_job

    if "youtu" not in url:
        return "Send a YouTube link (youtube.com / youtu.be)."
    job = create_job(JobCreate(url=url))
    return f"🎬 Job {job['id']} started for {url}\nI'll message you when the clips are ready."


def status() -> str:
    lines = []
    with db.session() as s:
        running = s.exec(select(db.Job).where(db.Job.status.in_(("running", "queued")))).all()  # type: ignore[attr-defined]
    for j in running:
        lines.append(f"▶ Job {j.id} {j.title or j.source[:40]} — {j.status} {j.stage} {round((j.progress or 0) * 100)}%")
    if not running:
        lines.append("No job running.")
    lines.append(queue_next(3))
    return "\n".join(lines)


def queue_next(n: int = 5) -> str:
    zone = load_config().publishing.timezone
    up = [it for it in q.items(statuses=("queued", "uploading", "ready", "scheduled")) if it.slot_utc and as_utc(it.slot_utc) >= now_utc()]
    if not up:
        return "Nothing scheduled."
    rows = [f"{utc_to_local(it.slot_utc, zone):%a %d %b %H:%M} · {'IG' if it.platform == 'instagram' else 'YT'} · {it.title[:50]}"
            for it in up[:n]]
    return "Next posts (IST):\n" + "\n".join(rows)


def _job(job_id: int) -> db.Job:
    job = db.get_job(job_id)
    if not job or not job.video_id:
        raise ValueError(f"No job {job_id} with clips.")
    return job


def set_approved(job_id: int, ranks: list[int], approved: bool) -> int:
    job = _job(job_id)
    ws = pipeline.open_workspace(job.video_id, load_config())  # type: ignore[arg-type]
    n = 0
    with pipeline.candidates_lock(ws.video_id):
        cands = pipeline.load_candidates(ws)
        for c in cands:
            if c.rank in ranks:
                c.approved = approved
                n += 1
        pipeline.save_candidates(ws, cands)
    return n


def schedule_job(job_id: int, platform: str) -> str:
    job = _job(job_id)
    cfg = load_config()
    targets = [c for c in chs.all_channels() if not c.paused and (platform == "both" or (c.platform or "youtube") == platform)]
    if not targets:
        return f"No active {platform} account."
    targets.sort(key=lambda c: c.platform == "instagram")  # YouTube first ("Instagram X hours after YouTube")
    out = []
    for ch in targets:
        try:
            added = q.add_clips(ch.id, job.video_id, None, cfg)  # type: ignore[arg-type]
        except q.QueueError as exc:
            out.append(f"{ch.title}: {exc}")
            continue
        for it in added:
            q.generate_metadata(it.id, cfg)  # type: ignore[arg-type]
        done = q.autoschedule(ch.id, [it.id for it in added], cfg)  # type: ignore[misc]
        out.append(f"{ch.title}: {len(done)} scheduled")
    return "📅 " + "; ".join(out)


def _find_account(name: str) -> db.Channel:
    n = name.lower().lstrip("@")
    hits = [c for c in chs.all_channels() if n in (c.title or "").lower() or n in (c.handle or "").lower() or n == c.id.lower()]
    if len(hits) != 1:
        raise ValueError("Which account? " + ", ".join(c.title for c in chs.all_channels()))
    return hits[0]


def set_paused(name: str, paused: bool) -> str:
    ch = _find_account(name)
    chs.update(ch.id, paused=paused)
    return f"{'⏸ Paused' if paused else '▶ Resumed'} {ch.title}."


def dashboard() -> str:
    from clipforge.api.ig_routes import overview
    from clipforge.publish import analytics

    ov = overview()
    lines = [f"YouTube: {ov['youtube']['followers']:,} subs · {ov['youtube']['posts']} posts · {ov['youtube']['views']:,} views",
             f"Instagram: {ov['instagram']['followers']:,} followers · {ov['instagram']['posts']} posts · {ov['instagram']['views']:,} views"]
    cfg = load_config()
    for ch in chs.all_channels():
        if (ch.platform or "youtube") == "youtube":
            y = analytics.ypp_progress(ch.id, cfg.ypp, dt.date.today())
            lines.append(f"YPP {ch.title}: {y['subscribers']:,}/{y['targets']['ads']['subs']:,} subs · "
                         f"{y['shorts_views_90d']:,}/{y['targets']['ads']['shorts_views_90d']:,} Shorts views (90 d)")
    return "\n".join(lines)


def daily_summary(day: dt.date) -> str:
    zone = load_config().publishing.timezone
    posted = [it for it in q.items(statuses=("published",)) if it.published_at and utc_to_local(it.published_at, zone).date() == day]
    head = f"🌙 ClipForge — {day:%a %d %b}\nPosted today: {len(posted)} " \
           f"({sum(1 for i in posted if i.platform == 'youtube')} YouTube, {sum(1 for i in posted if i.platform == 'instagram')} Instagram)"
    earn = _earnings()
    return "\n".join([head, dashboard(), queue_next(3), f"Campaign earnings (est.): ₹{earn:,.0f}"])


def _earnings() -> float:
    try:
        from clipforge.api.routes import list_posts

        return float(sum(p.get("earnings", 0) for p in list_posts()["posts"]))
    except Exception:
        return 0.0


def top_clips(job_id: int, n: int = 3) -> tuple[str, list[dict]]:
    job = _job(job_id)
    cfg = load_config()
    ws = pipeline.open_workspace(job.video_id, cfg)  # type: ignore[arg-type]
    cands = sorted([c for c in pipeline.load_candidates(ws) if c.enabled], key=lambda c: c.score, reverse=True)[:n]
    out = []
    for c in cands:
        f = ws.find_clip(c.rank)
        prev = preview_for(f) if f else None
        out.append({"rank": c.rank, "title": c.title, "score": c.score, "preview": prev or Path("missing")})
    return job.title or job.video_id or "", out  # type: ignore[return-value]


def preview_for(clip: Path) -> Path | None:
    from clipforge.stages.preview import ensure_preview

    try:
        return ensure_preview(clip)
    except Exception:
        return None
