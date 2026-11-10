"""Post history / audit log (2026-11-10): every change to a planned post is kept forever in `HistoryEvent`.

Queue rows change and can be deleted (Clear day, deleted in Studio, …); history rows never are. Written from
`queue.save` (added / status change / time moved), `queue.delete`, `reconcile.drop_deleted` and `clear_range`.
`backfill()` rebuilds the past once from what the DB still knows (VideoPerf = every post we ever got stats for,
UploadLedger = every upload, the queue). `days()` is what the History page, the API and Telegram /history show.
Recording never raises: a history problem must not stop a publish.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from zoneinfo import ZoneInfo

from sqlmodel import select

from clipforge.api import db
from clipforge.publish.timez import as_utc, now_utc
from clipforge.utils.log import log

IST = ZoneInfo("Asia/Kolkata")
LABELS = {
    "added": "Added to the schedule", "moved": "Time changed", "queued": "Waiting for its time",
    "uploading": "Uploading", "processing": "Processing on the platform", "ready": "Ready to publish",
    "scheduled": "Uploaded + scheduled", "uploaded": "Uploaded (private/unlisted)", "published": "Published",
    "failed": "Failed", "missed": "Missed its time", "needs_manual": "Needs you", "waiting_capacity": "Waiting (upload limit)",
    "locked_private": "Locked private by the platform", "cancelled": "Cancelled", "skipped_duplicate": "Skipped (duplicate)",
    "draft": "Draft", "removed": "Removed from the schedule", "cleared": "Removed (Clear day/week)",
    "deleted_on_platform": "Deleted on the platform (Studio)", "backfill": "Imported from old records",
}


def link_of(it: db.QueueItem) -> str:
    if it.permalink:
        return it.permalink
    if it.youtube_video_id:
        return f"https://youtube.com/shorts/{it.youtube_video_id}"
    return ""


def post_id_of(it: db.QueueItem) -> str:
    if it.youtube_video_id:
        return it.youtube_video_id
    return f"ig:{it.ig_media_id}" if it.ig_media_id else ""


def record(it: db.QueueItem, event: str, detail: str = "", at: dt.datetime | None = None) -> None:
    try:
        row = db.HistoryEvent(
            at=as_utc(at) if at else now_utc(), platform=it.platform or "youtube", channel_id=it.channel_id or "",
            queue_id=it.id, clip_key=it.clip_key or "", video_id=it.video_id or "", title=(it.title or "")[:200],
            event=event, status=it.status or "", slot_utc=as_utc(it.slot_utc) if it.slot_utc else None,
            post_id=post_id_of(it), link=link_of(it), detail=(detail or it.reason or "")[:300])
        with db.session() as s:
            s.add(row)
            s.commit()
    except Exception as exc:  # never block a publish because of the log
        log.warning(f"[history] could not record {event} for queue item {it.id}: {exc}")


def on_save(old_status: str | None, old_slot: dt.datetime | None, it: db.QueueItem) -> None:
    """Called by queue.save after a commit."""
    if old_status is None:
        record(it, "added")
        return
    if old_status != it.status:
        at = None
        if it.status == "published":
            at = it.published_at or (it.slot_utc if it.slot_utc and as_utc(it.slot_utc) <= now_utc() else None)
        record(it, it.status, at=at)
    elif old_slot and it.slot_utc and as_utc(old_slot) != as_utc(it.slot_utc):
        record(it, "moved", f"{_ist(old_slot)} → {_ist(it.slot_utc)}")


def _ist(t: dt.datetime | None) -> str:
    return as_utc(t).astimezone(IST).strftime("%d %b %H:%M") if t else ""


# ----------------------------------------------------------------------------- back-fill (once)
def backfill(live_youtube: set[str] | None = None, now: dt.datetime | None = None) -> int:
    """Rebuild the history before this log existed. Runs once (a `backfill_done` marker row). Returns rows added.
    `live_youtube` = YouTube ids that still exist (from the API): an old stats row whose video is gone was deleted in
    Studio (often before it went live) → logged as deleted, not published. Stats rows dated in the future were only
    scheduled."""
    now = now or now_utc()
    with db.session() as s:
        if s.exec(select(db.HistoryEvent.id).where(db.HistoryEvent.event == "backfill_done").limit(1)).first() is not None:
            return 0
        items = {it.id: it for it in s.exec(select(db.QueueItem)).all()}
        perfs = s.exec(select(db.VideoPerf)).all()
        ledger = s.exec(select(db.UploadLedger).where(db.UploadLedger.succeeded_at.is_not(None))).all()  # type: ignore[union-attr]
        rows: list[db.HistoryEvent] = []
        for it in items.values():
            rows.append(db.HistoryEvent(at=as_utc(it.created_at), platform=it.platform or "youtube",
                                        channel_id=it.channel_id, queue_id=it.id, clip_key=it.clip_key,
                                        video_id=it.video_id, title=it.title, event="added", status=it.status,
                                        slot_utc=it.slot_utc, post_id=post_id_of(it), link=link_of(it), detail="backfill"))
        for lg in ledger:
            it = items.get(lg.queue_id or -1)
            rows.append(db.HistoryEvent(at=as_utc(lg.succeeded_at), platform=lg.platform, channel_id=lg.channel_id,  # type: ignore[arg-type]
                                        queue_id=lg.queue_id, clip_key=it.clip_key if it else "",
                                        video_id=it.video_id if it else "", title=it.title if it else "",
                                        event="scheduled" if lg.publish_at else "uploaded", status="",
                                        slot_utc=lg.publish_at, post_id=(lg.video_id if lg.platform == "youtube" else f"ig:{lg.video_id}"),
                                        link=f"https://youtube.com/shorts/{lg.video_id}" if lg.platform == "youtube" else "",
                                        detail="backfill"))
        for p in perfs:
            if not p.published_at:
                continue
            it = items.get(p.queue_id or -1)
            yt_row = (p.platform or "youtube") == "youtube"
            event, at, why = "published", as_utc(p.published_at), "backfill"
            if yt_row and live_youtube is not None and p.youtube_video_id not in live_youtube:
                event, at = "deleted_on_platform", min(at, now)
                why = f"was set for {_ist(p.published_at)} IST; deleted in YouTube Studio"
            elif at > now:
                event, at, why = "scheduled", now, f"set for {_ist(p.published_at)} IST"
            rows.append(db.HistoryEvent(at=at, platform=p.platform or "youtube", channel_id=p.channel_id,
                                        queue_id=p.queue_id, clip_key=it.clip_key if it else "",
                                        video_id=it.video_id if it else "", title=p.title, event=event,
                                        status=event, slot_utc=p.published_at if event != "published" else None,
                                        post_id=p.youtube_video_id,
                                        link=p.permalink or (f"https://youtube.com/shorts/{p.youtube_video_id}"
                                                             if yt_row else ""),
                                        detail=why))
        rows.append(db.HistoryEvent(event="backfill_done", detail="backfill"))
        for r in rows:
            s.add(r)
        s.commit()
    log.info(f"[history] back-filled {len(rows)} event(s) from the queue, upload ledger and stats")
    return len(rows)


# ----------------------------------------------------------------------------- reading
def days(n: int = 30, platform: str = "all", channel_id: str | None = None, now: dt.datetime | None = None) -> list[dict]:
    """Newest day first: {date, posts: [published that IST day, with current stats], events: [every change]}."""
    now = now or now_utc()
    since = now - dt.timedelta(days=n)
    backfill()
    with db.session() as s:
        q = select(db.HistoryEvent).where(db.HistoryEvent.at >= since, db.HistoryEvent.event != "backfill_done")
        if platform in ("youtube", "instagram"):
            q = q.where(db.HistoryEvent.platform == platform)
        if channel_id:
            q = q.where(db.HistoryEvent.channel_id == channel_id)
        evs = s.exec(q.order_by(db.HistoryEvent.at.desc())).all()  # type: ignore[attr-defined]
        perf = {p.youtube_video_id: p for p in s.exec(select(db.VideoPerf)).all()}
        names = {c.id: c.title for c in s.exec(select(db.Channel)).all()}
    by_day: dict[str, dict] = defaultdict(lambda: {"posts": [], "events": []})
    seen_posts: set[str] = set()
    for e in evs:
        day = as_utc(e.at).astimezone(IST).date().isoformat()
        d = by_day[day]
        d["events"].append({
            "at": as_utc(e.at).isoformat(), "time": as_utc(e.at).astimezone(IST).strftime("%H:%M"), "event": e.event,
            "label": LABELS.get(e.event, e.event.replace("_", " ").capitalize()), "platform": e.platform,
            "account": names.get(e.channel_id, e.channel_id), "title": e.title, "detail": "" if e.detail == "backfill" else e.detail,
            "slot": _ist(e.slot_utc), "link": e.link, "queue_id": e.queue_id})
        key = e.post_id or f"q{e.queue_id}:{e.platform}"
        if e.event == "published" and key not in seen_posts and as_utc(e.at) <= now:
            seen_posts.add(key)
            p = perf.get(e.post_id) if e.post_id else None
            d["posts"].append({
                "time": as_utc(e.at).astimezone(IST).strftime("%H:%M"), "platform": e.platform,
                "account": names.get(e.channel_id, e.channel_id), "title": e.title or (p.title if p else ""),
                "link": e.link or (p.permalink if p else ""), "views": p.views if p else None,
                "likes": p.likes if p else None, "comments": p.comments if p else None, "queue_id": e.queue_id})
    out = []
    for day in sorted(by_day, reverse=True):
        d = by_day[day]
        d["posts"].sort(key=lambda x: x["time"])
        out.append({"date": day, "posts": d["posts"], "events": d["events"],
                    "published": len(d["posts"]),
                    "views": sum(x["views"] or 0 for x in d["posts"])})
    return out
