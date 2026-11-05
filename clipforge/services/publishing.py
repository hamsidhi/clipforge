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
from clipforge.publish import modes, quota, states
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
    d["status_text"] = status_text(it, progress=d["uploading_progress"])
    return d


def account_mode(ch: db.Channel | None) -> modes.Mode:
    if ch is None:
        return modes.Mode("assisted", "default")
    return modes.resolve(ch)


def status_text(it: db.QueueItem, progress: float | None = None) -> dict:
    """The ONE wording of an item's state (web app + Telegram): states.status_text with the account's mode."""
    ch = chs.get(it.channel_id)
    return states.status_text(it, zone(it.channel_id), account_mode(ch).mode, progress)


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
    """Upload + publish right away (YouTube: public; Instagram: next tick). The web app and Telegram both ask for an
    explicit confirmation first. Never for a clip that is already on the platform (we can't change it there)."""
    from clipforge.publish.worker import run_scheduler, worker

    require_publisher()
    it = item_or_404(item_id)
    if it.youtube_video_id or it.ig_media_id or it.status in q.ON_PLATFORM:
        raise q.QueueError("Already uploaded — publish it on the platform (YouTube Studio / Instagram).")
    ch = chs.get(it.channel_id)
    mode = account_mode(ch)
    if not mode.auto:
        raise q.QueueError("This account is in assisted mode: Publish now uploads with the API. Switch the account to "
                           "auto (Accounts page), or upload it yourself.")
    if mode.problem:
        raise q.QueueError(mode.problem)
    p = load_config().publishing
    if it.platform == "youtube" and p.unverified_lock and not p.api_audited:
        raise q.QueueError("Your API project is set as locked (unverified_lock) and not audited: a public upload would be "
                           "locked private. Upload it in YouTube Studio instead.")
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


def set_account_mode(channel_id: str, mode: str) -> db.Channel:
    """Per-account mode: 'auto' | 'assisted' | '' (follow the global default). Explicit choice beats the default."""
    import json as _json

    if mode not in ("auto", "assisted", ""):
        raise BadRequest("Mode must be auto, assisted or empty (default).")
    ch = ch_or_404(channel_id)
    if mode == "auto" and ch.platform == "instagram":
        from clipforge.publish import instagram as ig

        if ig.is_manual(ch):
            raise BadRequest("This Instagram account was added manually (no Facebook Login): ClipForge can't post to it. "
                             "Connect it with Facebook Login first (Accounts → Add Instagram account).")
    settings = dict(ch.settings)
    if mode:
        settings["mode"] = mode
    else:
        settings.pop("mode", None)
    return chs.update(channel_id, settings_json=_json.dumps(settings))  # type: ignore[return-value]


def do_switch_assisted(item_id: int) -> db.QueueItem:
    """'Switch to assisted' on an item whose account can't do auto: the whole account goes assisted."""
    it = item_or_404(item_id)
    set_account_mode(it.channel_id, "assisted")
    if it.status == "failed" and it.reason == "auto_unavailable":
        it.status, it.reason, it.error = "queued", "", ""
        it = q.save(it)
    return it


def do_switch_auto(item_id: int) -> db.QueueItem:
    it = item_or_404(item_id)
    set_account_mode(it.channel_id, "auto")
    return it


def do_studio_instead(item_id: int) -> db.QueueItem:
    """Assisted for THIS clip only (e.g. locked private, or the channel's upload limit): put it in a Studio batch.
    A locked video's id is kept in yt_state (history) — the new Studio upload is linked by Sync later."""
    import json as _json

    from clipforge.publish import batch

    it = item_or_404(item_id)
    if it.platform != "youtube":
        raise q.QueueError("Only for YouTube clips.")
    if it.status == "published":
        raise q.QueueError("Already published.")
    if it.status not in ("failed", "missed", "locked_private", "needs_manual", "queued"):
        raise q.QueueError("Only for clips that couldn't be published automatically.")
    if it.youtube_video_id:
        if it.status != "locked_private":
            raise q.QueueError("Already on YouTube — change it in YouTube Studio.")
        st = it.yt_state
        st["replaced_video_id"] = it.youtube_video_id
        it.yt_state_json, it.youtube_video_id = _json.dumps(st), ""
    it.status, it.reason, it.error, it.batch = "queued", "", "", ""
    if it.slot_utc and as_utc(it.slot_utc) < now_utc() + slots.LEAD:
        q.save(it)
        it = q.reschedule_next(item_id)
    q.save(it)
    batch.prepare(it.channel_id, ids=[item_id], force=True)
    it = item_or_404(item_id)
    it.status, it.reason = "needs_manual", "assisted_batch"
    it.error = "Upload this clip in YouTube Studio from its batch folder (Open batch), then Sync with YouTube."
    return q.save(it)


FIXES = {"reschedule": do_reschedule, "retry": do_retry, "unbatch": do_unbatch, "publish_now": do_publish_now,
         "switch_assisted": do_switch_assisted, "switch_auto": do_switch_auto, "studio_instead": do_studio_instead}


def fix(item_id: int, action: str) -> db.QueueItem:
    """One-click fix for a "Needs you" item (same as the web app's buttons)."""
    if action not in FIXES:
        raise BadRequest(f"'{action}' can't be done from here.")
    return wrap(FIXES[action], item_id)


def require_publisher() -> None:
    """Exactly one machine publishes: a laptop in worker mode never uploads (the server does); a fenced server neither."""
    from clipforge.server.dispatch import publishing_allowed

    ok, why = publishing_allowed()
    if not ok:
        raise Conflict(f"Not publishing from this computer: {why}. Use the server's app or Telegram.")


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


# reasons that resolve themselves — never a "Needs you" (Publishing v2)
SELF_HEALING = ("retrying", "quota", "channel_limit", "safety_limit", "upload_limit", "duplicate", "ig_limit",
                "ig_find_media")


def needs_you_accounts() -> list[dict]:
    """Account-level human actions only: reconnect, missing Page link / permissions, audit problems."""
    from clipforge.publish import instagram as ig

    cfg = load_config()
    out = []
    for ch in chs.all_channels():
        if ch.auth_status == "needs_reauth":
            out.append({"account": ch.title, "platform": ch.platform, "action": "reconnect",
                        "text": f"Reconnect {ch.title} (login refused). Laptop → Accounts → Reconnect."})
            continue
        m = modes.resolve(ch, cfg)
        if m.auto and m.problem:
            out.append({"account": ch.title, "platform": ch.platform, "action": "reconnect", "text": m.problem})
        elif ch.platform == "instagram" and m.auto and not ig.is_manual(ch):
            e = ig.eligibility(ch, cfg)
            if not e["auto_ready"]:
                out.append({"account": ch.title, "platform": "instagram", "action": "reconnect", "text": e["summary"]})
    if cfg.publishing.audit_status == "rejected":
        out.append({"account": "YouTube API", "platform": "youtube", "action": "audit_info",
                    "text": "The YouTube API audit was rejected: uploads keep the default quota. See Settings → Publishing."})
    return out


def needs_you() -> list[dict]:
    """Items that need a HUMAN action, with the reason sentence and the fix actions (states.REASONS). Routine waits
    and automatic retries are not listed."""
    out = []
    for it in q.items(statuses=states.ATTENTION):
        if it.reason in SELF_HEALING:
            continue
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
        accounts.append(account_summary(ch, cfg) | {"attention": len(attention)})
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
    plan_today = " / ".join(f"{'IG' if a['platform'] == 'instagram' else 'YT'} {a['plan'][0]['count']}"
                            for a in accounts if a.get("plan") and not a["paused"])
    return {
        "heartbeat": heartbeat(st, worker.next_tick_at, worker.ticker_on, tasks),
        "db": db_status(),
        "plan_today": plan_today,
        "last_run_at": st.get("last_run_at"), "last_source": st.get("last_source"), "last_result": st.get("last_result", {}),
        "runs": st.get("runs", {}), "next_run_at": worker.next_tick_at, "ticker": worker.ticker_on,
        "mode": cfg.publishing.mode, "quota": rem, "audit_status": cfg.publishing.audit_status,
        "api_audited": cfg.publishing.api_audited, "accounts": accounts, "keep_awake": awake, "tasks": tasks,
        "server_time": now_utc().isoformat(), "disk": disk_usage(), "deployment": cfg.deployment.mode,
    }


def db_status() -> dict:
    try:
        with db.session() as s:
            s.exec(select(db.QueueItem.id).limit(1)).first()
        return {"ok": True, "text": "Database OK."}
    except Exception as exc:
        return {"ok": False, "text": f"Database problem: {str(exc)[:150]}"}


def account_summary(ch: db.Channel, cfg=None) -> dict:
    """Per account: connection, mode (and why auto can't work), audit (YouTube), last confirmed publication,
    next upcoming post. Shared by the Dashboard health panel and Telegram /health."""
    cfg = cfg or load_config()
    mode = modes.resolve(ch, cfg)
    pubs = [it for it in q.items(ch.id, ("published",)) if it.youtube_video_id or it.ig_media_id]
    last = max(pubs, key=lambda it: as_utc(it.published_at or it.slot_utc or it.updated_at), default=None)
    soon = [it for it in q.items(ch.id, ("queued", "uploading", "processing", "ready", "scheduled", "waiting_capacity"))
            if it.slot_utc and as_utc(it.slot_utc) >= now_utc()]
    nxt = soon[0] if soon else None
    tz = chs.settings_of(ch, cfg)["timezone"]
    from clipforge.publish.timez import utc_to_local

    def when(t):
        return utc_to_local(t, tz).strftime("%a %d %b %H:%M") if t else None

    d = {"id": ch.id, "title": ch.title, "platform": ch.platform, "paused": ch.paused, "token": token_health(ch),
         "mode": mode.mode, "mode_source": mode.source, "mode_problem": mode.problem,
         "last_published": ({"title": last.title, "at": when(last.published_at or last.slot_utc),
                             "url": (f"https://youtube.com/shorts/{last.youtube_video_id}" if last.youtube_video_id
                                     else last.permalink or None)} if last else None),
         "next_post": ({"title": nxt.title, "at": when(nxt.slot_utc), "status": states.status_text(nxt, tz, mode.mode)["label"]}
                       if nxt else None),
         "upcoming": len(soon)}
    from clipforge.publish import limits, plan

    now = now_utc()
    if ch.platform != "instagram":
        d["audit_status"] = cfg.publishing.audit_status
        d["api_audited"] = cfg.publishing.api_audited
        d["limits"] = limits.status(ch.id, cfg, now)
        d["auto_ready"] = mode.auto and not mode.problem and ch.auth_status == "ok" and not d["limits"]["cooldown_until"]
        d["connected"] = ch.auth_status == "ok"
    else:
        from clipforge.publish import instagram as ig

        e = ig.eligibility(ch, cfg)
        d["eligibility"] = e
        d["auto_ready"], d["connected"] = mode.auto and e["auto_ready"], e["connected"]
        d["limits"] = {"uploads_24h": len(limits.successes_24h(ch.id, now, "instagram")), "cooldown_until": None,
                       "recent_failures": limits.status(ch.id, cfg, now)["recent_failures"]}
    try:
        d["plan"] = plan.preview(ch, cfg, now) if cfg.schedule.enabled else []
    except Exception as exc:  # the plan is informational: never break /health
        d["plan"] = []
        log_warn(f"plan preview failed for {ch.id}: {exc}")
    return d


def log_warn(msg: str) -> None:
    from clipforge.utils.log import log

    log.warning(msg)


def heartbeat(st: dict, next_tick: str | None, ticker: bool, tasks: list) -> dict:
    """Scheduler heartbeat in plain words: last run (IST), source, items processed, next run, and whether anything
    runs it while the app is closed."""
    from clipforge.publish.timez import utc_to_local

    last = st.get("last_run_at")
    res = st.get("last_result") or {}
    processed = sum(v for k, v in res.items() if isinstance(v, int) and k in
                    ("uploaded", "published", "failed", "locked", "missed", "rescheduled"))
    ist = (lambda iso: utc_to_local(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")), "Asia/Kolkata")
           .strftime("%d %b %H:%M IST") if iso else None)
    age = None
    if last:
        age = (now_utc() - dt.datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() / 60
    ok = age is not None and age < (5 if ticker else 45)
    return {"ok": ok, "last_run": ist(last), "last_run_minutes_ago": round(age) if age is not None else None,
            "source": st.get("last_source"), "items_processed": processed, "next_run": ist(next_tick),
            "in_app_ticker": ticker, "windows_task": bool(tasks),
            "text": (f"Last run {ist(last)} ({st.get('last_source')}), {processed} item(s) changed."
                     if last else "The scheduler hasn't run yet.") +
                    ("" if tasks else " Runs only while the app is open (no Windows task).")}


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
