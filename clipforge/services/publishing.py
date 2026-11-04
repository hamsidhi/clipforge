"""Publishing actions shared by the web app, Telegram and the CLI: queue fixes, add/schedule clips, health, stats.

Safety rules live in the publish/ modules these call (private + publishAt by default, paused/unconfirmed accounts never
upload, daily quota, one claim per clip). Nothing here bypasses them.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlmodel import select

from clipforge.api import db
from clipforge.config import load_config
from clipforge.publish import batch, slots
from clipforge.publish import channels as chs
from clipforge.publish import queue as q
from clipforge.publish import quota, states
from clipforge.publish import youtube as yt
from clipforge.publish.timez import as_utc, now_utc, utc_to_local
from clipforge.services.errors import BadRequest, Conflict, NotFound

UNSAFE_BULK = ("interrupted", "batched_in_api", "assisted_unlinked", "not_scheduled_in_studio")


# ============================================================================ lookups
def ch_or_404(channel_id: str) -> db.Channel:
    ch = chs.get(channel_id)
    if not ch:
        raise NotFound("Unknown channel")
    return ch


def item_or_404(item_id: int) -> db.QueueItem:
    it = q.get(item_id)
    if not it:
        raise NotFound("Unknown queue item")
    return it


def zone(channel_id: str | None = None) -> str:
    if channel_id:
        ch = chs.get(channel_id)
        if ch:
            return chs.settings_of(ch)["timezone"]
    return load_config().publishing.timezone


def item_out(it: db.QueueItem) -> dict:
    from clipforge.publish.worker import worker

    d = q.public(it, zone(it.channel_id))
    d["problems"] = q.problems(it) if it.status in ("draft", "queued", "needs_manual") and it.meta_status == "ready" else []
    d["uploading_progress"] = worker.upload_progress.get(it.id or -1)
    return d


def wrap(fn, *a, **kw):
    """publish/ errors → service errors with the same sentence."""
    try:
        return fn(*a, **kw)
    except q.QueueError as exc:
        raise BadRequest(str(exc))
    except FileNotFoundError as exc:
        raise BadRequest(str(exc))
    except yt.NeedsReauth as exc:
        raise Conflict(str(exc))


# ============================================================================ queue fixes ("Needs you" buttons)
def do_reschedule(item_id: int) -> db.QueueItem:
    return q.reschedule_next(item_id)


def do_retry(item_id: int) -> db.QueueItem:
    it = item_or_404(item_id)
    if it.status not in ("failed", "missed", "needs_manual", "queued") or it.youtube_video_id or it.ig_media_id:
        raise q.QueueError("Only failed or missed clips that aren't on the platform yet can be retried.")
    if it.slot_utc and as_utc(it.slot_utc) < now_utc() + slots.LEAD:
        return q.reschedule_next(item_id)  # the old time is gone: next free slot
    it.status, it.reason, it.error, it.attempts, it.next_attempt_at = "queued", "", "", 0, None
    it.ig_container_id, it.ig_container_at = "", None
    return q.save(it)


def do_unbatch(item_id: int) -> db.QueueItem:
    it = item_or_404(item_id)
    if it.youtube_video_id or it.ig_media_id or it.status in q.ON_PLATFORM:
        raise q.QueueError("Already uploaded — nothing to change.")
    it.batch, it.reason, it.error = "", "", ""
    if it.status in ("needs_manual", "missed"):
        if it.slot_utc and as_utc(it.slot_utc) < now_utc() + slots.LEAD:
            q.save(it)
            return q.reschedule_next(item_id)
        it.status = "queued"
    return q.save(it)


def do_publish_now(item_id: int) -> db.QueueItem:
    from clipforge.publish.worker import run_scheduler, worker

    require_publisher()
    it = item_or_404(item_id)
    if it.youtube_video_id or it.ig_media_id or it.status in q.ON_PLATFORM:
        raise q.QueueError("Already uploaded — publish it on the platform (YouTube Studio / Instagram).")
    if it.platform == "youtube" and load_config().publishing.mode != "api":
        raise q.QueueError("Publish now uploads with the API — switch to API mode, or upload it in YouTube Studio.")
    ch = chs.get(it.channel_id)
    if ch and ch.paused:
        raise q.QueueError(f"{ch.title} is paused — resume it first.")
    now = now_utc()
    it.slot_utc, it.status, it.reason, it.error, it.batch = now, "queued", "", "", ""
    it.attempts, it.next_attempt_at = 0, None
    it.ig_container_id, it.ig_container_at = "", None  # a fresh Instagram container (the old one may be stale)
    if it.platform == "youtube":
        it.privacy = "public"
    q.save(it)
    worker.submit("run", "Scheduler run", run_scheduler)
    return it


FIXES = {"reschedule": do_reschedule, "retry": do_retry, "unbatch": do_unbatch, "publish_now": do_publish_now}


def fix(item_id: int, action: str) -> db.QueueItem:
    """One-click fix for a "Needs you" item (same as the web app's buttons)."""
    if action not in FIXES:
        raise BadRequest(f"'{action}' can't be done from here.")
    return wrap(FIXES[action], item_id)


def require_publisher() -> None:
    """Exactly one machine publishes: a laptop in worker mode never uploads (the server does)."""
    mode = load_config().deployment.mode
    if mode == "worker":
        raise Conflict("This computer is a worker — the server publishes. Do this on the server (or in Telegram).")


# ============================================================================ add + schedule
def postable_accounts(platform: str = "both") -> list[db.Channel]:
    """Accounts that may receive new posts: confirmed, not paused, login OK."""
    out = []
    for c in chs.all_channels():
        p = c.platform or "youtube"
        if platform not in ("both", p):
            continue
        if c.paused or not c.confirmed or c.auth_status != "ok":
            continue
        out.append(c)
    out.sort(key=lambda c: c.platform == "instagram")  # YouTube first ("Instagram X hours after YouTube")
    return out


def queue_add(channel_ids: list[str], video_id: str, ranks: list[int] | None, background_meta: bool = True) -> list[db.QueueItem]:
    if not channel_ids:
        raise BadRequest("Pick at least one account")
    for cid in channel_ids:
        ch_or_404(cid)
    added: list[db.QueueItem] = []
    for cid in channel_ids:
        added += wrap(q.add_clips, cid, video_id, ranks)
    if background_meta:
        from clipforge.publish.worker import worker

        for it in added:
            worker.submit(f"meta:{it.id}", f"Metadata for queue item {it.id}", lambda i=it.id: q.generate_metadata(i).title)
    return added


def schedule_clips(video_id: str, ranks: list[int] | None, channel_ids: list[str],
                   at_utc: dt.datetime | None = None) -> dict[str, list[db.QueueItem]]:
    """Add clips to accounts, write metadata now, then auto-slot them (or put them at `at_utc`)."""
    cfg = load_config()
    out: dict[str, list[db.QueueItem]] = {}
    for cid in channel_ids:
        ch = ch_or_404(cid)
        if ch.paused or not ch.confirmed:
            raise Conflict(f"{ch.title} is {'paused' if ch.paused else 'not confirmed yet'} — fix that on the Accounts page first.")
        added = queue_add([cid], video_id, ranks, background_meta=False)
        for it in added:
            q.generate_metadata(it.id, cfg)  # type: ignore[arg-type]
        if at_utc is not None:
            done = [wrap(q.move, it.id, at_utc + dt.timedelta(minutes=5 * i)) for i, it in enumerate(added)]
        else:
            done = wrap(q.autoschedule, cid, [it.id for it in added], cfg)
        out[cid] = done
    return out


def set_paused(channel_id: str, paused: bool) -> db.Channel:
    ch_or_404(channel_id)
    return chs.update(channel_id, paused=paused)  # type: ignore[return-value]


def make_batch(channel_id: str, n: int | None = None) -> dict:
    ch_or_404(channel_id)
    res = wrap(batch.prepare, channel_id, n)
    res["url"] = f"/api/batches/files/{res['relative']}/batch.html"
    return res


# ============================================================================ views
def upcoming(days: int = 7) -> list[db.QueueItem]:
    end = now_utc() + dt.timedelta(days=days)
    return [it for it in q.items(statuses=("queued", "uploading", "ready", "scheduled"))
            if it.slot_utc and now_utc() <= as_utc(it.slot_utc) <= end]


def needs_you() -> list[dict]:
    """Every item that needs the user, with its reason sentence and the fix actions (states.REASONS)."""
    out = []
    for it in q.items(statuses=states.ATTENTION):
        ch = chs.get(it.channel_id)
        out.append({"item": it, "account": ch.title if ch else it.channel_id, "label": states.label(it.reason) or it.error[:120],
                    "actions": states.actions(it.reason) or (["retry"] if it.status == "failed" else [])})
    return out


def token_health(ch: db.Channel) -> dict:
    """Read-only look at the saved token (no network): present, refresh token, expiry."""
    if ch.platform == "instagram":
        from clipforge.publish import instagram as ig

        return ig.token_health(ch)
    p = yt.token_path(ch.id)
    if not p.exists():
        return {"ok": False, "text": "No saved login — Reconnect."}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {"ok": False, "text": "Saved login is unreadable — Reconnect."}
    if ch.auth_status != "ok":
        return {"ok": False, "text": ch.auth_error or "Needs re-login."}
    if not data.get("refresh_token"):
        return {"ok": False, "text": "No refresh token — Reconnect."}
    return {"ok": True, "text": "Login OK (refreshes automatically)."}


def health() -> dict:
    from clipforge.publish import runner
    from clipforge.publish.worker import worker

    cfg = load_config()
    st = runner.read_state(cfg)
    rem = quota.remaining(cfg)
    accounts = []
    for ch in chs.all_channels():
        attention = [it for it in q.items(ch.id, states.ATTENTION)]
        accounts.append({"id": ch.id, "title": ch.title, "platform": ch.platform, "paused": ch.paused,
                         "token": token_health(ch), "attention": len(attention)})
    try:
        from clipforge.utils import power

        awake = power.status()
    except Exception:
        awake = {"mode": "unknown", "holding": False}
    tasks = []
    try:
        from clipforge.cli_publish import task_status

        tasks = task_status()
    except Exception:
        pass
    return {
        "last_run_at": st.get("last_run_at"), "last_source": st.get("last_source"), "last_result": st.get("last_result", {}),
        "runs": st.get("runs", {}), "next_run_at": worker.next_tick_at, "ticker": worker.ticker_on,
        "mode": cfg.publishing.mode, "quota": rem, "audit_status": cfg.publishing.audit_status,
        "api_audited": cfg.publishing.api_audited, "accounts": accounts, "keep_awake": awake, "tasks": tasks,
        "server_time": now_utc().isoformat(), "disk": disk_usage(), "deployment": cfg.deployment.mode,
    }


def disk_usage() -> dict:
    import shutil

    ws = load_config().workspace
    ws.mkdir(parents=True, exist_ok=True)
    u = shutil.disk_usage(ws)
    return {"total": u.total, "used": u.used, "free": u.free, "pct": round(100 * u.used / u.total, 1) if u.total else 0}


def reminders(channel_id: str | None = None) -> list[dict]:
    """Published posts not yet submitted to a campaign."""
    with db.session() as s:
        posts = list(s.exec(select(db.Post)).all())
    submitted = {p.queue_id for p in posts if p.queue_id and p.submission_status != "not_submitted"}
    out = []
    for it in q.items(channel_id, ("published",)):
        if it.id not in submitted:
            url = it.permalink if it.platform == "instagram" else f"https://youtube.com/shorts/{it.youtube_video_id}"
            out.append({"queue_id": it.id, "title": it.title, "channel_id": it.channel_id, "platform": it.platform,
                        "youtube_url": url, "url": url, "published_at": it.published_at})
    return out


def mark_submitted(queue_id: int) -> db.Post:
    """Campaign tracker: create/update the Post row of a published queue item as submitted."""
    it = item_or_404(queue_id)
    if it.status != "published":
        raise Conflict("Only published posts can be submitted to a campaign.")
    with db.session() as s:
        p = s.exec(select(db.Post).where(db.Post.queue_id == queue_id)).first()
        if not p:
            url = it.permalink if it.platform == "instagram" else f"https://youtube.com/shorts/{it.youtube_video_id}"
            p = db.Post(queue_id=queue_id, clip_title=it.title, platform=it.platform, url=url,
                        youtube_video_id=it.youtube_video_id,
                        posted_at=(it.published_at.date() if it.published_at else None))
        p.submission_status = "submitted"
        s.add(p)
        s.commit()
        s.refresh(p)
        return p


def stats(days: int = 7) -> dict[str, dict[str, Any]]:
    """Per platform: posts published in the window and their latest views / likes."""
    since = now_utc() - dt.timedelta(days=days)
    out: dict[str, dict[str, Any]] = {}
    with db.session() as s:
        perfs = list(s.exec(select(db.VideoPerf)).all())
    for p in perfs:
        if p.published_at and as_utc(p.published_at) < since:
            continue
        d = out.setdefault(p.platform or "youtube", {"posts": 0, "views": 0, "likes": 0, "comments": 0})
        d["posts"] += 1
        d["views"] += p.views
        d["likes"] += p.likes
        d["comments"] += p.comments
    return out


def top(n: int = 5, days: int = 28) -> list[db.VideoPerf]:
    since = now_utc() - dt.timedelta(days=days)
    with db.session() as s:
        perfs = [p for p in s.exec(select(db.VideoPerf)).all() if not p.published_at or as_utc(p.published_at) >= since]
    return sorted(perfs, key=lambda p: p.views, reverse=True)[:n]


def earnings() -> dict:
    from clipforge.publish import analytics

    with db.session() as s:
        posts = list(s.exec(select(db.Post)).all())
    total = sum(analytics.earnings(p) for p in posts)
    pending = sum(analytics.earnings(p) for p in posts if p.submission_status in ("not_submitted", "submitted", "approved"))
    return {"total": round(total, 2), "unpaid": round(pending, 2), "posts": len(posts)}


def when_local(it: db.QueueItem) -> str:
    return utc_to_local(it.slot_utc, zone(it.channel_id)).strftime("%a %d %b %H:%M") if it.slot_utc else "not scheduled"
