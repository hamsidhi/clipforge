"""Publishing worker: a light second thread (uploads are network-bound and must not wait behind a 1-hour
transcription in the main job queue). Tasks are de-duplicated by key. A ticker runs the scheduler every 10 min."""

from __future__ import annotations

import collections
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from clipforge.config import load_config
from clipforge.utils.log import log

TICK_SECONDS = 600


@dataclass
class PTask:
    key: str
    label: str
    fn: Callable[[], Any]
    progress: dict = field(default_factory=dict)


class PublishWorker:
    def __init__(self) -> None:
        self.q: queue.Queue[PTask] = queue.Queue()
        self.pending: dict[str, PTask] = {}
        self.current: PTask | None = None
        self.lock = threading.Lock()
        self.history: collections.deque = collections.deque(maxlen=50)
        self.upload_progress: dict[int, float] = {}
        self._started = False

    def start(self, ticker: bool = True) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, name="clipforge-publish", daemon=True).start()
        if ticker:
            threading.Thread(target=self._tick, name="clipforge-publish-tick", daemon=True).start()

    def submit(self, key: str, label: str, fn: Callable[[], Any]) -> bool:
        """False if an identical task is already waiting."""
        with self.lock:
            if key in self.pending:
                return False
            t = PTask(key, label, fn)
            self.pending[key] = t
        self.q.put(t)
        return True

    def busy_keys(self) -> list[str]:
        with self.lock:
            keys = list(self.pending)
            if self.current:
                keys.append(self.current.key)
            return keys

    def state(self) -> dict:
        with self.lock:
            return {"current": self.current.label if self.current else None,
                    "pending": [t.label for t in self.pending.values()],
                    "history": list(self.history)[-15:], "upload_progress": dict(self.upload_progress)}

    def _loop(self) -> None:
        while True:
            t = self.q.get()
            with self.lock:
                self.pending.pop(t.key, None)
                self.current = t
            started = time.time()
            try:
                result = t.fn()
                summary = result if isinstance(result, (str, int, float)) else _summarize(result)
                self.history.append({"task": t.label, "ok": True, "at": started, "result": summary})
            except Exception as exc:  # never kill the worker
                log.error(f"[publish] {t.label} failed: {exc}\n{traceback.format_exc()}")
                self.history.append({"task": t.label, "ok": False, "at": started, "error": str(exc)})
            finally:
                with self.lock:
                    self.current = None
                    self.upload_progress.clear()

    def _tick(self) -> None:
        time.sleep(30)
        while True:
            try:
                from clipforge.publish import channels

                if channels.all_channels():
                    self.submit("run", "Scheduler run", run_scheduler)
            except Exception as exc:
                log.warning(f"[publish] tick failed: {exc}")
            time.sleep(TICK_SECONDS)


def _summarize(r: Any) -> Any:
    if r is None:
        return None
    if hasattr(r, "__dataclass_fields__"):
        from dataclasses import asdict

        d = asdict(r)
        return {k: (len(v) if isinstance(v, list) else v) for k, v in d.items() if v}
    if isinstance(r, dict):
        return {k: (len(v) if isinstance(v, list) else v) for k, v in r.items()}
    return str(r)[:200]


worker = PublishWorker()


def run_scheduler():
    from clipforge.publish import runner

    return runner.run_once(load_config(), progress=lambda item_id, f: worker.upload_progress.__setitem__(item_id, round(f, 3)))
