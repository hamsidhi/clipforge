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
    rescheduled: list[int] = field(default_factory=list)
    published: list[int] = field(default_factory=list)
    synced: list[str] = field(default_factory=list)
    skipped_channels: dict[str, str] = field(default_factory=dict)
    quota_exhausted: bool = False
    messages: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)  # things you're told on Telegram (moved slots, limits)


def upload_body(it: db.QueueItem, settings: dict, slot: dt.datetime, synthetic: bool = False) -> dict:
    """privacy 'scheduled' (default): private + publishAt, so YouTube itself makes it public at the slot.
    'public' is only uploaded at the slot (laptop must be on); 'private'/'unlisted' never go public by themselves."""
    lang = settings.get("language", "hi")
    privacy = it.privacy or "scheduled"
    status: dict = {"privacyStatus": "private" if privacy == "scheduled" else privacy,
                    "selfDeclaredMadeForKids": False, "containsSyntheticMedia": bool(synthetic), "embeddable": True}
    if privacy == "scheduled":
        status["publishAt"] = iso_utc(slot)
    return {
        "snippet": {"title": it.title[:100], "description": it.description[:5000], "tags": it.tags,
                    "categoryId": it.category_id or "27", "defaultLanguage": lang, "defaultAudioLanguage": lang},
        "status": status,
    }


def yt_state(video: dict, now: dt.datetime | None = None) -> dict:
    st, pd = video.get("status", {}), video.get("processingDetails", {})
    return {k: v for k, v in {
        "uploadStatus": st.get("uploadStatus"), "privacyStatus": st.get("privacyStatus"), "publishAt": st.get("publishAt"),
        "processingStatus": pd.get("processingStatus"), "failureReason": st.get("failureReason"),
        "rejectionReason": st.get("rejectionReason"), "checked_at": iso_utc(now or now_utc()),
    }.items() if v}


def classify(video: dict, expected_publish: dt.datetime | None, privacy: str = "scheduled") -> tuple[str, str]:
    """Map a videos.list item to a queue status: (status, message)."""
    status, _reason, msg = classify_ex(video, expected_publish, privacy)
    return status, msg


def classify_ex(video: dict, expected_publish: dt.datetime | None, privacy: str = "scheduled") -> tuple[str, str, str]:
    """(status, reason code, message)."""
    st = video.get("status", {})
    upload_status = st.get("uploadStatus", "")
    if upload_status in ("rejected", "failed"):
        reason = st.get("rejectionReason") or st.get("failureReason") or upload_status
        return "failed", "rejected", f"YouTube {upload_status} the upload: {reason}"
    vis = st.get("privacyStatus")
    if vis == "public":
        return "published", "", ""
    if vis == "private" and expected_publish is not None and not st.get("publishAt"):
        return "locked_private", "locked_private", (
            "YouTube kept this video private and dropped the schedule (API project lock). Publish it from YouTube "
            "Studio, or request the API audit.")
    if upload_status == "uploaded":  # received, still processing
        pd = video.get("processingDetails", {})
        return "processing", "", f"YouTube is processing it ({pd.get('processingStatus') or 'processing'})."
    if privacy in ("private", "unlisted") and vis == privacy:
        return "uploaded", "", ""
    return "scheduled", "", ""


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


LOCK_STALE = dt.timedelta(hours=6)  # the lock is refreshed after every upload; older = the holder died


def pid_alive(pid: int) -> bool:
    """Is a process with this pid running? (Never uses os.kill(pid, 0): on Windows that TERMINATES the process.)"""
    import os
    import sys

    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class _ProcessLock:
    """Only one process publishes at a time (the web app's worker and Task Scheduler's `scheduler --once` could
    otherwise upload the same clip twice). The lock file holds pid + token + heartbeat; it is taken over only when
    its process is dead or its heartbeat is older than LOCK_STALE, and only the owner (same token) removes it."""

    def __init__(self, cfg: Config) -> None:
        import uuid

        self.path = cfg.workspace / "publish.lock"
        self.token = uuid.uuid4().hex
        self.held = False

    def _write(self, fd: int | None = None) -> None:
        import os

        data = f"{os.getpid()} {self.token} {now_utc().isoformat()}".encode()
        if fd is None:
            self.path.write_bytes(data)
        else:
            os.write(fd, data)
            os.close(fd)

    def _read(self) -> tuple[int, str, dt.datetime] | None:
        try:
            pid, token, ts = self.path.read_text().split()
            return int(pid), token, dt.datetime.fromisoformat(ts)
        except (OSError, ValueError):
            return None

    def __enter__(self) -> bool:
        import os

        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                self._write(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                self.held = True
                return True
            except FileExistsError:
                info = self._read()
                if info and pid_alive(info[0]) and now_utc() - as_utc(info[2]) < LOCK_STALE:
                    return False
                self.path.unlink(missing_ok=True)  # holder died (or the file is garbage): take over
        return False

    def heartbeat(self) -> None:
        info = self._read()
        if self.held and info and info[1] == self.token:
            self._write()

    def __exit__(self, *exc) -> None:
        info = self._read()
        if self.held and info and info[1] == self.token:
            self.path.unlink(missing_ok=True)


_current_lock: _ProcessLock | None = None


def recover_stuck(now: dt.datetime) -> int:
    """Items left 'uploading' by a crash. We can't know whether YouTube received the file (the process may have died
    after the last chunk but before saving the id), so never re-upload blindly: ask the user to check Studio + Sync."""
    n = 0
    for it in q.items(statuses=("uploading",)):
        if it.platform != "youtube":
            continue  # Instagram containers are re-checked by the Instagram runner
        q.release(it)
        if it.youtube_video_id:
            it.status = "processing"  # the next status refresh reads the real state
        else:
            it.status, it.reason, it.error = "needs_manual", "interrupted", (
                "The upload was interrupted. Check YouTube Studio: if the video is there, use Sync with YouTube (or Link); "
                "if not, Reschedule.")
        q.save(it)
        n += 1
    return n


def run_once(cfg: Config | None = None, now: dt.datetime | None = None, progress=None, source: str = "app") -> RunReport:
    global _current_lock
    cfg = cfg or load_config()
    now = as_utc(now or now_utc())
    from clipforge.server.dispatch import publishing_allowed

    allowed, why = publishing_allowed()  # exactly one machine publishes (Phase 15b): never a worker, never a fenced server
    if not allowed:
        rep = RunReport()
        rep.messages.append(f"Not publishing here: {why}.")
        return rep
    lock = _ProcessLock(cfg)
    with lock as got:
        if not got:
            rep = RunReport()
            rep.messages.append("Another ClipForge process is publishing right now — skipped this run.")
            return rep
        recover_stuck(now)  # safe: we hold the lock, so nobody else is mid-upload
        backfill_reasons(now)
        _current_lock = lock
        try:
            rep = _run_locked(cfg, now, progress)
            _v2_housekeeping(cfg, now, rep, source)
        finally:
            _current_lock = None
        _write_state(cfg, now, rep, source)
        try:
            from clipforge.notify import telegram

            telegram.on_run_report(rep)
        except Exception as exc:
            log.debug(f"telegram hook failed: {exc}")
        for iid in rep.published:
            _audit_publish(iid)
        return rep


_keys_backfilled: set[str] = set()


def _v2_housekeeping(cfg: Config, now: dt.datetime, rep: RunReport, source: str) -> None:
    """Publishing v2, cheap and never fatal: pub_key backfill (once per process), weekly slot adaptation, and the
    17:45 IST warning when tonight's Instagram posts can't go out (nothing but a Task Scheduler run is alive)."""
    from clipforge.publish import dedup, modes, plan

    try:
        if str(cfg.workspace) not in _keys_backfilled:
            res = dedup.backfill(cfg)
            _keys_backfilled.add(str(cfg.workspace))
            if res["skipped"]:
                rep.notices.append(f"{res['skipped']} duplicate queue item(s) marked 'skipped (duplicate)' — kept, never posted.")
        if cfg.schedule.enabled:
            for ch in chs.all_channels():
                plan.weekly_apply(ch, cfg, now)
        _evening_watch(cfg, now, source, rep)
    except Exception as exc:
        log.warning(f"[publish] housekeeping: {exc}")
    try:  # weekly reach report (read-only Google calls; never blocks publishing)
        from clipforge.publish import reach

        import os

        if os.environ.get("CLIPFORGE_TESTING") != "1" and reach.weekly_due(cfg, now):
            reach.run(cfg, now)
    except Exception as exc:
        log.warning(f"[publish] weekly reach report failed: {exc}")


def _evening_watch(cfg: Config, now: dt.datetime, source: str, rep: RunReport) -> None:
    from clipforge.publish import modes
    from clipforge.publish.timez import utc_to_local

    local = utc_to_local(now, "Asia/Kolkata")
    if not (dt.time(17, 45) <= local.time() < dt.time(18, 15)):
        return
    tonight = [it for ch in chs.all_channels() if ch.platform == "instagram" and not ch.paused
               and modes.resolve(ch, cfg, check=False).auto
               for it in q.items(ch.id, ("queued", "processing", "ready"))
               if it.slot_utc and now <= as_utc(it.slot_utc) <= now + dt.timedelta(hours=8)]
    state = read_state(cfg)
    app_alive = any(src in ("app", "daemon") and now - as_utc(dt.datetime.fromisoformat(t.replace("Z", "+00:00")))
                    < dt.timedelta(minutes=5) for src, t in (state.get("runs") or {}).items())
    if tonight and source == "task" and not app_alive:
        rep.notices.append(f"Tonight {len(tonight)} Instagram post(s) are due until 01:30 IST, but the ClipForge app/daemon "
                           "isn't running — start `clipforge ui` (or `clipforge scheduler --daemon`) and keep the laptop on.")


def _audit_publish(item_id: int) -> None:
    it = q.get(item_id)
    if not it:
        return
    try:
        from clipforge.api.auth import audit

        audit("publish", f"{it.platform}: {it.title} → {it.permalink or it.youtube_video_id}")
    except Exception:
        pass


STATE_FILE = "scheduler_state.json"


def _write_state(cfg: Config, now: dt.datetime, rep: RunReport, source: str) -> None:
    """Last run info for the Health panel (any process: web app, daemon, Task Scheduler)."""
    import json
    from dataclasses import asdict

    path = cfg.workspace / STATE_FILE
    data = read_state(cfg)
    summary = {k: (len(v) if isinstance(v, list) else v) for k, v in asdict(rep).items() if v}
    data.update({"last_run_at": iso_utc(now), "last_source": source, "last_result": summary})
    data.setdefault("runs", {})[source] = iso_utc(now)
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning(f"Couldn't write {path}: {exc}")


def read_state(cfg: Config | None = None) -> dict:
    import json

    path = (cfg or load_config()).workspace / STATE_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}


def _is_auth_error(exc: Exception) -> bool:
    from google.auth.exceptions import RefreshError

    from clipforge.publish import yt_errors as ye

    if isinstance(exc, (yt.NeedsReauth, RefreshError)):
        return True
    return ye.classify(exc).type == ye.AUTH  # the one error table (yt_errors.REASON_TYPE)


def backfill_reasons(now: dt.datetime) -> int:
    """Rows written by older versions as a bare 'needs_manual' (no reason): give them one, so the UI shows a fix."""
    n = 0
    for it in q.items(statuses=("needs_manual",)):
        if it.reason or it.youtube_video_id or it.ig_media_id:
            continue
        if it.platform == "instagram":
            it.reason = "ig_assisted"
        elif it.batch:
            it.reason = "assisted_unlinked"
        else:
            it.status, it.reason = "missed", "slot_passed"
        it.error = it.error or "The publish time passed before it was uploaded."
        q.save(it)
        n += 1
    return n


def _instagram_pass(cfg: Config, now: dt.datetime, rep: RunReport, started: float) -> None:
    """Instagram must publish AT the slot, so it never waits behind long YouTube uploads: it runs first, again
    between uploads and at the end, each time with the clock advanced by the real time spent so far."""
    import time

    t = now + dt.timedelta(seconds=time.monotonic() - started)
    for ch in chs.all_channels():
        if ch.platform == "instagram":
            _run_instagram(ch, cfg, t, rep)


_quota_block: dict[str, dt.datetime] = {}  # workspace → when uploads may try again (next Pacific midnight)


def _next_pacific_midnight(now: dt.datetime) -> dt.datetime:
    from clipforge.publish.timez import PACIFIC

    local = now.astimezone(PACIFIC)
    nxt = (local + dt.timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return nxt.astimezone(dt.timezone.utc)


def _run_locked(cfg: Config, now: dt.datetime, progress) -> RunReport:
    import time

    rep = RunReport()
    started = time.monotonic()
    from clipforge.publish import modes

    budget = _upload_allowed(cfg, rep)
    any_auto = False
    blocked = _quota_block.get(str(cfg.workspace))
    if blocked and now < blocked:
        rep.quota_exhausted = True  # YouTube said "quota exceeded" earlier today: don't burn attempts every minute
    _instagram_pass(cfg, now, rep, started)
    for ch in chs.all_channels():
        if ch.platform == "instagram":
            continue
        if ch.paused:
            rep.skipped_channels[ch.id] = "paused"
            continue
        if ch.auth_status == "needs_reauth":
            rep.skipped_channels[ch.id] = "needs re-login"
            _flag_reauth_items(ch.id, now)
            continue
        mode = modes.resolve(ch, cfg)
        if mode.auto and mode.problem:  # auto chosen but impossible: say so on the clips, never fall back silently
            rep.skipped_channels[ch.id] = f"auto mode blocked: {mode.problem}"
            _flag_auto_unavailable(ch, cfg, now, mode.problem, rep)
            continue
        if mode.auto:
            any_auto = True
            _requeue_auto_unavailable(ch.id)
        try:
            client = yt.client_for(ch.id, cfg)
            _handle_late(ch, client, cfg, now, rep, assisted=not mode.auto)  # never a bare "Needs you"
            if mode.auto and budget != 0 and not rep.quota_exhausted:
                budget = _upload_due(ch, client, cfg, now, rep, budget, progress,
                                     between=lambda: _instagram_pass(cfg, now, rep, started))
            since = now - _last_refresh.get(f"{cfg.workspace}|{ch.id}", _EPOCH)
            if since >= REFRESH_EVERY or since < dt.timedelta(0) or rep.uploaded:
                _refresh_status(ch, client, cfg, now, rep)
                _last_refresh[f"{cfg.workspace}|{ch.id}"] = now
            last = ch.last_sync_at
            if last is None or now - as_utc(last) >= dt.timedelta(hours=cfg.publishing.sync_every_hours):
                analytics.sync_channel(ch.id, client, cfg, now)
                rep.synced.append(ch.id)
        except FileNotFoundError as exc:
            rep.skipped_channels[ch.id] = str(exc)
        except Exception as exc:  # one channel's problem must never stop the others
            if _is_auth_error(exc):
                chs.mark_reauth(ch.id, str(exc) if isinstance(exc, yt.NeedsReauth) else
                                f"Google refused the saved login or permissions ({yt.describe_http_error(exc)}). Reconnect.")
                rep.skipped_channels[ch.id] = "needs re-login"
                _flag_reauth_items(ch.id, now)
            else:
                log.error(f"[publish] channel {ch.id}: {yt.describe_http_error(exc)}")
                rep.skipped_channels[ch.id] = f"error: {yt.describe_http_error(exc)[:200]}"
    if rep.quota_exhausted and any_auto:
        _quota_block.setdefault(str(cfg.workspace), _next_pacific_midnight(now))
        if _quota_block[str(cfg.workspace)] <= now:
            _quota_block[str(cfg.workspace)] = _next_pacific_midnight(now)
        _mark_quota_waiting(now)
    elif not rep.quota_exhausted:
        _quota_block.pop(str(cfg.workspace), None)
    _instagram_pass(cfg, now, rep, started)
    return rep


def _flag_auto_unavailable(ch: db.Channel, cfg: Config, now: dt.datetime, problem: str, rep: RunReport) -> None:
    """Auto mode was chosen for this account but can't upload: clips that are due to upload become FAILED with
    reason auto_unavailable (Reconnect / Switch to assisted). They go back to the queue by themselves once it works."""
    days = cfg.publishing.upload_ahead_days
    horizon = now + dt.timedelta(days=days if days > 0 else 3650)
    for it in q.items(ch.id, ("queued",)):
        if it.youtube_video_id or it.batch or not it.slot_utc or as_utc(it.slot_utc) > horizon:
            continue
        it.status, it.reason, it.error = "failed", "auto_unavailable", problem
        q.save(it)
        rep.failed.append(it.id)  # type: ignore[arg-type]


def _requeue_auto_unavailable(channel_id: str) -> None:
    for it in q.items(channel_id, ("failed",)):
        if it.reason == "auto_unavailable" and not it.youtube_video_id and not it.ig_media_id:
            it.status, it.reason, it.error = "queued", "", ""
            q.save(it)


def _mark_quota_waiting(now: dt.datetime) -> None:
    """Every YouTube clip still waiting to upload says why (all channels share one upload quota)."""
    for it in q.items(statuses=("queued",)):
        if it.platform == "youtube" and not it.youtube_video_id and not it.batch and it.reason in ("", "slot_too_close"):
            it.reason, it.error = "quota", "Upload quota used up today — continues tomorrow automatically (Pacific time)."
            q.save(it)


def _run_instagram(ch: db.Channel, cfg: Config, now: dt.datetime, rep: RunReport) -> None:
    try:
        from clipforge.publish import ig_runner

        ig_runner.process_account(ch, cfg, now, rep)
    except Exception as exc:  # never crash the scheduler
        log.error(f"[publish] instagram {ch.id}: {exc}")
        rep.skipped_channels[ch.id] = f"error: {str(exc)[:200]}"


def _flag_reauth_items(channel_id: str, now: dt.datetime) -> None:
    """Clips due soon on an account whose login expired: say so on the clip (the fix is Reconnect)."""
    for it in q.items(channel_id, ("queued",)):
        if it.slot_utc and as_utc(it.slot_utc) < now + dt.timedelta(days=2) and it.reason != "login_expired":
            it.reason, it.error = "login_expired", "The account's login expired — click Reconnect on the Accounts page."
            q.save(it)


SYNC_THROTTLE = dt.timedelta(minutes=30)
REFRESH_EVERY = dt.timedelta(minutes=10)  # the ticker runs every minute; YouTube statuses don't need that
_last_refresh: dict[str, dt.datetime] = {}
_last_auto_sync: dict[str, dt.datetime] = {}
_EPOCH = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)


def _late_cutoff(it: db.QueueItem, now: dt.datetime) -> bool:
    """Is it too late to upload this clip for its slot?"""
    slot = as_utc(it.slot_utc)  # type: ignore[arg-type]
    if it.privacy == "public":  # uploaded at the slot itself; give a sleeping laptop an hour
        return now > slot + dt.timedelta(hours=1)
    if it.privacy in ("private", "unlisted"):
        return False  # never published, so never late
    return slot < now + MIN_LEAD


def _local(t: dt.datetime, ch: db.Channel, cfg: Config) -> str:
    from clipforge.publish.timez import utc_to_local

    return utc_to_local(t, chs.settings_of(ch, cfg)["timezone"]).strftime("%a %d %b %H:%M")


def _handle_late(ch: db.Channel, client, cfg: Config, now: dt.datetime, rep: RunReport,
                 assisted: bool | None = None) -> None:
    from clipforge.publish import modes, reconcile

    def waiting() -> list[db.QueueItem]:
        return [it for it in q.items(ch.id, ("queued",)) if it.slot_utc and not it.youtube_video_id]

    queued = waiting()
    if assisted is None:
        assisted = not modes.resolve(ch, cfg, check=False).auto
    # 1. Studio uploads first: link them (read-only), so nothing that is really scheduled gets flagged
    due_batched = [it for it in queued if it.batch and as_utc(it.slot_utc) < now + MIN_LEAD]  # type: ignore[arg-type]
    if due_batched and now - _last_auto_sync.get(f"{cfg.workspace}|{ch.id}", _EPOCH) >= SYNC_THROTTLE:
        _last_auto_sync[f"{cfg.workspace}|{ch.id}"] = now
        try:
            res = reconcile.sync(ch.id, cfg, client)
            rep.messages += [f"Linked Studio upload “{m['title']}”" for m in res["matched"]]
        except Exception as exc:
            if _is_auth_error(exc):
                raise
            log.warning(f"[publish] auto-sync {ch.id} failed: {yt.describe_http_error(exc)}")
        queued = waiting()
    for it in queued:
        slot = as_utc(it.slot_utc)  # type: ignore[arg-type]
        if assisted:
            if slot > now:
                continue
            if it.batch:
                it.status, it.reason = "needs_manual", "assisted_unlinked"
                it.error = (f"Assisted mode: this clip was in a Studio batch for {_local(slot, ch, cfg)}. If you uploaded it "
                            "in YouTube Studio, it isn't linked yet → Sync with YouTube, or Link it to the video. "
                            "If you didn't upload it, Reschedule.")
            else:
                it.status, it.reason = "missed", "assisted_no_batch"
                it.error = ("Assisted mode: ClipForge doesn't upload by itself, and this clip wasn't in a Studio batch, so "
                            "nobody uploaded it. Reschedule it (then Prepare batch), or switch to API mode.")
            q.save(it)
            rep.missed.append(it.id)  # type: ignore[arg-type]
            continue
        # ---- api mode
        if it.batch:
            if slot < now + MIN_LEAD:
                it.status, it.reason = "needs_manual", "batched_in_api"
                it.error = ("This clip is in a Studio batch, so API mode didn't upload it (no double uploads). If you "
                            "uploaded it in Studio: Sync or Link. Otherwise: Upload with API instead (removes it from the batch).")
                q.save(it)
                rep.missed.append(it.id)  # type: ignore[arg-type]
            elif it.reason != "batched_in_api":
                it.reason = "batched_in_api"
                it.error = "In a Studio batch — API mode won't upload it unless you remove it from the batch."
                q.save(it)
            continue
        if not _late_cutoff(it, now):
            continue
        policy = cfg.publishing.late_policy
        code = "slot_passed" if slot <= now else "slot_too_close"
        if policy == "publish_now":
            it.privacy, it.slot_utc, it.reason = "public", now, ""
            it.error = f"Was due {_local(slot, ch, cfg)} — publishing now (late_policy = publish_now)."
            q.save(it)
            rep.messages.append(f"“{it.title}” missed its slot — publishing now.")
            continue
        if policy == "next_slot":
            try:
                moved = q.reschedule_next(it.id, now)
                new = as_utc(moved.slot_utc)  # type: ignore[arg-type]
                moved.error = f"Moved automatically from {_local(slot, ch, cfg)} to {_local(new, ch, cfg)} (the slot was too close or had passed)."
                q.save(moved)
                rep.rescheduled.append(it.id)  # type: ignore[arg-type]
                rep.messages.append(f"“{it.title}” moved to {_local(new, ch, cfg)}.")
                continue
            except q.QueueError as exc:
                log.warning(f"[publish] couldn't reschedule {it.id}: {exc}")
        it.status, it.reason = "missed", code
        it.error = (f"The slot ({_local(slot, ch, cfg)}) passed before ClipForge could upload it — usually the laptop was off "
                    "or asleep. Reschedule to the next free slot, or Publish now.")
        q.save(it)
        rep.missed.append(it.id)  # type: ignore[arg-type]


def _upload_due(ch: db.Channel, client, cfg: Config, now: dt.datetime, rep: RunReport, budget: int | None, progress,
                between=None) -> int | None:
    """Publishing v2 order per job: duplicate → (account health: checked by the caller) → limits/cooldown → slot →
    file → metadata (never regenerated here) → upload → confirm (videos.list) → status. One channel's block never
    touches other channels or Instagram."""
    from clipforge.publish import dedup, limits
    from clipforge.publish import yt_errors as ye

    days = cfg.publishing.upload_ahead_days
    horizon = now + dt.timedelta(days=days) if days > 0 else dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    _release_waiting(ch, cfg, now, rep)

    def due(it: db.QueueItem) -> bool:
        if not it.slot_utc or it.youtube_video_id or it.batch:
            return False
        slot = as_utc(it.slot_utc)
        if it.privacy == "public":
            return slot <= now + dt.timedelta(minutes=1) and not _late_cutoff(it, now)
        if it.privacy in ("private", "unlisted"):
            return True
        return now + MIN_LEAD <= slot <= horizon

    candidates = [it.id for it in q.items(ch.id, ("queued",)) if due(it)]
    done = 0
    for item_id in candidates:
        if budget is not None and budget <= 0:
            break
        # re-read the channel and the item right before uploading: the user may have paused, unscheduled,
        # moved or deleted things while earlier uploads (minutes each) were running
        live_ch = chs.get(ch.id)
        if not live_ch or live_ch.paused or live_ch.auth_status != "ok":
            break
        it = q.get(item_id)
        if not it or it.status != "queued" or not due(it):
            continue
        if it.next_attempt_at and as_utc(it.next_attempt_at) > now:
            continue
        twin = dedup.duplicate_of(it)  # 1. duplicate (queue + history)
        if twin is not None:
            dedup.mark_skipped(it, twin)
            rep.messages.append(f"Skipped a duplicate of “{twin.title}”.")
            continue
        verdict = limits.check(ch.id, cfg, now, done)  # 2. limits / cooldown
        if not verdict.ok:
            _note_wait(ch.id, verdict, now, candidates)
            if verdict.code == "project_quota":
                rep.quota_exhausted = True
            break
        slot = as_utc(it.slot_utc)  # type: ignore[arg-type]
        if not live_ch.confirmed:
            it.reason, it.error = "channel_unconfirmed", "Confirm this channel (Accounts page) before the first upload."
            q.save(it)
            continue
        try:  # 3. file
            path = q.clip_file(it, cfg)
        except q.QueueError as exc:
            it.status, it.reason, it.error = "needs_manual", "file_missing", str(exc)
            q.save(it)
            continue
        problems = q.problems(it)  # 4. metadata (stored once; a retry never calls the LLM)
        if problems:
            it.status, it.reason, it.error = "needs_manual", "metadata", " ".join(problems)
            q.save(it)
            continue
        if not q.claim_for_upload(it.id, it.slot_utc):  # type: ignore[arg-type]
            continue  # changed under us between the read and the claim
        it = q.get(item_id)
        settings = chs.settings_of(live_ch, cfg)
        body = upload_body(it, settings, slot, cfg.publishing.contains_synthetic_media)
        publish_at = slot if "publishAt" in body["status"] else None
        done += 1
        try:
            vid = client.upload(path, body, on_progress=(lambda f, i=it: progress(i.id, f)) if progress else None)
        except KeyboardInterrupt:
            q.release(it)
            it.status = "queued"
            q.save(it)
            raise
        except Exception as exc:
            q.release(it)
            e = ye.classify(exc)
            limits.record("youtube", ch.id, queue_id=it.id, pub_key=it.pub_key, result="refused" if e.type in (
                ye.CHANNEL_LIMIT, ye.PROJECT_QUOTA) else "error", error_type=e.type, error_reason=e.text(),
                privacy=body["status"]["privacyStatus"], publish_at=publish_at, at=now)
            it.attempts = max(0, it.attempts - 1) if e.type != ye.TEMPORARY and e.type != ye.PERMANENT else it.attempts
            if e.type == ye.AUTH:  # the caller marks the channel REQUIRES_REAUTHENTICATION
                it.status, it.reason = "queued", "login_expired"
                it.error = "Google refused the saved login: Reconnect the channel on the Accounts page."
                q.save(it)
                raise yt.NeedsReauth(e.text()) from exc
            if e.type == ye.PROJECT_QUOTA:
                it.status, it.reason, it.next_attempt_at = "queued", "quota", None
                it.error = f"YouTube: the API project's daily quota is used up ({e.text()}). Continues after midnight Pacific."
                q.save(it)
                rep.quota_exhausted = True
                q.log_quota_rollover(cfg)
                break
            if e.type == ye.CHANNEL_LIMIT:
                it.status = "queued"
                q.save(it)
                until = limits.start_cooldown(ch.id, now, cfg, e.text())
                _wait_for_capacity(ch, cfg, now, until, e.text(), rep)
                break
            if e.type == ye.PERMANENT or it.attempts >= MAX_ATTEMPTS:
                it.status, it.reason = "failed", "upload_failed"
                it.error = (f"YouTube refused the upload: {e.text()}" if e.type == ye.PERMANENT
                            else f"Upload failed {it.attempts}×: {e.text()}")
                rep.failed.append(it.id)
            else:  # temporary: exponential backoff
                it.status, it.reason = "queued", "retrying"
                it.error = f"Attempt {it.attempts} failed: {e.text()}"
                it.next_attempt_at = now + dt.timedelta(minutes=min(360, 5 * 2 ** it.attempts))
            q.save(it)
            continue
        # real id from YouTube; the state comes from videos.list right below (_verify), never assumed
        limits.record("youtube", ch.id, queue_id=it.id, pub_key=it.pub_key, video_id=vid, result="ok",
                      privacy=body["status"]["privacyStatus"], publish_at=publish_at, at=now)
        if verdict.probe:
            limits.probe_succeeded(ch.id)
            rep.messages.append(f"{ch.title}: YouTube accepts uploads again — resuming.")
        it.youtube_video_id, it.status, it.reason, it.error, it.next_attempt_at = vid, "processing", "", "", None
        q.release(it)
        q.save(it)
        rep.uploaded.append(it.id)
        if budget is not None:
            budget -= 1
        log.info(f"Uploaded queue item {it.id} → {vid} ({it.privacy}, slot {slot.isoformat()})")
        _verify(it, client, slot, rep, now)
        if _current_lock is not None:
            _current_lock.heartbeat()
        if between is not None:
            between()  # Instagram posts due during a long upload batch go out on time
        if verdict.probe:
            break  # the probe worked; the batch continues next run (min gap)
    if rep.quota_exhausted:  # say so on every clip still waiting (they continue automatically tomorrow)
        for item_id in candidates:
            it = q.get(item_id)
            if it and it.status == "queued" and not it.youtube_video_id and it.reason != "quota":
                it.reason, it.error = "quota", "Upload quota used up today — continues tomorrow automatically (Pacific time)."
                q.save(it)
    return budget


def _note_wait(channel_id: str, verdict, now: dt.datetime, candidates: list[int]) -> None:
    """A limit (not an error) holds the uploads: the waiting clips say why. Gap/batch/busy are silent (next minute)."""
    if verdict.code not in ("safety_limit", "cooldown"):
        return
    from clipforge.publish.timez import utc_to_local

    if verdict.code == "cooldown":  # clips scheduled after the limit was hit wait like the others
        for item_id in candidates:
            it = q.get(item_id)
            if it and it.status == "queued" and not it.youtube_video_id:
                it.status, it.reason, it.next_attempt_at = "waiting_capacity", "channel_limit", verdict.until
                q.save(it)
        return

    when = utc_to_local(verdict.until, "Asia/Kolkata").strftime("%a %d %b %H:%M IST") if verdict.until else "later"
    for item_id in candidates:
        it = q.get(item_id)
        if it and it.status == "queued" and not it.youtube_video_id and it.reason in ("", "safety_limit", "upload_limit"):
            new = (f"{verdict.why} Next upload possible around {when}.")
            if it.reason != "safety_limit" or it.error != new:
                it.reason, it.error = "safety_limit", new
                q.save(it)


def _wait_for_capacity(ch: db.Channel, cfg: Config, now: dt.datetime, until: dt.datetime, why: str, rep: RunReport) -> None:
    """uploadLimitExceeded: every YouTube clip of this channel still to upload → WAITING_FOR_CHANNEL_CAPACITY until
    the cooldown ends (slots kept; a slot that passes moves to the next free slot then, and you're told)."""
    n = 0
    for it in q.items(ch.id, ("queued",)):
        if it.platform != "youtube" or it.youtube_video_id or it.batch or not it.slot_utc:
            continue
        it.status, it.reason, it.next_attempt_at = "waiting_capacity", "channel_limit", until
        it.error = f"({why})"
        q.save(it)
        n += 1
    rep.messages.append(f"{ch.title}: YouTube's upload limit reached — {n} clip(s) wait until "
                        f"{_local(until, ch, cfg)}, then ONE test upload decides whether to continue.")
    rep.notices.append(rep.messages[-1])


def _release_waiting(ch: db.Channel, cfg: Config, now: dt.datetime, rep: RunReport) -> None:
    """Cooldown over → waiting clips return to the queue (the controller allows exactly one probe upload first).
    Clips whose slot is too close/passed by then move to the next free slot (and you're told)."""
    from clipforge.publish import limits

    cd = limits.cooldown_until(ch.id)
    if cd and now < cd and not limits._override(cfg, now):
        return
    for it in q.items(ch.id, ("waiting_capacity",)):
        it.status, it.reason, it.error, it.next_attempt_at = "queued", "", "", None
        q.save(it)
        if it.slot_utc and _late_cutoff(it, now) and (it.privacy or "scheduled") == "scheduled":
            old = as_utc(it.slot_utc)
            try:
                moved = q.reschedule_next(it.id, now)
                moved.error = (f"Moved from {_local(old, ch, cfg)} to {_local(as_utc(moved.slot_utc), ch, cfg)}: "  # type: ignore[arg-type]
                               "YouTube's upload limit held it past its slot.")
                q.save(moved)
                rep.rescheduled.append(it.id)  # type: ignore[arg-type]
                rep.notices.append(f"“{it.title}” moved to {_local(as_utc(moved.slot_utc), ch, cfg)} (upload limit).")  # type: ignore[arg-type]
            except q.QueueError as exc:
                log.warning(f"[publish] couldn't move {it.id}: {exc}")


def upload_error_kind(exc: Exception, msg: str = "") -> str:
    """Back-compat wrapper over yt_errors.classify: quota | upload_limit | permanent | transient | auth."""
    from clipforge.publish import yt_errors as ye

    t = ye.classify(exc).type
    return {ye.PROJECT_QUOTA: "quota", ye.CHANNEL_LIMIT: "upload_limit", ye.TEMPORARY: "transient"}.get(t, t)


def _verify(it: db.QueueItem, client, slot: dt.datetime, rep: RunReport, now: dt.datetime) -> None:
    """Right after an upload: read back the real state (status + processing) and store it."""
    try:
        vids = client.videos([it.youtube_video_id])
    except Exception as exc:
        log.warning(f"Couldn't verify status of {it.youtube_video_id}: {exc}")
        return
    if vids:
        apply_state(it, vids[0], slot if (it.privacy or "scheduled") == "scheduled" else None, rep, now)


def apply_state(it: db.QueueItem, v: dict, expected: dt.datetime | None, rep: RunReport | None, now: dt.datetime) -> db.QueueItem:
    import json

    status, reason, msg = classify_ex(v, expected, it.privacy or "scheduled")
    it.yt_state_json = json.dumps(yt_state(v, now))
    pa = v.get("status", {}).get("publishAt")
    if pa and status == "scheduled":  # time changed in Studio → follow YouTube
        it.slot_utc = as_utc(dt.datetime.fromisoformat(pa.replace("Z", "+00:00")))
    if status != it.status:
        it.status, it.reason, it.error = status, reason, msg
        if status == "published":
            pub = v.get("snippet", {}).get("publishedAt")
            it.published_at = (as_utc(dt.datetime.fromisoformat(pub.replace("Z", "+00:00"))) if pub and not it.slot_utc
                               else it.slot_utc or now)
            if rep is not None:
                rep.published.append(it.id)  # type: ignore[arg-type]
        elif rep is not None and status == "locked_private":
            rep.locked.append(it.id)  # type: ignore[arg-type]
        elif rep is not None and status == "failed":
            rep.failed.append(it.id)  # type: ignore[arg-type]
    return q.save(it)


def _refresh_status(ch: db.Channel, client, cfg: Config, now: dt.datetime, rep: RunReport) -> None:
    """Re-read every clip that is on YouTube but not final yet: scheduled → published, processing state, time
    changes made in Studio, later rejections; linked needs_manual items (uploaded in Studio, no schedule yet) too."""
    from clipforge.publish import reconcile

    linked = [it for it in q.items(ch.id, ("needs_manual",)) if it.youtube_video_id]
    if linked:
        by = {v["id"]: v for v in client.videos([it.youtube_video_id for it in linked])}
        for it in linked:
            if it.youtube_video_id in by:
                reconcile.apply_video(it, by[it.youtube_video_id])
    watch = [it for it in q.items(ch.id, ("processing", "scheduled", "locked_private")) if it.youtube_video_id]
    if not watch:
        return
    by_id = {v["id"]: v for v in client.videos([it.youtube_video_id for it in watch])}
    for it in watch:
        v = by_id.get(it.youtube_video_id)
        if not v:
            if it.slot_utc and as_utc(it.slot_utc) <= now:
                it.status, it.reason, it.error = "failed", "deleted", "Video not found on YouTube any more (deleted?)."
                q.save(it)
            continue
        expected = as_utc(it.slot_utc) if it.slot_utc and (it.privacy or "scheduled") == "scheduled" else None
        apply_state(it, v, expected, rep, now)
