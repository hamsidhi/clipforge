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


def _ch_or_404(channel_id: str) -> db.Channel:
    ch = chs.get(channel_id)
    if not ch:
        raise HTTPException(404, "Unknown channel")
    return ch


def _item_or_404(item_id: int) -> db.QueueItem:
    it = q.get(item_id)
    if not it:
        raise HTTPException(404, "Unknown queue item")
    return it


def _zone(channel_id: str | None = None) -> str:
    if channel_id:
        ch = chs.get(channel_id)
        if ch:
            return chs.settings_of(ch)["timezone"]
    return load_config().publishing.timezone


def _item_out(it: db.QueueItem) -> dict:
    d = q.public(it, _zone(it.channel_id))
    d["problems"] = q.problems(it) if it.status in ("draft", "queued", "needs_manual") and it.meta_status == "ready" else []
    d["uploading_progress"] = worker.upload_progress.get(it.id or -1)
    return d


def _wrap(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except q.QueueError as exc:
        raise HTTPException(400, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc))
    except yt.NeedsReauth as exc:
        raise HTTPException(409, str(exc))


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
    elif not p.api_audited:
        banner = {"level": "info", "text": "Google lifted the private-only lock for unaudited API projects on 2 Nov 2026, so "
                  "API mode works now. The audit is only needed for more quota (100 uploads/day by default).",
                  "link": AUDIT_FORM}
    return {
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


# ============================================================================ channels + OAuth
def _channel_out(ch: db.Channel) -> dict:
    d = chs.public(ch)
    counts: dict[str, int] = {}
    for it in q.items(ch.id):
        counts[it.status] = counts.get(it.status, 0) + 1
    d["counts"] = counts
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
    _ch_or_404(channel_id)
    ok = chs.check_auth(channel_id)
    return {"ok": ok, "channel": _channel_out(chs.get(channel_id))}  # type: ignore[arg-type]


@router.delete("/channels/{channel_id}")
def delete_channel(channel_id: str) -> dict:
    _ch_or_404(channel_id)
    if any(it.status == "uploading" for it in q.items(channel_id)):
        raise HTTPException(409, "An upload is running for this channel — try again in a minute.")
    with db.session() as s:
        for it in s.exec(select(db.QueueItem).where(db.QueueItem.channel_id == channel_id,
                                                    db.QueueItem.status.in_(("draft", "queued", "needs_manual", "failed")))).all():  # type: ignore[attr-defined]
            s.delete(it)
        s.commit()
    chs.remove(channel_id)
    return {"ok": True}


# ============================================================================ queue
class QueueAdd(BaseModel):
    channel_id: str
    job_id: Optional[int] = None
    video_id: Optional[str] = None
    ranks: Optional[list[int]] = None  # None = all approved clips


def _queue_metadata(ids: list[int]) -> None:
    for i in ids:
        worker.submit(f"meta:{i}", f"Metadata for queue item {i}", lambda i=i: q.generate_metadata(i).title)


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
    _ch_or_404(body.channel_id)
    added = _wrap(q.add_clips, body.channel_id, video_id, body.ranks)
    _queue_metadata([it.id for it in added])  # type: ignore[misc]
    return {"added": [_item_out(it) for it in added], "skipped_duplicates": (len(body.ranks) - len(added)) if body.ranks else None}


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


@router.patch("/queue/{item_id}")
def queue_patch(item_id: int, body: QueuePatch) -> dict:
    it = _item_or_404(item_id)
    if it.status in ("uploading", "scheduled", "published") and (body.title or body.description or body.tags or body.slot_local):
        raise HTTPException(409, "Already on YouTube — edit it in YouTube Studio.")
    import json

    from clipforge.publish.metadata_opt import tags_within_limit

    if body.title is not None:
        it.title = body.title.strip()
    if body.description is not None:
        it.description = body.description
    if body.tags is not None:
        it.tags_json = json.dumps(tags_within_limit(body.tags), ensure_ascii=False)
    if body.category_id is not None:
        it.category_id = body.category_id
    if body.ig_caption is not None:
        it.ig_caption = body.ig_caption
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


class BulkIn(BaseModel):
    ids: list[int]
    action: Literal["unschedule", "delete", "shift", "regenerate"]
    days: float = 1


@router.post("/queue/bulk")
def queue_bulk(body: BulkIn) -> dict:
    done, errors = [], []
    for i in body.ids:
        try:
            if body.action == "unschedule":
                q.unschedule(i)
            elif body.action == "delete":
                q.delete(i)
            elif body.action == "regenerate":
                queue_regenerate(i)  # raises 409 for clips already on YouTube / in a batch
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
    _ch_or_404(channel_id)
    res = _wrap(batch.prepare, channel_id, body.n)
    res["url"] = f"/api/batches/files/{res['relative']}/batch.html"
    return res


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
    with db.session() as s:
        posts = list(s.exec(select(db.Post)).all())
    submitted = {p.queue_id for p in posts if p.queue_id and p.submission_status != "not_submitted"}
    out = []
    for it in q.items(channel_id, ("published",)):
        if it.id not in submitted:
            out.append({"queue_id": it.id, "title": it.title, "channel_id": it.channel_id,
                        "youtube_url": f"https://youtube.com/shorts/{it.youtube_video_id}",
                        "published_at": it.published_at})
    return out


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
