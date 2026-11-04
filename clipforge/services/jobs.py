"""Jobs: create (URL / uploaded file), list, cancel, retry, re-run a stage, delete (+ disk freed)."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from clipforge import pipeline
from clipforge.api import db
from clipforge.api.jobs import job_config, manager
from clipforge.config import Config, ConfigError, load_config
from clipforge.models import ClipCandidate, ClipMetadata
from clipforge.services.errors import BadRequest, Conflict, NotFound, ServiceError
from clipforge.workspace import Workspace, is_url, video_id_for

STAGES = pipeline.STAGES  # download → transcribe → analyze → select → reframe → captions → render
RERUN_STAGES = ("download", "transcribe", "analyze", "select", "render", "metadata")


# ============================================================================ helpers
def cfg() -> Config:
    try:
        return load_config()
    except ConfigError as exc:
        raise ServiceError(str(exc), 500)


def job_or_404(job_id: int) -> db.Job:
    job = db.get_job(job_id)
    if not job:
        raise NotFound(f"Job {job_id} not found")
    return job


def ws_for(job: db.Job) -> Workspace:
    if not job.video_id:
        raise Conflict("This job has not downloaded its video yet.")
    try:
        return pipeline.open_workspace(job.video_id, cfg())
    except FileNotFoundError:
        raise NotFound(f"The files of job {job.id} are gone (deleted?). Retry the job to rebuild them.")


def parse_clip_id(clip_id: str) -> tuple[db.Job, int]:
    try:
        job_part, rank_part = clip_id.split("-", 1)
        job_id, rank = int(job_part), int(rank_part)
    except ValueError:
        raise BadRequest("Clip id must look like '<job_id>-<rank>', e.g. 3-7")
    return job_or_404(job_id), rank


def find_cand(ws: Workspace, rank: int) -> tuple[list[ClipCandidate], ClipCandidate]:
    cands = pipeline.load_candidates(ws)
    for c in cands:
        if c.rank == rank:
            return cands, c
    raise NotFound(f"Clip {rank} not found")


def metas(ws: Workspace) -> dict[int, ClipMetadata]:
    if not ws.metadata_json.exists():
        return {}
    return {m["rank"]: ClipMetadata.model_validate(m) for m in json.loads(ws.metadata_json.read_text(encoding="utf-8"))}


def clip_out(job: db.Job, ws: Workspace, c: ClipCandidate, metas_: dict[int, ClipMetadata]) -> dict:
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
        "metadata": metas_[c.rank].model_dump() if c.rank in metas_ else None,
    }


def job_out(job: db.Job) -> dict:
    d = job.model_dump()
    d["settings"] = job.settings
    d.pop("settings_json", None)
    d["busy"] = manager.busy(job.id)  # type: ignore[arg-type]
    d["thumbnail_url"] = f"/api/jobs/{job.id}/thumbnail" if job.video_id else None
    if job.video_id:
        clips_dir = cfg().workspace / job.video_id / "clips"
        d["n_rendered"] = len(list(clips_dir.glob("*.mp4"))) if clips_dir.exists() else 0
    else:
        d["n_rendered"] = 0
    d["runs_on"] = where_running(job.id)  # type: ignore[arg-type]
    d["transcript"] = transcript_status(job)
    return d


def where_running(job_id: int) -> str:
    """'laptop' / 'server' / '<worker name>' — where the job's heavy work happens (Part B)."""
    try:
        from clipforge.server import leases

        return leases.where(job_id)
    except Exception:
        from clipforge.server.status import node_name

        return f"on {node_name()}"


# ============================================================================ uploads
UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def uploads_dir() -> Path:
    p = cfg().workspace / "uploads"
    p.mkdir(parents=True, exist_ok=True)
    return p


def upload_path(upload_id: str) -> Path:
    if not UPLOAD_ID_RE.match(upload_id):  # ids are generated by us; anything else could be a path
        raise NotFound("Unknown upload")
    return uploads_dir() / upload_id


def new_upload(filename: str, size: int) -> str:
    uid = uuid.uuid4().hex[:12]
    d = uploads_dir()
    (d / uid).write_bytes(b"")
    (d / f"{uid}.json").write_text(json.dumps({"filename": Path(filename).name, "size": size, "complete": False}))
    return uid


def finish_upload(upload_id: str) -> None:
    path = upload_path(upload_id)
    meta_p = path.with_suffix(".json")
    if not meta_p.exists():
        raise NotFound("Unknown upload")
    meta = json.loads(meta_p.read_text())
    size = path.stat().st_size
    if size != meta["size"]:
        raise BadRequest(f"Upload incomplete: {size}/{meta['size']} bytes")
    meta["complete"] = True
    meta_p.write_text(json.dumps(meta))


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
    transcript_source: Optional[Literal["auto", "pasted", "youtube", "whisper"]] = None


class JobCreate(BaseModel):
    url: Optional[str] = None
    upload_id: Optional[str] = None
    settings: JobSettings = JobSettings()
    transcript: Optional[str] = None  # pasted transcript with timestamps (YouTube copy, SRT, VTT, [hh:mm:ss] text)


def create_job(body: JobCreate, remote: bool = False) -> dict:
    """remote=True: the request came from a phone (web or Telegram) — a laptop file path is not accepted."""
    settings = body.settings.model_dump(exclude_none=True)
    try:
        job_config(settings)  # validate before touching the upload, so a bad setting doesn't consume it
    except Exception as exc:
        raise BadRequest(f"Invalid settings: {exc}")
    if body.upload_id:
        path = upload_path(body.upload_id)
        meta = path.with_suffix(".json")
        if not path.exists() or not meta.exists() or not json.loads(meta.read_text())["complete"]:
            raise BadRequest("Upload not found or not complete")
        name = json.loads(meta.read_text())["filename"]
        final = path.with_name(f"{body.upload_id}_{Path(name).name}")
        if not final.exists():
            path.replace(final)
        source = str(final)
        title = Path(name).stem
    elif body.url and body.url.strip():
        source = body.url.strip()
        if not is_url(source) and (remote or not Path(source).exists()):
            # a file path on the laptop is only accepted from the laptop itself; phones upload the file instead
            raise BadRequest("Paste a YouTube link (https://…) or upload a video file.")
        title = ""
    else:
        raise BadRequest("Provide a YouTube URL or upload a file.")
    _check_disk_for_new_job()
    section = tuple(settings["section"]) if settings.get("section") else None
    pasted = None
    if body.transcript and body.transcript.strip():
        from clipforge.stages import transcript_input as ti

        try:
            pasted = ti.parse_or_raise(body.transcript)
        except ti.TranscriptParseError as exc:
            raise BadRequest(str(exc))
    with db.session() as s:
        job = db.Job(source=source, title=title, settings_json=json.dumps(settings),
                     video_id=video_id_for(source, section))  # type: ignore[arg-type]
        s.add(job)
        s.commit()
        s.refresh(job)
    if pasted is not None:
        from clipforge.stages import transcript_input as ti

        ti.save_input(Workspace(cfg().workspace, job.video_id), body.transcript or "", section)  # type: ignore[arg-type]
    manager.submit("pipeline", job.id)  # type: ignore[arg-type]
    return job_out(job)


def transcript_status(job: db.Job) -> dict | None:
    """{source, label, word_timing, warnings, saved_sec, …} once the transcribe stage ran."""
    if not job.video_id:
        return None
    from clipforge.stages import transcript_input as ti

    return ti.read_report(cfg().workspace / job.video_id)


def use_whisper(job_id: int) -> dict:
    """The transcript looked wrong: transcribe this job with Whisper after all (and redo everything after it)."""
    job = job_or_404(job_id)
    if job.settings.get("transcript_source") == "whisper":
        raise Conflict("This job already uses Whisper.")
    db.update_job(job_id, settings_json=json.dumps({**job.settings, "transcript_source": "whisper"}))
    if manager.busy(job_id):  # it kept going with the transcript: stop it, the new run starts after it
        manager.cancel(job_id)
    # the transcribe fingerprint includes the source → transcribe runs again, and select after it
    manager.submit("pipeline", job_id, {"force_select": True})
    return job_out(db.get_job(job_id) or job)


def _check_disk_for_new_job() -> None:
    """Server mode: refuse new jobs above server.disk_pause_pct (standalone keeps its old behaviour: warn only)."""
    c = cfg()
    if c.deployment.mode != "server":
        return
    from clipforge.server import housekeeping

    housekeeping.refuse_if_disk_full(c)


def jobs_for_video(video_id: str) -> list[db.Job]:
    return [j for j in db.list_jobs() if j.video_id == video_id]


def duplicate_of(url: str, section: tuple[float, float] | None = None) -> db.Job | None:
    """An existing job for the same video (youtu.be/ID and watch?v=ID&si=… are the same video)."""
    vid = video_id_for(url, section)
    hits = [j for j in jobs_for_video(vid) if j.status != "cancelled"]
    return hits[0] if hits else None


def list_jobs() -> list[dict]:
    return [job_out(j) for j in db.list_jobs()]


def get_job(job_id: int) -> dict:
    return job_out(job_or_404(job_id))


def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


def delete_job(job_id: int, delete_files: bool = False) -> dict:
    """Returns {"ok", "freed_bytes"} — files are only deleted when no other job uses the same video
    and no unpublished queue item still needs them."""
    job = job_or_404(job_id)
    if manager.busy(job_id):
        manager.cancel(job_id)
    with db.session() as s:
        obj = s.get(db.Job, job_id)
        if obj:
            s.delete(obj)
            s.commit()
    freed = 0
    kept_reason = ""
    if delete_files and job.video_id:
        others = jobs_for_video(job.video_id)
        if others:
            kept_reason = f"job {others[0].id} uses the same video"
        elif _queued_items(job.video_id):
            kept_reason = "clips of it are still waiting to be published"
        else:
            d = cfg().workspace / job.video_id
            freed = _dir_size(d)
            shutil.rmtree(d, ignore_errors=True)
    return {"ok": True, "freed_bytes": freed, "kept_files_because": kept_reason}


def _queued_items(video_id: str) -> list[db.QueueItem]:
    from clipforge.publish import queue as q

    return [it for it in q.items() if it.video_id == video_id and it.status not in ("published",)]


def cancel_job(job_id: int) -> dict:
    job_or_404(job_id)
    manager.cancel(job_id)
    return {"ok": True}


def retry_job(job_id: int) -> dict:
    job = job_or_404(job_id)
    if manager.busy(job_id):
        raise Conflict("Job is already running or queued")
    manager.submit("pipeline", job_id)
    return job_out(db.get_job(job_id) or job)


def rerun_stage(job_id: int, stage: str) -> dict:
    """Forget one stage's cache (and everything after it), then run the job again from there."""
    job = job_or_404(job_id)
    stage = stage.lower()
    if stage not in RERUN_STAGES:
        raise BadRequest(f"Stage must be one of: {', '.join(RERUN_STAGES)}")
    if manager.busy(job_id):
        raise Conflict("Job is already running or queued")
    if stage in ("render", "metadata"):
        ws_for(job)
        manager.submit("render", job_id, {"ranks": None, "force": stage == "render"})
        return job_out(job)
    if job.video_id and (cfg().workspace / job.video_id).exists():
        ws = Workspace(cfg().workspace, job.video_id)
        later = STAGES[STAGES.index(stage):]
        ws.invalidate(*later)
    payload = {"force_select": stage in ("download", "transcribe", "analyze", "select")}
    manager.submit("pipeline", job_id, payload)
    return job_out(db.get_job(job_id) or job)
