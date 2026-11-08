"""Publish queue: clips planned for channels. Used by the API, the CLI and the background scheduler."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from sqlmodel import select

from clipforge import pipeline
from clipforge.api import db
from clipforge.config import Config, load_config
from clipforge.models import ClipCandidate
from clipforge.publish import channels as chs
from clipforge.publish import metadata_opt, slots
from clipforge.publish.timez import as_utc, now_utc, quota_day, utc_to_local
from clipforge.utils.log import log

ACTIVE = ("draft", "queued", "uploading", "ready", "scheduled", "uploaded", "published", "needs_manual",
          "locked_private", "missed", "failed")
PLANNED = ("queued", "uploading", "processing", "ready", "scheduled", "published", "needs_manual", "waiting_capacity")
ON_PLATFORM = ("uploading", "processing", "ready", "scheduled", "uploaded", "published", "locked_private")


class QueueError(ValueError):
    """User-facing problem (bad input, duplicate, wrong state)."""


def clip_key(video_id: str, c: ClipCandidate) -> str:
    return f"{video_id}:{c.start:.2f}-{c.end:.2f}"


def items(channel_id: str | None = None, statuses: tuple[str, ...] | None = None) -> list[db.QueueItem]:
    with db.session() as s:
        q = select(db.QueueItem)
        if channel_id:
            q = q.where(db.QueueItem.channel_id == channel_id)
        if statuses:
            q = q.where(db.QueueItem.status.in_(statuses))  # type: ignore[attr-defined]
        rows = list(s.exec(q).all())
    far = dt.datetime(9999, 1, 1, tzinfo=dt.timezone.utc)
    return sorted(rows, key=lambda r: (as_utc(r.slot_utc) if r.slot_utc else far, r.id or 0))


def get(item_id: int) -> db.QueueItem | None:
    with db.session() as s:
        return s.get(db.QueueItem, item_id)


def save(item: db.QueueItem) -> db.QueueItem:
    """Persist an item. Every status change is checked against states.TRANSITIONS (illegal → IllegalTransition)."""
    from clipforge.publish import states

    item.updated_at = db.now()
    if not item.idempotency_key and item.clip_key:  # one publication per (clip, platform, account)
        item.idempotency_key = f"{item.platform}:{item.channel_id}:{item.clip_key}"
    with db.session() as s:
        old = s.exec(select(db.QueueItem.status).where(db.QueueItem.id == item.id)).first() if item.id else None
        states.check(old, item.status, item=item)
        s.add(item)
        s.commit()
        s.refresh(item)
        return item


def claim_for_container(item_id: int) -> bool:
    """Instagram: atomically queued → uploading (only if no container/media yet and nobody changed it)."""
    from sqlalchemy import update

    with db.session() as s:
        res = s.exec(update(db.QueueItem)  # type: ignore[call-overload]
                     .where(db.QueueItem.id == item_id, db.QueueItem.status == "queued",
                            db.QueueItem.ig_container_id == "", db.QueueItem.ig_media_id == "")
                     .values(status="uploading", attempts=db.QueueItem.attempts + 1, updated_at=db.now(),
                             **lease_values(item_id)))
        s.commit()
        return res.rowcount == 1


def claim_for_upload(item_id: int, slot_utc: dt.datetime) -> bool:
    """Atomically move queued → uploading, only if nobody changed the item (status, slot, link, batch) meanwhile.
    The claim is also a lease (owner = this process, expires after LEASE): see `lease_values`."""
    from sqlalchemy import update

    with db.session() as s:
        res = s.exec(update(db.QueueItem)  # type: ignore[call-overload]
                     .where(db.QueueItem.id == item_id, db.QueueItem.status == "queued",
                            db.QueueItem.slot_utc == slot_utc, db.QueueItem.youtube_video_id == "", db.QueueItem.batch == "")
                     .values(status="uploading", attempts=db.QueueItem.attempts + 1, updated_at=db.now(),
                             **lease_values(item_id)))
        s.commit()
        return res.rowcount == 1


LEASE = dt.timedelta(hours=2)  # longest plausible upload; the process lock (runner._ProcessLock) is the first guard


def lease_owner() -> str:
    import os
    import socket

    return f"{socket.gethostname()}:{os.getpid()}"


def lease_values(item_id: int) -> dict:
    now = now_utc()
    return {"lease_owner": lease_owner(), "lease_expires_at": now + LEASE, "last_attempt_at": now}


def release(it: db.QueueItem) -> None:
    """Clear the lease fields on the object (caller saves)."""
    it.lease_owner, it.lease_expires_at = "", None


def delete(item_id: int) -> None:
    with db.session() as s:
        it = s.get(db.QueueItem, item_id)
        if it:
            if it.status in ON_PLATFORM:
                raise QueueError("This clip is already on the platform — delete it there (YouTube Studio / Instagram).")
            s.delete(it)
            s.commit()


_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)  # aware: DB datetimes load as UTC-aware


def existing_titles(channel_id: str, exclude_id: int | None = None) -> list[str]:
    """Titles of the channel's last 100 uploads/plans (local DB + synced YouTube videos)."""
    with db.session() as s:
        q = s.exec(select(db.QueueItem).where(db.QueueItem.channel_id == channel_id)).all()
        perf = s.exec(select(db.VideoPerf).where(db.VideoPerf.channel_id == channel_id)).all()
    planned = [it for it in q if it.id != exclude_id and it.status != "draft" and it.title]
    planned.sort(key=lambda it: as_utc(it.updated_at) if it.updated_at else _EPOCH, reverse=True)
    titles = [it.title for it in planned[:100]]
    own_vid = next((it.youtube_video_id for it in q if it.id == exclude_id), "")
    # the item's own YouTube upload (synced into VideoPerf) is not a "duplicate"
    others = [p for p in perf if p.queue_id != exclude_id and (not own_vid or p.youtube_video_id != own_vid)]
    titles += [p.title for p in sorted(others, key=lambda p: as_utc(p.published_at) if p.published_at else _EPOCH, reverse=True)[:100]]
    return titles


# ----------------------------------------------------------------------------- add / metadata
def add_clips(channel_id: str, video_id: str, ranks: list[int] | None, cfg: Config | None = None) -> list[db.QueueItem]:
    """Create draft queue items (metadata comes next). The same clip can't be queued twice on a channel."""
    cfg = cfg or load_config()
    ch = chs.get(channel_id)
    if not ch:
        raise QueueError("Unknown channel")
    ws = pipeline.open_workspace(video_id, cfg)
    info = pipeline.load_info(ws)
    cands = [c for c in pipeline.load_candidates(ws) if c.enabled and (ranks is None or c.rank in ranks)]
    if ranks is None:
        cands = [c for c in cands if c.approved]
    if not cands:
        raise QueueError("No clips to add (approve some clips first, or pass their ranks).")
    from clipforge.publish import dedup

    out = []
    for c in cands:
        f = ws.find_clip(c.rank)
        if not f:
            raise QueueError(f"Clip {c.rank} isn't rendered yet.")
        platform = ch.platform or "youtube"
        new = db.QueueItem(channel_id=channel_id, video_id=video_id, rank=c.rank, clip_key=clip_key(video_id, c),
                           episode_title=info.title, topic=c.topic, score=c.score, duration=c.duration,
                           title=c.title, category_id=str(chs.settings_of(ch, cfg).get("category_id", "27")),
                           platform=platform)
        new.pub_key = dedup.make_key(video_id, c.rank, dedup.file_sha256(f), platform, channel_id, "original")
        row, outcome = dedup.add_or_skip(new)  # atomic check-and-create (unique pub_key)
        if outcome in ("created", "resumed") and row is not None:
            out.append(row)
        else:
            log.info(f"[queue] clip {video_id}#{c.rank} on {channel_id}: {outcome}")
    return out


def candidate_for(item: db.QueueItem, cfg: Config) -> tuple[Any, ClipCandidate]:
    ws = pipeline.open_workspace(item.video_id, cfg)
    for c in pipeline.load_candidates(ws):
        if clip_key(item.video_id, c) == item.clip_key:
            return ws, c
    raise QueueError("This clip was changed or removed in its episode (re-trimmed?) — remove it from the queue and add it again.")


def clip_file(item: db.QueueItem, cfg: Config) -> Path:
    ws, c = candidate_for(item, cfg)
    f = ws.find_clip(c.rank)
    if not f:
        raise QueueError("The clip's video file is missing — re-render it in the gallery.")
    root = cfg.workspace.resolve()
    if not Path(f).resolve().is_relative_to(root):  # uploads only ever send files from workspace/
        raise QueueError("The clip file is outside the workspace folder — refusing to upload it.")
    return f


def generate_metadata(item_id: int, cfg: Config | None = None, llm=None) -> db.QueueItem:
    cfg = cfg or load_config()
    it = get(item_id)
    if not it:
        raise QueueError("Unknown queue item")
    ch = chs.get(it.channel_id)
    if not ch:
        raise QueueError("Unknown channel")
    if it.meta_status == "ready" and (it.title or it.ig_caption):
        return it  # metadata is written once and kept: a publish retry never calls the LLM (Regenerate sets pending)
    ws, c = candidate_for(it, cfg)
    info = pipeline.load_info(ws)
    words = pipeline.load_transcript(ws).all_words()
    if llm is None:
        llm = pipeline.get_llm(cfg, required=False)
    from clipforge.publish import llm_budget

    if llm is not None:
        llm = llm_budget.Budgeted(llm, cfg)
    try:
        meta = metadata_opt.generate(c, info, words, chs.settings_of(ch, cfg), llm, existing_titles(it.channel_id, it.id))
    except llm_budget.BudgetExceeded as exc:  # only the metadata step pauses
        it.meta_status, it.error = "pending", str(exc)
        return save(it)
    it.title, it.description = meta["title"], meta["description"]
    it.tags_json, it.variants_json = json.dumps(meta["tags"], ensure_ascii=False), json.dumps(meta["variants"], ensure_ascii=False)
    it.ig_caption, it.category_id = meta["ig_caption"], meta["category_id"]
    if it.platform == "instagram":
        it.meta_status = "ready" if it.ig_caption else "failed"
        it.error = "" if it.ig_caption else "Couldn't write a caption — write one."
        if not it.title:
            it.title = meta["title"] or c.title
        return save(it)
    it.meta_status = "ready" if meta["title"] else "failed"
    it.error = "" if meta["title"] else "Couldn't produce a unique title — write one."
    return save(it)


def problems(it: db.QueueItem) -> list[str]:
    if it.platform == "instagram":
        from clipforge.publish import ig_meta

        return ig_meta.problems(it.ig_caption)
    return metadata_opt.validate(it.title, it.description, it.tags, existing_titles(it.channel_id, it.id))


# ----------------------------------------------------------------------------- scheduling
def daily_capacity_fn(cfg: Config, exclude_channel: str | None = None):
    """API mode: all channels share one upload quota, so at most `insert_calls_per_day` posts per day overall."""
    from clipforge.publish import modes

    ch = chs.get(exclude_channel) if exclude_channel else None
    if ch and ch.platform == "instagram":
        return None
    if not any(modes.resolve(c, cfg, check=False).auto for c in chs.all_channels() if c.platform != "instagram"):
        return None
    counts: dict[dt.date, int] = {}
    for it in items(statuses=PLANNED):
        if it.slot_utc and it.channel_id != exclude_channel and it.platform == "youtube":
            d = utc_to_local(it.slot_utc, cfg.publishing.timezone).date()
            counts[d] = counts.get(d, 0) + 1
    return lambda day: max(0, cfg.publishing.insert_calls_per_day - counts.get(day, 0))


def plan_for(ch: db.Channel, cfg: Config, now: dt.datetime) -> slots.Plan:
    """Publishing v2 daily plan (schedule.enabled) unless this account has its own slot list in its settings."""
    from clipforge.publish import plan as daily

    settings = chs.settings_of(ch, cfg)
    p = slots.Plan.from_settings(settings)
    own_slots = "slots" in (ch.settings or {}) and not (cfg.schedule.smart and (ch.settings or {}).get("smart", True))
    if cfg.schedule.enabled and not own_slots:  # smart plan also replaces an account's own fixed slot list
        platform = ch.platform or "youtube"
        p.day_plan = lambda day: daily.day_slots(ch, cfg, day, now)
        p.cutoff = cfg.schedule.day_cutoff
        p.min_gap_hours = daily.min_gap_hours(platform, cfg)
        p.slots = daily.base_slots(platform, cfg, 9 if platform == "instagram" else 6)
        p.peak_slots = (["19:30", "21:00", "22:15"] if platform == "instagram" else ["18:30", "20:30"])
        if cfg.schedule.smart:
            from clipforge.publish import smart

            p.peak_slots = smart.day_plan(ch, cfg, now.date(), now)["peak"]
    return p


def autoschedule(channel_id: str, item_ids: list[int] | None = None, cfg: Config | None = None,
                 now: dt.datetime | None = None) -> list[db.QueueItem]:
    """Put draft items (metadata ready, no problems) into the next free slots of the channel."""
    cfg = cfg or load_config()
    ch = chs.get(channel_id)
    if not ch:
        raise QueueError("Unknown channel")
    plan = plan_for(ch, cfg, now or now_utc())
    if not plan.slots and plan.day_plan is None:
        raise QueueError("This channel has no publish slots — set them in Channels → Schedule settings.")
    drafts = [it for it in items(channel_id, ("draft",)) if item_ids is None or it.id in item_ids]
    ready, seen = [], set()
    for it in drafts:  # also no two identical titles within this batch
        key = metadata_opt._norm(it.title)
        if it.meta_status == "ready" and not problems(it) and key not in seen:
            ready.append(it)
            seen.add(key)
    occupied = [slots.Occupied(it.slot_utc, it.topic, it.video_id) for it in items(channel_id, PLANNED) if it.slot_utc]
    picks = slots.fill([slots.ClipRef(it.id, it.score, it.video_id, it.topic) for it in ready], plan, occupied,
                       now or now_utc(), daily_capacity_fn(cfg, channel_id))
    by_id = {it.id: it for it in ready}
    out = []
    for item_id, slot in picks:
        it = by_id[item_id]
        it.slot_utc, it.status, it.error, it.reason = slot, "queued", "", ""
        out.append(save(it))
    if ch.platform == "instagram":
        out = [_after_youtube(it, plan, cfg) for it in out]
    return out


def _after_youtube(it: db.QueueItem, plan: slots.Plan, cfg: Config) -> db.QueueItem:
    """'Post to Instagram X hours after YouTube': if the same clip is planned on a YouTube channel, the Reel goes
    to the first free slot at least `hours_after_youtube` after it (0 = any slot, but never before YouTube's)."""
    ch = chs.get(it.channel_id)
    hours = float(chs.settings_of(ch, cfg).get("hours_after_youtube", 0) if ch else 0)
    with db.session() as s:
        twins = s.exec(select(db.QueueItem).where(db.QueueItem.clip_key == it.clip_key,
                                                  db.QueueItem.platform == "youtube")).all()
    yt_slots = [as_utc(t.slot_utc) for t in twins if t.slot_utc]
    if not yt_slots or not it.slot_utc:
        return it
    earliest = min(yt_slots) + dt.timedelta(hours=hours)
    if as_utc(it.slot_utc) >= earliest:
        return it
    taken = [as_utc(o.slot_utc) for o in items(it.channel_id, PLANNED) if o.slot_utc and o.id != it.id]
    for t, _peak in slots.free_slots(plan, taken, earliest - slots.LEAD, 60):
        if t >= earliest:
            it.slot_utc = t
            return save(it)
    return it


def move(item_id: int, slot_utc: dt.datetime, now: dt.datetime | None = None) -> db.QueueItem:
    it = get(item_id)
    if not it:
        raise QueueError("Unknown queue item")
    if it.status not in ("draft", "queued", "needs_manual", "failed", "missed"):
        raise QueueError(f"Can't move a clip that is {it.status} — change it on the platform (YouTube Studio).")
    if it.youtube_video_id or it.ig_media_id:  # already uploaded (e.g. linked from Studio): moving would upload twice
        raise QueueError("This clip is already on YouTube — change its time in YouTube Studio (Content → Visibility), "
                         "then Sync: ClipForge picks up the new time.")
    if as_utc(slot_utc) < (now or now_utc()) + slots.LEAD:
        raise QueueError("Pick a time at least 30 minutes in the future.")
    clash = [o for o in items(it.channel_id, PLANNED) if o.id != it.id and o.slot_utc and as_utc(o.slot_utc) == as_utc(slot_utc)]
    if clash:
        raise QueueError("Another clip is already scheduled at that exact time.")
    it.slot_utc, it.status, it.error, it.reason, it.attempts, it.next_attempt_at = as_utc(slot_utc), "queued", "", "", 0, None
    it.batch = ""  # a new time means a new batch (the old checklist shows the old time)
    it.ig_container_id, it.ig_container_at = "", None  # an Instagram container expires after 24 h anyway
    return save(it)


def next_free_slot(channel_id: str, now: dt.datetime | None = None, exclude_id: int | None = None,
                   cfg: Config | None = None) -> dt.datetime | None:
    """The channel's next free publish time (respects its slots, days and posts/day)."""
    ch = chs.get(channel_id)
    if not ch:
        return None
    cfg = cfg or load_config()
    plan = plan_for(ch, cfg, now or now_utc())
    taken = [as_utc(o.slot_utc) for o in items(channel_id, PLANNED) if o.slot_utc and o.id != exclude_id]
    for t, _peak in slots.free_slots(plan, taken, now or now_utc(), 60):
        return t
    return None


def reschedule_next(item_id: int, now: dt.datetime | None = None) -> db.QueueItem:
    """Move a clip to the channel's next free slot (one-click fix for missed / failed clips)."""
    it = get(item_id)
    if not it:
        raise QueueError("Unknown queue item")
    when = next_free_slot(it.channel_id, now, exclude_id=it.id)
    if when is None:
        raise QueueError("No free slot in the next 60 days — add slots or raise posts per day in the channel settings.")
    return move(item_id, when, now)


def unschedule(item_id: int) -> db.QueueItem:
    it = get(item_id)
    if not it:
        raise QueueError("Unknown queue item")
    if it.status in ON_PLATFORM or it.youtube_video_id or it.ig_media_id:
        raise QueueError("Already on the platform — change or delete it there (YouTube Studio / Instagram).")
    it.slot_utc, it.status, it.batch, it.reason, it.error = None, "draft", "", "", ""
    return save(it)


def public(it: db.QueueItem, zone: str = "Asia/Kolkata") -> dict:
    d = it.model_dump()
    d["tags"] = it.tags
    d["variants"] = json.loads(it.variants_json or "[]")
    d.pop("tags_json", None)
    d.pop("variants_json", None)
    d["slot_local"] = utc_to_local(it.slot_utc, zone).strftime("%Y-%m-%dT%H:%M") if it.slot_utc else None
    d["slot_utc"] = as_utc(it.slot_utc).isoformat() if it.slot_utc else None
    d["clip_id_hint"] = f"{it.video_id}:{it.rank}"
    d["youtube_url"] = f"https://youtube.com/shorts/{it.youtube_video_id}" if it.youtube_video_id else None
    d["url"] = d["youtube_url"] or it.permalink or None
    d["yt_state"] = it.yt_state
    d.pop("yt_state_json", None)
    from clipforge.publish import states

    d["reason_label"] = states.label(it.reason)
    d["actions"] = states.actions(it.reason)
    return d


def log_quota_rollover(cfg: Config) -> None:
    log.info(f"Upload quota for {quota_day()} used up — remaining uploads continue tomorrow (Pacific time).")
