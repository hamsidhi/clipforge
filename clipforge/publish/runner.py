"""Background publishing work: `run_once()` is what the in-app scheduler and `clipforge scheduler --once` run.

API mode: upload every queued clip whose slot is within `upload_ahead_days` as PRIVATE + publishAt=slot, so
YouTube publishes it on time even if the laptop is off. Then check status (locked/rejected), refresh scheduled →
published, and sync stats when due. Assisted mode: only the read-only parts (status refresh + stats).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from clipforge.api import db
from clipforge.config import Config, load_config
from clipforge.publish import analytics, channels as chs, queue as q
from clipforge.publish import youtube as yt
from clipforge.publish.timez import as_utc, iso_utc, now_utc
from clipforge.utils.log import log

MAX_ATTEMPTS = 5
MIN_LEAD = dt.timedelta(minutes=15)  # YouTube needs publishAt in the future; leave processing time


@dataclass
class RunReport:
    uploaded: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    locked: list[int] = field(default_factory=list)
    missed: list[int] = field(default_factory=list)
    published: list[int] = field(default_factory=list)
    synced: list[str] = field(default_factory=list)
    skipped_channels: dict[str, str] = field(default_factory=dict)
    quota_exhausted: bool = False
    messages: list[str] = field(default_factory=list)


def upload_body(it: db.QueueItem, settings: dict, slot: dt.datetime) -> dict:
    lang = settings.get("language", "hi")
    return {
        "snippet": {"title": it.title[:100], "description": it.description[:5000], "tags": it.tags,
                    "categoryId": it.category_id or "27", "defaultLanguage": lang, "defaultAudioLanguage": lang},
        # private + publishAt: YouTube itself makes it public at the slot. Nothing goes public without a slot.
        "status": {"privacyStatus": "private", "publishAt": iso_utc(slot), "selfDeclaredMadeForKids": False,
                   "containsSyntheticMedia": False, "embeddable": True},
    }


def classify(video: dict, expected_publish: dt.datetime | None) -> tuple[str, str]:
    """Map a videos.list item to a queue status: (status, message)."""
    st = video.get("status", {})
    upload_status = st.get("uploadStatus", "")
    if upload_status in ("rejected", "failed"):
        reason = st.get("rejectionReason") or st.get("failureReason") or upload_status
        return "failed", f"YouTube {upload_status} the upload: {reason}"
    privacy = st.get("privacyStatus")
    if privacy == "public":
        return "published", ""
    if privacy == "private" and expected_publish is not None and not st.get("publishAt"):
        return "locked_private", ("YouTube kept this video private and dropped the schedule (API project lock). "
                                  "Publish it from YouTube Studio, or request the API audit.")
    return "scheduled", ""


def _upload_allowed(cfg: Config, rep: RunReport) -> int | None:
    """None = unlimited (quota permitting); 1 = test mode (lock active, not audited)."""
    p = cfg.publishing
    if p.unverified_lock and not p.api_audited:
        done = [it for it in q.items(statuses=("scheduled", "published", "locked_private")) if it.youtube_video_id]
        if done:
            rep.messages.append("API test upload already done — while the unverified-project lock applies, use assisted mode.")
            return 0
        return 1
    return None


LOCK_STALE = dt.timedelta(hours=2)


class _ProcessLock:
    """Only one process publishes at a time (the web app's worker and Task Scheduler's `scheduler --once` could
    otherwise upload the same clip twice). Atomic file creation; a lock older than 2 h is considered stale."""

    def __init__(self, cfg: Config) -> None:
        self.path = cfg.workspace / "publish.lock"
        self.held = False

    def __enter__(self) -> bool:
        import os

        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {now_utc().isoformat()}".encode())
                os.close(fd)
                self.held = True
                return True
            except FileExistsError:
                try:
                    age = now_utc() - dt.datetime.fromisoformat(self.path.read_text().split()[1])
                except (OSError, ValueError, IndexError):
                    age = LOCK_STALE
                if age < LOCK_STALE:
                    return False
                self.path.unlink(missing_ok=True)  # stale (crashed run): take over
        return False

    def __exit__(self, *exc) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)


def recover_stuck(now: dt.datetime) -> int:
    """Items left 'uploading' by a crash: if YouTube never returned an id, try again (queued)."""
    n = 0
    for it in q.items(statuses=("uploading",)):
        if it.youtube_video_id:
            it.status = "scheduled"
        else:
            it.status, it.error = "queued", "The previous upload was interrupted — retrying."
        q.save(it)
        n += 1
    return n


def run_once(cfg: Config | None = None, now: dt.datetime | None = None, progress=None) -> RunReport:
    cfg = cfg or load_config()
    now = as_utc(now or now_utc())
    with _ProcessLock(cfg) as got:
        if not got:
            rep = RunReport()
            rep.messages.append("Another ClipForge process is publishing right now — skipped this run.")
            return rep
        recover_stuck(now)  # safe: we hold the lock, so nobody else is mid-upload
        return _run_locked(cfg, now, progress)


def _run_locked(cfg: Config, now: dt.datetime, progress) -> RunReport:
    rep = RunReport()
    budget = _upload_allowed(cfg, rep) if cfg.publishing.mode == "api" else 0
    for ch in chs.all_channels():
        if ch.paused:
            rep.skipped_channels[ch.id] = "paused"
            continue
        if ch.auth_status == "needs_reauth":
            rep.skipped_channels[ch.id] = "needs re-login"
            continue
        try:
            client = yt.client_for(ch.id, cfg)
        except yt.NeedsReauth as exc:
            chs.mark_reauth(ch.id, str(exc))
            rep.skipped_channels[ch.id] = "needs re-login"
            continue
        except FileNotFoundError as exc:
            rep.skipped_channels[ch.id] = str(exc)
            continue
        try:
            if cfg.publishing.mode == "api" and budget != 0 and not rep.quota_exhausted:
                budget = _upload_due(ch, client, cfg, now, rep, budget, progress)
            _refresh_status(ch, client, cfg, now, rep)
            last = ch.last_sync_at
            if last is None or now - as_utc(last) >= dt.timedelta(hours=cfg.publishing.sync_every_hours):
                analytics.sync_channel(ch.id, client, cfg, now)
                rep.synced.append(ch.id)
        except yt.NeedsReauth as exc:
            chs.mark_reauth(ch.id, str(exc))
            rep.skipped_channels[ch.id] = "needs re-login"
    return rep


def _upload_due(ch: db.Channel, client, cfg: Config, now: dt.datetime, rep: RunReport, budget: int | None, progress) -> int | None:
    settings = chs.settings_of(ch, cfg)
    horizon = now + dt.timedelta(days=cfg.publishing.upload_ahead_days)
    for it in q.items(ch.id, ("queued",)):
        slot = as_utc(it.slot_utc) if it.slot_utc else None
        if slot is None:
            continue
        if slot < now + MIN_LEAD:
            it.status, it.error = "needs_manual", "The slot passed before the clip could be uploaded — pick a new time."
            q.save(it)
            rep.missed.append(it.id)
            continue
        if slot > horizon:
            break  # items are sorted by slot
        if it.next_attempt_at and as_utc(it.next_attempt_at) > now:
            continue
        if not ch.confirmed:
            it.error = "Confirm this channel (Channels page) before the first upload."
            q.save(it)
            continue
        if budget is not None and budget <= 0:
            break
        problems = q.problems(it)
        if problems:
            it.status, it.error = "needs_manual", " ".join(problems)
            q.save(it)
            continue
        try:
            path = q.clip_file(it, cfg)
            it.status, it.attempts = "uploading", it.attempts + 1
            q.save(it)
            vid = client.upload(path, upload_body(it, settings, slot),
                                on_progress=(lambda f, i=it: progress(i.id, f)) if progress else None)
            it.youtube_video_id, it.status, it.error, it.next_attempt_at = vid, "scheduled", "", None
            q.save(it)
            rep.uploaded.append(it.id)
            if budget is not None:
                budget -= 1
            log.info(f"Uploaded queue item {it.id} → {vid} (publishes {slot.isoformat()})")
            _check_lock(it, client, slot, rep)
        except yt.QuotaExceeded as exc:
            it.status, it.error = "queued", str(exc)
            q.save(it)
            rep.quota_exhausted = True
            q.log_quota_rollover(cfg)
            break
        except (yt.NeedsReauth, KeyboardInterrupt):
            it.status = "queued"
            q.save(it)
            raise
        except q.QueueError as exc:
            it.status, it.error = "needs_manual", str(exc)
            q.save(it)
        except Exception as exc:  # network / API error: retry with backoff
            msg = yt.describe_http_error(exc)
            if "quotaExceeded" in msg or "uploadLimitExceeded" in msg:
                it.status, it.error = "queued", "YouTube says the daily upload limit is reached — retrying tomorrow."
                it.next_attempt_at = now + dt.timedelta(hours=12)
                q.save(it)
                rep.quota_exhausted = True
                break
            if it.attempts >= MAX_ATTEMPTS:
                it.status, it.error = "failed", f"Upload failed {it.attempts}× — {msg}"
                rep.failed.append(it.id)
            else:
                it.status, it.error = "queued", f"Attempt {it.attempts} failed, retrying: {msg}"
                it.next_attempt_at = now + dt.timedelta(minutes=min(360, 5 * 2 ** it.attempts))
            q.save(it)
    return budget


def _check_lock(it: db.QueueItem, client, slot: dt.datetime, rep: RunReport) -> None:
    try:
        vids = client.videos([it.youtube_video_id])
    except Exception as exc:
        log.warning(f"Couldn't verify status of {it.youtube_video_id}: {exc}")
        return
    if not vids:
        return
    status, msg = classify(vids[0], slot)
    if status in ("locked_private", "failed"):
        it.status, it.error = status, msg
        q.save(it)
        (rep.locked if status == "locked_private" else rep.failed).append(it.id)


def _refresh_status(ch: db.Channel, client, cfg: Config, now: dt.datetime, rep: RunReport) -> None:
    """scheduled → published once YouTube made it public (also catches later rejections)."""
    due = [it for it in q.items(ch.id, ("scheduled",)) if it.youtube_video_id and it.slot_utc
           and as_utc(it.slot_utc) <= now]
    if not due:
        return
    by_id = {v["id"]: v for v in client.videos([it.youtube_video_id for it in due])}
    for it in due:
        v = by_id.get(it.youtube_video_id)
        if not v:
            it.status, it.error = "failed", "Video not found on YouTube any more (deleted?)."
        else:
            status, msg = classify(v, as_utc(it.slot_utc))
            if status == "scheduled":
                continue
            it.status, it.error = status, msg
            if status == "published":
                it.published_at = it.slot_utc
                rep.published.append(it.id)
        q.save(it)
