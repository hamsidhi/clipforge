"""Workers + stage tasks with leases and fencing tokens (Phase 15b). Runs on the server (deployment.mode: server).

Life of a task:  queued ──claim──▶ leased(worker, fence=n, lease_until) ──complete(fence=n)──▶ done
                   ▲                    │ heartbeat every 30 s renews the lease
                   └── lease expired ───┘ (worker slept / crashed / lost network) → queued again, next claim: fence n+1
  * complete / heartbeat / fail are only accepted with the CURRENT fence → a late, stale worker can't overwrite anything.
  * completing twice with the same fence is a no-op (idempotent), so a retried HTTP request never does work twice.
  * a queued task nobody claims within deployment.worker_grace_minutes falls back to the server (unless `hold`, e.g.
    YouTube blocked the server's own download — then it waits for the laptop however long it takes).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
from sqlalchemy import text
from sqlmodel import select

from clipforge.api import db
from clipforge.config import load_config

ONLINE_SLACK = 30  # seconds beyond 2 heartbeats before a worker counts as offline


class StaleLease(Exception):
    """The worker's fence is not the current one (its lease expired and the task moved on)."""


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(t: dt.datetime | None) -> dt.datetime | None:
    return t.replace(tzinfo=dt.timezone.utc) if t is not None and t.tzinfo is None else t


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# ============================================================================ workers
def add_worker(name: str, capabilities: list[str] | None = None) -> tuple[db.WorkerNode, str]:
    """Returns (worker, key). The key is shown once; only its SHA-256 is stored."""
    key = "cfw_" + secrets.token_urlsafe(32)
    w = db.WorkerNode(id="w_" + secrets.token_hex(4), name=name.strip()[:40] or "laptop", key_hash=_hash(key),
                      capabilities_json=json.dumps(capabilities or load_config().deployment.capabilities))
    with db.session() as s:
        s.add(w)
        s.commit()
        s.refresh(w)
    _audit(f"Worker '{w.name}' ({w.id}) added")
    return w, key


def remove_worker(worker_id: str) -> bool:
    with db.session() as s:
        w = s.get(db.WorkerNode, worker_id)
        if not w:
            return False
        w.revoked = True
        s.add(w)
        s.commit()
    requeue_worker(worker_id)
    _audit(f"Worker {worker_id} revoked")
    return True


def auth(key: str) -> db.WorkerNode | None:
    if not key or not key.startswith("cfw_") or len(key) > 100:
        return None
    with db.session() as s:
        w = s.exec(select(db.WorkerNode).where(db.WorkerNode.key_hash == _hash(key))).first()
    if not w or w.revoked:
        return None
    return w


def hello(worker_id: str, capabilities: list[str], max_parallel: int, gpu: bool, version: str) -> db.WorkerNode:
    allowed = {"download", "transcribe", "select", "render", "import"}
    with db.session() as s:
        w = s.get(db.WorkerNode, worker_id)
        assert w is not None
        was_offline = not _online(w)
        w.capabilities_json = json.dumps([c for c in capabilities if c in allowed])
        w.max_parallel, w.gpu, w.version, w.last_seen = max(1, min(4, max_parallel)), gpu, version[:40], now()
        s.add(w)
        s.commit()
        s.refresh(w)
    if was_offline:
        _notify(f"💻 {w.name} is online — heavy jobs go there now.", f"worker_on:{w.id}:{now():%Y%m%d%H}")
    return w


def touch(worker_id: str) -> None:
    with db.session() as s:
        w = s.get(db.WorkerNode, worker_id)
        if w:
            w.last_seen = now()
            s.add(w)
            s.commit()


def _online(w: db.WorkerNode) -> bool:
    hb = load_config().deployment.heartbeat_seconds
    return bool(not w.revoked and w.last_seen and (now() - _aware(w.last_seen)).total_seconds() < 2 * hb + ONLINE_SLACK)  # type: ignore[operator]


def workers() -> list[dict]:
    with db.session() as s:
        rows = list(s.exec(select(db.WorkerNode).where(db.WorkerNode.revoked == False)).all())  # noqa: E712
        busy = {t.worker_id: t for t in s.exec(select(db.StageTask).where(db.StageTask.status == "leased")).all()}
    return [{"id": w.id, "name": w.name, "online": _online(w), "capabilities": w.capabilities, "gpu": w.gpu,
             "last_seen": w.last_seen, "version": w.version,
             "task": ({"id": busy[w.id].id, "job_id": busy[w.id].job_id, "stage": busy[w.id].stage,
                       "progress": busy[w.id].progress} if w.id in busy else None)} for w in rows]


def online_for(stage: str) -> list[dict]:
    return [w for w in workers() if w["online"] and stage in w["capabilities"]]


def mark_offline_workers() -> None:
    """Tell Telegram when a worker went away (once per hour per worker)."""
    for w in workers():
        if not w["online"] and w["last_seen"] and (now() - _aware(w["last_seen"])).total_seconds() < 3600:  # type: ignore[operator]
            _notify(f"💤 {w['name']} went offline — the server takes over its unfinished work.",
                    f"worker_off:{w['id']}:{now():%Y%m%d%H}")


# ============================================================================ tasks
def create(job_id: int, stage: str, payload: dict | None = None, hold: bool = False) -> db.StageTask:
    grace = load_config().deployment.worker_grace_minutes
    with db.session() as s:
        live = s.exec(select(db.StageTask).where(db.StageTask.job_id == job_id, db.StageTask.stage == stage,
                                                 db.StageTask.status.in_(("queued", "leased")))).first()  # type: ignore[attr-defined]
        if live:  # never two live tasks for the same job + stage
            if hold and not live.hold:
                live.hold, live.local_fallback_at = True, None
                s.add(live)
                s.commit()
            return live
        t = db.StageTask(job_id=job_id, stage=stage, payload_json=json.dumps(payload or {}, default=str), hold=hold,
                         local_fallback_at=None if hold else now() + dt.timedelta(minutes=grace))
        s.add(t)
        s.commit()
        s.refresh(t)
        return t


def get(task_id: int) -> db.StageTask | None:
    with db.session() as s:
        return s.get(db.StageTask, task_id)


def claim(worker: db.WorkerNode, capabilities: list[str]) -> db.StageTask | None:
    """Atomically lease the oldest queued task this worker can do. BEGIN IMMEDIATE = one claimer at a time."""
    reap()
    caps = [c for c in capabilities if c in worker.capabilities] or []
    if not caps:
        return None
    lease = load_config().deployment.lease_seconds
    with db.engine().connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        marks = ",".join(f":c{i}" for i in range(len(caps)))
        row = conn.execute(text(f"SELECT id FROM stagetask WHERE status='queued' AND stage IN ({marks}) "
                                "ORDER BY id LIMIT 1"), {f"c{i}": c for i, c in enumerate(caps)}).fetchone()
        if not row:
            conn.exec_driver_sql("ROLLBACK")
            return None
        until = now() + dt.timedelta(seconds=lease)
        conn.execute(text("UPDATE stagetask SET status='leased', worker_id=:w, fence=fence+1, attempts=attempts+1, "
                          "lease_until=:u, updated_at=:n, error='' WHERE id=:id AND status='queued'"),
                     {"w": worker.id, "u": until, "n": now(), "id": row[0]})
        conn.exec_driver_sql("COMMIT")
    touch(worker.id)
    t = get(row[0])
    if t:
        _job_running_on(t, worker.name)
    return t


def lease_to(task_id: int, worker: db.WorkerNode) -> db.StageTask | None:
    """Lease one specific queued task to this worker (imports: the worker created it itself)."""
    lease = load_config().deployment.lease_seconds
    with db.session() as s:
        res = s.execute(db.StageTask.__table__.update()
                        .where(db.StageTask.id == task_id, db.StageTask.status == "queued")
                        .values(status="leased", worker_id=worker.id, fence=db.StageTask.fence + 1,
                                attempts=db.StageTask.attempts + 1, lease_until=now() + dt.timedelta(seconds=lease),
                                updated_at=now()))
        s.commit()
        if res.rowcount != 1:
            return None
    touch(worker.id)
    return get(task_id)


def _check(s, task_id: int, worker_id: str, fence: int) -> db.StageTask:
    t = s.get(db.StageTask, task_id)
    if not t or t.worker_id != worker_id or t.fence != fence:
        raise StaleLease(f"task {task_id}: not your lease any more (fence {fence})")
    return t


def heartbeat(worker_id: str, task_id: int, fence: int, progress: float = 0, eta: float | None = None,
              message: str = "") -> str:
    """'ok' (lease renewed) | 'cancel' (the job was cancelled — stop). StaleLease if the fence is old."""
    lease = load_config().deployment.lease_seconds
    with db.session() as s:
        t = _check(s, task_id, worker_id, fence)
        if t.status == "cancelled":
            return "cancel"
        if t.status != "leased":
            raise StaleLease(f"task {task_id} is {t.status}")
        t.lease_until, t.progress, t.eta_sec, t.message, t.updated_at = (
            now() + dt.timedelta(seconds=lease), max(0.0, min(1.0, progress)), eta, message[:200], now())
        s.add(t)
        s.commit()
        job_id, stage = t.job_id, t.stage
    touch(worker_id)
    db.update_job(job_id, stage=stage, progress=progress, eta_sec=eta, message=message[:200])
    return "ok"


def complete(worker_id: str, task_id: int, fence: int, result: dict | None = None) -> bool:
    """True = accepted now; False = this exact completion was already accepted (idempotent retry)."""
    with db.session() as s:
        t = s.get(db.StageTask, task_id)
        if t and t.status == "done" and t.worker_id == worker_id and t.fence == fence:
            return False
        t = _check(s, task_id, worker_id, fence)
        if t.status != "leased":
            raise StaleLease(f"task {task_id} is {t.status}")
        res = s.execute(db.StageTask.__table__.update()
                        .where(db.StageTask.id == task_id, db.StageTask.fence == fence, db.StageTask.status == "leased")
                        .values(status="done", progress=1.0, updated_at=now(),
                                payload_json=json.dumps({**t.payload, "result": result or {}}, default=str)))
        s.commit()
        if res.rowcount != 1:
            raise StaleLease(f"task {task_id} changed while completing")
    touch(worker_id)
    return True


def fail(worker_id: str, task_id: int, fence: int, error: str, retryable: bool = True) -> str:
    """'requeued' | 'failed'. Retryable failures go back to the queue (max 3 attempts)."""
    with db.session() as s:
        t = _check(s, task_id, worker_id, fence)
        if t.status != "leased":
            raise StaleLease(f"task {task_id} is {t.status}")
        if retryable and t.attempts < 3:
            t.status, t.worker_id, t.lease_until = "queued", "", None
            out = "requeued"
        else:
            t.status = "failed"
            out = "failed"
        t.error, t.updated_at = error[:2000], now()
        s.add(t)
        s.commit()
    return out


def cancel_job(job_id: int) -> int:
    with db.session() as s:
        rows = s.exec(select(db.StageTask).where(db.StageTask.job_id == job_id,
                                                 db.StageTask.status.in_(("queued", "leased")))).all()  # type: ignore[attr-defined]
        for t in rows:
            t.status, t.updated_at = "cancelled", now()
            s.add(t)
        s.commit()
        return len(rows)


def requeue_worker(worker_id: str) -> int:
    with db.session() as s:
        rows = s.exec(select(db.StageTask).where(db.StageTask.worker_id == worker_id, db.StageTask.status == "leased")).all()
        for t in rows:
            t.status, t.worker_id, t.lease_until, t.message = "queued", "", None, "worker removed"
            s.add(t)
        s.commit()
        return len(rows)


def reap(at: dt.datetime | None = None) -> dict[str, list[int]]:
    """Expired leases → queued again. Queued past their fallback time (and not `hold`) → 'fallback' (the server does
    the stage itself). Returns {"requeued": [task ids], "fallback": [job ids]}."""
    t0 = at or now()
    out: dict[str, list[int]] = {"requeued": [], "fallback": []}
    with db.session() as s:
        for t in s.exec(select(db.StageTask).where(db.StageTask.status == "leased")).all():
            if t.lease_until and _aware(t.lease_until) < t0:  # type: ignore[operator]
                t.status, t.worker_id, t.lease_until = "queued", "", None
                t.message = "worker stopped answering — waiting for a worker again"
                grace = load_config().deployment.worker_grace_minutes
                if not t.hold:
                    t.local_fallback_at = t0 + dt.timedelta(minutes=grace)
                s.add(t)
                out["requeued"].append(t.id)  # type: ignore[arg-type]
        for t in s.exec(select(db.StageTask).where(db.StageTask.status == "queued", db.StageTask.hold == False)).all():  # noqa: E712
            if t.local_fallback_at and _aware(t.local_fallback_at) < t0:  # type: ignore[operator]
                t.status, t.updated_at, t.message = "fallback", t0, "no worker took it — running on the server"
                s.add(t)
                out["fallback"].append(t.job_id)
        s.commit()
    return out


def active(job_id: int) -> db.StageTask | None:
    with db.session() as s:
        return s.exec(select(db.StageTask).where(db.StageTask.job_id == job_id,
                                                 db.StageTask.status.in_(("queued", "leased")))
                      .order_by(db.StageTask.id.desc())).first()  # type: ignore[attr-defined]


def tasks_for(job_id: int) -> list[db.StageTask]:
    with db.session() as s:
        return list(s.exec(select(db.StageTask).where(db.StageTask.job_id == job_id).order_by(db.StageTask.id)).all())  # type: ignore[arg-type]


def counts() -> dict[str, int]:
    with db.session() as s:
        rows = s.exec(select(db.StageTask).where(db.StageTask.status.in_(("queued", "leased")))).all()  # type: ignore[attr-defined]
    out: dict[str, int] = {}
    for t in rows:
        out[t.status] = out.get(t.status, 0) + 1
    return out


def where(job_id: int) -> str:
    """'on <laptop>' / 'waiting for a worker' / 'on <this server>' — shown in Telegram progress."""
    from clipforge.server.status import node_name

    t = active(job_id)
    if t and t.status == "leased":
        with db.session() as s:
            w = s.get(db.WorkerNode, t.worker_id)
        return f"on {w.name if w else 'a worker'}"
    if t and t.status == "queued":
        return "waiting for the laptop" if t.hold else "waiting for a worker (server takes over soon)"
    mode = load_config().deployment.mode
    return f"on {node_name()}" + (" (server)" if mode == "server" else "")


def workers_line() -> str:
    if load_config().deployment.mode != "server":
        return ""
    ws = workers()
    if not ws:
        return "Workers: none (add one: clipforge server add-worker)"
    return "Workers: " + ", ".join(f"{'🟢' if w['online'] else '⚪'} {w['name']}" for w in ws)


def _job_running_on(t: db.StageTask, name: str) -> None:
    db.update_job(t.job_id, status="running", stage=t.stage, message=f"{t.stage} on {name}")


def _audit(detail: str) -> None:
    try:
        from clipforge.api.auth import audit

        audit("server", detail)
    except Exception:
        pass


def _notify(text_: str, key: str) -> None:
    try:
        from clipforge.notify import telegram

        telegram.bot().notify("job_done", text_, key=key)
    except Exception:
        pass
