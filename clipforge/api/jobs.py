"""Background job queue (no Redis/Celery): one worker thread runs heavy tasks one at a time.

Tasks:
  ("pipeline", job_id, {})            full run (resumes from cache — this is also "retry")
  ("render", job_id, {"ranks": [..]}) re-render some clips after edits (+ refresh metadata)
Progress/log events are kept in a per-job ring buffer and pushed to SSE subscribers.
"""

from __future__ import annotations

import collections
import json
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from clipforge import pipeline
from clipforge.api import db
from clipforge.config import Config, load_config
from clipforge.utils.log import CancelledError, log

# settings keys accepted from the New Job form -> config dotted keys
SETTING_KEYS = {
    "clips": "clips.count", "min_sec": "clips.min_sec", "max_sec": "clips.max_sec",
    "caption_script": "captions.script", "caption_preset": "captions.preset", "layout": "reframe.layout",
    "provider": "llm.provider", "model": "llm.model", "whisper_model": "whisper.model", "language": "whisper.language",
}


def job_config(settings: dict[str, Any]) -> Config:
    cfg = load_config()
    overrides = {SETTING_KEYS[k]: v for k, v in settings.items() if k in SETTING_KEYS and v not in (None, "")}
    if "llm.provider" in overrides and "llm.model" not in overrides and overrides["llm.provider"] != cfg.llm.provider:
        overrides["llm.model"] = "auto"
    if overrides.get("captions.preset") == "hindi-devanagari":
        overrides["captions.script"] = "devanagari"
    return cfg.with_overrides(overrides)


@dataclass
class JobChannel:
    events: collections.deque = field(default_factory=lambda: collections.deque(maxlen=500))
    subscribers: list[queue.Queue] = field(default_factory=list)
    seq: int = 0


class ApiReporter:
    """ProgressReporter that writes to SQLite (throttled) and publishes SSE events."""

    def __init__(self, manager: "JobManager", job_id: int) -> None:
        self.m = manager
        self.job_id = job_id
        self._last_db = 0.0
        self.stage_name = ""

    def stage(self, name: str, status: str = "running", message: str = "") -> None:
        self.stage_name = name
        db.update_job(self.job_id, stage=name, stage_status=status, progress=1.0 if status in ("done", "skipped") else 0.0,
                      eta_sec=None, message=message)
        self.m.publish(self.job_id, {"type": "stage", "stage": name, "status": status, "message": message})
        if message or status != "running":
            self.log(f"{name}: {status}{' — ' + message if message else ''}")

    def progress(self, fraction: float, eta_sec: float | None = None, message: str = "") -> None:
        self.m.publish(self.job_id, {"type": "progress", "stage": self.stage_name, "progress": round(fraction, 4),
                                     "eta_sec": round(eta_sec) if eta_sec is not None else None, "message": message})
        t = time.monotonic()
        if t - self._last_db > 2:
            self._last_db = t
            db.update_job(self.job_id, progress=fraction, eta_sec=eta_sec, message=message)

    def log(self, message: str, level: str = "info") -> None:
        getattr(log, level if level in ("info", "warning", "error") else "info")(f"[job {self.job_id}] {message}")
        self.m.publish(self.job_id, {"type": "log", "level": level, "message": message, "ts": time.time()})

    def cancelled(self) -> bool:
        return self.job_id in self.m.cancel_flags

    def clip_done(self, rank: int) -> None:
        self.m.publish(self.job_id, {"type": "clip_done", "rank": rank})


class JobManager:
    def __init__(self) -> None:
        self.q: queue.Queue[tuple[str, int, dict]] = queue.Queue()
        self.channels: dict[int, JobChannel] = collections.defaultdict(JobChannel)
        self.cancel_flags: set[int] = set()
        self.current: tuple[str, int] | None = None
        self.pending: list[tuple[str, int]] = []
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._worker, name="clipforge-worker", daemon=True)

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        # jobs that were running when the server stopped: mark them so the user can retry (resumes from cache)
        for job in db.list_jobs():
            if job.status in ("running", "queued"):
                db.update_job(job.id, status="failed", error="Interrupted (server restarted). Click Retry — finished stages are reused.")
        self.thread.start()

    def submit(self, kind: str, job_id: int, payload: dict | None = None) -> None:
        self.cancel_flags.discard(job_id)
        with self.lock:
            self.pending.append((kind, job_id))
        if kind == "pipeline":
            db.update_job(job_id, status="queued", error="", stage="", progress=0, finished_at=None)
        self.publish(job_id, {"type": "status", "status": "queued", "kind": kind})
        self.q.put((kind, job_id, payload or {}))

    def cancel(self, job_id: int) -> None:
        self.cancel_flags.add(job_id)
        with self.lock:
            queued_only = any(j == job_id for _, j in self.pending) and not (self.current and self.current[1] == job_id)
        if queued_only:
            db.update_job(job_id, status="cancelled")
            self.publish(job_id, {"type": "status", "status": "cancelled"})

    def busy(self, job_id: int) -> bool:
        with self.lock:
            return (self.current is not None and self.current[1] == job_id) or any(j == job_id for _, j in self.pending)

    # -------------------------------------------------------------- events
    def publish(self, job_id: int, ev: dict) -> None:
        ch = self.channels[job_id]
        with self.lock:
            ch.seq += 1
            ev = {**ev, "seq": ch.seq, "job_id": job_id}
            ch.events.append(ev)
            subs = list(ch.subscribers)
        for sq in subs:
            sq.put(ev)

    def subscribe(self, job_id: int) -> tuple[queue.Queue, list[dict]]:
        sq: queue.Queue = queue.Queue()
        ch = self.channels[job_id]
        with self.lock:
            ch.subscribers.append(sq)
            backlog = list(ch.events)
        return sq, backlog

    def unsubscribe(self, job_id: int, sq: queue.Queue) -> None:
        with self.lock:
            subs = self.channels[job_id].subscribers
            if sq in subs:
                subs.remove(sq)

    # -------------------------------------------------------------- worker
    def _worker(self) -> None:
        while True:
            kind, job_id, payload = self.q.get()
            with self.lock:
                if (kind, job_id) in self.pending:
                    self.pending.remove((kind, job_id))
                self.current = (kind, job_id)
            try:
                if job_id in self.cancel_flags:
                    continue
                self._run(kind, job_id, payload)
            finally:
                with self.lock:
                    self.current = None
                self.cancel_flags.discard(job_id)

    def _run(self, kind: str, job_id: int, payload: dict) -> None:
        job = db.get_job(job_id)
        if not job:
            return
        rep = ApiReporter(self, job_id)
        cfg = job_config(job.settings)
        if kind == "pipeline":
            db.update_job(job_id, status="running", error="")
        self.publish(job_id, {"type": "status", "status": "running", "kind": kind})
        try:
            if kind == "pipeline":
                section = tuple(job.settings["section"]) if job.settings.get("section") else None
                ws = pipeline.prepare(job.source, cfg, rep, section, job.video_id)  # type: ignore[arg-type]
                info = pipeline.load_info(ws)
                db.update_job(job_id, video_id=ws.video_id, title=info.title, channel=info.channel, duration=info.duration)
                cands = pipeline.select(ws, cfg, rep)
                db.update_job(job_id, n_clips=len(cands))
                self.publish(job_id, {"type": "clips", "count": len(cands)})
                pipeline.render(ws, cfg, rep)
                rep.stage("metadata", "running")
                pipeline.metadata(ws, cfg, rep)
                rep.stage("metadata", "done")
                db.update_job(job_id, status="done", stage="done", progress=1.0, eta_sec=None,
                              finished_at=db.now(), message=f"{len(cands)} clips ready")
                self.publish(job_id, {"type": "status", "status": "done"})
            elif kind == "render":
                if not job.video_id:
                    raise RuntimeError("This job has no video yet — run it first.")
                ws = pipeline.open_workspace(job.video_id, cfg)
                ranks = payload.get("ranks")
                pipeline.render(ws, cfg, rep, only=ranks, force=bool(payload.get("force")))
                pipeline.metadata(ws, cfg, rep)
                self.publish(job_id, {"type": "render_done", "ranks": ranks})
                if job.status != "running":
                    db.update_job(job_id, message="re-render finished")
        except CancelledError:
            db.update_job(job_id, status="cancelled", error="Cancelled by user", finished_at=db.now())
            self.publish(job_id, {"type": "status", "status": "cancelled"})
            rep.log("Cancelled", "warning")
        except Exception as exc:
            msg = str(exc)
            log.error(f"[job {job_id}] {kind} failed: {msg}\n{traceback.format_exc()}")
            if kind == "pipeline":
                db.update_job(job_id, status="failed", error=msg, finished_at=db.now())
                self.publish(job_id, {"type": "status", "status": "failed", "error": msg})
            else:
                self.publish(job_id, {"type": "render_failed", "error": msg, "ranks": payload.get("ranks")})
            rep.log(msg, "error")


manager = JobManager()


def event_json(ev: dict) -> str:
    return json.dumps(ev, ensure_ascii=False, default=str)
