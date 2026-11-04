"""Background job queue (no Redis/Celery): one worker thread runs heavy tasks one at a time.

Tasks:
  pipeline  full run (resumes from cache — this is also "retry")
  render    re-render some clips after edits (+ refresh metadata)
Each task has its own id, so cancelling one task never affects a task queued later for the same job.
Progress/log events are kept in a per-job ring buffer and pushed to SSE subscribers.
"""

from __future__ import annotations

import collections
import json
import queue
import threading
import time
import traceback
import uuid
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
    "provider": "llm.provider", "model": "llm.model", "whisper_model": "whisper.model", "language": "whisper.language", "transcript_source": "transcript.source",
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


@dataclass(eq=False)
class Task:
    id: int
    kind: str  # pipeline | render
    job_id: int
    payload: dict


class ApiReporter:
    """ProgressReporter that writes to SQLite (throttled) and publishes SSE events."""

    def __init__(self, manager: "JobManager", task: Task, cfg: Config) -> None:
        self.m = manager
        self.job_id = task.job_id
        self.task_id = task.id
        self.cfg = cfg
        self._last_db = 0.0
        self.stage_name = ""

    def stage(self, name: str, status: str = "running", message: str = "") -> None:
        self.stage_name = name
        db.update_job(self.job_id, stage=name, stage_status=status, progress=1.0 if status in ("done", "skipped") else 0.0,
                      eta_sec=None, message=message)
        self.m.publish(self.job_id, {"type": "stage", "stage": name, "status": status, "message": message})
        if message or status != "running":
            self.log(f"{name}: {status}{' — ' + message if message else ''}")
        if name == "download" and status in ("done", "skipped"):
            self._fill_job_info()
        if name == "transcribe" and status == "done":
            self._transcript_warnings()

    def _transcript_warnings(self) -> None:
        """A pasted/YouTube transcript that may not fit the video → Telegram asks whether to use Whisper instead."""
        job = db.get_job(self.job_id)
        if not job or not job.video_id:
            return
        from clipforge.stages import transcript_input as ti

        rep = ti.read_report(self.cfg.workspace / job.video_id) or {}
        if rep.get("warnings") and rep.get("source") != "whisper":
            self.m.publish(self.job_id, {"type": "transcript_warning", "warnings": rep["warnings"]})
            from clipforge.notify import telegram

            threading.Thread(target=telegram.on_transcript_warning, args=(self.job_id, rep["warnings"]), daemon=True).start()

    def _fill_job_info(self) -> None:
        """Show title/duration on the dashboard right after the download, not only after transcription."""
        job = db.get_job(self.job_id)
        if not job or not job.video_id:
            return
        try:
            info = json.loads((self.cfg.workspace / job.video_id / "info.json").read_text(encoding="utf-8"))
            db.update_job(self.job_id, title=info.get("title", ""), channel=info.get("channel", ""),
                          duration=float(info.get("duration") or 0))
        except (OSError, ValueError):
            pass

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
        return self.task_id in self.m.cancelled_tasks

    def clip_done(self, rank: int) -> None:
        self.m.publish(self.job_id, {"type": "clip_done", "rank": rank})


class JobManager:
    def __init__(self) -> None:
        self.q: queue.Queue[Task] = queue.Queue()
        self.channels: dict[int, JobChannel] = collections.defaultdict(JobChannel)
        self.cancelled_tasks: set[int] = set()
        self.current: Task | None = None
        self.pending: list[Task] = []
        self.lock = threading.Lock()
        self._next_id = 0
        self.boot_id = uuid.uuid4().hex[:8]  # lets clients notice a server restart (event seq restarts at 1)
        self.thread = threading.Thread(target=self._worker, name="clipforge-worker", daemon=True)

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        # jobs that were running when the server stopped: mark them so the user can retry (resumes from cache)
        server = load_config().deployment.mode == "server"
        if server:
            from clipforge.server import leases

            leases.reset_after_restart()
        for job in db.list_jobs():
            if job.status in ("running", "queued"):
                if _delegated(job.id):  # a worker still has it (server mode): it reports back by itself
                    continue
                if server:  # unattended box: just continue it (finished stages are reused)
                    self.submit("pipeline", job.id)  # type: ignore[arg-type]
                    continue
                db.update_job(job.id, status="failed", error="Interrupted (server restarted). Click Retry — finished stages are reused.")
        self.thread.start()

    def submit(self, kind: str, job_id: int, payload: dict | None = None) -> int:
        with self.lock:
            self._next_id += 1
            task = Task(self._next_id, kind, job_id, payload or {})
            self.pending.append(task)
        if kind == "pipeline":
            db.update_job(job_id, status="queued", error="", stage="", progress=0, finished_at=None)
        self.publish(job_id, {"type": "status", "status": "queued", "kind": kind})
        self.q.put(task)
        return task.id

    def cancel(self, job_id: int) -> None:
        """Cancel the running and the queued tasks of this job."""
        with self.lock:
            queued = [t for t in self.pending if t.job_id == job_id]
            running = self.current if self.current and self.current.job_id == job_id else None
            self.cancelled_tasks.update(t.id for t in queued)
            if running:
                self.cancelled_tasks.add(running.id)
        for t in queued:  # never started: report now (the running one reports when it stops)
            self._report_cancel(t)
        if _delegated(job_id):  # server mode: a worker has a stage of it
            from clipforge.server import leases

            leases.cancel_job(job_id)
            if not running and not queued:
                db.update_job(job_id, status="cancelled", error="Cancelled by user", finished_at=db.now())
                self.publish(job_id, {"type": "status", "status": "cancelled"})

    def _report_cancel(self, t: Task) -> None:
        if t.kind == "pipeline":
            db.update_job(t.job_id, status="cancelled", error="Cancelled by user", finished_at=db.now())
            self.publish(t.job_id, {"type": "status", "status": "cancelled"})
        else:  # a cancelled re-render must not mark a finished job as cancelled
            self.publish(t.job_id, {"type": "render_failed", "error": "Cancelled", "ranks": t.payload.get("ranks")})

    def busy(self, job_id: int) -> bool:
        with self.lock:
            here = (self.current is not None and self.current.job_id == job_id) or any(t.job_id == job_id for t in self.pending)
        return here or _delegated(job_id)

    # -------------------------------------------------------------- events
    def _notify(self, job_id: int, ev: dict) -> None:
        """Telegram hooks run on their own thread: a slow network never slows the pipeline."""
        from clipforge.notify import telegram

        if ev.get("type") == "status" and ev.get("status") in ("done", "failed"):
            threading.Thread(target=telegram.on_job_status, args=(job_id, ev["status"], ev.get("error", "")),
                             daemon=True).start()
        elif ev.get("type") in ("render_done", "render_failed"):
            threading.Thread(target=telegram.on_render, args=(job_id, ev.get("ranks"), ev["type"] == "render_done",
                                                              ev.get("error", "")), daemon=True).start()

    def publish(self, job_id: int, ev: dict) -> None:
        self._notify(job_id, ev)
        ch = self.channels[job_id]
        with self.lock:
            ch.seq += 1
            ev = {**ev, "seq": ch.seq, "job_id": job_id}
            ch.events.append(ev)
            subs = list(ch.subscribers)
        for sq in subs:
            sq.put(ev)

    def subscribe(self, job_id: int) -> tuple[queue.Queue, list[dict], int]:
        """Returns (live queue, backlog, seq of the newest backlog event)."""
        sq: queue.Queue = queue.Queue()
        ch = self.channels[job_id]
        with self.lock:
            ch.subscribers.append(sq)
            return sq, list(ch.events), ch.seq

    def unsubscribe(self, job_id: int, sq: queue.Queue) -> None:
        with self.lock:
            subs = self.channels[job_id].subscribers
            if sq in subs:
                subs.remove(sq)

    # -------------------------------------------------------------- worker
    def _worker(self) -> None:
        while True:
            task = self.q.get()
            with self.lock:
                if task in self.pending:
                    self.pending.remove(task)
                self.current = task
                skip = task.id in self.cancelled_tasks
            try:
                if not skip:
                    self._run(task)
            except Exception as exc:  # never let one bad task (e.g. a broken config.yaml) kill the worker
                log.error(f"[job {task.job_id}] worker error: {exc}\n{traceback.format_exc()}")
                try:
                    self._fail(task, str(exc))
                except Exception:
                    pass
            finally:
                with self.lock:
                    self.current = None
                    self.cancelled_tasks.discard(task.id)

    def _fail(self, task: Task, msg: str) -> None:
        if task.kind == "pipeline":
            db.update_job(task.job_id, status="failed", error=msg, finished_at=db.now())
            self.publish(task.job_id, {"type": "status", "status": "failed", "error": msg})
        else:
            self.publish(task.job_id, {"type": "render_failed", "error": msg, "ranks": task.payload.get("ranks")})

    def _run(self, task: Task) -> None:
        job = db.get_job(task.job_id)
        if not job:
            return
        cfg = job_config(job.settings)
        rep = ApiReporter(self, task, cfg)
        job_id = task.job_id
        if task.kind == "pipeline":
            db.update_job(job_id, status="running", error="")
        self.publish(job_id, {"type": "status", "status": "running", "kind": task.kind})
        try:
            if task.kind == "pipeline":
                from clipforge.server import dispatch

                if dispatch.delegate(job, cfg, rep, task.payload):
                    return  # a worker has the next stage; the job continues here when it reports back
                section = tuple(job.settings["section"]) if job.settings.get("section") else None
                ws = pipeline.prepare(job.source, cfg, rep, section, job.video_id)  # type: ignore[arg-type]
                info = pipeline.load_info(ws)
                db.update_job(job_id, video_id=ws.video_id, title=info.title, channel=info.channel, duration=info.duration)
                if dispatch.delegate(db.get_job(job_id) or job, cfg, rep, task.payload):
                    return  # server mode: a laptop came online meanwhile — the remaining heavy stages go there
                cands = pipeline.select(ws, cfg, rep, force=bool(task.payload.get("force_select")))
                db.update_job(job_id, n_clips=len(cands))
                self.publish(job_id, {"type": "clips", "count": len(cands)})
                if dispatch.delegate(db.get_job(job_id) or job, cfg, rep, task.payload):
                    return
                pipeline.render(ws, cfg, rep)
                rep.stage("metadata", "running")
                pipeline.metadata(ws, cfg, rep)
                rep.stage("metadata", "done")
                db.update_job(job_id, status="done", stage="done", progress=1.0, eta_sec=None,
                              finished_at=db.now(), message=f"{len(cands)} clips ready")
                self.publish(job_id, {"type": "status", "status": "done"})
            else:
                if not job.video_id:
                    raise RuntimeError("This job has no video yet — run it first.")
                ws = pipeline.open_workspace(job.video_id, cfg)
                ranks = task.payload.get("ranks")
                from clipforge.server import dispatch

                if dispatch.delegate_render(job, ranks, bool(task.payload.get("force"))):
                    return  # render_done is published when the worker reports back
                pipeline.render(ws, cfg, rep, only=ranks, force=bool(task.payload.get("force")))
                pipeline.metadata(ws, cfg, rep)
                self.publish(job_id, {"type": "render_done", "ranks": ranks})
                db.update_job(job_id, message="re-render finished")
        except CancelledError:
            self._report_cancel(task)
            rep.log("Cancelled", "warning")
        except Exception as exc:
            log.error(f"[job {job_id}] {task.kind} failed: {exc}\n{traceback.format_exc()}")
            self._fail(task, str(exc))
            rep.log(str(exc), "error")


def _delegated(job_id: int) -> bool:
    """Server mode: a worker holds (or will take) a stage of this job."""
    if load_config().deployment.mode != "server":
        return False
    try:
        from clipforge.server import leases

        return leases.active(job_id) is not None
    except Exception:
        return False


manager = JobManager()


def event_json(ev: dict) -> str:
    return json.dumps(ev, ensure_ascii=False, default=str)
