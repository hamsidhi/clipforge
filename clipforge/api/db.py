"""SQLite persistence (workspace/clipforge.db): jobs, tracker posts, custom caption presets.

Clip content itself lives in each video's candidates.json (single source of truth shared with the CLI).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Optional

from sqlalchemy import event
from sqlmodel import Field, Session, SQLModel, create_engine, select

from clipforge.config import load_config


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


class Job(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    source: str
    video_id: Optional[str] = None
    title: str = ""
    channel: str = ""
    duration: float = 0
    status: str = "queued"          # queued | running | done | failed | cancelled
    stage: str = ""
    stage_status: str = ""
    progress: float = 0             # 0..1 within the current stage
    eta_sec: Optional[float] = None
    message: str = ""
    error: str = ""
    settings_json: str = "{}"
    n_clips: int = 0
    created_at: dt.datetime = Field(default_factory=now)
    updated_at: dt.datetime = Field(default_factory=now)
    finished_at: Optional[dt.datetime] = None

    @property
    def settings(self) -> dict[str, Any]:
        return json.loads(self.settings_json or "{}")


class Post(SQLModel, table=True):
    """Tracker row: a clip posted somewhere + its (manually entered) views."""

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: Optional[int] = None
    clip_rank: Optional[int] = None
    clip_title: str = ""
    platform: str = "instagram"     # instagram | youtube
    url: str = ""
    posted_at: Optional[dt.date] = None
    campaign: str = ""
    rate_per_1k: float = 0          # campaign payout per 1,000 views
    views: int = 0
    notes: str = ""
    created_at: dt.datetime = Field(default_factory=now)


class Preset(SQLModel, table=True):
    """User-defined caption style preset (built-ins live in stages/captions.py)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    label: str
    style_json: str = "{}"


_engine = None


def engine():
    global _engine
    if _engine is None:
        cfg = load_config()
        cfg.workspace.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(f"sqlite:///{cfg.workspace / 'clipforge.db'}", connect_args={"check_same_thread": False})

        @event.listens_for(_engine, "connect")
        def _pragma(conn, _):  # WAL: the worker thread writes while requests read
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

        SQLModel.metadata.create_all(_engine)
    return _engine


def session() -> Session:
    return Session(engine(), expire_on_commit=False)


def get_job(job_id: int) -> Job | None:
    with session() as s:
        return s.get(Job, job_id)


def update_job(job_id: int, **fields: Any) -> Job | None:
    with session() as s:
        job = s.get(Job, job_id)
        if not job:
            return None
        for k, v in fields.items():
            setattr(job, k, v)
        job.updated_at = now()
        s.add(job)
        s.commit()
        s.refresh(job)
        return job


def list_jobs() -> list[Job]:
    with session() as s:
        return list(s.exec(select(Job).order_by(Job.created_at.desc())).all())  # type: ignore[attr-defined]
