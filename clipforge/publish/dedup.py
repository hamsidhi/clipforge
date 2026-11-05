"""No duplicates (Publishing v2). One publication = one QueueItem row with a unique `pub_key`:

    pub_key = sha256(source_video_id | clip_rank | clip_file_sha256 | platform | account_id | intent)
    intent  = "original" | "republish:<n>"

The clip file's sha256 is computed once per render (cached by size + mtime in clips/_work/sha256.json).
Creation is check-and-create in one DB transaction plus a unique index on pub_key (`ux_queue_pub_key`), so two
workers adding the same clip at the same moment end with exactly one row.

A *logical twin* (same episode + rank + platform + account + intent, e.g. after a re-render changed the file hash, or
a row from before v2 without a key) also counts as a duplicate: the same clip is never published twice on one account
unless you ask for a republish. Duplicates are never deleted: they become `skipped_duplicate` with a reason.

| existing record                                   | adding the clip again / uploading it         |
| active (queued/uploading/processing/ready/waiting) | SKIPPED_DUPLICATE                            |
| has a platform media id                            | SKIPPED_DUPLICATE (status refresh reconciles) |
| retryable failure / missed, no media id            | resume the same row                          |
| permanent failure                                  | not re-created (Needs you only if it needs you) |
| explicit republish                                 | new intent → new key → new row               |
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from clipforge.api import db
from clipforge.utils.log import log

RETRYABLE_FAILED = ("auto_unavailable", "ig_container_failed", "retrying", "login_expired", "channel_limit")
ON_PLATFORM = ("uploading", "processing", "ready", "scheduled", "uploaded", "published", "locked_private")


def file_sha256(path: Path) -> str:
    """sha256 of a rendered clip, cached per render (size + mtime) next to the clip."""
    path = Path(path)
    st = path.stat()
    cache = path.parent / "_work" / "sha256.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    except (OSError, ValueError):
        data = {}
    hit = data.get(path.name)
    if hit and hit.get("size") == st.st_size and hit.get("mtime") == int(st.st_mtime):
        return hit["sha256"]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    data[path.name] = {"size": st.st_size, "mtime": int(st.st_mtime), "sha256": digest}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError as exc:
        log.warning(f"Couldn't cache the clip hash in {cache}: {exc}")
    return digest


def make_key(video_id: str, rank: int, file_sha: str, platform: str, account_id: str, intent: str = "original") -> str:
    raw = "|".join([video_id, str(rank), file_sha or "nofile", platform, account_id, intent or "original"])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def key_for(it: db.QueueItem, cfg=None) -> str:
    from clipforge.config import load_config
    from clipforge.publish import queue as q

    try:
        sha = file_sha256(q.clip_file(it, cfg or load_config()))
    except (q.QueueError, OSError):
        sha = ""
    return make_key(it.video_id, it.rank, sha, it.platform or "youtube", it.channel_id, it.intent or "original")


def _twins(it: db.QueueItem) -> list[db.QueueItem]:
    with db.session() as s:
        rows = s.exec(select(db.QueueItem).where(db.QueueItem.channel_id == it.channel_id,
                                                 db.QueueItem.video_id == it.video_id,
                                                 db.QueueItem.clip_key == it.clip_key,
                                                 db.QueueItem.platform == (it.platform or "youtube"))).all()
    return [r for r in rows if r.id != it.id and (r.intent or "original") == (it.intent or "original")
            and r.status not in ("skipped_duplicate", "cancelled")]


def _counts(r: db.QueueItem) -> bool:
    """Does this existing row already stand for the publication?"""
    return bool(r.youtube_video_id or r.ig_media_id) or r.status in ON_PLATFORM or r.status in (
        "queued", "waiting_capacity", "needs_manual", "draft")


def duplicate_of(it: db.QueueItem) -> db.QueueItem | None:
    """Right before an upload: an OLDER row for the same publication that is active or already on the platform."""
    for r in sorted(_twins(it), key=lambda r: r.id or 0):
        if (r.id or 0) < (it.id or 0) and _counts(r) and r.status != "draft":
            return r
        if r.youtube_video_id or r.ig_media_id:  # on the platform already, whatever its age
            return r
    return None


def mark_skipped(it: db.QueueItem, twin: db.QueueItem) -> db.QueueItem:
    from clipforge.publish import queue as q

    it.status, it.reason = "skipped_duplicate", "duplicate"
    it.error = f"Duplicate of queue item #{twin.id} (“{twin.title[:60]}”, {twin.status}) — kept for the record, never posted."
    log.info(f"[dedup] queue item {it.id} skipped: duplicate of {twin.id}")
    return q.save(it)


def add_or_skip(new: db.QueueItem) -> tuple[db.QueueItem | None, str]:
    """Atomic check-and-create. Returns (row, outcome) with outcome created | skipped_duplicate | resumed | exists |
    permanent_failure. Never raises on a duplicate."""
    from clipforge.publish import queue as q
    from clipforge.publish.timez import as_utc, now_utc

    for r in _twins(new):
        if r.youtube_video_id or r.ig_media_id or r.status in ON_PLATFORM or r.status in ("queued", "waiting_capacity",
                                                                                          "needs_manual"):
            return r, "skipped_duplicate"
        if r.status == "draft":
            return r, "exists"
        if r.status == "missed" or (r.status == "failed" and r.reason in RETRYABLE_FAILED):
            # back to planning, same row; its old slot may belong to another clip now → autoschedule picks one
            r.status, r.reason, r.error, r.next_attempt_at, r.slot_utc = "draft", "", "", None, None
            return q.save(r), "resumed"
        if r.status == "failed":
            return r, "permanent_failure"
    try:
        return q.save(new), "created"
    except IntegrityError:  # another worker created the same pub_key a moment ago
        with db.session() as s:
            row = s.exec(select(db.QueueItem).where(db.QueueItem.pub_key == new.pub_key)).first()
        return row, "skipped_duplicate"


def republish(item_id: int) -> db.QueueItem:
    """Explicit republish of a clip that was already published: a new intent → a new key → a new draft row."""
    from clipforge.publish import queue as q

    src = q.get(item_id)
    if not src:
        raise q.QueueError("Unknown queue item")
    if not (src.youtube_video_id or src.ig_media_id or src.status == "published"):
        raise q.QueueError("Only a published clip can be republished — this one isn't out yet.")
    with db.session() as s:
        n = len([r for r in s.exec(select(db.QueueItem).where(db.QueueItem.channel_id == src.channel_id,
                                                              db.QueueItem.video_id == src.video_id,
                                                              db.QueueItem.rank == src.rank)).all()
                 if (r.intent or "").startswith("republish:")]) + 1
    new = db.QueueItem(**{k: v for k, v in src.model_dump().items() if k in (
        "channel_id", "video_id", "rank", "clip_key", "episode_title", "topic", "score", "duration", "title",
        "description", "tags_json", "category_id", "variants_json", "meta_status", "ig_caption", "platform", "privacy",
        "ig_thumb_offset_ms")})
    new.intent, new.status = f"republish:{n}", "draft"
    new.pub_key = key_for(new)
    row, outcome = add_or_skip(new)
    if outcome != "created" or row is None:
        raise q.QueueError("That republish already exists.")
    return row


def backfill(cfg=None) -> dict:
    """Give rows from before v2 their pub_key; a later row of the same publication becomes skipped_duplicate (only if
    it isn't on the platform itself — two real uploads are both kept and reported). Idempotent."""
    from clipforge.publish import queue as q

    out = {"keyed": 0, "skipped": 0, "both_on_platform": 0}
    rows = sorted(q.items(), key=lambda r: r.id or 0)
    for it in rows:
        if it.pub_key or it.status in ("skipped_duplicate",):
            continue
        it = q.get(it.id)  # type: ignore[arg-type]
        if not it or it.pub_key:
            continue
        twin = next((r for r in sorted(_twins(it), key=lambda r: r.id or 0)
                     if (r.id or 0) < (it.id or 0) and (_counts(r) or r.status == "published")), None)
        if twin and not (it.youtube_video_id or it.ig_media_id) and it.status not in ON_PLATFORM:
            mark_skipped(it, twin)
            out["skipped"] += 1
            continue
        if twin:
            out["both_on_platform"] += 1
            log.warning(f"[dedup] queue items {twin.id} and {it.id} are both on the platform — kept both.")
            continue
        it.pub_key = key_for(it, cfg)
        try:
            q.save(it)
            out["keyed"] += 1
        except IntegrityError:  # another row already holds this exact key
            with db.session() as s:
                holder = s.exec(select(db.QueueItem).where(db.QueueItem.pub_key == it.pub_key)).first()
            it = q.get(it.id)  # type: ignore[arg-type,assignment]
            if it and holder and not (it.youtube_video_id or it.ig_media_id):
                mark_skipped(it, holder)
                out["skipped"] += 1
    return out
