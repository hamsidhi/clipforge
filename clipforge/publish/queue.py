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

ACTIVE = ("draft", "queued", "uploading", "scheduled", "published", "needs_manual", "locked_private")
PLANNED = ("queued", "uploading", "scheduled", "published", "needs_manual")


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
    item.updated_at = db.now()
    with db.session() as s:
        s.add(item)
        s.commit()
        s.refresh(item)
        return item


def delete(item_id: int) -> None:
    with db.session() as s:
        it = s.get(db.QueueItem, item_id)
        if it:
            if it.status in ("uploading", "scheduled", "published"):
                raise QueueError("This clip is already on YouTube — delete it in YouTube Studio instead.")
            s.delete(it)
            s.commit()


def existing_titles(channel_id: str, exclude_id: int | None = None) -> list[str]:
    """Titles of the channel's last 100 uploads/plans (local DB + synced YouTube videos)."""
    with db.session() as s:
        q = s.exec(select(db.QueueItem).where(db.QueueItem.channel_id == channel_id)).all()
        perf = s.exec(select(db.VideoPerf).where(db.VideoPerf.channel_id == channel_id)).all()
    planned = [it for it in q if it.id != exclude_id and it.status != "draft" and it.title]
    planned.sort(key=lambda it: it.updated_at, reverse=True)
    titles = [it.title for it in planned[:100]]
    titles += [p.title for p in sorted(perf, key=lambda p: p.published_at or dt.datetime.min, reverse=True)[:100]]
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
    with db.session() as s:
        have = {it.clip_key for it in s.exec(select(db.QueueItem).where(db.QueueItem.channel_id == channel_id)).all()}
    out = []
    for c in cands:
        if not ws.find_clip(c.rank):
            raise QueueError(f"Clip {c.rank} isn't rendered yet.")
        key = clip_key(video_id, c)
        if key in have:
            continue  # duplicate guard
        out.append(save(db.QueueItem(channel_id=channel_id, video_id=video_id, rank=c.rank, clip_key=key,
                                     episode_title=info.title, topic=c.topic, score=c.score, duration=c.duration,
                                     title=c.title, category_id=str(chs.settings_of(ch, cfg).get("category_id", "27")))))
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
    return f


def generate_metadata(item_id: int, cfg: Config | None = None, llm=None) -> db.QueueItem:
    cfg = cfg or load_config()
    it = get(item_id)
    if not it:
        raise QueueError("Unknown queue item")
    ch = chs.get(it.channel_id)
    if not ch:
        raise QueueError("Unknown channel")
    ws, c = candidate_for(it, cfg)
    info = pipeline.load_info(ws)
    words = pipeline.load_transcript(ws).all_words()
    if llm is None:
        llm = pipeline.get_llm(cfg, required=False)
    meta = metadata_opt.generate(c, info, words, chs.settings_of(ch, cfg), llm, existing_titles(it.channel_id, it.id))
    it.title, it.description = meta["title"], meta["description"]
    it.tags_json, it.variants_json = json.dumps(meta["tags"], ensure_ascii=False), json.dumps(meta["variants"], ensure_ascii=False)
    it.ig_caption, it.category_id = meta["ig_caption"], meta["category_id"]
    it.meta_status = "ready" if meta["title"] else "failed"
    it.error = "" if meta["title"] else "Couldn't produce a unique title — write one."
    return save(it)


def problems(it: db.QueueItem) -> list[str]:
    return metadata_opt.validate(it.title, it.description, it.tags, existing_titles(it.channel_id, it.id))


# ----------------------------------------------------------------------------- scheduling
def daily_capacity_fn(cfg: Config, exclude_channel: str | None = None):
    """API mode: all channels share one upload quota, so at most `insert_calls_per_day` posts per day overall."""
    if cfg.publishing.mode != "api":
        return None
    counts: dict[dt.date, int] = {}
    for it in items(statuses=PLANNED):
        if it.slot_utc and it.channel_id != exclude_channel:
            d = utc_to_local(it.slot_utc, cfg.publishing.timezone).date()
            counts[d] = counts.get(d, 0) + 1
    return lambda day: max(0, cfg.publishing.insert_calls_per_day - counts.get(day, 0))


def autoschedule(channel_id: str, item_ids: list[int] | None = None, cfg: Config | None = None,
                 now: dt.datetime | None = None) -> list[db.QueueItem]:
    """Put draft items (metadata ready, no problems) into the next free slots of the channel."""
    cfg = cfg or load_config()
    ch = chs.get(channel_id)
    if not ch:
        raise QueueError("Unknown channel")
    plan = slots.Plan.from_settings(chs.settings_of(ch, cfg))
    if not plan.slots:
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
        it.slot_utc, it.status, it.error = slot, "queued", ""
        out.append(save(it))
    return out


def move(item_id: int, slot_utc: dt.datetime, now: dt.datetime | None = None) -> db.QueueItem:
    it = get(item_id)
    if not it:
        raise QueueError("Unknown queue item")
    if it.status not in ("draft", "queued", "needs_manual", "failed"):
        raise QueueError(f"Can't move a clip that is {it.status} — change it in YouTube Studio.")
    if as_utc(slot_utc) < (now or now_utc()) + slots.LEAD:
        raise QueueError("Pick a time at least 30 minutes in the future.")
    clash = [o for o in items(it.channel_id, PLANNED) if o.id != it.id and o.slot_utc and as_utc(o.slot_utc) == as_utc(slot_utc)]
    if clash:
        raise QueueError("Another clip is already scheduled at that exact time.")
    it.slot_utc, it.status, it.error, it.attempts, it.next_attempt_at = as_utc(slot_utc), "queued", "", 0, None
    return save(it)


def unschedule(item_id: int) -> db.QueueItem:
    it = get(item_id)
    if not it:
        raise QueueError("Unknown queue item")
    if it.status in ("uploading", "scheduled", "published"):
        raise QueueError("Already on YouTube — change or delete it in YouTube Studio.")
    it.slot_utc, it.status, it.batch = None, "draft", ""
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
    return d


def log_quota_rollover(cfg: Config) -> None:
    log.info(f"Upload quota for {quota_day()} used up — remaining uploads continue tomorrow (Pacific time).")
