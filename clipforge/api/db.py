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
    platform: str = "youtube"                 # youtube | instagram (id is "ig:<ig user id>")
    extra_json: str = "{}"                    # instagram: username, page id/name, followers, token expiry
    settings_json: str = "{}"
    created_at: dt.datetime = Field(default_factory=now)
    last_sync_at: Optional[dt.datetime] = None

    @property
    def settings(self) -> dict[str, Any]:
        return json.loads(self.settings_json or "{}")

    @property
    def extra(self) -> dict[str, Any]:
        return json.loads(self.extra_json or "{}")


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
    status: str = "draft"                      # see clipforge/publish/states.py
    reason: str = ""                           # why it needs attention (code in states.REASONS) — never a bare "Needs you"
    platform: str = "youtube"                  # youtube | instagram
    privacy: str = "scheduled"                 # youtube: scheduled (private+publishAt) | public | unlisted | private
    youtube_video_id: str = ""
    yt_state_json: str = "{}"                  # last verified YouTube state: uploadStatus, privacy, publishAt, processing…
    ig_container_id: str = ""                  # instagram: media container (expires after 24 h)
    ig_container_at: Optional[dt.datetime] = None
    ig_media_id: str = ""
    permalink: str = ""
    ig_thumb_offset_ms: Optional[int] = None
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
    # Publishing repair (2026-11-05): leases (one uploader per item), last attempt, stable idempotency key
    # ("<platform>:<account>:<clip_key>", unique with the clip/platform/account index below).
    last_attempt_at: Optional[dt.datetime] = None
    lease_owner: str = ""                      # "<host>:<pid>" while uploading
    lease_expires_at: Optional[dt.datetime] = None
    idempotency_key: str = ""
    # Publishing v2: publication identity (one per clip render × platform × account × intent; unique when set)
    pub_key: str = Field(default="", index=True)
    intent: str = "original"                   # original | republish:<n>
    created_at: dt.datetime = Field(default_factory=now)
    updated_at: dt.datetime = Field(default_factory=now)

    @property
    def tags(self) -> list[str]:
        return json.loads(self.tags_json or "[]")

    @property
    def yt_state(self) -> dict[str, Any]:
        return json.loads(self.yt_state_json or "{}")


class AuthSession(SQLModel, table=True):
    """A logged-in device (Phase 14). Only the SHA-256 of the cookie token is stored."""

    id: Optional[int] = Field(default=None, primary_key=True)
    token_hash: str = Field(index=True, unique=True)
    csrf: str = ""
    device: str = ""                           # user-agent summary / name the user gave
    ip: str = ""
    remember: bool = False
    created_at: dt.datetime = Field(default_factory=now)
    last_seen: dt.datetime = Field(default_factory=now)
    expires_at: dt.datetime = Field(default_factory=now)
    revoked: bool = False


class AuditEvent(SQLModel, table=True):
    """Security / publishing audit log shown in Settings → Security."""

    id: Optional[int] = Field(default=None, primary_key=True)
    at: dt.datetime = Field(default_factory=now, index=True)
    kind: str = ""                             # login_ok | login_failed | lockout | logout | revoke | password | publish | telegram
    detail: str = ""
    ip: str = ""


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
    platform: str = "youtube"                  # instagram rows use id "ig:<media id>"
    shares: int = 0
    saves: int = 0
    reach: int = 0
    permalink: str = ""
    updated_at: dt.datetime = Field(default_factory=now)


class UploadLedger(SQLModel, table=True):
    """Every platform publish attempt (Publishing v2). Only rows with succeeded_at count as uploads; a video going
    public later is not an upload. Used for the 24-h window, cooldowns and /health (no secrets stored)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="youtube", index=True)
    channel_id: str = Field(index=True)
    pub_key: str = ""
    queue_id: Optional[int] = None
    video_id: str = ""                         # YouTube video id / Instagram media id
    attempted_at: dt.datetime = Field(default_factory=now, index=True)
    succeeded_at: Optional[dt.datetime] = None
    result: str = ""                           # ok | refused | error
    error_type: str = ""                       # project_quota | channel_limit | auth | temporary | permanent
    error_reason: str = ""                     # the platform's reason code + message (no tokens)
    privacy: str = ""
    publish_at: Optional[dt.datetime] = None


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


# ----------------------------------------------------------------------------- Telegram (Phase 15)
class TgChat(SQLModel, table=True):
    """A paired Telegram chat. Only these chats (and in groups only the user who paired) can use the bot."""

    chat_id: int = Field(primary_key=True)
    user_id: int = Field(index=True)          # the person; in a private chat equal to chat_id
    role: str = "owner"                       # owner = everything | viewer = read-only, receives clips/stats
    kind: str = "private"                     # private | group | supergroup
    name: str = ""
    paired_at: dt.datetime = Field(default_factory=now)
    last_seen: Optional[dt.datetime] = None


class TgCallback(SQLModel, table=True):
    """Inline-button action: callback_data is only 'c:<id>' (≤ 64 bytes); the real action lives here."""

    id: str = Field(primary_key=True)
    chat_id: int = Field(index=True)
    user_id: Optional[int] = None             # set = only this user may press it (confirmations)
    action: str = ""
    payload_json: str = "{}"
    one_shot: bool = False                    # confirmations: never re-usable (idempotency)
    used_at: Optional[dt.datetime] = None
    expires_at: dt.datetime = Field(default_factory=now, index=True)


class TgConversation(SQLModel, table=True):
    """Waiting for typed input (ForceReply): one per chat, expires after 10 minutes, /cancel ends it."""

    chat_id: int = Field(primary_key=True)
    step: str = ""
    payload_json: str = "{}"
    expires_at: dt.datetime = Field(default_factory=now)


class TgKV(SQLModel, table=True):
    """Small durable bot state: getUpdates offset, poller lease, progress messages, wizards, PIN lockout…"""

    key: str = Field(primary_key=True)
    value: str = ""
    expires_at: Optional[dt.datetime] = Field(default=None, index=True)


class TgFile(SQLModel, table=True):
    """Telegram file_id of a file we already sent (re-sending by id is instant and costs no upload)."""

    key: str = Field(primary_key=True)        # path|size|mtime|method
    file_id: str = ""
    created_at: dt.datetime = Field(default_factory=now)


# ----------------------------------------------------------------------------- server + workers (Phase 15b)
class WorkerNode(SQLModel, table=True):
    """A laptop allowed to take heavy work from this server. Only the SHA-256 of its key is stored."""

    id: str = Field(primary_key=True)          # w_<hex>
    name: str = ""
    key_hash: str = Field(index=True)
    capabilities_json: str = "[]"
    max_parallel: int = 1
    gpu: bool = False
    version: str = ""
    created_at: dt.datetime = Field(default_factory=now)
    last_seen: Optional[dt.datetime] = None
    revoked: bool = False

    @property
    def capabilities(self) -> list[str]:
        return json.loads(self.capabilities_json or "[]")


class StageTask(SQLModel, table=True):
    """One pipeline stage of one job, given to a worker under a lease.

    queued → leased (worker_id, lease_until, fence) → done | failed | cancelled. A lease that isn't renewed goes back
    to queued; every claim increments `fence`, and results are only accepted with the current fence — so a late,
    stale worker can never overwrite newer results, and a job is never done twice."""

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(index=True)
    stage: str = ""                            # download | transcribe | select | render | import
    status: str = Field(default="queued", index=True)
    worker_id: str = ""
    fence: int = 0
    lease_until: Optional[dt.datetime] = None
    attempts: int = 0
    hold: bool = False                         # wait for a worker however long it takes (e.g. YouTube blocked the server)
    local_fallback_at: Optional[dt.datetime] = None  # no worker took it by then → the server does it itself
    progress: float = 0
    eta_sec: Optional[float] = None
    message: str = ""
    error: str = ""
    payload_json: str = "{}"
    created_at: dt.datetime = Field(default_factory=now)
    updated_at: dt.datetime = Field(default_factory=now)

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json or "{}")


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
SCHEMA_VERSION = 17  # bump when tables/columns change: an older database is copied to clipforge.db.bak-v<old> first


def _backup_before_migration(path) -> None:
    """Versioned, safe migrations: before an older database is upgraded, keep a copy next to it."""
    import shutil
    import sqlite3
    from contextlib import closing

    if not path.exists():
        return
    with closing(sqlite3.connect(path)) as conn:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver >= SCHEMA_VERSION:
        return
    bak = path.with_name(f"clipforge.db.bak-v{ver}")
    if not bak.exists():
        with closing(sqlite3.connect(path)) as a, closing(sqlite3.connect(bak)) as b:
            a.backup(b)
    # and a timestamped copy in workspace/backups (Publishing repair rule: back up before every migration)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = path.parent / "backups" / f"clipforge-{stamp}-v{ver}.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as a, closing(sqlite3.connect(dest)) as b:
        a.backup(b)
    from clipforge.utils.log import log

    log.info(f"Database backed up to {dest} before migrating v{ver} → v{SCHEMA_VERSION}.")


# one publication per (clip, platform, account); a platform media id belongs to one publication
UNIQUE_INDEXES = {
    # v17: identity = pub_key (clip render × platform × account × intent), so an explicit republish can coexist
    "ux_queue_pub_key": "queueitem (pub_key) WHERE pub_key != ''",
    "ux_queue_youtube_media": "queueitem (platform, youtube_video_id) WHERE youtube_video_id != ''",
    "ux_queue_instagram_media": "queueitem (platform, ig_media_id) WHERE ig_media_id != ''",
}


def _unique_indexes(conn) -> None:
    conn.exec_driver_sql("DROP INDEX IF EXISTS ux_queue_clip_platform_account")  # replaced by ux_queue_pub_key
    for name, spec in UNIQUE_INDEXES.items():
        try:
            conn.exec_driver_sql(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {spec}")
        except Exception as exc:  # existing duplicates: keep the data, say so (never drop history)
            from clipforge.utils.log import log

            log.warning(f"Couldn't create unique index {name} (duplicate rows already exist?): {exc}")


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

        _backup_before_migration(cfg.workspace / "clipforge.db")
        SQLModel.metadata.create_all(_engine)
        with _engine.begin() as conn:
            _migrate(conn)
            _unique_indexes(conn)
            conn.exec_driver_sql(f"PRAGMA user_version={SCHEMA_VERSION}")
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
