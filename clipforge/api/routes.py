"""REST + SSE endpoints. OpenAPI docs at /docs."""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import json
import math
import queue
import uuid
import zipfile
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlmodel import select
from starlette.background import BackgroundTask

from clipforge import pipeline
from clipforge.api import db
from clipforge.api.jobs import event_json, job_config, manager  # noqa: F401
from clipforge.config import Config, load_secrets, mask_secret, save_config, save_secret  # noqa: F401
from clipforge.models import ClipMetadata, CropPlan
from clipforge.utils.log import log
from clipforge.workspace import is_url

router = APIRouter(prefix="/api")


# ============================================================================ helpers (implementation: clipforge/services)
from clipforge.services import clips as clip_svc  # noqa: E402
from clipforge.services import jobs as job_svc  # noqa: E402
from clipforge.services.jobs import JobCreate, JobSettings  # noqa: E402,F401  (re-exported: older imports)

_cfg = job_svc.cfg
_job_or_404 = job_svc.job_or_404
_ws = job_svc.ws_for
_parse_clip_id = job_svc.parse_clip_id
_find_cand = job_svc.find_cand
_metas = job_svc.metas
_clip_out = job_svc.clip_out
_job_out = job_svc.job_out


# ============================================================================ jobs
@router.post("/jobs", summary="Create a clip job from a YouTube URL or an uploaded file")
def create_job(body: JobCreate, request: Request = None) -> dict:  # type: ignore[assignment]
    remote = request is not None and not getattr(request.state, "local", True)
    return job_svc.create_job(body, remote=remote)


@router.get("/jobs")
def list_jobs() -> list[dict]:
    return [_job_out(j) for j in db.list_jobs()]


@router.get("/jobs/{job_id}")
def get_job(job_id: int) -> dict:
    return _job_out(_job_or_404(job_id))


@router.delete("/jobs/{job_id}")
def delete_job(job_id: int, delete_files: bool = False) -> dict:
    return job_svc.delete_job(job_id, delete_files)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int) -> dict:
    return job_svc.cancel_job(job_id)


@router.post("/jobs/{job_id}/retry", summary="Re-run a failed/cancelled job (finished stages are reused)")
def retry_job(job_id: int) -> dict:
    return job_svc.retry_job(job_id)


@router.post("/jobs/{job_id}/rerun/{stage}", summary="Forget one stage's cache and run the job again from there")
def rerun_stage(job_id: int, stage: str) -> dict:
    return job_svc.rerun_stage(job_id, stage)


@router.get("/jobs/{job_id}/events", summary="Server-Sent Events: stage, progress, ETA, logs")
async def job_events(job_id: int, request: Request) -> StreamingResponse:
    job = _job_or_404(job_id)
    # build the snapshot BEFORE the stream starts: an error here becomes a normal HTTP error response,
    # instead of "response already started" once streaming has begun
    job_out = _job_out(job)
    sq, backlog, last_seq = manager.subscribe(job_id)

    async def gen():
        try:
            # replayed backlog (seq <= last_seq) rebuilds the UI state; clients only toast/react to newer events.
            # boot_id changes on server restart (seq restarts at 1) so clients can reset their de-dup set.
            snap = {"type": "snapshot", "job": job_out, "last_seq": last_seq, "boot_id": manager.boot_id}
            yield f"event: snapshot\ndata: {event_json(snap)}\n\n"
            for ev in backlog:
                yield f"data: {event_json(ev)}\n\n"
            idle = 0.0
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = sq.get_nowait()
                    yield f"data: {event_json(ev)}\n\n"
                    idle = 0
                    continue
                except queue.Empty:
                    pass
                await asyncio.sleep(0.25)
                idle += 0.25
                if idle >= 15:
                    yield ": keep-alive\n\n"
                    idle = 0
        finally:
            manager.unsubscribe(job_id, sq)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/jobs/{job_id}/thumbnail")
def job_thumbnail(job_id: int) -> FileResponse:
    ws = _ws(_job_or_404(job_id))
    if not ws.thumbnail.exists():
        raise HTTPException(404, "No thumbnail yet")
    return FileResponse(ws.thumbnail, media_type="image/jpeg")


@router.get("/jobs/{job_id}/clips")
def job_clips(job_id: int) -> list[dict]:
    return clip_svc.list_clips(job_id)


class RenderRequest(BaseModel):
    ranks: Optional[list[int]] = None
    force: bool = False
    only_approved: bool = False
    only_stale: bool = False


@router.post("/jobs/{job_id}/render-all")
def render_all(job_id: int, body: RenderRequest = RenderRequest()) -> dict:
    queued = clip_svc.render(job_id, body.ranks, body.force, body.only_approved, body.only_stale)
    return {"ok": True, "queued": queued}


class BulkApprove(BaseModel):
    min_score: float
    approve: bool = True


@router.post("/jobs/{job_id}/approve", summary="Approve every clip with score ≥ min_score")
def bulk_approve(job_id: int, body: BulkApprove) -> dict:
    return {"ok": True, "changed": clip_svc.approve_above(job_id, body.min_score, body.approve)}


@router.get("/jobs/{job_id}/source-waveform", summary="Waveform peaks for [start, end] of the source audio")
def source_waveform(job_id: int, start: float = 0, end: float = 60, per_sec: int = Query(50, le=200)) -> dict:
    from clipforge.stages.analyze_audio import waveform_peaks

    ws = _ws(_job_or_404(job_id))
    info = pipeline.load_info(ws)
    start, end = max(0.0, start), min(info.duration, end)
    if end - start > 900:
        raise HTTPException(400, "Max 15 minutes per request")
    if not ws.audio.exists():
        raise HTTPException(404, "The episode's audio file is gone (deleted to save space?) — Retry the job to rebuild it.")
    return {"start": start, "end": end, "per_sec": per_sec, "peaks": waveform_peaks(ws, start, end, per_sec)}


@router.get("/jobs/{job_id}/source-video", summary="Source video (supports HTTP Range for seeking)")
def source_video(job_id: int) -> FileResponse:
    ws = _ws(_job_or_404(job_id))
    return FileResponse(ws.source, media_type="video/mp4")


@router.get("/jobs/{job_id}/transcript", summary="Words in [start, end] (for the editor timeline)")
def transcript_words(job_id: int, start: float = 0, end: float = 60, script: str = "roman") -> list[dict]:
    ws = _ws(_job_or_404(job_id))
    tr = pipeline.load_transcript(ws)
    return [{"start": w.start, "end": w.end, "text": w.display(script)} for w in tr.all_words() if start <= w.start <= end]


@router.get("/jobs/{job_id}/export.zip", summary="ZIP of approved clips (or all if none approved) + metadata.csv")
def export_zip(job_id: int, all_clips: bool = False) -> FileResponse:
    job = _job_or_404(job_id)
    ws = _ws(job)
    cands = [c for c in pipeline.load_candidates(ws) if c.enabled]
    chosen = cands if all_clips or not any(c.approved for c in cands) else [c for c in cands if c.approved]
    metas = _metas(ws)
    # build on disk, not in RAM (20 clips can be ~1 GB); deleted after it has been sent
    zip_path = ws.dir / f"export_{uuid.uuid4().hex[:8]}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:  # mp4 is already compressed
        rows = []
        for c in chosen:
            f = ws.find_clip(c.rank)
            if f:
                z.write(f, f"clips/{f.name}")
                if c.rank in metas:
                    rows.append(metas[c.rank])
        out = io.StringIO()
        w = csv.writer(out)
        cols = list(ClipMetadata.model_fields)
        w.writerow(cols)
        for m in rows:
            d = m.model_dump()
            d["hashtags"] = " ".join(m.hashtags)
            w.writerow([d[k] for k in cols])
        z.writestr("metadata.csv", "﻿" + out.getvalue())
        z.writestr("metadata.json", json.dumps([m.model_dump() for m in rows], indent=2, ensure_ascii=False))
    return FileResponse(zip_path, media_type="application/zip", filename=f"clipforge_{job.video_id}.zip",
                        background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)))


# ============================================================================ clips
@router.get("/clips/{clip_id}")
def get_clip(clip_id: str) -> dict:
    return clip_svc.get_clip(clip_id)


ClipPatch = clip_svc.ClipPatch


@router.patch("/clips/{clip_id}", summary="Edit a clip (times snap to word boundaries; re-render to apply)")
def patch_clip(clip_id: str, body: ClipPatch) -> dict:
    return clip_svc.patch_clip(clip_id, body)


@router.post("/clips/{clip_id}/render", summary="Re-render one clip (queued; progress on the job's SSE stream)")
def render_clip(clip_id: str) -> dict:
    return clip_svc.render_clip(clip_id)


@router.post("/clips/{clip_id}/undo", summary="Restore the previous render and its settings")
def undo_clip(clip_id: str) -> dict:
    return clip_svc.undo(clip_id)


@router.get("/clips/{clip_id}/video", summary="Rendered clip (supports HTTP Range); ?preview=1 = 540×960 low-data copy")
def clip_video(clip_id: str, download: bool = False, preview: bool = False) -> FileResponse:
    job, rank = _parse_clip_id(clip_id)
    f = _ws(job).find_clip(rank)
    if not f:
        raise HTTPException(404, "Clip not rendered yet")
    headers = {"Cache-Control": "private, max-age=86400"}  # URLs carry ?v=<mtime>, so a re-render gets a new URL
    if preview and not download:
        from clipforge.stages.preview import ensure_preview

        try:
            return FileResponse(ensure_preview(f), media_type="video/mp4", headers=headers)
        except Exception as exc:
            log.warning(f"preview proxy failed ({exc}); serving the full clip")
    return FileResponse(f, media_type="video/mp4", filename=f.name if download else None, headers=headers,
                        content_disposition_type="attachment" if download else "inline")


@router.get("/clips/{clip_id}/thumbnail")
def clip_thumbnail(clip_id: str) -> FileResponse:
    job, rank = _parse_clip_id(clip_id)
    p = _ws(job).clips_dir / "_work" / f"{rank:02d}" / "thumb.jpg"
    if not p.exists():
        raise HTTPException(404, "No thumbnail")
    return FileResponse(p, media_type="image/jpeg")


@router.get("/clips/{clip_id}/shots", summary="Camera shots of the clip with a source frame per shot (crop override UI)")
def clip_shots(clip_id: str) -> dict:
    from clipforge.stages import scenes

    job, rank = _parse_clip_id(clip_id)
    ws = _ws(job)
    _, c = _find_cand(ws, rank)
    info = pipeline.load_info(ws)
    cuts = scenes.ensure(ws, [(c.start, c.end)], info.duration)
    shots = scenes.shots_for(cuts, c.start, c.end)
    plan_file = ws.clips_dir / "_work" / f"{rank:02d}" / "crop_plan.json"
    plan = CropPlan.model_validate_json(plan_file.read_text(encoding="utf-8")) if plan_file.exists() else None
    out = []
    for i, s in enumerate(shots):
        seg = plan.segments[i] if plan and i < len(plan.segments) else None
        out.append({"index": i, "start": s.start, "end": s.end,
                    "frame_url": f"/api/clips/{clip_id}/frame?t={c.start + (s.start + s.end) / 2:.2f}",
                    "mode": seg.mode if seg else None,
                    "cx": (sum(seg.xs) / len(seg.xs)) if seg and seg.xs else None})
    crop_w = min(1.0, (info.height * 9 / 16) / info.width) if info.width else 0.316
    return {"shots": out, "crop_width_frac": crop_w, "src_width": info.width, "src_height": info.height}


@router.get("/clips/{clip_id}/frame", summary="A JPEG frame of the source at time t")
def clip_frame(clip_id: str, t: float) -> FileResponse:
    from clipforge.utils.ffmpeg import extract_frame

    job, rank = _parse_clip_id(clip_id)
    ws = _ws(job)
    _find_cand(ws, rank)  # 404 for unknown clips instead of creating folders
    duration = pipeline.load_info(ws).duration
    if not math.isfinite(t) or not 0 <= t <= duration:
        raise HTTPException(400, "t must be a time inside the video")
    cache = ws.work_dir(rank) / f"frame_{t:.1f}.jpg"
    if not cache.exists():
        tmp = cache.with_name(f"{cache.stem}_{uuid.uuid4().hex[:6]}.jpg")  # parallel requests don't collide
        extract_frame(ws.source, round(t, 1), tmp, width=960)
        tmp.replace(cache)
    return FileResponse(cache, media_type="image/jpeg")


# ============================================================================ uploads (chunked)
_uploads_dir = job_svc.uploads_dir


class UploadInit(BaseModel):
    filename: str
    size: int


@router.post("/uploads", summary="Start a chunked upload of a local video file")
def upload_init(body: UploadInit) -> dict:
    return {"upload_id": job_svc.new_upload(body.filename, body.size)}


_upload_path = job_svc.upload_path


@router.put("/uploads/{upload_id}", summary="Write a chunk (raw body) at ?offset= (re-sending a chunk is safe)")
async def upload_chunk(upload_id: str, request: Request, offset: int = 0) -> dict:
    path = _upload_path(upload_id)
    if not path.exists():
        raise HTTPException(404, "Unknown upload")
    size = path.stat().st_size
    if offset > size:
        raise HTTPException(409, f"Expected offset {size}")
    declared = json.loads(path.with_suffix(".json").read_text())["size"]
    with open(path, "r+b") as f:
        f.truncate(offset)  # a retried chunk after a partial write overwrites the partial data
        f.seek(offset)
        written = offset
        async for chunk in request.stream():
            written += len(chunk)
            if written > declared:  # never more than the file size announced at the start (disk-filling)
                f.truncate(offset)
                raise HTTPException(400, f"More data than the declared {declared} bytes")
            f.write(chunk)
    return {"received": path.stat().st_size}


@router.get("/uploads/{upload_id}", summary="How many bytes arrived (resume an interrupted upload from here)")
def upload_state(upload_id: str) -> dict:
    path = _upload_path(upload_id)
    meta_p = path.with_suffix(".json")
    if not path.exists() or not meta_p.exists():
        raise HTTPException(404, "Unknown upload")
    meta = json.loads(meta_p.read_text())
    return {"received": path.stat().st_size, "size": meta["size"], "complete": meta["complete"], "filename": meta["filename"]}


@router.post("/uploads/{upload_id}/complete")
def upload_complete(upload_id: str) -> dict:
    job_svc.finish_upload(upload_id)
    return {"ok": True, "upload_id": upload_id}


@router.get("/probe", summary="Title / duration / thumbnail of a YouTube URL (no download)")
def probe_url(url: str) -> dict:
    if not is_url(url):
        raise HTTPException(400, "Not a URL")
    import yt_dlp

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
    except Exception as exc:
        raise HTTPException(400, f"Could not read this link: {str(exc).splitlines()[0][:200]}")
    return {"title": info.get("title"), "channel": info.get("channel") or info.get("uploader"),
            "duration": info.get("duration"), "thumbnail": info.get("thumbnail"),
            "chapters": len(info.get("chapters") or [])}


@router.get("/fonts/{name}", include_in_schema=False)
def font_file(name: str) -> FileResponse:
    from clipforge.config import FONTS_DIR

    p = (FONTS_DIR / name).resolve()
    if p.parent != FONTS_DIR.resolve() or p.suffix != ".ttf" or not p.exists():
        raise HTTPException(404, "Font not found")
    return FileResponse(p, media_type="font/ttf", headers={"Cache-Control": "max-age=86400"})


# ============================================================================ settings / system
SECRET_NAMES = ["GEMINI_API_KEY", "GROQ_API_KEY"]


@router.get("/settings")
def get_settings() -> dict:
    cfg = _cfg()
    sec = load_secrets()
    return {
        "config": cfg.model_dump(mode="json"),
        "secrets": {"GEMINI_API_KEY": mask_secret(sec.gemini_api_key), "GROQ_API_KEY": mask_secret(sec.groq_api_key)},
    }


class SettingsUpdate(BaseModel):
    config: Optional[dict[str, Any]] = None
    secrets: Optional[dict[str, Optional[str]]] = None


LAPTOP_ONLY_SECTIONS = ("auth", "output", "remote")  # login rules, where files go, the watch folder


@router.put("/settings", summary="Update config.yaml values and/or API keys (.env)")
def put_settings(body: SettingsUpdate, request: Request) -> dict:
    if body.config is not None:
        cur = _cfg().model_dump(mode="json")
        if not getattr(request.state, "local", True):
            changed = [s for s in LAPTOP_ONLY_SECTIONS if isinstance(body.config.get(s), dict)
                       and any(cur.get(s, {}).get(k) != v for k, v in body.config[s].items())]
            if changed:
                raise HTTPException(403, f"Change {', '.join(changed)} settings on the laptop itself.")
        for section, values in body.config.items():
            if section in cur and isinstance(values, dict):
                cur[section].update(values)
        try:
            cfg = Config.model_validate(cur)
        except Exception as exc:
            raise HTTPException(400, f"Invalid settings: {exc}")
        save_config(cfg)
    for name, value in (body.secrets or {}).items():
        if name not in SECRET_NAMES:
            raise HTTPException(400, f"Unknown secret {name}")
        if value is not None and "…" not in value and "•" not in value:  # ignore masked echoes
            try:
                save_secret(name, value.strip())
            except ValueError as exc:
                raise HTTPException(400, str(exc))
    return get_settings()


@router.get("/settings/ollama-models")
def ollama_models() -> dict:
    from clipforge.llm.ollama_provider import list_ollama_models, pick_model

    try:
        models = list_ollama_models(_cfg().llm.ollama_url)
        return {"models": models, "recommended": pick_model(models), "running": True}
    except Exception:
        return {"models": [], "recommended": None, "running": False}


@router.get("/doctor")
def doctor() -> list[dict]:
    from clipforge.doctor import run_checks

    return [c.to_dict() for c in run_checks()]


@router.post("/doctor/fix", summary="Download missing fonts and face models")
def doctor_fix() -> list[dict]:
    from clipforge.doctor import run_checks

    return [c.to_dict() for c in run_checks(fix_assets=True)]


@router.get("/system")
def system() -> dict:
    from clipforge.utils.gpu import cpu_count, cuda_available

    cfg = _cfg()
    return {"cuda": cuda_available(), "cpu_count": cpu_count(), "whisper_model": cfg.whisper.model,
            "provider": cfg.llm.provider}


@router.get("/estimate", summary="Rough processing-time estimate (seconds) for a video of `duration` seconds")
def estimate(duration: float = 3600, clips: int = 20, provider: Optional[str] = None) -> dict:
    from clipforge.utils.gpu import cpu_count, cuda_available

    cfg = _cfg()
    cuda = cuda_available()
    # measured on a Ryzen 7 (8 cores, no CUDA): turbo int8 greedy ≈ 0.4× real time; qwen2.5:7b ≈ 120 s per 8-min window
    whisper_rt = 0.06 if cuda else (0.45 if cpu_count() >= 12 else 0.8)
    prov = provider or cfg.llm.provider
    llm_per_window = {"ollama": 30 if cuda else 130, "gemini": 10, "groq": 5, "mock": 0}.get(prov, 60)
    llm_per_clip = {"ollama": 8 if cuda else 30, "gemini": 3, "groq": 2, "mock": 0}.get(prov, 10)  # romanize + copy
    windows = max(1, int(duration / ((cfg.llm.window_min - cfg.llm.overlap_min) * 60)) + 1)
    parts = {
        "download": max(20, duration * 0.03),
        "transcribe": duration * whisper_rt,
        "select": windows * llm_per_window,
        "captions": clips * llm_per_clip,
        "render": clips * (4 if cuda else 8),
    }
    return {"total_sec": round(sum(parts.values())), "parts": {k: round(v) for k, v in parts.items()}}


@router.get("/presets", summary="Caption style presets (built-in + yours)")
def presets() -> list[dict]:
    from clipforge.stages.captions import PRESETS

    out = [{"key": k, "builtin": True, **v} for k, v in PRESETS.items()]
    with db.session() as s:
        for p in s.exec(select(db.Preset)).all():
            out.append({"key": p.key, "builtin": False, "label": p.label, **json.loads(p.style_json)})
    return out


class PresetIn(BaseModel):
    label: str
    font: str = "Montserrat-ExtraBold"
    font_size: int = 80
    text_color: str = "#FFFFFF"
    highlight_color: str = "#FFE600"
    keyword_color: str = "#00FF88"
    uppercase: bool = True


@router.post("/presets")
def create_preset(body: PresetIn) -> dict:
    from clipforge.stages.captions import PRESETS
    from clipforge.utils.text import slugify

    key = "custom-" + slugify(body.label, 30)
    style = body.model_dump(exclude={"label"})
    with db.session() as s:
        existing = s.exec(select(db.Preset).where(db.Preset.key == key)).first()
        p = existing or db.Preset(key=key, label=body.label)
        p.label, p.style_json = body.label, json.dumps(style)
        s.add(p)
        s.commit()
    PRESETS[key] = {"label": body.label, "pop": 1.15, "outline": 6, "shadow": 3, **style}
    return {"key": key, "label": body.label, "builtin": False, **style}


@router.delete("/presets/{key}")
def delete_preset(key: str) -> dict:
    from clipforge.stages.captions import PRESETS

    with db.session() as s:
        p = s.exec(select(db.Preset).where(db.Preset.key == key)).first()
        if not p:
            raise HTTPException(404, "Preset not found (built-in presets can't be deleted)")
        s.delete(p)
        s.commit()
    PRESETS.pop(key, None)
    return {"ok": True}


def load_custom_presets() -> None:
    """Register saved custom presets so the renderer can use them."""
    from clipforge.stages.captions import PRESETS

    with db.session() as s:
        for p in s.exec(select(db.Preset)).all():
            PRESETS[p.key] = {"label": p.label, "pop": 1.15, "outline": 6, "shadow": 3, **json.loads(p.style_json)}


# ============================================================================ tracker
class PostIn(BaseModel):
    job_id: Optional[int] = None
    clip_rank: Optional[int] = None
    clip_title: str = ""
    platform: Literal["instagram", "youtube"] = "instagram"
    url: str = ""
    posted_at: Optional[dt.date] = None
    campaign: str = ""
    rate_per_1k: float = 0
    views: int = 0
    notes: str = ""
    campaign_fee_pct: float = 0
    submission_status: Literal["not_submitted", "submitted", "approved", "rejected", "paid"] = "not_submitted"
    submission_link: str = ""
    approved_views: Optional[int] = None
    queue_id: Optional[int] = None
    youtube_video_id: str = ""


def _post_out(p: db.Post) -> dict:
    from clipforge.publish.analytics import earnings

    d = p.model_dump()
    # approved views × rate per 1,000 × (1 − campaign fee); rejected submissions earn nothing
    d["earnings"] = 0.0 if p.submission_status == "rejected" else earnings(p)
    return d


@router.get("/posts")
def list_posts() -> dict:
    with db.session() as s:
        posts = list(s.exec(select(db.Post).order_by(db.Post.posted_at.desc(), db.Post.id.desc())).all())  # type: ignore[union-attr]
    rows = [_post_out(p) for p in posts]
    return {"posts": rows, "totals": {"posts": len(rows), "views": sum(r["views"] for r in rows),
                                      "earnings": round(sum(r["earnings"] for r in rows), 2)}}


@router.post("/posts")
def create_post(body: PostIn) -> dict:
    with db.session() as s:
        p = db.Post(**body.model_dump())
        s.add(p)
        s.commit()
        s.refresh(p)
        return _post_out(p)


@router.patch("/posts/{post_id}")
def update_post(post_id: int, body: dict) -> dict:
    allowed = set(PostIn.model_fields)
    with db.session() as s:
        p = s.get(db.Post, post_id)
        if not p:
            raise HTTPException(404, "Post not found")
        clean = PostIn.model_validate({**p.model_dump(include=allowed), **{k: v for k, v in body.items() if k in allowed}})
        for k, v in clean.model_dump().items():
            setattr(p, k, v)
        s.add(p)
        s.commit()
        s.refresh(p)
        return _post_out(p)


@router.delete("/posts/{post_id}")
def delete_post(post_id: int) -> dict:
    with db.session() as s:
        p = s.get(db.Post, post_id)
        if not p:
            raise HTTPException(404, "Post not found")
        s.delete(p)
        s.commit()
    return {"ok": True}


@router.get("/posts.csv")
def posts_csv() -> Response:
    data = list_posts()["posts"]
    out = io.StringIO()
    cols = ["id", "platform", "url", "posted_at", "campaign", "clip_title", "views", "approved_views", "rate_per_1k",
            "campaign_fee_pct", "submission_status", "submission_link", "earnings", "notes"]
    w = csv.writer(out)
    w.writerow(cols)
    for r in data:
        w.writerow([r.get(c, "") for c in cols])
    return Response("﻿" + out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="clipforge_tracker.csv"'})
