"""REST + SSE endpoints. OpenAPI docs at /docs."""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import json
import queue
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import select

from clipforge import pipeline
from clipforge.api import db
from clipforge.api.jobs import event_json, job_config, manager
from clipforge.config import (Config, ConfigError, load_config, load_secrets, mask_secret, save_config, save_secret)
from clipforge.models import CaptionStyle, ClipCandidate, ClipMetadata, CropOverride, CropPlan
from clipforge.workspace import Workspace, is_url, video_id_for

router = APIRouter(prefix="/api")


# ============================================================================ helpers
def _cfg() -> Config:
    try:
        return load_config()
    except ConfigError as exc:
        raise HTTPException(500, str(exc))


def _job_or_404(job_id: int) -> db.Job:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    return job


def _ws(job: db.Job) -> Workspace:
    if not job.video_id:
        raise HTTPException(409, "This job has not downloaded its video yet.")
    try:
        return pipeline.open_workspace(job.video_id, _cfg())
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))


def _parse_clip_id(clip_id: str) -> tuple[db.Job, int]:
    try:
        job_part, rank_part = clip_id.split("-", 1)
        job_id, rank = int(job_part), int(rank_part)
    except ValueError:
        raise HTTPException(400, "Clip id must look like '<job_id>-<rank>', e.g. 3-7")
    return _job_or_404(job_id), rank


def _find_cand(ws: Workspace, rank: int) -> tuple[list[ClipCandidate], ClipCandidate]:
    cands = pipeline.load_candidates(ws)
    for c in cands:
        if c.rank == rank:
            return cands, c
    raise HTTPException(404, f"Clip {rank} not found")


def _metas(ws: Workspace) -> dict[int, ClipMetadata]:
    if not ws.metadata_json.exists():
        return {}
    return {m["rank"]: ClipMetadata.model_validate(m) for m in json.loads(ws.metadata_json.read_text(encoding="utf-8"))}


def _clip_out(job: db.Job, ws: Workspace, c: ClipCandidate, metas: dict[int, ClipMetadata]) -> dict:
    f = ws.find_clip(c.rank)
    work = ws.clips_dir / "_work" / f"{c.rank:02d}"
    rendered_at = f.stat().st_mtime if f else None
    stale = False
    rj = work / "render.json"
    if f and rj.exists():
        prev = json.loads(rj.read_text(encoding="utf-8")).get("candidate", {})
        keys = ("start", "end", "title", "hook", "layout", "caption_style", "word_edits", "crop_overrides")
        stale = any(prev.get(k) != c.model_dump().get(k) for k in keys)
    cid = f"{job.id}-{c.rank}"
    v = int(rendered_at or 0)
    return {
        "id": cid, "job_id": job.id, "rank": c.rank, "start": c.start, "end": c.end, "duration": c.duration,
        "title": c.title, "hook": c.hook, "topic": c.topic, "why": c.why, "score": c.score, "scores": c.scores.model_dump(),
        "enabled": c.enabled, "approved": c.approved, "layout": c.layout, "caption_style": c.caption_style.model_dump(),
        "word_edits": c.word_edits, "crop_overrides": [o.model_dump() for o in c.crop_overrides],
        "rendered": f is not None, "stale": stale, "file": f.name if f else None,
        "video_url": f"/api/clips/{cid}/video?v={v}" if f else None,
        "thumbnail_url": f"/api/clips/{cid}/thumbnail?v={v}" if (work / "thumb.jpg").exists() else None,
        "has_previous": (work / "previous.mp4").exists(),
        "metadata": metas[c.rank].model_dump() if c.rank in metas else None,
    }


def _job_out(job: db.Job) -> dict:
    d = job.model_dump()
    d["settings"] = job.settings
    d.pop("settings_json", None)
    d["busy"] = manager.busy(job.id)  # type: ignore[arg-type]
    d["thumbnail_url"] = f"/api/jobs/{job.id}/thumbnail" if job.video_id else None
    if job.video_id:
        cfg = _cfg()
        clips_dir = cfg.workspace / job.video_id / "clips"
        d["n_rendered"] = len(list(clips_dir.glob("*.mp4"))) if clips_dir.exists() else 0
    else:
        d["n_rendered"] = 0
    return d


# ============================================================================ jobs
class JobSettings(BaseModel):
    clips: Optional[int] = Field(None, ge=1, le=50)
    min_sec: Optional[float] = None
    max_sec: Optional[float] = None
    caption_script: Optional[Literal["roman", "devanagari"]] = None
    caption_preset: Optional[str] = None
    layout: Optional[Literal["auto", "track", "split", "blur"]] = None
    provider: Optional[Literal["ollama", "gemini", "groq", "mock"]] = None
    model: Optional[str] = None
    whisper_model: Optional[str] = None
    language: Optional[str] = None
    section: Optional[tuple[float, float]] = None


class JobCreate(BaseModel):
    url: Optional[str] = None
    upload_id: Optional[str] = None
    settings: JobSettings = JobSettings()


@router.post("/jobs", summary="Create a clip job from a YouTube URL or an uploaded file")
def create_job(body: JobCreate) -> dict:
    if body.upload_id:
        path = _uploads_dir() / body.upload_id
        meta = path.with_suffix(".json")
        if not path.exists() or not meta.exists() or not json.loads(meta.read_text())["complete"]:
            raise HTTPException(400, "Upload not found or not complete")
        name = json.loads(meta.read_text())["filename"]
        final = path.with_name(f"{body.upload_id}_{Path(name).name}")
        if not final.exists():
            path.replace(final)
        source = str(final)
        title = Path(name).stem
    elif body.url and body.url.strip():
        source = body.url.strip()
        if not is_url(source) and not Path(source).exists():
            raise HTTPException(400, "Paste a YouTube link (https://…) or upload a video file.")
        title = ""
    else:
        raise HTTPException(400, "Provide a YouTube URL or upload a file.")
    settings = body.settings.model_dump(exclude_none=True)
    try:
        job_config(settings)  # validate early
    except Exception as exc:
        raise HTTPException(400, f"Invalid settings: {exc}")
    section = tuple(settings["section"]) if settings.get("section") else None
    with db.session() as s:
        job = db.Job(source=source, title=title, settings_json=json.dumps(settings),
                     video_id=video_id_for(source, section))  # type: ignore[arg-type]
        s.add(job)
        s.commit()
        s.refresh(job)
    manager.submit("pipeline", job.id)  # type: ignore[arg-type]
    return _job_out(job)


@router.get("/jobs")
def list_jobs() -> list[dict]:
    return [_job_out(j) for j in db.list_jobs()]


@router.get("/jobs/{job_id}")
def get_job(job_id: int) -> dict:
    return _job_out(_job_or_404(job_id))


@router.delete("/jobs/{job_id}")
def delete_job(job_id: int, delete_files: bool = False) -> dict:
    job = _job_or_404(job_id)
    if manager.busy(job_id):
        manager.cancel(job_id)
    with db.session() as s:
        obj = s.get(db.Job, job_id)
        if obj:
            s.delete(obj)
            s.commit()
    if delete_files and job.video_id:
        others = [j for j in db.list_jobs() if j.video_id == job.video_id]
        if not others:
            shutil.rmtree(_cfg().workspace / job.video_id, ignore_errors=True)
    return {"ok": True}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int) -> dict:
    _job_or_404(job_id)
    manager.cancel(job_id)
    return {"ok": True}


@router.post("/jobs/{job_id}/retry", summary="Re-run a failed/cancelled job (finished stages are reused)")
def retry_job(job_id: int) -> dict:
    job = _job_or_404(job_id)
    if manager.busy(job_id):
        raise HTTPException(409, "Job is already running or queued")
    manager.submit("pipeline", job_id)
    return _job_out(db.get_job(job_id) or job)


@router.get("/jobs/{job_id}/events", summary="Server-Sent Events: stage, progress, ETA, logs")
async def job_events(job_id: int, request: Request) -> StreamingResponse:
    job = _job_or_404(job_id)
    sq, backlog = manager.subscribe(job_id)

    async def gen():
        try:
            yield f"event: snapshot\ndata: {event_json({'type': 'snapshot', 'job': _job_out(job)})}\n\n"
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
    job = _job_or_404(job_id)
    if not job.video_id or not (_cfg().workspace / job.video_id / "candidates.json").exists():
        return []
    ws = _ws(job)
    metas = _metas(ws)
    return [_clip_out(job, ws, c, metas) for c in pipeline.load_candidates(ws)]


class RenderRequest(BaseModel):
    ranks: Optional[list[int]] = None
    force: bool = False
    only_approved: bool = False
    only_stale: bool = False


@router.post("/jobs/{job_id}/render-all")
def render_all(job_id: int, body: RenderRequest = RenderRequest()) -> dict:
    job = _job_or_404(job_id)
    ws = _ws(job)
    cands = pipeline.load_candidates(ws)
    ranks = body.ranks
    if body.only_approved:
        ranks = [c.rank for c in cands if c.approved]
    manager.submit("render", job_id, {"ranks": ranks, "force": body.force})
    return {"ok": True, "queued": ranks or "all"}


class BulkApprove(BaseModel):
    min_score: float
    approve: bool = True


@router.post("/jobs/{job_id}/approve", summary="Approve every clip with score ≥ min_score")
def bulk_approve(job_id: int, body: BulkApprove) -> dict:
    ws = _ws(_job_or_404(job_id))
    cands = pipeline.load_candidates(ws)
    n = 0
    for c in cands:
        if c.score >= body.min_score:
            c.approved = body.approve
            n += 1
    pipeline.save_candidates(ws, cands)
    return {"ok": True, "changed": n}


@router.get("/jobs/{job_id}/source-waveform", summary="Waveform peaks for [start, end] of the source audio")
def source_waveform(job_id: int, start: float = 0, end: float = 60, per_sec: int = Query(50, le=200)) -> dict:
    from clipforge.stages.analyze_audio import waveform_peaks

    ws = _ws(_job_or_404(job_id))
    info = pipeline.load_info(ws)
    start, end = max(0.0, start), min(info.duration, end)
    if end - start > 900:
        raise HTTPException(400, "Max 15 minutes per request")
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
def export_zip(job_id: int, all_clips: bool = False) -> StreamingResponse:
    job = _job_or_404(job_id)
    ws = _ws(job)
    cands = [c for c in pipeline.load_candidates(ws) if c.enabled]
    chosen = cands if all_clips or not any(c.approved for c in cands) else [c for c in cands if c.approved]
    metas = _metas(ws)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:  # mp4 is already compressed
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
    buf.seek(0)
    name = f"clipforge_{job.video_id}.zip"
    return StreamingResponse(buf, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ============================================================================ clips
@router.get("/clips/{clip_id}")
def get_clip(clip_id: str) -> dict:
    job, rank = _parse_clip_id(clip_id)
    ws = _ws(job)
    cands, c = _find_cand(ws, rank)
    out = _clip_out(job, ws, c, _metas(ws))
    from clipforge.stages.captions import resolve_style
    from clipforge.utils.timecode import words_in_range

    cfg = job_config(job.settings)
    script = resolve_style(cfg.captions, c.caption_style).script
    tr = pipeline.load_transcript(ws)
    inside = words_in_range(tr.all_words(), c.start, c.end)
    out["words"] = [{"index": i, "start": w.start, "end": w.end, "text": w.display(script),
                     "edited": c.word_edits.get(str(i))} for i, w in enumerate(inside)]
    out["script"] = script
    info = pipeline.load_info(ws)
    out["source"] = {"duration": info.duration, "width": info.width, "height": info.height, "title": info.title}
    plan_file = ws.clips_dir / "_work" / f"{rank:02d}" / "crop_plan.json"
    out["crop_plan"] = json.loads(plan_file.read_text(encoding="utf-8")) if plan_file.exists() else None
    return out


class ClipPatch(BaseModel):
    start: Optional[float] = None
    end: Optional[float] = None
    title: Optional[str] = None
    hook: Optional[str] = None
    approved: Optional[bool] = None
    enabled: Optional[bool] = None
    layout: Optional[Literal["auto", "track", "split", "blur"]] = None
    clear_layout: bool = False
    caption_style: Optional[CaptionStyle] = None
    word_edits: Optional[dict[str, str]] = None
    crop_overrides: Optional[list[CropOverride]] = None
    snap: bool = True


@router.patch("/clips/{clip_id}", summary="Edit a clip (times snap to word boundaries; re-render to apply)")
def patch_clip(clip_id: str, body: ClipPatch) -> dict:
    from clipforge.utils.timecode import snap_to_words

    job, rank = _parse_clip_id(clip_id)
    ws = _ws(job)
    cands, c = _find_cand(ws, rank)
    data = body.model_dump(exclude_unset=True, exclude={"snap", "clear_layout"})
    if "start" in data or "end" in data:
        start = data.get("start", c.start)
        end = data.get("end", c.end)
        info = pipeline.load_info(ws)
        start, end = max(0.0, start), min(info.duration, end)
        if end - start < 3:
            raise HTTPException(400, "A clip must be at least 3 seconds long")
        if body.snap:
            tr = pipeline.load_transcript(ws)
            starts, ends = tr.boundaries()
            start, end, _ = snap_to_words(tr.all_words(), start, end, pad_start=0.15, pad_end=0.3, search=1.5,
                                          seg_starts=starts, seg_ends=ends)
        if (start, end) != (c.start, c.end):
            c.word_edits = {}  # word indices shift when the range changes
            c.crop_overrides = []
        c.start, c.end = round(start, 3), round(end, 3)
        data.pop("start", None)
        data.pop("end", None)
    for k, v in data.items():
        if k == "caption_style" and v is not None:
            c.caption_style = CaptionStyle.model_validate(v)
        elif k == "crop_overrides" and v is not None:
            c.crop_overrides = [CropOverride.model_validate(o) for o in v]
        elif v is not None:
            setattr(c, k, v)
    if body.clear_layout:
        c.layout = None
    pipeline.save_candidates(ws, cands)
    return _clip_out(job, ws, c, _metas(ws))


@router.post("/clips/{clip_id}/render", summary="Re-render one clip (queued; progress on the job's SSE stream)")
def render_clip(clip_id: str) -> dict:
    job, rank = _parse_clip_id(clip_id)
    _find_cand(_ws(job), rank)
    manager.submit("render", job.id, {"ranks": [rank]})  # type: ignore[arg-type]
    return {"ok": True}


@router.post("/clips/{clip_id}/undo", summary="Restore the previous render and its settings")
def undo_clip(clip_id: str) -> dict:
    job, rank = _parse_clip_id(clip_id)
    ws = _ws(job)
    cands, c = _find_cand(ws, rank)
    work = ws.work_dir(rank)
    prev_mp4, prev_json = work / "previous.mp4", work / "previous.json"
    if not prev_mp4.exists() or not prev_json.exists():
        raise HTTPException(404, "No previous version to restore")
    prev = json.loads(prev_json.read_text(encoding="utf-8"))
    old = ClipCandidate.model_validate(prev["candidate"])
    old.rank, old.approved, old.score = c.rank, c.approved, c.score
    cur = ws.find_clip(rank)
    swap_mp4, swap_json = work / "swap.mp4", work / "swap.json"
    if cur:
        cur.replace(swap_mp4)
        shutil.copy2(work / "render.json", swap_json)
    target = ws.clip_path(rank, old.title)
    prev_mp4.replace(target)
    shutil.copy2(prev_json, work / "render.json")
    if swap_mp4.exists():  # current becomes "previous" so undo can be undone
        swap_mp4.replace(prev_mp4)
        swap_json.replace(prev_json)
    else:
        prev_json.unlink(missing_ok=True)
    idx = cands.index(c)
    cands[idx] = old
    pipeline.save_candidates(ws, cands)
    try:
        pipeline.metadata(ws, job_config(job.settings))
    except Exception:
        pass
    return _clip_out(job, ws, old, _metas(ws))


@router.get("/clips/{clip_id}/video", summary="Rendered clip (supports HTTP Range)")
def clip_video(clip_id: str, download: bool = False) -> FileResponse:
    job, rank = _parse_clip_id(clip_id)
    f = _ws(job).find_clip(rank)
    if not f:
        raise HTTPException(404, "Clip not rendered yet")
    return FileResponse(f, media_type="video/mp4", filename=f.name if download else None,
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
    cache = ws.work_dir(rank) / f"frame_{t:.2f}.jpg"
    if not cache.exists():
        extract_frame(ws.source, t, cache, width=960)
    return FileResponse(cache, media_type="image/jpeg")


# ============================================================================ uploads (chunked)
def _uploads_dir() -> Path:
    p = _cfg().workspace / "uploads"
    p.mkdir(parents=True, exist_ok=True)
    return p


class UploadInit(BaseModel):
    filename: str
    size: int


@router.post("/uploads", summary="Start a chunked upload of a local video file")
def upload_init(body: UploadInit) -> dict:
    uid = uuid.uuid4().hex[:12]
    d = _uploads_dir()
    (d / uid).write_bytes(b"")
    (d / f"{uid}.json").write_text(json.dumps({"filename": body.filename, "size": body.size, "complete": False}))
    return {"upload_id": uid}


@router.put("/uploads/{upload_id}", summary="Append a chunk (raw body) at ?offset=")
async def upload_chunk(upload_id: str, request: Request, offset: int = 0) -> dict:
    d = _uploads_dir()
    path = d / upload_id
    if not path.exists() or "/" in upload_id or "\\" in upload_id:
        raise HTTPException(404, "Unknown upload")
    size = path.stat().st_size
    if offset != size:
        raise HTTPException(409, f"Expected offset {size}")
    with open(path, "ab") as f:
        async for chunk in request.stream():
            f.write(chunk)
    return {"received": path.stat().st_size}


@router.post("/uploads/{upload_id}/complete")
def upload_complete(upload_id: str) -> dict:
    d = _uploads_dir()
    meta_p = d / f"{upload_id}.json"
    if not meta_p.exists():
        raise HTTPException(404, "Unknown upload")
    meta = json.loads(meta_p.read_text())
    size = (d / upload_id).stat().st_size
    if size != meta["size"]:
        raise HTTPException(400, f"Upload incomplete: {size}/{meta['size']} bytes")
    meta["complete"] = True
    meta_p.write_text(json.dumps(meta))
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


@router.put("/settings", summary="Update config.yaml values and/or API keys (.env)")
def put_settings(body: SettingsUpdate) -> dict:
    if body.config is not None:
        cur = _cfg().model_dump(mode="json")
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
            save_secret(name, value.strip())
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


def _post_out(p: db.Post) -> dict:
    d = p.model_dump()
    d["earnings"] = round(p.views / 1000 * p.rate_per_1k, 2)
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
    cols = ["id", "platform", "url", "posted_at", "campaign", "clip_title", "views", "rate_per_1k", "earnings", "notes"]
    w = csv.writer(out)
    w.writerow(cols)
    for r in data:
        w.writerow([r.get(c, "") for c in cols])
    return Response("﻿" + out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="clipforge_tracker.csv"'})
