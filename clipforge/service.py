"""`clipforge service`: keep ClipForge (web app + publishing scheduler) running on the laptop (Phase 14).

A hidden Windows Task Scheduler task starts `pythonw -m clipforge service run` at logon. `run` is a watchdog: it starts
`clipforge ui --no-browser`, restarts it when it exits/crashes (backoff 5 s → 5 min), and logs to
workspace/logs/service.log (rotated). The UI process itself logs to workspace/logs/clipforge.log.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from clipforge.config import ROOT_DIR, load_config

TASK = "ClipForge Service"
BACKOFF = (5, 10, 30, 60, 120, 300)
HEALTHY_AFTER = 300  # a run longer than this resets the backoff


def task_xml() -> str:
    from clipforge.utils import winsched

    return winsched.task_xml("ClipForge: web app + publishing scheduler, restarted automatically (watchdog).",
                             winsched.pythonw(), "-m clipforge service run", str(ROOT_DIR), logon=True,
                             restart_on_failure=True, time_limit="PT0S")


def ui_command(port: int = 8000) -> list[str]:
    return [sys.executable, "-m", "clipforge", "ui", "--no-browser", "--port", str(port)]


def _logger() -> logging.Logger:
    d = load_config().workspace / "logs"
    d.mkdir(parents=True, exist_ok=True)
    lg = logging.getLogger("clipforge.service")
    if not lg.handlers:
        h = RotatingFileHandler(d / "service.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
    return lg


def next_delay(failures: int) -> int:
    return BACKOFF[min(failures, len(BACKOFF) - 1)]


def run(port: int = 8000, max_restarts: int | None = None, popen=subprocess.Popen, sleep=time.sleep,
        clock=time.monotonic) -> int:
    """Watchdog loop. `max_restarts`/`popen`/`sleep`/`clock` exist for tests."""
    lg = _logger()
    failures = restarts = 0
    out = (load_config().workspace / "logs" / "ui-console.log")
    while True:
        started = clock()
        lg.info(f"starting: {' '.join(ui_command(port))}")
        with open(out, "a", encoding="utf-8", errors="replace") as fh:
            flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
            proc = popen(ui_command(port), cwd=str(ROOT_DIR), stdout=fh, stderr=subprocess.STDOUT, creationflags=flags)
            code = proc.wait()
        ran = clock() - started
        failures = 0 if ran > HEALTHY_AFTER else failures + 1
        delay = next_delay(failures)
        lg.info(f"ClipForge exited with code {code} after {ran:.0f}s — restarting in {delay}s")
        restarts += 1
        if max_restarts is not None and restarts >= max_restarts:
            return restarts
        _trim(out)
        sleep(delay)


def _trim(path: Path, keep: int = 2_000_000) -> None:
    """Keep the console log small (the UI's own log is rotated separately)."""
    try:
        if path.stat().st_size > keep * 2:
            data = path.read_bytes()[-keep:]
            path.write_bytes(data)
    except OSError:
        pass


def install() -> tuple[bool, str]:
    from clipforge.utils import winsched

    return winsched.install(TASK, task_xml())


def remove() -> tuple[bool, str]:
    from clipforge.utils import winsched

    return winsched.remove(TASK)


def state() -> dict | None:
    from clipforge.utils import winsched

    return winsched.query(TASK)
