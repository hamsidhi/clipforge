"""Publishing API (Phase 12): channels + OAuth, metadata, queue/schedule, batches, sync, dashboard."""

from __future__ import annotations

import csv
import datetime as dt
import io
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlmodel import select

from clipforge.api import db
from clipforge.config import load_config
from clipforge.publish import analytics, batch, channels as chs, quota, reconcile, slots
from clipforge.publish import queue as q
from clipforge.publish import youtube as yt
from clipforge.publish.timez import as_utc, now_utc, parse_local, utc_to_local
from clipforge.publish.worker import run_scheduler, worker

router = APIRouter(prefix="/api", tags=["publishing"])
AUDIT_FORM = "https://support.google.com/youtube/contact/yt_api_form"


from clipforge.services import publishing as pub  # noqa: E402  (shared with Telegram + CLI)

_ch_or_404 = pub.ch_or_404
_item_or_404 = pub.item_or_404
_zone = pub.zone
_item_out = pub.item_out
_wrap = pub.wrap


# ============================================================================ status
@router.get("/publishing/status", summary="Publishing mode, quota left today, worker state, setup checklist")
def publishing_status() -> dict:
    cfg = load_config()
    p = cfg.publishing
    rem = quota.remaining(cfg)
    banner = None
    if p.mode == "api" and p.unverified_lock and not p.api_audited:
        banner = {"level": "warning", "text": "Your API project isn't audited and the unverified-project lock is on: "
                  "API uploads stay private. Only a single test upload is allowed — use assisted mode or request the audit.",
                  "link": AUDIT_FORM}
    elif p.mode == "assisted":
        banner = {"level": "info", "text": "Assisted mode: ClipForge prepares everything, you upload the batch in YouTube "
                  "Studio (~10 min/week), then Sync. Clips are never uploaded automatically in this mode. API mode "
                  "uploads by itself (private + scheduled) — Google lifted the unaudited-project lock on 2 Nov 2026.",
                  "link": None}
    elif not p.api_audited:
        banner = {"level": "info", "text": "Google lifted the private-only lock for unaudited API projects on 2 Nov 2026, so "
                  "API mode works now. The audit is only needed for more quota (100 uploads/day by default).",
                  "link": AUDIT_FORM}
    return {
        "audit_status": p.audit_status, "audit_applied_on": p.audit_applied_on, "late_policy": p.late_policy,
        "mode": p.mode, "api_audited": p.api_audited, "unverified_lock": p.unverified_lock, "banner": banner,
        "audit_form": AUDIT_FORM, "quota": rem, "uploads_left_today": rem["inserts"],
        "client_secret": yt.client_secret_path().exists(), "channels": len(chs.all_channels()),
        "worker": worker.state(), "timezone": p.timezone,
        "scope_help": [{"scope": s, "why": yt.SCOPE_HELP[s]} for s in yt.SCOPES],
    }


@router.post("/publishing/client-secret", summary="Upload client_secret.json (raw JSON body)")
async def upload_client_secret(request: Request) -> dict:
    data = await request.body()
    if len(data) > 100_000:
        raise HTTPException(400, "File too large for a client secret")
    try:
        return chs.save_client_secret(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/publishing/run", summary="Run the scheduler now (upload due clips, refresh statuses, sync stats)")
def run_now() -> dict:
    return {"queued": worker.submit("run", "Scheduler run", run_scheduler)}


_token_health = pub.token_health


@router.get("/publishing/health", summary="Scheduler + accounts health for the Dashboard")
def publishing_health() -> dict:
    return pub.health()


class AuditIn(BaseModel):
    status: Literal["not_applied", "applied", "approved", "rejected"]
    applied_on: Optional[str] = None


@router.post("/publishing/audit", summary="Record the YouTube API audit status (approved → api_audited = true)")
def set_audit(body: AuditIn) -> dict:
    from clipforge.config import save_config

    cfg = load_config()
    over: dict[str, Any] = {"publishing.audit_status": body.status}
    if body.applied_on is not None:
        try:
            dt.date.fromisoformat(body.applied_on)
        except ValueError:
            raise HTTPException(400, "Date must look like 2026-11-04")
        over["publishing.audit_applied_on"] = body.applied_on
    if body.status == "approved":
        over["publishing.api_audited"] = True
    elif body.status in ("not_applied", "rejected"):
        over["publishing.api_audited"] = False
    save_config(cfg.with_overrides(over))
    return publishing_status()


# ============================================================================ channels + OAuth
def _channel_out(ch: db.Channel) -> dict:
    d = chs.public(ch)
    counts: dict[str, int] = {}
    for it in q.items(ch.id):
        counts[it.status] = counts.get(it.status, 0) + 1
    d["counts"] = counts
    if ch.platform == "instagram":
        from clipforge.publish import instagram as ig

        d["token_present"] = ig.is_manual(ch) or ig.token_path(ig.ig_id_of(ch)).exists()
        d["token"] = ig.token_health(ch)
    else:
        d["token_present"] = yt.token_path(ch.id).exists()
    return d


@router.get("/channels")
def list_channels() -> list[dict]:
    return [_channel_out(c) for c in chs.all_channels()]


@router.post("/channels/oauth/start", summary="Get the Google consent URL for adding/reconnecting a channel")
def oauth_start(request: Request) -> dict:
    host = request.url.hostname or ""
    if host not in ("localhost", "127.0.0.1", "::1"):
        raise HTTPException(400, "Add channels from the laptop itself (open http://localhost:8000) — Google only "
                                 "redirects desktop-app logins back to localhost.")
    port = request.url.port or 80
    redirect = f"http://{'localhost' if host == 'localhost' else '127.0.0.1'}:{port}/api/oauth/callback"
    try:
        return {"url": chs.start_oauth(redirect)}
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc))


@router.get("/oauth/callback", include_in_schema=False)
def oauth_callback(request: Request, state: str = "", error: str = "") -> Response:
    if error:
        return RedirectResponse(f"/channels?error={error}")
    try:
        res = chs.finish_oauth(str(request.url), state)
    except Exception as exc:
        return HTMLResponse(f"<body style='font-family:system-ui;background:#111;color:#eee;padding:40px'>"
                            f"<h3>Couldn't connect the channel</h3><p>{exc}</p><a style='color:#ffe600' href='/channels'>Back</a></body>",
                            status_code=400)
    if "pick" in res:
        return RedirectResponse(f"/channels?pick={res['pick']}")
    return RedirectResponse(f"/channels?connected={res['connected'][0]['id']}")


@router.get("/channels/pick/{pid}")
def pick_options(pid: str) -> list[dict]:
    opts = chs.pending(pid)
    if opts is None:
        raise HTTPException(404, "That choice expired — add the channel again.")
    return opts


class PickIn(BaseModel):
    channel_id: str


@router.post("/channels/pick/{pid}")
def pick(pid: str, body: PickIn) -> dict:
    try:
        return _channel_out(chs.pick_pending(pid, body.channel_id))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class ChannelPatch(BaseModel):
    paused: Optional[bool] = None
    settings: Optional[dict[str, Any]] = None


SETTINGS_KEYS = set(chs.default_settings().keys())


@router.patch("/channels/{channel_id}")
def patch_channel(channel_id: str, body: ChannelPatch) -> dict:
    ch = _ch_or_404(channel_id)
    fields: dict[str, Any] = {}
    if body.paused is not None:
        fields["paused"] = body.paused
    if body.settings is not None:
        bad = set(body.settings) - SETTINGS_KEYS
        if bad:
            raise HTTPException(400, f"Unknown settings: {', '.join(sorted(bad))}")
        merged = {**ch.settings, **body.settings}
        for s in merged.get("slots", []) + merged.get("peak_slots", []):
            try:
                h, m = (int(x) for x in str(s).split(":"))
                assert 0 <= h < 24 and 0 <= m < 60
            except Exception:
                raise HTTPException(400, f"'{s}' isn't a time like 18:30")
        if not 1 <= int(merged.get("posts_per_day", 3)) <= 20:
            raise HTTPException(400, "Posts per day must be 1–20")
        import json

        fields["settings_json"] = json.dumps(merged, ensure_ascii=False)
    return _channel_out(chs.update(channel_id, **fields) or ch)


@router.post("/channels/{channel_id}/confirm", summary="Confirm this is the right channel before the first upload")
def confirm_channel(channel_id: str) -> dict:
    _ch_or_404(channel_id)
    return _channel_out(chs.update(channel_id, confirmed=True))  # type: ignore[arg-type]


@router.post("/channels/{channel_id}/check-auth")
def check_auth(channel_id: str) -> dict:
    ch = _ch_or_404(channel_id)
    if ch.platform == "instagram":
        from clipforge.publish import instagram as ig

        if ig.is_manual(ch):
            return {"ok": True, "channel": _channel_out(ch)}
        try:
            ig.client().account(ig.ig_id_of(ch), ig.page_token(ch))
            ok = True
            if ch.auth_status != "ok":
                chs.update(channel_id, auth_status="ok", auth_error="")
        except ig.IGError as exc:
            ok = False
            if exc.auth:
                chs.mark_reauth(channel_id, str(exc))
        return {"ok": ok, "channel": _channel_out(chs.get(channel_id))}  # type: ignore[arg-type]
    ok = chs.check_auth(channel_id)
    return {"ok": ok, "channel": _channel_out(chs.get(channel_id))}  # type: ignore[arg-type]


@router.delete("/channels/{channel_id}", summary="Disconnect: revoke access at Google/Meta and delete the saved login")
def delete_channel(channel_id: str) -> dict:
    ch = _ch_or_404(channel_id)
    if any(it.status == "uploading" for it in q.items(channel_id)):
        raise HTTPException(409, "An upload is running for this channel — try again in a minute.")
    with db.session() as s:
        for it in s.exec(select(db.QueueItem).where(db.QueueItem.channel_id == channel_id,
                                                    db.QueueItem.status.in_(("draft", "queued", "needs_manual", "failed", "missed", "ready")))).all():  # type: ignore[attr-defined]
            s.delete(it)
        # stored statistics for this account are deleted too (privacy policy: Disconnect deletes its data)
        perf_ids = [p.youtube_video_id for p in s.exec(select(db.VideoPerf).where(db.VideoPerf.channel_id == channel_id)).all()]
        for p in s.exec(select(db.VideoPerf).where(db.VideoPerf.channel_id == channel_id)).all():
            s.delete(p)
        if perf_ids:
            for snap in s.exec(select(db.StatSnapshot).where(db.StatSnapshot.youtube_video_id.in_(perf_ids))).all():  # type: ignore[attr-defined]
                s.delete(snap)
        for d in s.exec(select(db.ChannelDaily).where(db.ChannelDaily.channel_id == channel_id)).all():
            s.delete(d)
        s.commit()
    if ch.platform == "instagram":
        from clipforge.publish import instagram as ig

        revoked = ig.revoke(ch)
    else:
        revoked = yt.revoke(channel_id)
    chs.remove(channel_id)
    return {"ok": True, "revoked": revoked}


# ============================================================================ queue
class QueueAdd(BaseModel):
    channel_id: Optional[str] = None
    channel_ids: Optional[list[str]] = None  # several accounts at once (YouTube + Instagram)
    job_id: Optional[int] = None
    video_id: Optional[str] = None
    ranks: Optional[list[int]] = None  # None = all approved clips


@router.post("/queue", summary="Add clips (default: all approved) to a channel's queue; metadata is generated in the background")
def queue_add(body: QueueAdd) -> dict:
    video_id = body.video_id
    if body.job_id is not None:
        job = db.get_job(body.job_id)
        if not job or not job.video_id:
            raise HTTPException(404, "Unknown job")
        video_id = job.video_id
    if not video_id:
        raise HTTPException(400, "Give a job_id or video_id")
    targets = list(dict.fromkeys([*(body.channel_ids or []), *([body.channel_id] if body.channel_id else [])]))
    added = pub.queue_add(targets, video_id, body.ranks)
    return {"added": [_item_out(it) for it in added],
            "skipped_duplicates": (len(body.ranks) * len(targets) - len(added)) if body.ranks else None}


def _queue_metadata(ids: list[int]) -> None:
    for i in ids:
        worker.submit(f"meta:{i}", f"Metadata for queue item {i}", lambda i=i: q.generate_metadata(i).title)


@router.get("/queue")
def queue_list(channel_id: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
    st = tuple(status.split(",")) if status else None
    return [_item_out(it) for it in q.items(channel_id, st)]


class QueuePatch(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[list[str]] = None
    slot_local: Optional[str] = None  # "2026-11-05T21:00" in the channel's timezone
    category_id: Optional[str] = None
    ig_caption: Optional[str] = None
    privacy: Optional[Literal["scheduled", "public", "unlisted", "private"]] = None
    ig_thumb_offset_ms: Optional[int] = None


@router.patch("/queue/{item_id}")
def queue_patch(item_id: int, body: QueuePatch) -> dict:
    it = _item_or_404(item_id)
    if (it.status in q.ON_PLATFORM or it.youtube_video_id or it.ig_media_id) and (
            body.title or body.description or body.tags or body.slot_local or body.privacy or body.ig_caption):
        raise HTTPException(409, "Already on the platform — edit it there (YouTube Studio / Instagram).")
    import json

    from clipforge.publish.metadata_opt import tags_within_limit

    if body.title is not None:
        if len(body.title.strip()) > 100:
            raise HTTPException(400, "YouTube titles can be at most 100 characters.")
        it.title = body.title.strip()
    if body.description is not None and len(body.description) > 5000:
        raise HTTPException(400, "YouTube descriptions can be at most 5,000 characters.")
    if body.privacy is not None:
        it.privacy = body.privacy
    if body.ig_thumb_offset_ms is not None:
        it.ig_thumb_offset_ms = max(0, body.ig_thumb_offset_ms)
    if body.description is not None:
        it.description = body.description
    if body.tags is not None:
        it.tags_json = json.dumps(tags_within_limit(body.tags), ensure_ascii=False)
    if body.category_id is not None:
        it.category_id = body.category_id
    if body.ig_caption is not None:
        from clipforge.publish import ig_meta

        it.ig_caption = ig_meta.clean_caption(body.ig_caption)
    if any(v is not None for v in (body.title, body.description, body.tags)):
        it.meta_status = "ready"
    it = q.save(it)
    if body.slot_local:
        try:
            when = parse_local(body.slot_local, _zone(it.channel_id))
        except ValueError:
            raise HTTPException(400, "Time must look like 2026-11-05T21:00")
        it = _wrap(q.move, item_id, when)
    return _item_out(it)


@router.post("/queue/{item_id}/regenerate", summary="Write new title/description/tags with the LLM")
def queue_regenerate(item_id: int) -> dict:
    it = _item_or_404(item_id)
    if it.youtube_video_id or it.status in ("uploading", "scheduled", "published"):
        raise HTTPException(409, "Already on YouTube — new metadata here wouldn't change the video. Edit it in Studio.")
    if it.batch:
        raise HTTPException(409, "This clip is in a prepared Studio batch — regenerating would no longer match the "
                                 "checklist. Unschedule it first (removes it from the batch).")
    it.meta_status = "pending"
    q.save(it)
    _queue_metadata([item_id])
    return _item_out(it)


@router.post("/queue/{item_id}/unschedule")
def queue_unschedule(item_id: int) -> dict:
    return _item_out(_wrap(q.unschedule, item_id))


@router.delete("/queue/{item_id}")
def queue_delete(item_id: int) -> dict:
    _wrap(q.delete, item_id)
    return {"ok": True}


UNSAFE_BULK = pub.UNSAFE_BULK
_do_reschedule = pub.do_reschedule
_do_retry = pub.do_retry
_do_unbatch = pub.do_unbatch
_do_publish_now = pub.do_publish_now


@router.post("/queue/{item_id}/reschedule", summary="Move to the channel's next free slot")
def queue_reschedule(item_id: int) -> dict:
    return _item_out(_wrap(_do_reschedule, item_id))


@router.post("/queue/{item_id}/retry", summary="Retry a failed/missed clip (next free slot if its time passed)")
def queue_retry(item_id: int) -> dict:
    return _item_out(_wrap(_do_retry, item_id))


@router.post("/queue/{item_id}/unbatch", summary="Remove from a Studio batch so API mode uploads it")
def queue_unbatch(item_id: int) -> dict:
    return _item_out(_wrap(_do_unbatch, item_id))


@router.post("/queue/{item_id}/publish-now", summary="Upload/publish this clip now (API mode)")
def queue_publish_now(item_id: int) -> dict:
    return _item_out(_wrap(_do_publish_now, item_id))


class BulkIn(BaseModel):
    ids: list[int] = []
    action: Literal["unschedule", "delete", "shift", "regenerate", "reschedule", "retry", "unbatch", "to_batch",
                    "reschedule_missed", "retry_failed"]
    days: float = 1
    channel_id: Optional[str] = None


@router.post("/queue/bulk")
def queue_bulk(body: BulkIn) -> dict:
    done, errors = [], []
    ids = list(body.ids)
    action = body.action
    if action in ("reschedule_missed", "retry_failed"):  # "all of them" — no ids needed
        # only clips that certainly aren't on the platform: "interrupted", "batched_in_api", "Uploaded in Studio? not
        # linked" etc. may already be uploaded — they keep their per-clip buttons (check Studio first)
        want = ("missed",) if action == "reschedule_missed" else ("failed",)
        ids = [it.id for it in q.items(body.channel_id, want)  # type: ignore[misc]
               if not it.youtube_video_id and not it.ig_media_id and it.reason not in UNSAFE_BULK]
        action = "reschedule" if action == "reschedule_missed" else "retry"
    if action == "to_batch":
        by_channel: dict[str, list[int]] = {}
        for i in ids:
            it = q.get(i)
            if it and it.platform == "youtube":
                by_channel.setdefault(it.channel_id, []).append(i)
        out = []
        for ch_id, ch_ids in by_channel.items():
            try:
                res = batch.prepare(ch_id, len(ch_ids), force=True, ids=ch_ids)
                done += [e["id"] for e in res["items"]]
                out.append({"relative": res["relative"], "url": f"/api/batches/files/{res['relative']}/batch.html"})
            except q.QueueError as exc:
                errors.append({"id": None, "error": str(exc)})
        return {"done": done, "errors": errors, "batches": out}
    for i in ids:
        try:
            if action == "unschedule":
                q.unschedule(i)
            elif action == "delete":
                q.delete(i)
            elif action == "regenerate":
                queue_regenerate(i)  # raises 409 for clips already on YouTube / in a batch
            elif action == "reschedule":
                _do_reschedule(i)
            elif action == "retry":
                _do_retry(i)
            elif action == "unbatch":
                _do_unbatch(i)
            else:
                it = q.get(i)
                if not it or not it.slot_utc:
                    raise q.QueueError("not scheduled")
                q.move(i, as_utc(it.slot_utc) + dt.timedelta(days=body.days))
            done.append(i)
        except (q.QueueError, HTTPException) as exc:
            errors.append({"id": i, "error": getattr(exc, "detail", str(exc))})
    return {"done": done, "errors": errors}


class AutoIn(BaseModel):
    item_ids: Optional[list[int]] = None


@router.post("/channels/{channel_id}/autoschedule", summary="Fill the next free slots with ready draft clips")
def autoschedule(channel_id: str, body: AutoIn = AutoIn()) -> dict:
    _ch_or_404(channel_id)
    done = _wrap(q.autoschedule, channel_id, body.item_ids)
    waiting = [it for it in q.items(channel_id, ("draft",))]
    return {"scheduled": [_item_out(it) for it in done],
            "still_draft": [{"id": it.id, "title": it.title, "meta_status": it.meta_status, "problems": q.problems(it)}
                            for it in waiting]}


@router.get("/channels/{channel_id}/free-slots", summary="Next free publish times (for moving a clip)")
def free_slots(channel_id: str, n: int = 12) -> list[dict]:
    ch = _ch_or_404(channel_id)
    settings = chs.settings_of(ch)
    plan = slots.Plan.from_settings(settings)
    taken = [as_utc(it.slot_utc) for it in q.items(channel_id, q.PLANNED) if it.slot_utc]
    out = []
    for t, peak in slots.free_slots(plan, taken, now_utc(), 30):
        out.append({"slot_utc": t.isoformat(), "slot_local": utc_to_local(t, plan.zone).strftime("%Y-%m-%dT%H:%M"), "peak": peak})
        taken.append(t)
        if len(out) >= n:
            break
    return out


@router.get("/queue.csv")
def queue_csv(channel_id: Optional[str] = None) -> Response:
    out = io.StringIO()
    cols = ["id", "channel_id", "status", "slot_local", "title", "youtube_url", "episode_title", "topic", "score", "error"]
    w = csv.writer(out)
    w.writerow(cols)
    for it in q.items(channel_id):
        d = q.public(it, _zone(it.channel_id))
        w.writerow([d.get(c, "") for c in cols])
    return Response("﻿" + out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="clipforge_queue.csv"'})


# ============================================================================ assisted mode
class BatchIn(BaseModel):
    n: Optional[int] = None


@router.post("/channels/{channel_id}/batch", summary="Assisted mode: build an upload folder + checklist for YouTube Studio")
def make_batch(channel_id: str, body: BatchIn = BatchIn()) -> dict:
    return pub.make_batch(channel_id, body.n)

@router.get("/batches")
def batches() -> list[dict]:
    out = batch.list_batches()
    for b in out:
        b["url"] = f"/api/batches/files/{b['relative']}/batch.html"
    return out


@router.get("/batches/files/{path:path}", include_in_schema=False)
def batch_file(path: str) -> FileResponse:
    # validate BEFORE touching the filesystem: on Windows, resolving "//host/share" would open an SMB connection
    parts = path.replace("\\", "/").split("/")
    if (not path.startswith("batches/") or ":" in path or path.startswith(("/", "\\"))
            or any(p in ("", ".", "..") for p in parts)):
        raise HTTPException(404, "Not found")
    root = (load_config().workspace / "batches").resolve()
    target = (load_config().workspace / path).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(target)


@router.post("/channels/{channel_id}/sync", summary="Link your Studio uploads to the queue + refresh statuses; stats in background")
def sync_channel(channel_id: str) -> dict:
    _ch_or_404(channel_id)
    try:
        res = reconcile.sync(channel_id)
    except yt.NeedsReauth as exc:
        chs.mark_reauth(channel_id, str(exc))
        raise HTTPException(409, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"YouTube didn't answer: {yt.describe_http_error(exc)}")

    def stats():
        cfg = load_config()
        return analytics.sync_channel(channel_id, yt.client_for(channel_id, cfg), cfg, now_utc())

    worker.submit(f"stats:{channel_id}", f"Stats sync {channel_id}", stats)
    return res


class LinkIn(BaseModel):
    video_id: str


@router.post("/queue/{item_id}/link", summary="Manually link a queue item to a YouTube video id")
def link_item(item_id: int, body: LinkIn) -> dict:
    _item_or_404(item_id)
    vid = body.video_id.strip().split("/")[-1].split("?")[0]
    return _item_out(_wrap(reconcile.link, item_id, vid))


# ============================================================================ dashboard
@router.get("/channels/{channel_id}/dashboard")
def dashboard(channel_id: str) -> dict:
    ch = _ch_or_404(channel_id)
    cfg = load_config()
    settings = chs.settings_of(ch, cfg)
    today = utc_to_local(now_utc(), settings["timezone"]).date()
    hm = analytics.heatmap(channel_id, settings["timezone"])
    with db.session() as s:
        daily = sorted(s.exec(select(db.ChannelDaily).where(db.ChannelDaily.channel_id == channel_id)).all(),
                       key=lambda r: r.day)
    return {
        "channel": _channel_out(ch), "ypp": analytics.ypp_progress(channel_id, cfg.ypp, today),
        "working": analytics.whats_working(channel_id), "heatmap": hm,
        "recommendation": analytics.recommend_slots(hm, settings["slots"], settings["posts_per_day"]),
        "daily": [{"day": r.day.isoformat(), "subscribers": r.subscribers, "shorts_views_28d": r.shorts_views_28d,
                   "shorts_views_90d": r.shorts_views_90d} for r in daily[-90:]],
        "reminders": reminders(channel_id), "last_sync_at": ch.last_sync_at,
    }


class SlotsIn(BaseModel):
    slots: list[str]


@router.post("/channels/{channel_id}/apply-slots", summary="Apply recommended slots (only when you click)")
def apply_slots(channel_id: str, body: SlotsIn) -> dict:
    return patch_channel(channel_id, ChannelPatch(settings={"slots": body.slots}))


@router.get("/posts/reminders", summary="Published Shorts not yet logged/submitted to a campaign")
def reminders(channel_id: Optional[str] = None) -> list[dict]:
    return pub.reminders(channel_id)


@router.get("/channels/{channel_id}/stats.csv")
def stats_csv(channel_id: str) -> Response:
    _ch_or_404(channel_id)
    with db.session() as s:
        perfs = list(s.exec(select(db.VideoPerf).where(db.VideoPerf.channel_id == channel_id)).all())
    cols = ["youtube_video_id", "title", "published_at", "duration", "views", "likes", "comments", "views_24h",
            "views_72h", "avg_view_pct", "subs_gained"]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(cols)
    for p in perfs:
        w.writerow([getattr(p, c) for c in cols])
    return Response("﻿" + out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="clipforge_stats_{channel_id}.csv"'})
