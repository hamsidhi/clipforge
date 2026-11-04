"""Worker protocol endpoints (server side) + server admin endpoints. Spec: docs/WORKER_PROTOCOL.md.

/api/worker/* and /api/server/fence: machine-to-machine, authenticated by the worker's own key (Authorization: Bearer
cfw_…) — never the owner's password or browser session (AuthMiddleware skips them; every route here checks the key).
"""

from __future__ import annotations

import json
import threading
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from clipforge.api import db
from clipforge.config import load_config
from clipforge.server import artifacts, leases
from clipforge.utils.log import log

router = APIRouter(prefix="/api", tags=["server"])
MAX_CHUNK = 16 * 1024 * 1024
_commit_locks: dict[int, threading.Lock] = {}
_commit_guard = threading.Lock()


def _task_lock(task_id: int) -> threading.Lock:
    with _commit_guard:
        return _commit_locks.setdefault(task_id, threading.Lock())


def worker_auth(request: Request) -> db.WorkerNode:
    h = request.headers.get("authorization", "")
    key = h[7:].strip() if h.lower().startswith("bearer ") else ""
    w = leases.auth(key)
    if not w:
        raise HTTPException(401, "Unknown or revoked worker key")
    if load_config().deployment.mode != "server":
        raise HTTPException(409, "This computer isn't a ClipForge server (deployment.mode is not 'server').")
    return w


def _lease(w: db.WorkerNode, task_id: int, fence: int) -> db.StageTask:
    t = leases.get(task_id)
    if not t or t.worker_id != w.id or t.fence != fence or t.status != "leased":
        raise HTTPException(409, "Not your lease any more (it expired or the job was cancelled)")
    return t


# ============================================================================ protocol
class HelloIn(BaseModel):
    capabilities: list[str] = []
    max_parallel: int = 1
    gpu: bool = False
    version: str = ""


@router.post("/worker/hello")
def hello(body: HelloIn, w: db.WorkerNode = Depends(worker_auth)) -> dict:
    from clipforge.server.status import node_name

    w = leases.hello(w.id, body.capabilities, body.max_parallel, body.gpu, body.version)
    return {"worker_id": w.id, "name": w.name, "capabilities": w.capabilities, "server": node_name(),
            "lease_seconds": load_config().deployment.lease_seconds,
            "heartbeat_seconds": load_config().deployment.heartbeat_seconds}


class ClaimIn(BaseModel):
    capabilities: list[str] = []


@router.post("/worker/claim")
def claim(body: ClaimIn, w: db.WorkerNode = Depends(worker_auth)) -> dict:
    t = leases.claim(w, body.capabilities or w.capabilities)
    if not t:
        return {"task": None}
    job = db.get_job(t.job_id)
    if not job:
        leases.cancel_job(t.job_id)
        return {"task": None}
    from clipforge.api.jobs import job_config
    from clipforge.workspace import Workspace

    cfg = job_config(job.settings)
    p = t.payload
    manifest = []
    if job.video_id and (cfg.workspace / job.video_id).exists() and t.stage != "import":
        manifest = artifacts.manifest(Workspace(cfg.workspace, job.video_id), t.stage)
    return {"task": {"id": t.id, "fence": t.fence, "job_id": t.job_id, "stage": t.stage, "video_id": job.video_id,
                     "source": job.source, "section": job.settings.get("section"), "title": job.title,
                     "config": cfg.model_dump(mode="json"), "ranks": p.get("ranks"), "force": bool(p.get("force")),
                     "force_select": bool(p.get("force_select")), "manifest": manifest}}


class BeatIn(BaseModel):
    task_id: int
    fence: int
    progress: float = 0
    eta: Optional[float] = None
    message: str = ""


@router.post("/worker/heartbeat")
def heartbeat(body: BeatIn, w: db.WorkerNode = Depends(worker_auth)) -> dict:
    try:
        return {"status": leases.heartbeat(w.id, body.task_id, body.fence, body.progress, body.eta, body.message)}
    except leases.StaleLease as exc:
        raise HTTPException(409, str(exc))


class FileIn(BaseModel):
    path: str
    size: int
    sha256: str


class CompleteIn(BaseModel):
    task_id: int
    fence: int
    files: list[FileIn] = []
    deleted: list[str] = []
    state: dict = {}
    result: dict = {}


@router.post("/worker/complete")
def complete(body: CompleteIn, w: db.WorkerNode = Depends(worker_auth)) -> dict:
    """Idempotent: the same completion twice is accepted once. Stale fence → 409 and nothing is changed."""
    from clipforge.server import dispatch

    with _task_lock(body.task_id):
        t = leases.get(body.task_id)
        if t and t.status == "done" and t.worker_id == w.id and t.fence == body.fence:
            return {"ok": True, "already": True}
        t = _lease(w, body.task_id, body.fence)
        job = db.get_job(t.job_id)
        if not job:
            raise HTTPException(404, "The job was deleted")
        cfg = load_config()
        from clipforge.workspace import Workspace

        if not job.video_id:  # never from the worker's own result: the server decides which workspace it writes
            raise HTTPException(400, "This job has no video id yet")
        ws = Workspace(cfg.workspace, job.video_id)
        try:  # leased → committing atomically (fence checked); the reaper never touches a committing task
            leases.begin_commit(w.id, t.id, t.fence)  # type: ignore[arg-type]
        except leases.StaleLease as exc:
            raise HTTPException(409, str(exc))
        try:
            n = artifacts.commit(ws, cfg.workspace, t.id, t.fence, [f.model_dump() for f in body.files],  # type: ignore[arg-type]
                                 body.state, body.deleted)
        except artifacts.ArtifactError as exc:
            leases.abort_commit(t.id, t.fence)  # type: ignore[arg-type]  # back to leased: the worker can re-upload
            raise HTTPException(422, str(exc))
        missing = dispatch.stage_output_missing(t, job)
        if missing:  # e.g. an empty hand-in: never mark a stage done that isn't — it would be delegated again forever
            leases.abort_commit(t.id, t.fence)  # type: ignore[arg-type]
            raise HTTPException(422, f"stage output missing: {missing}")
        try:
            leases.complete(w.id, t.id, t.fence, {**body.result, "files": n})  # type: ignore[arg-type]
        except leases.StaleLease as exc:
            raise HTTPException(409, str(exc))
    try:
        dispatch.on_task_done(t.id)  # type: ignore[arg-type]
    except Exception as exc:  # the reaper's repair step re-submits the job — never leave it hanging silently
        log.error(f"[server] continuing job {t.job_id} after task {t.id} failed: {exc}")
    return {"ok": True, "files": n}


class FailIn(BaseModel):
    task_id: int
    fence: int
    error: str
    retryable: bool = True
    blocked: bool = False


@router.post("/worker/fail")
def fail(body: FailIn, w: db.WorkerNode = Depends(worker_auth)) -> dict:
    from clipforge.server import dispatch

    try:
        out = leases.fail(w.id, body.task_id, body.fence, body.error, body.retryable)
    except leases.StaleLease as exc:
        raise HTTPException(409, str(exc))
    artifacts.discard(load_config().workspace, body.task_id)
    if out == "failed":
        dispatch.on_task_failed(body.task_id, body.error, body.blocked)
    return {"status": out}


def _artifact_target(w: db.WorkerNode, task_id: int, fence: int, path: str):
    _lease(w, task_id, fence)
    t = leases.get(task_id)
    job = db.get_job(t.job_id) if t else None
    if not job or not job.video_id:
        raise HTTPException(404, "No workspace")
    try:
        return artifacts.resolve(load_config().workspace / job.video_id, path)
    except artifacts.ArtifactError as exc:
        raise HTTPException(400, str(exc))


@router.get("/worker/artifacts/{task_id}/{path:path}")
def pull(task_id: int, path: str, fence: int, w: db.WorkerNode = Depends(worker_auth)):
    """Download a workspace file (HTTP Range supported → resumable)."""
    f = _artifact_target(w, task_id, fence, path)
    if not f.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(f)


@router.get("/worker/staged/{task_id}/{path:path}")
def staged(task_id: int, path: str, fence: int, w: db.WorkerNode = Depends(worker_auth)) -> dict:
    """How many bytes of an upload already arrived (resume from here)."""
    _lease(w, task_id, fence)
    try:
        return {"size": artifacts.staged_size(load_config().workspace, task_id, fence, path)}
    except artifacts.ArtifactError as exc:
        raise HTTPException(400, str(exc))


@router.put("/worker/artifacts/{task_id}/{path:path}")
async def push(task_id: int, path: str, fence: int, request: Request, offset: int = 0,
               w: db.WorkerNode = Depends(worker_auth)) -> dict:
    _lease(w, task_id, fence)
    body = bytearray()
    async for part in request.stream():  # ≤ 16 MB per chunk (the worker sends 8 MB): never buffer an unbounded body
        body += part
        if len(body) > MAX_CHUNK:
            raise HTTPException(413, "Chunk too large (max 16 MB)")
    try:
        size = artifacts.write_chunk(load_config().workspace, task_id, fence, path, offset, [bytes(body)])
    except artifacts.ArtifactError as exc:
        raise HTTPException(409 if "offset" in str(exc) else 400, str(exc))
    return {"size": size}


class ImportIn(BaseModel):
    video_id: str
    source: str
    title: str = ""
    settings: dict = {}


@router.post("/worker/import")
def import_job(body: ImportIn, w: db.WorkerNode = Depends(worker_auth)) -> dict:
    """A job the laptop ran while the server was unreachable: create it here, the worker then uploads its files."""
    import re

    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,79}", body.video_id) or ".." in body.video_id:
        raise HTTPException(400, "Bad video id")
    if (load_config().workspace / body.video_id).exists() or any(j.video_id == body.video_id for j in db.list_jobs()):
        raise HTTPException(409, "That video already exists on the server — nothing was overwritten.")
    from clipforge.server import housekeeping

    housekeeping.refuse_if_disk_full()
    with db.session() as s:
        job = db.Job(source=body.source[:500], video_id=body.video_id, title=body.title[:200], status="running",
                     stage="import", message=f"importing from {w.name}", settings_json=json.dumps(body.settings))
        s.add(job)
        s.commit()
        s.refresh(job)
    t = leases.create(job.id, "import", {"from": w.id}, hold=True)  # type: ignore[arg-type]
    t = leases.lease_to(t.id, w)  # type: ignore[arg-type]
    if not t:
        raise HTTPException(409, "Couldn't reserve the import")
    return {"job_id": job.id, "task_id": t.id, "fence": t.fence}


# ============================================================================ fence (emergency standalone)
class FenceIn(BaseModel):
    reason: str = ""


@router.post("/server/fence")
def fence(body: FenceIn, w: db.WorkerNode = Depends(worker_auth)) -> dict:
    """The laptop is switching to emergency standalone: this server stops publishing until `clipforge server unfence`."""
    from clipforge.server import dispatch

    dispatch.set_fenced(f"{w.name}: {body.reason[:200]}")
    return {"fenced": True}


# ============================================================================ admin (owner)
def _local_only(request: Request) -> None:
    if not getattr(request.state, "local", False):
        raise HTTPException(403, "Do this on the server itself (`clipforge server …` over SSH) — it creates a secret key.")


@router.get("/server/status")
def server_status() -> dict:
    from clipforge.server import dispatch, status

    return {**status.snapshot(), "fenced": dispatch.fenced()}


@router.get("/server/workers")
def server_workers() -> list[dict]:
    return leases.workers()


class WorkerAdd(BaseModel):
    name: str


@router.post("/server/workers")
def server_add_worker(body: WorkerAdd, request: Request) -> dict:
    _local_only(request)
    wk, key = leases.add_worker(body.name)
    return {"id": wk.id, "name": wk.name, "key": key}


@router.delete("/server/workers/{worker_id}")
def server_remove_worker(worker_id: str, request: Request) -> dict:
    _local_only(request)
    if not leases.remove_worker(worker_id):
        raise HTTPException(404, "Unknown worker")
    return {"ok": True}


@router.get("/node/status", summary="This computer's role: mode, worker loop state (laptop UI)")
def node_status() -> dict:
    from clipforge.server import worker as wk
    from clipforge.server.status import node_name

    cfg = load_config()
    return {"mode": cfg.deployment.mode, "node": node_name(), "server_url": cfg.deployment.server_url,
            "worker": wk.state() if cfg.deployment.mode == "worker" else None}
