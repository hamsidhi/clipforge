"""Server-side scheduling (deployment.mode: server): which stage goes to a worker, which the server does itself.

The pipeline task of a job asks `delegate()` before running. For the first stage that isn't done yet:
  * a worker that can do it is online and the stage is in deployment.prefer_worker_for → a StageTask is created and the
    job thread is released at once (it never blocks waiting). When the worker reports back (`on_task_done`) the job's
    pipeline task is submitted again: finished stages are skipped by the normal workspace cache, the next stage is
    delegated again, … until everything is done.
  * no worker → the server runs it itself. A download that YouTube blocks on the server becomes a `hold` task: it waits
    for the laptop however long it takes (Telegram explains the options).
  * a task nobody claims within worker_grace_minutes → the server does it (`reap` → pipeline with {"local": [...]}).
Publishing is guarded separately: `publishing_allowed()` is False on a worker and on a fenced server.
"""

from __future__ import annotations

import threading

from clipforge.api import db
from clipforge.config import Config, load_config
from clipforge.server import leases
from clipforge.utils.log import log

DELEGATABLE = ("download", "transcribe", "select", "render")
MAX_TASKS_PER_STAGE = 4
_reaper: threading.Thread | None = None
_stop = threading.Event()


# ============================================================================ publishing guard / fence
def fenced() -> str:
    """'' = not fenced. Fails CLOSED: if the fence can't be read, a server must not publish (it might be fenced)."""
    try:
        from clipforge.notify import tg_store as st

        return st.kv_get("publishing_fenced") or ""
    except Exception as exc:
        return f"couldn't read the fence ({type(exc).__name__}) — not publishing until it can"


def set_fenced(reason: str) -> None:
    from clipforge.notify import tg_store as st

    st.kv_set("publishing_fenced", reason or "fenced")
    leases._audit(f"Publishing fenced: {reason}")


def unfence() -> None:
    from clipforge.notify import tg_store as st

    st.kv_del("publishing_fenced")
    leases._audit("Publishing unfenced")


def publishing_allowed() -> tuple[bool, str]:
    """Exactly one machine publishes. A worker never does; a server that was fenced (emergency standalone on the
    laptop) doesn't until `clipforge server unfence`."""
    mode = load_config().deployment.mode
    if mode == "worker":
        return False, "worker mode — the server publishes"
    if mode == "server" and fenced():
        return False, f"publishing fenced ({fenced()}) — run `clipforge server unfence` when the laptop is a worker again"
    return True, ""


# ============================================================================ delegation
def _stage_done(stage: str, ws, cfg: Config, section) -> bool:
    from clipforge import pipeline

    if stage == "download":
        return pipeline.download_done(ws, section)
    if stage == "transcribe":
        return pipeline.transcribe_done(ws, cfg)
    if stage == "select":
        return pipeline.select_done(ws, cfg)
    if stage == "render":
        return ws.candidates.exists() and not pipeline.render_todo(ws)
    return True


def delegate(job: db.Job, cfg: Config, reporter, payload: dict) -> bool:
    """True = handed to a worker (or parked); the caller returns and the job continues when the worker reports back.
    False = run the normal (local) pipeline now — remaining stages are done here."""
    from clipforge import pipeline
    from clipforge.stages.download import DownloadBlocked
    from clipforge.workspace import Workspace, is_url, video_id_for

    if load_config().deployment.mode != "server" or payload.get("local_all"):
        return False
    section = tuple(job.settings["section"]) if job.settings.get("section") else None
    video_id = job.video_id or video_id_for(job.source, section)
    if not job.video_id:
        db.update_job(job.id, video_id=video_id)  # type: ignore[arg-type]
    ws = Workspace(cfg.workspace, video_id)
    local = set(payload.get("local", []))
    prefer = set(load_config().deployment.prefer_worker_for)
    for stage in DELEGATABLE:
        if stage == "select" and not pipeline.download_done(ws, section):
            return False
        if stage == "select":
            pipeline.run_analyze(ws, reporter)  # cheap (seconds): always on the server, needs the audio
        if _stage_done(stage, ws, cfg, section):
            continue
        if stage in local or stage not in prefer:
            return False
        if sum(1 for t in leases.tasks_for(job.id) if t.stage == stage) >= MAX_TASKS_PER_STAGE:
            reporter.log(f"{stage}: workers didn't finish it after {MAX_TASKS_PER_STAGE} tries — doing it here", "warning")
            return False
        if stage == "download" and not is_url(job.source):
            pipeline.run_download(ws, job.source, cfg, reporter, section)  # an uploaded file is already here
            continue
        if not leases.online_for(stage):
            if stage != "download":
                return False  # nobody to give it to: the server does it (and everything after it)
            try:
                pipeline.run_download(ws, job.source, cfg, reporter, section)
                continue
            except DownloadBlocked as exc:
                _park_download(job, str(exc))
                return True
        t = leases.create(job.id, stage, {"force_select": bool(payload.get("force_select")) and stage == "select"})  # type: ignore[arg-type]
        db.update_job(job.id, status="running", stage=stage, progress=0, eta_sec=None,  # type: ignore[arg-type]
                      message=f"{stage}: waiting for a worker (task {t.id})")
        reporter.log(f"{stage} handed to a worker (task {t.id})")
        return True
    return False


def delegate_render(job: db.Job, ranks: list[int] | None, force: bool) -> bool:
    """A re-render request on the server: give it to an online worker if there is one."""
    if load_config().deployment.mode != "server" or "render" not in load_config().deployment.prefer_worker_for:
        return False
    if not leases.online_for("render"):
        return False
    t = leases.create(job.id, "render", {"ranks": ranks, "force": force, "kind": "rerender"})  # type: ignore[arg-type]
    db.update_job(job.id, message=f"re-render handed to a worker (task {t.id})")  # type: ignore[arg-type]
    return True


def _park_download(job: db.Job, why: str) -> None:
    leases.create(job.id, "download", {}, hold=True)  # type: ignore[arg-type]
    db.update_job(job.id, status="running", stage="download", message="YouTube blocked the server — waiting for the laptop")  # type: ignore[arg-type]
    try:
        from clipforge.notify import telegram

        b = telegram.bot()
        if b.enabled():
            from clipforge.notify import tg_store as st
            from clipforge.notify.tg_core import Ctx

            for c in st.chats():
                if c.role != "owner":
                    continue
                ctx = Ctx(b, c.chat_id, c.user_id, c.role)
                ctx.send(f"🚧 <b>Job {job.id}</b>: YouTube asked the server to prove it's not a bot.\nOptions:\n"
                         "• <b>wait</b> — the laptop downloads it automatically when it's on (nothing to do)\n"
                         "• <b>send me the video file</b> here (needs the local Bot API server for files over 20 MB)\n"
                         "• add a cookies file on the server (see AUTOMATION_GUIDE Part I — use a throwaway account)",
                         [[ctx.btn("⏹ Cancel job", "job.cancel", j=job.id)]])
    except Exception as exc:
        log.warning(f"[server] couldn't tell Telegram about the blocked download: {exc}")


def stage_output_missing(t: db.StageTask, job: db.Job) -> str:
    """'' if the stage's results are really in the workspace now (checked before a task is marked done)."""
    from clipforge import pipeline
    from clipforge.api.jobs import job_config
    from clipforge.workspace import Workspace

    if t.stage == "import" or not job.video_id:
        return ""
    cfg = job_config(job.settings)
    ws = Workspace(cfg.workspace, job.video_id)
    section = tuple(job.settings["section"]) if job.settings.get("section") else None
    if t.stage == "render":
        try:
            pipeline.adopt_renders(ws, cfg)
        except Exception as exc:
            return f"render results unreadable ({exc})"
        todo = pipeline.render_todo(ws)
        ranks = t.payload.get("ranks")
        left = [r for r in todo if ranks is None or r in ranks]
        return f"clips {left} not rendered" if left else ""
    return "" if _stage_done(t.stage, ws, cfg, section) else f"{t.stage} results not complete"


def on_task_done(task_id: int) -> None:
    """A worker finished a stage: adopt its results and continue the job."""
    from clipforge import pipeline
    from clipforge.api.jobs import job_config, manager
    from clipforge.workspace import Workspace

    t = leases.get(task_id)
    if not t:
        return
    job = db.get_job(t.job_id)
    if not job:
        return
    if t.stage == "import":
        cfg = load_config()
        ws = Workspace(cfg.workspace, job.video_id)  # type: ignore[arg-type]
        n = len(pipeline.load_candidates(ws))
        db.update_job(job.id, status="done", stage="done", progress=1.0, n_clips=n, finished_at=db.now(),  # type: ignore[arg-type]
                      message="imported from the laptop")
        manager.publish(job.id, {"type": "status", "status": "done"})  # type: ignore[arg-type]
        return
    if t.stage == "render":
        cfg = job_config(job.settings)
        ws = Workspace(cfg.workspace, job.video_id)  # type: ignore[arg-type]
        try:
            pipeline.adopt_renders(ws, cfg)
        except Exception as exc:
            log.warning(f"[server] adopting renders of job {job.id} failed: {exc}")
        if t.payload.get("kind") == "rerender":
            manager.publish(job.id, {"type": "render_done", "ranks": t.payload.get("ranks")})  # type: ignore[arg-type]
            db.update_job(job.id, message="re-render finished on a worker")  # type: ignore[arg-type]
            return
    manager.submit("pipeline", job.id)  # type: ignore[arg-type]


def on_task_failed(task_id: int, error: str, blocked: bool = False) -> None:
    from clipforge.api.jobs import manager

    t = leases.get(task_id)
    if not t:
        return
    if t.payload.get("kind") == "rerender":
        manager.publish(t.job_id, {"type": "render_failed", "error": error[:500], "ranks": t.payload.get("ranks")})
        return
    msg = error[:1000] if not blocked else ("YouTube blocked the download on the laptop too: " + error[:500])
    db.update_job(t.job_id, status="failed", error=msg, finished_at=db.now())
    manager.publish(t.job_id, {"type": "status", "status": "failed", "error": msg})


# ============================================================================ reaper
def tick() -> dict:
    """Expired leases → back to the queue; unclaimed tasks past the grace time → the server does them; tasks that
    crashed workers 3 times → the server / failed; and any job left 'running' with nothing working on it is resumed."""
    from clipforge.api.jobs import manager

    out = leases.reap()
    for job_id in set(out["fallback"]):
        if not manager.busy(job_id):
            stages = [t.stage for t in leases.tasks_for(job_id) if t.status == "fallback"]
            log.info(f"[server] no worker took job {job_id} ({', '.join(stages)}) — running it here")
            manager.submit("pipeline", job_id, {"local": stages, "local_all": True})
    for task_id in out.get("failed", []):
        t = leases.get(task_id)
        on_task_failed(task_id, (t.error if t else "") or "crashed the worker 3 times")
    repair_stuck_jobs()
    try:
        leases.mark_offline_workers()
    except Exception:
        pass
    try:
        from clipforge.server import backup

        backup.nightly_if_due()  # nightly cleanup + encrypted backup (server only)
    except Exception as exc:
        log.debug(f"[server] nightly: {exc}")
    return out


def repair_stuck_jobs(min_age_sec: float = 120) -> list[int]:
    """A job 'running'/'queued' with no live task and nothing queued here (a lost hand-off, a crash between a worker
    finishing and the job continuing): submit its pipeline again — finished stages are skipped by the cache."""
    import datetime as dt

    from clipforge.api.jobs import manager

    resumed = []
    now = dt.datetime.now(dt.timezone.utc)
    for job in db.list_jobs():
        if job.status not in ("running", "queued") or manager.busy(job.id):  # type: ignore[arg-type]
            continue
        upd = job.updated_at.replace(tzinfo=dt.timezone.utc) if job.updated_at.tzinfo is None else job.updated_at
        if (now - upd).total_seconds() < min_age_sec:
            continue
        tasks = leases.tasks_for(job.id)  # type: ignore[arg-type]
        local_all = bool(tasks) and tasks[-1].status == "fallback"
        log.info(f"[server] job {job.id} was waiting on nothing — resuming it")
        manager.submit("pipeline", job.id, {"local_all": True} if local_all else {})  # type: ignore[arg-type]
        resumed.append(job.id)  # type: ignore[arg-type]
    return resumed


def start_reaper(interval: float = 15.0) -> None:
    global _reaper
    if load_config().deployment.mode != "server" or (_reaper and _reaper.is_alive()):
        return
    _stop.clear()

    def loop() -> None:
        while not _stop.wait(interval):
            try:
                tick()
            except Exception as exc:
                log.debug(f"[server] reaper: {exc}")

    _reaper = threading.Thread(target=loop, name="clipforge-reaper", daemon=True)
    _reaper.start()


def stop_reaper() -> None:
    _stop.set()
