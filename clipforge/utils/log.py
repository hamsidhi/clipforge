"""Shared rich console + logger, plus a progress-reporting hook the API can subscribe to."""

from __future__ import annotations

import logging
import os
from typing import Callable, Protocol

from rich.console import Console
from rich.logging import RichHandler

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
# MediaPipe / TFLite (glog + absl) print INFO lines to stderr; keep them quiet
os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
console = Console(highlight=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, show_path=False, markup=False, rich_tracebacks=True)],
)
for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "huggingface_hub", "urllib3", "faster_whisper",
              "pyscenedetect", "absl", "multipart", "googleapiclient"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("clipforge")


class ProgressReporter(Protocol):
    """Receives stage progress. The CLI prints it; the API streams it over SSE."""

    def stage(self, name: str, status: str = "running", message: str = "") -> None: ...
    def progress(self, fraction: float, eta_sec: float | None = None, message: str = "") -> None: ...
    def log(self, message: str, level: str = "info") -> None: ...
    def cancelled(self) -> bool: ...


class NullReporter:
    """Default reporter: forwards log lines to the console logger only."""

    def stage(self, name: str, status: str = "running", message: str = "") -> None:
        if status == "running":
            log.info(f"▶ {name}{': ' + message if message else ''}")
        elif status == "skipped":
            log.info(f"⏭ {name} (cached){': ' + message if message else ''}")
        elif status == "done":
            log.info(f"✔ {name}{': ' + message if message else ''}")
        elif status == "error":
            log.error(f"✖ {name}: {message}")

    def progress(self, fraction: float, eta_sec: float | None = None, message: str = "") -> None:
        pass

    def log(self, message: str, level: str = "info") -> None:
        getattr(log, level if level in ("info", "warning", "error", "debug") else "info")(message)

    def cancelled(self) -> bool:
        return False


class CliReporter(NullReporter):
    """Rich progress bar with ETA for the current stage (used by the CLI)."""

    def __init__(self) -> None:
        from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn, TimeRemainingColumn

        self._progress = Progress(
            TextColumn("[bold]{task.fields[stage]:<11}"), BarColumn(), TextColumn("{task.percentage:>3.0f}%"),
            TimeElapsedColumn(), TextColumn("ETA"), TimeRemainingColumn(), TextColumn("[dim]{task.description}"),
            console=console, transient=True,
        )
        self._task = None

    def stage(self, name: str, status: str = "running", message: str = "") -> None:
        if self._task is not None:
            self._progress.remove_task(self._task)
            self._task = None
        if status == "running":
            self._progress.start()
            self._task = self._progress.add_task("", total=1.0, stage=name)
        else:
            self._progress.stop()
        super().stage(name, status, message)

    def progress(self, fraction: float, eta_sec: float | None = None, message: str = "") -> None:
        if self._task is not None:
            self._progress.update(self._task, completed=max(0.0, min(1.0, fraction)), description=message[:60])

    def close(self) -> None:
        self._progress.stop()


class CancelledError(RuntimeError):
    """Raised inside a stage when the user cancels the job."""


def check_cancel(reporter: ProgressReporter) -> None:
    if reporter.cancelled():
        raise CancelledError("Job cancelled by user")


ProgressCallback = Callable[[float, float | None, str], None]
