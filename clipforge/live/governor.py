"""CPU governor for live clipping: the live agent and every ffmpeg/whisper child run at low priority
(BELOW_NORMAL / IDLE) pinned to the last `max_cores` logical CPUs; when the whole laptop is busy, live work waits.
"""

from __future__ import annotations

import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from clipforge.utils.log import log

IDLE_PRIORITY_CLASS = 0x40
BELOW_NORMAL_PRIORITY_CLASS = 0x4000


def our_cores(max_cores: int) -> list[int]:
    n = os.cpu_count() or 1
    k = max(1, min(max_cores, n))
    return list(range(n - k, n))  # the last cores: games usually prefer the first ones


def make_low_priority(pid: int | None = None, max_cores: int = 2, idle: bool = True) -> None:
    """Lower a process's priority and pin it to our cores. Never raises (cosmetic if it fails)."""
    try:
        import psutil

        p = psutil.Process(pid or os.getpid())
        if sys.platform == "win32":
            p.nice(psutil.IDLE_PRIORITY_CLASS if idle else psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            p.nice(19 if idle else 10)
        if hasattr(p, "cpu_affinity"):
            p.cpu_affinity(our_cores(max_cores))
    except Exception as exc:
        log.warning(f"[live] could not lower the priority of pid {pid or os.getpid()}: {exc}")


def popen_kwargs() -> dict:
    """subprocess.Popen kwargs for a child that starts at idle priority (Windows) / nice 19."""
    if sys.platform == "win32":
        return {"creationflags": IDLE_PRIORITY_CLASS | 0x08000000}  # + CREATE_NO_WINDOW
    return {"preexec_fn": lambda: os.nice(19)}


@dataclass
class Governor:
    """Total CPU above `cpu_ceiling` for `sustain_sec` → "busy": chat/audio/HUD detectors and renders wait (the laptop's
    other work — the app, uploads, Ollama jobs — comes first)."""

    cpu_ceiling: float = 85
    sustain_sec: float = 5
    cpu_fn: Callable[[], float] | None = None       # tests inject; default psutil.cpu_percent()
    clock: Callable[[], float] = time.monotonic
    state: str = "ok"
    cpu: float = 0.0
    reason: str = ""
    _over_since: float | None = None
    history: deque = field(default_factory=lambda: deque(maxlen=120))

    def sample(self) -> str:
        """Call about once a second."""
        now = self.clock()
        if self.cpu_fn is not None:
            cpu = self.cpu_fn()
        else:
            import psutil

            cpu = psutil.cpu_percent(interval=None)
        self.cpu = float(cpu)
        self.history.append((round(now, 1), round(self.cpu, 1)))
        if self.cpu > self.cpu_ceiling:
            self._over_since = self._over_since if self._over_since is not None else now
        else:
            self._over_since = None
        if self._over_since is not None and now - self._over_since >= self.sustain_sec:
            self.state, self.reason = "busy", f"CPU {self.cpu:.0f}% > {self.cpu_ceiling:.0f}% — live clipping waits"
        else:
            self.state, self.reason = "ok", ""
        return self.state

    def allow_detectors(self) -> bool:
        return self.state == "ok"

    def allow_render(self) -> bool:
        return self.state == "ok"
