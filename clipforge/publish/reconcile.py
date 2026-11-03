"""Match videos you uploaded yourself (assisted mode) to queue items: fuzzy title + duration.

Read-only API calls only (playlistItems.list + videos.list), so it works for any API project.
"""

from __future__ import annotations

import datetime as dt
import difflib
import re

from clipforge.api import db
from clipforge.config import Config, load_config
from clipforge.publish import channels as chs
from clipforge.publish import queue as q
from clipforge.publish import youtube as yt
from clipforge.publish.runner import classify
from clipforge.publish.timez import as_utc

TITLE_MIN = 0.8
DURATION_TOL = 2.5  # seconds (Studio re-encodes; durations differ slightly)


def _norm(t: str) -> str:
    return re.sub(r"\W+", " ", (t or "").lower()).strip()


def match_score(item: db.QueueItem, video: dict) -> float:
    title = video.get("snippet", {}).get("title", "")
    ratio = difflib.SequenceMatcher(None, _norm(item.title), _norm(title)).ratio()
    dur = yt.parse_duration(video.get("contentDetails", {}).get("duration", ""))
    if item.duration and dur and abs(item.duration - dur) > DURATION_TOL:
        return 0.0
    return ratio


def match(items: list[db.QueueItem], videos: list[dict]) -> tuple[list[tuple[db.QueueItem, dict, float]], list[db.QueueItem], list[dict]]:
    """Greedy best-first matching. Returns (pairs, unmatched_items, unmatched_videos)."""
    scored = sorted(((match_score(it, v), i, j) for i, it in enumerate(items) for j, v in enumerate(videos)),
                    reverse=True)
    used_i: set[int] = set()
    used_j: set[int] = set()
    pairs = []
    for s, i, j in scored:
        if s < TITLE_MIN:
            break
        if i in used_i or j in used_j:
            continue
        used_i.add(i)
        used_j.add(j)
        pairs.append((items[i], videos[j], round(s, 3)))
    return pairs, [it for i, it in enumerate(items) if i not in used_i], [v for j, v in enumerate(videos) if j not in used_j]


def apply_video(it: db.QueueItem, video: dict) -> db.QueueItem:
    st = video.get("status", {})
    publish_at = st.get("publishAt")
    if publish_at:
        it.slot_utc = as_utc(dt.datetime.fromisoformat(publish_at.replace("Z", "+00:00")))
    it.youtube_video_id = video["id"]
    status, msg = classify(video, None)
    if status == "published":
        it.published_at = as_utc(dt.datetime.fromisoformat(video["snippet"]["publishedAt"].replace("Z", "+00:00")))
    elif status == "scheduled" and not publish_at:
        status, msg = "needs_manual", "Uploaded but not scheduled yet — set Visibility → Schedule in Studio."
    it.status, it.error = status, msg
    return q.save(it)


def sync(channel_id: str, cfg: Config | None = None, client: yt.YouTubeClient | None = None) -> dict:
    cfg = cfg or load_config()
    ch = chs.get(channel_id)
    if not ch:
        raise q.QueueError("Unknown channel")
    client = client or yt.client_for(channel_id, cfg)
    ids = client.uploads(ch.uploads_playlist, 100) if ch.uploads_playlist else []
    videos = client.videos(ids) if ids else []
    known = {it.youtube_video_id for it in q.items(channel_id) if it.youtube_video_id}
    fresh = [v for v in videos if v["id"] not in known]
    waiting = [it for it in q.items(channel_id, ("queued", "needs_manual")) if not it.youtube_video_id]
    pairs, left_items, left_videos = match(waiting, fresh)
    for it, v, _ in pairs:
        apply_video(it, v)
    # refresh items already linked (scheduled → published, or locked)
    by_id = {v["id"]: v for v in videos}
    for it in q.items(channel_id, ("scheduled", "locked_private")):
        v = by_id.get(it.youtube_video_id)
        if v:
            status, msg = classify(v, as_utc(it.slot_utc) if it.slot_utc else None)
            if status != it.status:
                it.status, it.error = status, msg
                if status == "published":
                    it.published_at = it.slot_utc
                q.save(it)
    return {
        "matched": [{"queue_id": it.id, "video_id": v["id"], "title": v["snippet"]["title"], "score": s} for it, v, s in pairs],
        "unmatched_items": [{"queue_id": it.id, "title": it.title} for it in left_items],
        "unmatched_videos": [{"video_id": v["id"], "title": v.get("snippet", {}).get("title", ""),
                              "duration": yt.parse_duration(v.get("contentDetails", {}).get("duration", ""))}
                             for v in left_videos if yt.parse_duration(v.get("contentDetails", {}).get("duration", "")) <= 180],
    }


def link(item_id: int, video_id: str, cfg: Config | None = None, client: yt.YouTubeClient | None = None) -> db.QueueItem:
    """Manual linking for anything the fuzzy match missed."""
    it = q.get(item_id)
    if not it:
        raise q.QueueError("Unknown queue item")
    client = client or yt.client_for(it.channel_id, cfg or load_config())
    others = [o for o in q.items(it.channel_id) if o.youtube_video_id == video_id and o.id != it.id]
    if others:
        raise q.QueueError(f"That video is already linked to another clip (“{others[0].title}”).")
    vids = client.videos([video_id])
    if not vids:
        raise q.QueueError("That video wasn't found on YouTube.")
    owner = vids[0].get("snippet", {}).get("channelId")
    if owner and owner != it.channel_id:
        raise q.QueueError("That video belongs to a different channel.")
    return apply_video(it, vids[0])
