"""The laptop side of Phase 15b: `clipforge worker` (or inside `clipforge ui` when deployment.mode is worker).

Loop: hello → claim → pull the files the stage needs (sha256-verified, only what differs) → run the stage with the
job's own config (same fingerprints as the server) → push changed files (resumable chunks) → complete. A heartbeat thread
renews the lease every heartbeat_seconds and stops the stage if the job was cancelled. Ctrl+C / shutdown releases the
lease (fail, retryable); a sleeping laptop just lets the lease expire and the server re-queues it.

A worker never holds platform tokens, never publishes and never polls Telegram.
"""

from __future__ import annotations

import os
import platform
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from clipforge import __version__
from clipforge.config import Config, Secrets, load_config
from clipforge.server import artifacts
from clipforge.utils.log import CancelledError, log

CHUNK = 8 * 1024 * 1024


class ServerError(RuntimeError):
    def __init__(self, msg: str, status: int = 0) -> None:
        super().__init__(msg)
        self.status = status


@dataclass
class WorkerState:
    connected: bool = False
    server: str = ""
    last_contact: float = 0
    last_heartbeat: float = 0
    task: dict | None = None
    progress: float = 0
    message: str = ""
    error: str = ""
    done: int = 0
    history: list[dict] = field(default_factory=list)


_state = WorkerState()


def state() -> dict:
    s = _state
    return {"connected": s.connected, "server": s.server, "last_contact": s.last_contact or None,
            "last_heartbeat": s.last_heartbeat or None, "task": s.task, "progress": round(s.progress, 3),
            "message": s.message, "error": s.error, "done": s.done, "history": s.history[-10:]}


class Reporter:
    """ProgressReporter for a stage running on the worker: progress goes to the server with the heartbeat."""

    def __init__(self) -> None:
        self.cancel = threading.Event()
        self.stage_name = ""
        self.fraction = 0.0
        self.eta: float | None = None
        self.message = ""

    def stage(self, name: str, status: str = "running", message: str = "") -> None:
        self.stage_name = name
        self.message = f"{name}: {status}" + (f" — {message}" if message else "")
        log.info(f"[worker] {self.message}")

    def progress(self, fraction: float, eta_sec: float | None = None, message: str = "") -> None:
        self.fraction, self.eta = fraction, eta_sec
        if message:
            self.message = message
        _state.progress, _state.message = fraction, message or _state.message

    def log(self, message: str, level: str = "info") -> None:
        getattr(log, level if level in ("info", "warning", "error") else "info")(f"[worker] {message}")

    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def clip_done(self, rank: int) -> None:
        pass


class WorkerClient:
    def __init__(self, server_url: str, key: str, cfg: Config | None = None, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, http: httpx.Client | None = None) -> None:
        self.cfg = cfg or load_config()
        self.server_url = server_url.rstrip("/")
        self.http = http or httpx.Client(base_url=self.server_url, headers={"Authorization": f"Bearer {key}"},
                                         timeout=httpx.Timeout(120, connect=15), transport=transport)
        self.sleep = sleep
        self.caps = list(self.cfg.deployment.capabilities)
        self.stop = threading.Event()
        self.hb_seconds = self.cfg.deployment.heartbeat_seconds
        _state.server = self.server_url

    # ---- http
    def call(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self.http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            _state.connected = False
            raise ServerError(f"server unreachable: {type(exc).__name__}") from exc
        _state.connected, _state.last_contact = True, time.time()
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ServerError(str(detail)[:500], r.status_code)
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r

    def hello(self) -> dict:
        from clipforge.utils.gpu import nvenc_available

        try:
            gpu = bool(nvenc_available())
        except Exception:
            gpu = False
        return self.call("POST", "/api/worker/hello", json={"capabilities": self.caps, "max_parallel": self.cfg.deployment.max_parallel,
                                                            "gpu": gpu, "version": f"{__version__} {platform.system()}"})

    # ---- one task
    def run_once(self) -> bool:
        """Claim and do one task. False = nothing to do."""
        res = self.call("POST", "/api/worker/claim", json={"capabilities": self.caps})
        task = res.get("task")
        if not task:
            return False
        _state.task = {k: task.get(k) for k in ("id", "fence", "job_id", "stage", "video_id", "title")}
        _state.error = ""
        rep = Reporter()
        beat = threading.Thread(target=self._heartbeat, args=(task, rep), daemon=True)
        beat.start()
        try:
            files, deleted, st, result = self.execute(task, rep)
            if rep.cancelled():
                raise CancelledError("cancelled on the server")
            self.push(task, files)
            self.call("POST", "/api/worker/complete", json={"task_id": task["id"], "fence": task["fence"], "files": [
                {"path": p, "size": s, "sha256": h} for p, s, h in files], "deleted": deleted, "state": st, "result": result})
            _state.done += 1
            _state.history.append({"task": task["id"], "job": task["job_id"], "stage": task["stage"], "ok": True, "at": time.time()})
            return True
        except CancelledError:
            log.info(f"[worker] task {task['id']} cancelled")
            return True
        except ServerError as exc:
            if exc.status == 409:  # our lease is gone (expired while we slept, or cancelled): drop the results
                log.warning(f"[worker] task {task['id']}: {exc} — results dropped (the server has newer ones)")
                return True
            raise
        except Exception as exc:
            from clipforge.stages.download import DownloadBlocked

            _state.error = str(exc)[:300]
            _state.history.append({"task": task["id"], "job": task["job_id"], "stage": task["stage"], "ok": False, "at": time.time()})
            log.error(f"[worker] task {task['id']} ({task['stage']}) failed: {exc}")
            try:
                self.call("POST", "/api/worker/fail", json={"task_id": task["id"], "fence": task["fence"], "error": str(exc)[:1500],
                                                            "retryable": not isinstance(exc, DownloadBlocked),
                                                            "blocked": isinstance(exc, DownloadBlocked)})
            except ServerError:
                pass
            return True
        finally:
            rep.cancel.set()  # stops the heartbeat thread
            _state.task = None

    def _heartbeat(self, task: dict, rep: Reporter) -> None:
        while not rep.cancel.wait(self.hb_seconds):
            try:
                res = self.call("POST", "/api/worker/heartbeat", json={
                    "task_id": task["id"], "fence": task["fence"], "progress": rep.fraction, "eta": rep.eta,
                    "message": rep.message[:200]})
                _state.last_heartbeat = time.time()
                if res.get("status") == "cancel":
                    rep.cancel.set()
            except ServerError as exc:
                if exc.status == 409:
                    rep.cancel.set()  # lease lost: stop working on it
                else:
                    log.warning(f"[worker] heartbeat failed: {exc}")

    def job_config(self, task: dict) -> Config:
        """The job's config from the server (same fingerprints), with this machine's own paths / Ollama URL."""
        data = dict(task["config"])
        local = self.cfg.model_dump(mode="json")
        data["output"] = local["output"]
        data["llm"] = {**data["llm"], "ollama_url": local["llm"]["ollama_url"]}
        return Config.model_validate(data)

    def execute(self, task: dict, rep: Reporter) -> tuple[list[tuple[str, int, str]], list[str], dict, dict]:
        from clipforge import pipeline
        from clipforge.workspace import Workspace

        cfg = self.job_config(task)
        if task["stage"] == "import":
            raise RuntimeError("import tasks are created by push_local_job")
        ws = Workspace(cfg.workspace, task["video_id"])
        self.pull(task, ws.dir)
        before = artifacts.snapshot(ws.dir)
        state_before = ws._state()
        section = tuple(task["section"]) if task.get("section") else None
        stage = task["stage"]
        if stage == "download":
            pipeline.run_download(ws, task["source"], cfg, rep, section)
        elif stage == "transcribe":
            pipeline.run_transcribe(ws, cfg, rep)
        elif stage == "select":
            pipeline.run_analyze(ws, rep)
            pipeline.select(ws, cfg, rep, force=bool(task.get("force_select")))
        elif stage == "render":
            pipeline.render(ws, cfg, rep, only=task.get("ranks"), force=bool(task.get("force")))
            pipeline.metadata(ws, cfg, rep)
        else:
            raise RuntimeError(f"unknown stage {stage}")
        changed, deleted = artifacts.changed_since(ws.dir, before)
        st = {k: v for k, v in ws._state().items() if state_before.get(k) != v and k in artifacts.STATE_KEYS}
        files = [(p, (ws.dir / p).stat().st_size, artifacts.local_sha(ws.dir, p) or "") for p in changed]
        return files, deleted, st, {"video_id": task["video_id"]}

    # ---- transfers
    def pull(self, task: dict, ws_dir: Path) -> int:
        n = 0
        for f in task.get("manifest") or []:
            rel = artifacts.check_path(f["path"])
            if artifacts.local_sha(ws_dir, rel) == f["sha256"]:
                continue  # already here (e.g. the laptop downloaded the source itself)
            dest = ws_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(dest.name + ".part")
            have = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            url = f"/api/worker/artifacts/{task['id']}/{rel}"
            with self.http.stream("GET", url, params={"fence": task["fence"]}, headers=headers) as r:
                if r.status_code >= 400:
                    raise ServerError(f"pull {rel}: HTTP {r.status_code}", r.status_code)
                mode = "ab" if have and r.status_code == 206 else "wb"
                with open(part, mode) as out:
                    for chunk in r.iter_bytes(1 << 20):
                        out.write(chunk)
            if artifacts.sha256(part) != f["sha256"]:
                part.unlink(missing_ok=True)
                raise RuntimeError(f"{rel}: checksum mismatch after download — will retry")
            os.replace(part, dest)
            n += 1
        return n

    def push(self, task: dict, files: list[tuple[str, int, str]], ws_dir: Path | None = None) -> None:
        from clipforge.workspace import Workspace

        ws_dir = ws_dir or Workspace(self.job_config(task).workspace, task["video_id"]).dir
        for rel, size, _ in files:
            have = self.call("GET", f"/api/worker/staged/{task['id']}/{rel}", params={"fence": task["fence"]})["size"]
            if have > size:
                have = 0
            with open(ws_dir / rel, "rb") as f:
                f.seek(have)
                offset = have
                while offset < size or size == 0:
                    chunk = f.read(CHUNK)
                    self.call("PUT", f"/api/worker/artifacts/{task['id']}/{rel}", params={"fence": task["fence"], "offset": offset},
                              content=chunk, headers={"content-type": "application/octet-stream"})
                    offset += len(chunk)
                    if not chunk:
                        break

    def push_local_job(self, job) -> int:
        """A job this laptop ran while the server was unreachable → create it on the server and upload its files."""
        from clipforge.workspace import Workspace

        res = self.call("POST", "/api/worker/import", json={"video_id": job.video_id, "source": job.source,
                                                            "title": job.title, "settings": job.settings})
        task = {"id": res["task_id"], "fence": res["fence"], "video_id": job.video_id}
        ws_dir = Workspace(self.cfg.workspace, job.video_id).dir
        rels = [p for p in artifacts.snapshot(ws_dir)]
        files = [(p, (ws_dir / p).stat().st_size, artifacts.local_sha(ws_dir, p) or "") for p in rels]
        self.push(task, files, ws_dir)
        ws = Workspace(self.cfg.workspace, job.video_id)
        self.call("POST", "/api/worker/complete", json={"task_id": task["id"], "fence": task["fence"],
                                                        "files": [{"path": p, "size": s, "sha256": h} for p, s, h in files],
                                                        "state": {k: v for k, v in ws._state().items() if k in artifacts.STATE_KEYS},
                                                        "result": {"video_id": job.video_id}})
        return res["job_id"]

    # ---- loop
    def push_pending_local_jobs(self) -> None:
        from clipforge.api import db
        from clipforge.notify import tg_store as st

        pushed = set(st.kv_get("pushed_jobs", []) or [])
        for job in db.list_jobs():
            if job.status == "done" and job.id not in pushed and job.video_id and not job.settings.get("from_server"):
                try:
                    remote_id = self.push_local_job(job)
                    log.info(f"[worker] local job {job.id} uploaded to the server as job {remote_id}")
                    pushed.add(job.id)
                    st.kv_set("pushed_jobs", sorted(pushed))
                except ServerError as exc:
                    log.warning(f"[worker] couldn't upload local job {job.id}: {exc}")
                    return

    def loop(self, idle: float = 10.0) -> None:
        backoff = 5.0
        said_hello = False
        while not self.stop.is_set():
            try:
                if not said_hello:
                    info = self.hello()
                    self.hb_seconds = info.get("heartbeat_seconds", self.hb_seconds)
                    log.info(f"[worker] connected to {info.get('server')} as {info.get('name')}")
                    said_hello = True
                    self.push_pending_local_jobs()
                did = self.run_once()
                backoff = 5.0
                if not did:
                    self.stop.wait(idle)
                    self.hello()  # also our "I'm online" ping
            except ServerError as exc:
                said_hello = False
                _state.error = str(exc)
                log.warning(f"[worker] {exc} — retrying in {backoff:.0f} s")
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 300)
            except Exception as exc:  # never die: a bad task must not stop the worker
                _state.error = str(exc)[:300]
                log.error(f"[worker] unexpected: {exc}")
                self.stop.wait(backoff)

    def shutdown(self) -> None:
        """Release a running lease cleanly (sleep / shutdown / Ctrl+C) so the server re-queues it at once."""
        self.stop.set()
        t = _state.task
        if t:
            try:
                self.call("POST", "/api/worker/fail", json={"task_id": t["id"], "fence": t.get("fence", 0),
                                                            "error": "worker stopped", "retryable": True})
            except Exception:
                pass


_client: WorkerClient | None = None


def client_from_config() -> WorkerClient:
    cfg = load_config()
    key = Secrets().clipforge_worker_key
    if not cfg.deployment.server_url or not key:
        raise RuntimeError("Set deployment.server_url in config.yaml and CLIPFORGE_WORKER_KEY in .env "
                           "(from `clipforge server add-worker` on the server).")
    return WorkerClient(cfg.deployment.server_url, key, cfg)


def start_background() -> None:
    global _client
    if _client is not None:
        return
    try:
        _client = client_from_config()
    except RuntimeError as exc:
        log.warning(f"[worker] not started: {exc}")
        _state.error = str(exc)
        return
    threading.Thread(target=_client.loop, name="clipforge-worker-loop", daemon=True).start()
