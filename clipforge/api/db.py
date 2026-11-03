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
    # campaign / Whop tracking (Phase 12)
    campaign_fee_pct: float = 0      # platform/campaign fee taken from earnings, in %
    submission_status: str = "not_submitted"  # not_submitted | submitted | approved | rejected | paid
    submission_link: str = ""
    approved_views: Optional[int] = None  # views the campaign approved (None = use views)
    queue_id: Optional[int] = None
    youtube_video_id: str = ""


class Preset(SQLModel, table=True):
    """User-defined caption style preset (built-ins live in stages/captions.py)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    label: str
    style_json: str = "{}"


class Channel(SQLModel, table=True):
    """A YouTube channel ClipForge can publish to (token in workspace/channels/<id>/token.json)."""

    id: str = Field(primary_key=True)          # YouTube channel id (UC…)
    title: str = ""
    handle: str = ""
    thumbnail: str = ""
    uploads_playlist: str = ""
    auth_status: str = "ok"                   # ok | needs_reauth | missing
    auth_error: str = ""
    paused: bool = False
    confirmed: bool = False                   # first-upload confirmation given (avatar + name shown)
    settings_json: str = "{}"
    created_at: dt.datetime = Field(default_factory=now)
    last_sync_at: Optional[dt.datetime] = None

    @property
    def settings(self) -> dict[str, Any]:
        return json.loads(self.settings_json or "{}")


class QueueItem(SQLModel, table=True):
    """One clip planned for one channel. Times are UTC; the UI shows IST."""

    id: Optional[int] = Field(default=None, primary_key=True)
    channel_id: str = Field(index=True)
    video_id: str                              # ClipForge workspace id of the source episode
    rank: int
    clip_key: str = Field(index=True)          # video_id + start/end — stable even if ranks change
    episode_title: str = ""
    topic: str = ""
    score: float = 0
    duration: float = 0
    slot_utc: Optional[dt.datetime] = Field(default=None, index=True)
    status: str = "draft"                      # draft|queued|uploading|scheduled|published|failed|locked_private|needs_manual
    youtube_video_id: str = ""
    attempts: int = 0
    next_attempt_at: Optional[dt.datetime] = None
    error: str = ""
    title: str = ""
    description: str = ""
    tags_json: str = "[]"
    category_id: str = "27"
    variants_json: str = "[]"                  # generated title variants with scores
    meta_status: str = "pending"               # pending | ready | failed
    ig_caption: str = ""
    batch: str = ""                            # assisted-mode batch folder this item was put in
    published_at: Optional[dt.datetime] = None
    created_at: dt.datetime = Field(default_factory=now)
    updated_at: dt.datetime = Field(default_factory=now)

    @property
    def tags(self) -> list[str]:
        return json.loads(self.tags_json or "[]")


class VideoPerf(SQLModel, table=True):
    """Latest performance numbers of one uploaded Short."""

    youtube_video_id: str = Field(primary_key=True)
    channel_id: str = Field(index=True)
    queue_id: Optional[int] = None
    title: str = ""
    published_at: Optional[dt.datetime] = None
    duration: float = 0
    views: int = 0
    likes: int = 0
    comments: int = 0
    views_24h: Optional[int] = None
    views_72h: Optional[int] = None
    avg_view_pct: Optional[float] = None
    subs_gained: Optional[int] = None
    updated_at: dt.datetime = Field(default_factory=now)


class StatSnapshot(SQLModel, table=True):
    """Views history (one row per video per sync) — used when the Analytics API isn't available."""

    id: Optional[int] = Field(default=None, primary_key=True)
    youtube_video_id: str = Field(index=True)
    taken_at: dt.datetime = Field(default_factory=now)
    views: int = 0


class ChannelDaily(SQLModel, table=True):
    """Channel-level numbers per sync day."""

    id: Optional[int] = Field(default=None, primary_key=True)
    channel_id: str = Field(index=True)
    day: dt.date
    subscribers: int = 0
    total_views: int = 0
    shorts_views_28d: Optional[int] = None
    shorts_views_90d: Optional[int] = None
    uploads_90d: Optional[int] = None


def _migrate(conn) -> None:
    """create_all() doesn't add columns to existing tables: add missing ones (SQLite ALTER TABLE ADD COLUMN)."""
    for table in SQLModel.metadata.sorted_tables:
        cols = {row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table.name}")').fetchall()}
        if not cols:
            continue
        for col in table.columns:
            if col.name not in cols:
                ddl = col.type.compile(dialect=conn.dialect)
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                lit = "" if default is None else f" DEFAULT {default!r}" if isinstance(default, str) else f" DEFAULT {int(default) if isinstance(default, bool) else default}"
                conn.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}{lit}')


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
        with _engine.begin() as conn:
            _migrate(conn)
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
