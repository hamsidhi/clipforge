"""Keep the laptop awake while ClipForge has work (Windows SetThreadExecutionState).

`power.keep_awake`: always | when_busy (a job is running, or an Instagram post is due within 60 min) | never.
The request is held by one long-lived thread (execution state is per thread) and released when not needed.
macOS/Linux: no-op (use `caffeinate` / systemd-inhibit if you need it).
"""

from __future__ import annotations

import datetime as dt
import sys
import threading
from typing import Callable

from clipforge.utils.log import log

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
BUSY_WINDOW = dt.timedelta(minutes=60)


def _default_call(flags: int) -> int:
    if sys.platform != "win32":
        return 1
    import ctypes

    return ctypes.windll.kernel32.SetThreadExecutionState(flags)  # type: ignore[attr-defined]


class KeepAwake:
    """State machine: holding / released. `set_call` swaps the Windows call (tests)."""

    def __init__(self, call: Callable[[int], int] = _default_call) -> None:
        self.call = call
        self.holding = False
        self.reason = ""
        self._cv = threading.Condition()
        self._want: bool | None = None
        self._thread: threading.Thread | None = None

    def want(self, on: bool, reason: str = "") -> None:
        """Ask for the state; applied on the keeper thread (same thread must set and clear the flag)."""
        self.reason = reason if on else ""
        if self.call is not _default_call:  # tests: apply synchronously
            self._apply(on)
            return
        with self._cv:
            self._want = on
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="clipforge-keepawake", daemon=True)
                self._thread.start()
            self._cv.notify()

    def _apply(self, on: bool) -> None:
        if on == self.holding:
            return
        ok = self.call(ES_CONTINUOUS | ES_SYSTEM_REQUIRED if on else ES_CONTINUOUS)
        if not ok:
            log.warning("Windows refused SetThreadExecutionState — the laptop may sleep during work.")
            return
        self.holding = on
        log.info("Keeping the laptop awake: " + self.reason if on else "Laptop may sleep again (nothing due).")

    def _run(self) -> None:
        while True:
            with self._cv:
                while self._want is None:
                    self._cv.wait()
                on, self._want = self._want, None
            self._apply(on)


keeper = KeepAwake()


def busy_reason(now: dt.datetime | None = None) -> str:
    """Why the laptop should stay awake right now ('' = no reason)."""
    from clipforge.api import db
    from clipforge.publish import queue as q
    from clipforge.publish.timez import as_utc, now_utc

    now = now or now_utc()
    try:
        from sqlmodel import select

        with db.session() as s:
            if s.exec(select(db.Job).where(db.Job.status == "running")).first():
                return "a ClipForge job is running"
    except Exception:
        pass
    from clipforge.config import load_config
    from clipforge.publish import channels as chs
    from clipforge.publish import instagram as ig

    from clipforge.publish import modes

    cfg = load_config()
    live = {c.id for c in chs.all_channels() if c.platform == "instagram" and not c.paused
            and c.auth_status == "ok" and not ig.is_manual(c) and modes.resolve(c, cfg, check=False).auto}
    if live:
        for it in q.items(statuses=("queued", "uploading", "processing", "ready")):
            if it.platform == "instagram" and it.channel_id in live and it.slot_utc and \
                    -dt.timedelta(minutes=5) <= as_utc(it.slot_utc) - now <= BUSY_WINDOW:
                return "an Instagram post is due within 60 min"
    for it in q.items(statuses=("uploading",)):
        return "an upload is running"
    return ""


def update(mode: str | None = None, now: dt.datetime | None = None) -> bool:
    """Apply the policy once (called every scheduler tick). Returns whether we hold the awake request."""
    from clipforge.config import load_config

    mode = mode or load_config().power.keep_awake
    if mode == "always":
        keeper.want(True, "power.keep_awake = always")
    elif mode == "never":
        keeper.want(False)
    else:
        why = busy_reason(now)
        keeper.want(bool(why), why)
    return keeper.holding


def status() -> dict:
    from clipforge.config import load_config

    return {"mode": load_config().power.keep_awake, "holding": keeper.holding, "reason": keeper.reason,
            "supported": sys.platform == "win32"}
