"""Shared rich console + logger, plus a progress-reporting hook the API can subscribe to."""

from __future__ import annotations

import logging
from typing import Callable, Protocol

from rich.console import Console
from rich.logging import RichHandler

console = Console(highlight=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, show_path=False, markup=False, rich_tracebacks=True)],
)
for noisy in ("httpx", "httpcore", "urllib3", "faster_whisper", "pyscenedetect", "absl", "multipart"):
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


class CancelledError(RuntimeError):
    """Raised inside a stage when the user cancels the job."""


def check_cancel(reporter: ProgressReporter) -> None:
    if reporter.cancelled():
        raise CancelledError("Job cancelled by user")


ProgressCallback = Callable[[float, float | None, str], None]
