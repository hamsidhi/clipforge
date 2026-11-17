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
    from clipforge.services.jobs import JobCreate, create_job

    if "youtu" not in url:
        return "Send a YouTube link (youtube.com / youtu.be)."
    job = create_job(JobCreate(url=url), remote=True)
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
    up.sort(key=lambda it: it.slot_utc)
    names = _names()
    rows = [f"{utc_to_local(it.slot_utc, zone):%a %d %b %H:%M} · {'IG' if it.platform == 'instagram' else 'YT'} "
            f"{names.get(it.channel_id, '')} · {it.title[:45]}" for it in up[:n]]
    return "Next posts (IST):\n" + "\n".join(rows)


def _job(job_id: int) -> db.Job:
    job = db.get_job(job_id)
    if not job or not job.video_id:
        raise ValueError(f"No job {job_id} with clips.")
    return job


def set_approved(job_id: int, ranks: list[int], approved: bool) -> int:
    from clipforge.services.clips import set_approved as _set

    return _set(job_id, ranks, approved)


def schedule_job(job_id: int, platform: str) -> str:
    from clipforge.notify.tg_publish import _schedule_job

    return _schedule_job(job_id, platform)


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


def _acct_label(a: dict) -> str:
    plat = "YouTube" if a["platform"] == "youtube" else "Instagram"
    tag = " · gaming" if a.get("content") == "gaming" else ""
    return f"{'▶️' if a['platform'] == 'youtube' else '📸'} {a['title'] or a['handle'] or a['id']} ({plat}{tag})"


def _delta(n: int | None) -> str:
    return f" ({'+' if n > 0 else ''}{n:,} this week)" if n else ""


def account_lines(a: dict, posted_today: int | None = None, ypp: dict | None = None) -> list[str]:
    """One account's block. Built from overview()['accounts'], so a newly added account shows up by itself."""
    unit = "subs" if a["platform"] == "youtube" else "followers"
    lines = [_acct_label(a)]
    if not a.get("ok"):
        why = "paused" if a.get("paused") else "login needed" if a.get("auth_status") != "ok" else "not confirmed yet"
        lines.append(f"   ⚠️ {why}: nothing will post until it's fixed")
    first = f"   {a['followers']:,} {unit}{_delta(a.get('followers_7d'))}"
    if posted_today is not None:
        first += f" · posted today: {posted_today}"
    lines.append(first)
    lines.append(f"   {a['posts']} posts · {a['views']:,} views · {a['avg_views']:,} avg/post · last 7 days: "
                 f"{a['posts_7d']} posts, {a['views_7d']:,} views")
    if ypp:
        lines.append(f"   YPP: {ypp['subscribers']:,}/{ypp['targets']['ads']['subs']:,} subs · "
                     f"{ypp['shorts_views_90d']:,}/{ypp['targets']['ads']['shorts_views_90d']:,} Shorts views (90 d)")
    if a.get("next_slot"):
        nxt = dt.datetime.fromisoformat(a["next_slot"])
        lines.append(f"   Next: {nxt:%a %d %b %H:%M} · {a['planned']} planned")
    else:
        lines.append("   Next: nothing planned")
    if a.get("attention"):
        lines.append(f"   🔔 {a['attention']} post(s) need you (Schedule → Needs you)")
    return lines


def dashboard(posted_by_account: dict[str, int] | None = None) -> str:
    """Totals + one block per connected account (YouTube and Instagram, any number of them)."""
    from clipforge.api.ig_routes import overview
    from clipforge.publish import analytics

    ov = overview()
    cfg = load_config()
    yt, ig = ov["youtube"], ov["instagram"]
    lines = [f"Total: {yt['accounts']} YouTube + {ig['accounts']} Instagram accounts · "
             f"{yt['followers'] + ig['followers']:,} followers · {yt['posts'] + ig['posts']} posts · "
             f"{yt['views'] + ig['views']:,} views"]
    for a in sorted(ov["accounts"], key=lambda a: (a["platform"] != "youtube", (a["title"] or "").lower())):
        ypp = None
        if a["platform"] == "youtube":
            try:
                ypp = analytics.ypp_progress(a["id"], cfg.ypp, dt.date.today())
            except Exception:
                ypp = None
        lines.append("")
        lines.extend(account_lines(a, None if posted_by_account is None else posted_by_account.get(a["id"], 0), ypp))
    return "\n".join(lines)


def _names() -> dict[str, str]:
    return {c.id: (c.title or c.handle or c.id) for c in chs.all_channels()}


def daily_summary(day: dt.date) -> str:
    zone = load_config().publishing.timezone
    posted = [it for it in q.items(statuses=("published",)) if it.published_at and utc_to_local(it.published_at, zone).date() == day]
    by_acct: dict[str, int] = {}
    for it in posted:
        by_acct[it.channel_id] = by_acct.get(it.channel_id, 0) + 1
    head = f"🌙 ClipForge — {day:%a %d %b}\nPosted today: {len(posted)} " \
           f"({sum(1 for i in posted if i.platform == 'youtube')} YouTube, {sum(1 for i in posted if i.platform == 'instagram')} Instagram)"
    earn = _earnings()
    return "\n".join([head, "", dashboard(by_acct), "", queue_next(5), "",
                      f"Campaign earnings (est.): ₹{earn:,.0f}", bot_activity(day)])


def bot_activity(day: dt.date) -> str:
    """"What the bot did today" — from the audit log (every command and button press is logged)."""
    start = dt.datetime.combine(day, dt.time(0), tzinfo=dt.timezone.utc) - dt.timedelta(hours=5, minutes=30)  # IST midnight
    with db.session() as s:
        rows = s.exec(select(db.AuditEvent).where(db.AuditEvent.kind == "telegram", db.AuditEvent.at >= start)).all()
    ok = [r for r in rows if r.detail.endswith(" ok")]
    confirmed = [r for r in rows if ": confirmed " in r.detail]
    failed = [r for r in rows if r.detail.endswith(" failed")]
    return (f"🤖 Bot today: {len(ok)} commands/buttons, {len(confirmed)} confirmed actions"
            + (f", {len(failed)} errors" if failed else "") + ".")


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
