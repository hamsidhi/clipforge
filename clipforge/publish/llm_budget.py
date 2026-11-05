"""Publishing v2: the publisher's only LLM use is writing metadata for a clip that has none. It is capped
(`llm.max_calls_per_day`, `llm.max_concurrent`); when the cap is reached only the metadata step pauses (the clip
stays a draft with "metadata pending"), uploads and everything else continue. Counts live in
workspace/llm_budget.json (local days)."""

from __future__ import annotations

import datetime as dt
import json
import threading

from clipforge.config import Config

_lock = threading.Lock()
_sems: dict[int, threading.BoundedSemaphore] = {}


class BudgetExceeded(RuntimeError):
    pass


def _path(cfg: Config):
    return cfg.workspace / "llm_budget.json"


def used_today(cfg: Config) -> int:
    try:
        return int(json.loads(_path(cfg).read_text(encoding="utf-8")).get(dt.date.today().isoformat(), 0))
    except (OSError, ValueError):
        return 0


def _bump(cfg: Config) -> None:
    p = _path(cfg)
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        data = {}
    day = dt.date.today().isoformat()
    data = {k: v for k, v in data.items() if k >= (dt.date.today() - dt.timedelta(days=14)).isoformat()}
    data[day] = int(data.get(day, 0)) + 1
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


def left(cfg: Config) -> int | None:
    cap = cfg.llm.max_calls_per_day
    return None if cap <= 0 else max(0, cap - used_today(cfg))


class Budgeted:
    """Wraps an LLM provider: every call counts against the daily cap and holds a concurrency slot."""

    def __init__(self, llm, cfg: Config) -> None:
        self._llm, self._cfg = llm, cfg
        with _lock:
            self._sem = _sems.setdefault(cfg.llm.max_concurrent, threading.BoundedSemaphore(cfg.llm.max_concurrent))
        self.calls = 0

    def __getattr__(self, name):
        attr = getattr(self._llm, name)
        if not callable(attr) or name not in ("complete_json", "complete", "chat"):
            return attr

        def call(*a, **kw):
            with _lock:
                lft = left(self._cfg)
                if lft is not None and lft <= 0:
                    raise BudgetExceeded(f"Daily LLM budget for publishing metadata reached "
                                         f"({self._cfg.llm.max_calls_per_day} calls) — continues tomorrow.")
                _bump(self._cfg)
            self.calls += 1
            with self._sem:
                return attr(*a, **kw)

        return call
