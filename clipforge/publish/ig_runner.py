"""Instagram part of the scheduler (called from runner.run_once every minute by the app/daemon, every 30 min by Task
Scheduler). Instagram has no native scheduling and containers expire after 24 h, so:

  slot − upload_lead_minutes:  create the REELS container + resumable upload of the local file   (queued → uploading)
  every tick:                  poll the container: IN_PROGRESS → wait, FINISHED → ready, ERROR → failed, EXPIRED → redo
  at the slot (ready):         check content_publishing_limit, media_publish, store media id + permalink → published
  slot + max_late_minutes:     never posted late → missed (Reschedule / Publish now)

The laptop must be on and the scheduler running at the slot. Assisted accounts (no Meta app / manual) get a kit instead.
"""

from __future__ import annotations

import datetime as dt
import json

from clipforge.api import db
from clipforge.config import Config
from clipforge.publish import channels as chs
from clipforge.publish import instagram as ig
from clipforge.publish import queue as q
from clipforge.publish.timez import as_utc
from clipforge.utils.log import log

MAX_ATTEMPTS = 4
UPLOAD_RETRIES = 3
INSIGHTS_EVERY = dt.timedelta(hours=12)


def _assisted(ch: db.Channel, cfg: Config) -> bool:
    return ig.is_manual(ch) or cfg.instagram.mode == "assisted" or not ig.configured()


def _late(it: db.QueueItem, now: dt.datetime, cfg: Config) -> bool:
    return bool(it.slot_utc) and now > as_utc(it.slot_utc) + dt.timedelta(minutes=cfg.instagram.max_late_minutes)  # type: ignore[arg-type]


def _mark(it: db.QueueItem, status: str, reason: str, error: str) -> db.QueueItem:
    it.status, it.reason, it.error = status, reason, error
    return q.save(it)


def process_account(ch: db.Channel, cfg: Config, now: dt.datetime, rep) -> None:
    items = [it for it in q.items(ch.id, ("queued", "uploading", "ready")) if it.slot_utc]
    if ch.paused:
        rep.skipped_channels[ch.id] = "paused"
        return
    if _assisted(ch, cfg):
        for it in items:
            if it.status == "queued" and as_utc(it.slot_utc) <= now:  # type: ignore[arg-type]
                _mark(it, "needs_manual", "ig_assisted",
                      "Assisted mode: post this Reel in the Instagram app (or schedule it in Meta Business Suite) using the "
                      "Instagram kit, then paste the Reel's link here to track it.")
                rep.missed.append(it.id)
        return
    if ch.auth_status == "needs_reauth":
        rep.skipped_channels[ch.id] = "needs re-login"
        for it in items:
            if it.reason != "login_expired" and as_utc(it.slot_utc) < now + dt.timedelta(days=2):  # type: ignore[arg-type]
                _mark(it, it.status, "login_expired", "The Instagram login expired — Reconnect on the Accounts page.")
        return
    client = ig.client(cfg)
    try:
        ig.refresh_if_needed(ch, cfg, now, client)
        token = ig.page_token(ch, cfg)
    except ig.IGError as exc:
        if exc.auth or not exc.transient:
            chs.mark_reauth(ch.id, f"Instagram login problem: {exc}. Reconnect.")
            rep.skipped_channels[ch.id] = "needs re-login"
        else:
            rep.skipped_channels[ch.id] = f"error: {exc}"
        return
    ig_id = ig.ig_id_of(ch)
    lead = dt.timedelta(minutes=cfg.instagram.upload_lead_minutes)
    for it in items:
        try:
            it = q.get(it.id)  # live data (the user may have moved/paused things meanwhile)
            if not it or it.status not in ("queued", "uploading", "ready"):
                continue
            if it.status == "uploading" and not it.ig_container_id:  # crashed between claim and container: redo
                it.status = "queued"
                q.save(it)
            slot = as_utc(it.slot_utc)  # type: ignore[arg-type]
            if _late(it, now, cfg):
                _mark(it, "missed", "ig_missed",
                      f"Instagram has no scheduling: ClipForge must publish at the slot, and the laptop/app wasn't running "
                      f"then (more than {cfg.instagram.max_late_minutes} min late — not posted late). Reschedule or Publish now.")
                rep.missed.append(it.id)
                continue
            fresh = False
            if it.status == "queued" and now >= slot - lead:
                fresh = _start_upload(it, ch, client, token, ig_id, cfg, now, rep)
            it = q.get(it.id)
            if it and it.status in ("uploading", "ready") and it.ig_container_id:
                _advance(it, client, token, ig_id, cfg, now, rep, fresh)
        except ig.IGError as exc:
            if exc.auth:
                chs.mark_reauth(ch.id, f"Instagram refused the saved login ({exc}). Reconnect.")
                rep.skipped_channels[ch.id] = "needs re-login"
                return
            log.warning(f"[instagram] item {it.id}: {exc}")
            _retry_or_fail(it, str(exc), now, rep)
    _sync_insights(ch, client, token, ig_id, cfg, now)


def _retry_or_fail(it: db.QueueItem, msg: str, now: dt.datetime, rep) -> None:
    it = q.get(it.id) or it
    if it.attempts >= MAX_ATTEMPTS:
        _mark(it, "failed", "upload_failed", f"Instagram upload failed {it.attempts}× — {msg}")
        rep.failed.append(it.id)
    else:
        it.status, it.error = ("ready" if it.ig_container_id and it.status == "ready" else "queued"), f"Retrying: {msg}"
        if it.status == "queued":
            it.ig_container_id, it.ig_container_at = "", None
        it.next_attempt_at = now + dt.timedelta(minutes=2 * (it.attempts or 1))
        q.save(it)


def _start_upload(it: db.QueueItem, ch: db.Channel, client: ig.IGClient, token: str, ig_id: str, cfg: Config,
                  now: dt.datetime, rep) -> bool:
    """Create the container and upload the file. True when this call uploaded it (no resume needed)."""
    if it.next_attempt_at and as_utc(it.next_attempt_at) > now:
        return False
    if not ch.confirmed:
        _mark(it, "queued", "channel_unconfirmed", "Confirm this Instagram account (Accounts page) before the first post.")
        return False
    probs = q.problems(it)
    if probs:
        _mark(it, "needs_manual", "metadata", " ".join(probs))
        return False
    try:
        path = q.clip_file(it, cfg)
    except q.QueueError as exc:
        _mark(it, "needs_manual", "file_missing", str(exc))
        return False
    bad = ig.preflight(path)
    if bad:
        _mark(it, "failed", "upload_failed", "The video doesn't meet Instagram's Reels specs: " + " ".join(bad))
        rep.failed.append(it.id)
        return False
    if not q.claim_for_container(it.id):
        return False
    it = q.get(it.id)  # type: ignore[assignment]
    settings = chs.settings_of(ch, cfg)
    c = client.create_reel(ig_id, token, it.ig_caption, bool(settings.get("share_to_feed", True)), it.ig_thumb_offset_ms)
    it.ig_container_id, it.ig_container_at = c["id"], now
    q.save(it)
    offset, size = 0, path.stat().st_size
    for attempt in range(UPLOAD_RETRIES):
        try:
            client.upload(it.ig_container_id, token, path, offset)
            break
        except ig.IGError as exc:
            if exc.auth or attempt == UPLOAD_RETRIES - 1:
                raise
            offset = client.upload_offset(it.ig_container_id, token)  # resume where Meta stopped receiving
            log.info(f"[instagram] upload interrupted ({exc}); resuming at byte {offset}/{size}")
            if offset >= size:
                break
    it.error = ""
    q.save(it)
    rep.uploaded.append(it.id)
    return True
    log.info(f"[instagram] uploaded queue item {it.id} → container {it.ig_container_id} (publishes {as_utc(it.slot_utc).isoformat()})")  # type: ignore[arg-type]


def _resume_upload(it: db.QueueItem, client: ig.IGClient, token: str, cfg: Config) -> None:
    """An item left 'uploading' by an earlier run/process (crash mid-upload): continue from Meta's offset.
    Safe: the process lock means no other upload of this container is running right now."""
    try:
        path = q.clip_file(it, cfg)
    except q.QueueError:
        return
    size = path.stat().st_size
    offset = client.upload_offset(it.ig_container_id, token)
    if offset < size:
        log.info(f"[instagram] resuming the upload of queue item {it.id} at byte {offset}/{size}")
        client.upload(it.ig_container_id, token, path, offset)


def _find_published(it: db.QueueItem, client: ig.IGClient, token: str, ig_id: str) -> dict | None:
    """Meta says the container is PUBLISHED but we never got the media id (timeout after media_publish)."""
    since = as_utc(it.ig_container_at) - dt.timedelta(minutes=5) if it.ig_container_at else None
    for m in client.recent_media(ig_id, token, 25):
        cap = (m.get("caption") or "").strip()
        ts = m.get("timestamp")
        when = dt.datetime.fromisoformat(ts.replace("Z", "+00:00").replace("+0000", "+00:00")) if ts else None
        if cap[:80] == (it.ig_caption or "").strip()[:80] and (since is None or when is None or when >= since):
            return m
    return None


def _advance(it: db.QueueItem, client: ig.IGClient, token: str, ig_id: str, cfg: Config, now: dt.datetime, rep,
             fresh: bool = False) -> None:
    if it.status == "uploading" and not fresh:
        _resume_upload(it, client, token, cfg)
    st = client.container_status(it.ig_container_id, token)
    code = st.get("status_code", "")
    if code == "PUBLISHED":  # already live (an earlier media_publish answer got lost): record it, never publish again
        m = _find_published(it, client, token, ig_id) or {}
        it.ig_media_id, it.permalink = m.get("id", ""), m.get("permalink", "")
        it.status, it.reason, it.published_at = "published", "", it.published_at or now
        it.error = "" if m else "Published on Instagram (ClipForge couldn't read its link — paste it to track stats)."
        q.save(it)
        rep.published.append(it.id)
        return
    if code == "IN_PROGRESS":
        return
    if code == "ERROR":
        _mark(it, "failed", "ig_container_failed", f"Instagram couldn't process the video: {st.get('status') or 'ERROR'}.")
        it.ig_container_id = ""
        q.save(it)
        rep.failed.append(it.id)
        return
    if code == "EXPIRED":  # >24 h old: make a new one next tick (if still in time)
        it.ig_container_id, it.ig_container_at, it.status = "", None, "queued"
        q.save(it)
        return
    if code == "FINISHED" and it.status == "uploading":
        it.status, it.reason = "ready", ""
        q.save(it)
    if it.status != "ready" or now < as_utc(it.slot_utc):  # type: ignore[arg-type]
        return
    lim = client.publishing_limit(ig_id, token)
    used = int(lim.get("quota_usage") or 0)
    total = int((lim.get("config") or {}).get("quota_total") or cfg.instagram.publish_limit)
    if used >= total - cfg.instagram.publish_limit_margin:
        _mark(it, "ready", "ig_limit", f"Instagram's limit of {total} API posts per 24 h is nearly used ({used}) — retrying.")
        return
    media_id = client.publish(ig_id, token, it.ig_container_id)
    it.ig_media_id, it.status, it.reason, it.error, it.published_at = media_id, "published", "", "", now
    try:
        it.permalink = client.media(media_id, token).get("permalink", "")
    except ig.IGError as exc:
        log.warning(f"[instagram] published {media_id} but couldn't read its link: {exc}")
    q.save(it)
    rep.published.append(it.id)
    log.info(f"[instagram] published queue item {it.id} → {it.permalink or media_id}")


def _sync_insights(ch: db.Channel, client: ig.IGClient, token: str, ig_id: str, cfg: Config, now: dt.datetime) -> None:
    """Reel insights + follower count, at most every 12 h. Stored in VideoPerf (id 'ig:<media>') / ChannelDaily."""
    from sqlmodel import select

    from clipforge.publish.timez import utc_to_local

    last = ch.last_sync_at
    if last is not None and now - as_utc(last) < INSIGHTS_EVERY:
        return
    try:
        acct = client.account(ig_id, token)
    except ig.IGError as exc:
        log.warning(f"[instagram] account stats failed: {exc}")
        return
    with db.session() as s:
        for it in s.exec(select(db.QueueItem).where(db.QueueItem.channel_id == ch.id, db.QueueItem.status == "published")).all():
            if not it.ig_media_id:
                continue
            try:
                m = client.insights(it.ig_media_id, token)
            except ig.IGError as exc:
                log.debug(f"[instagram] insights {it.ig_media_id}: {exc}")
                continue
            pid = f"ig:{it.ig_media_id}"
            perf = s.get(db.VideoPerf, pid) or db.VideoPerf(youtube_video_id=pid, channel_id=ch.id, platform="instagram")
            perf.queue_id, perf.title, perf.published_at, perf.duration = it.id, it.title, it.published_at, it.duration
            perf.views, perf.likes, perf.comments = int(m.get("views", 0)), int(m.get("likes", 0)), int(m.get("comments", 0))
            perf.shares, perf.saves, perf.reach = int(m.get("shares", 0)), int(m.get("saved", 0)), int(m.get("reach", 0))
            perf.permalink = it.permalink
            if it.published_at and perf.views_24h is None and now - as_utc(it.published_at) >= dt.timedelta(hours=24):
                perf.views_24h = perf.views  # first sync after a day ≈ 24 h views (for the best-time heatmap)
            perf.updated_at = db.now()
            s.add(perf)
            s.add(db.StatSnapshot(youtube_video_id=pid, taken_at=now, views=perf.views))
        today = utc_to_local(now, chs.settings_of(ch, cfg)["timezone"]).date()
        row = s.exec(select(db.ChannelDaily).where(db.ChannelDaily.channel_id == ch.id, db.ChannelDaily.day == today)).first() \
            or db.ChannelDaily(channel_id=ch.id, day=today)
        row.subscribers = int(acct.get("followers_count") or 0)
        s.add(row)
        s.commit()
    chs.update(ch.id, last_sync_at=now, extra_json=json.dumps({**ch.extra, "followers": int(acct.get("followers_count") or 0)}))


def publish_now(it: db.QueueItem) -> None:
    """Used by the 'Publish now' button: the next tick creates the container and publishes as soon as it's FINISHED."""
    it.status, it.reason, it.error = "queued", "", ""
    q.save(it)
