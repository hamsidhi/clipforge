"""Disk housekeeping (server or standalone): `clipforge server cleanup [--dry-run]`, nightly on a server.

Rules (config `server`):
  * source video + audio of a finished job are deleted `keep_sources_days` after it finished (re-editing needs a Retry,
    which downloads again) — but never while one of its clips is still waiting to be published;
  * rendered clips are deleted `keep_clips_days_after_publish` after the LAST queue item of that episode went out —
    never while any queue item of that episode isn't published yet, and never when nothing was ever queued from it
    (you may still want those clips);
  * new jobs are refused above `disk_pause_pct`; Telegram warns from `disk_warn_pct`.
"""

from __future__ import annotations

import datetime as dt
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from clipforge.api import db
from clipforge.config import Config, load_config


@dataclass
class Plan:
    delete: list[Path] = field(default_factory=list)
    kept: dict[str, str] = field(default_factory=dict)   # video_id -> why kept
    bytes: int = 0


def _aware(t: dt.datetime | None) -> dt.datetime | None:
    return t.replace(tzinfo=dt.timezone.utc) if t is not None and t.tzinfo is None else t


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


def plan(cfg: Config | None = None, now: dt.datetime | None = None) -> Plan:
    from clipforge.publish import queue as q

    cfg = cfg or load_config()
    now = now or dt.datetime.now(dt.timezone.utc)
    out = Plan()
    jobs_by_video: dict[str, list[db.Job]] = {}
    for j in db.list_jobs():
        if j.video_id:
            jobs_by_video.setdefault(j.video_id, []).append(j)
    items_by_video: dict[str, list[db.QueueItem]] = {}
    for it in q.items():
        items_by_video.setdefault(it.video_id, []).append(it)
    root = cfg.workspace
    for vid, jobs in jobs_by_video.items():
        d = root / vid
        if not d.exists():
            continue
        if any(j.status in ("running", "queued") for j in jobs):
            out.kept[vid] = "a job is running"
            continue
        items = items_by_video.get(vid, [])
        waiting = [it for it in items if it.status != "published"]
        finished = [_aware(j.finished_at) for j in jobs if j.finished_at]
        last_finish = max(finished) if finished else None  # type: ignore[type-var]
        # sources
        if waiting:
            out.kept[vid] = f"{len(waiting)} clip(s) not published yet"
            continue
        if last_finish and now - last_finish > dt.timedelta(days=cfg.server.keep_sources_days):
            for f in (d / "source.mp4", d / "audio.wav"):
                if f.exists():
                    out.delete.append(f)
        else:
            out.kept[vid] = "finished recently"
        # clips: only after everything queued from it went out (+N days)
        if items:
            published = [_aware(it.published_at) for it in items if it.published_at]
            last_pub = max(published) if published else None  # type: ignore[type-var]
            if last_pub and now - last_pub > dt.timedelta(days=cfg.server.keep_clips_days_after_publish):
                clips = d / "clips"
                if clips.exists():
                    out.delete += [f for f in clips.glob("*.mp4")]
                    work = clips / "_work"
                    if work.exists():
                        out.delete += [f for f in work.rglob("*.mp4")]  # previews / previous renders
    out.bytes = sum(_size(p) for p in out.delete)
    return out


def run(cfg: Config | None = None, dry_run: bool = False, now: dt.datetime | None = None) -> Plan:
    p = plan(cfg, now)
    if not dry_run:
        for f in p.delete:
            f.unlink(missing_ok=True)
        incoming = (cfg or load_config()).workspace / "_incoming"
        if incoming.exists():  # staging left behind by tasks that never completed (older than a day)
            cutoff = (now or dt.datetime.now(dt.timezone.utc)).timestamp() - 86400
            for d in incoming.iterdir():
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
    return p


def disk_pct(cfg: Config | None = None) -> float:
    cfg = cfg or load_config()
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    u = shutil.disk_usage(cfg.workspace)
    return round(100 * u.used / u.total, 1) if u.total else 0.0


def refuse_if_disk_full(cfg: Config | None = None) -> None:
    from clipforge.services.errors import Conflict

    cfg = cfg or load_config()
    pct = disk_pct(cfg)
    if pct >= cfg.server.disk_pause_pct:
        raise Conflict(f"The disk is {pct}% full — new jobs are paused above {cfg.server.disk_pause_pct}%. "
                       "Run `clipforge server cleanup` or delete old jobs, then try again.")
