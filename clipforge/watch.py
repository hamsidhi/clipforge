"""`clipforge watch <folder>`: start a job for every video file dropped into a folder (e.g. a Google Drive / OneDrive
folder synced to the laptop, so you can add episodes from your phone). Off by default.

A file is picked up once its size stopped changing for `settle` seconds (sync clients write in pieces). Processed files
are remembered in <folder>/.clipforge-seen.json, so a restart never starts the same episode twice.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi"}


def scan(folder: Path, seen: set[str], sizes: dict[str, tuple[int, float]], now: float, settle: float = 30) -> list[Path]:
    """Pure-ish: files that are new, video, and stable for `settle` seconds."""
    ready = []
    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.suffix.lower() not in VIDEO_EXT or f.name.startswith(".") or str(f) in seen:
            continue
        size = f.stat().st_size
        prev = sizes.get(str(f))
        if prev is None or prev[0] != size:
            sizes[str(f)] = (size, now)
            continue
        if now - prev[1] >= settle and size > 0:
            ready.append(f)
    return ready


def start_job(path: Path, port: int = 8000) -> int:
    """Start a pipeline job. If the web app is running, hand it over (so it shows up with live progress and its
    single worker runs it); otherwise run it in this process's own worker."""
    import httpx

    try:
        r = httpx.post(f"http://127.0.0.1:{port}/api/jobs", json={"url": str(path)}, timeout=10)
        if r.status_code == 200:
            return int(r.json()["id"])
    except httpx.HTTPError:
        pass
    from clipforge.api import db
    from clipforge.api.jobs import manager
    from clipforge.workspace import video_id_for

    with db.session() as s:
        job = db.Job(source=str(path), title=path.stem, settings_json="{}", video_id=video_id_for(str(path), None))
        s.add(job)
        s.commit()
        s.refresh(job)
    if not manager.thread.is_alive():
        manager.start()
    manager.submit("pipeline", job.id)  # type: ignore[arg-type]
    return job.id  # type: ignore[return-value]


def watch(folder: Path, interval: float = 10, settle: float = 30, on_job: Callable[[Path, int], None] | None = None,
          once: bool = False, start: Callable[[Path], int] = start_job) -> None:
    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        raise SystemExit(f"{folder} isn't a folder.")
    seen_file = folder / ".clipforge-seen.json"
    seen: set[str] = set(json.loads(seen_file.read_text(encoding="utf-8"))) if seen_file.exists() else set()
    sizes: dict[str, tuple[int, float]] = {}
    while True:
        for f in scan(folder, seen, sizes, time.time(), settle):
            jid = start(f)
            seen.add(str(f))
            seen_file.write_text(json.dumps(sorted(seen)), encoding="utf-8")
            if on_job:
                on_job(f, jid)
        if once:
            return
        time.sleep(interval)
