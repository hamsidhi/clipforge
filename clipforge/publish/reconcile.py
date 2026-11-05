"""Match videos you uploaded yourself (assisted mode) to queue items.

Signals, strongest first (each only when unambiguous):
  1. the original upload file name (`fileDetails.fileName`, visible to the channel owner): the clip file
     `NN_slug.mp4` from `workspace/<id>/clips/` or the batch copy `NN_<date>_<time>_slug.mp4`;
  2. the title, with hashtags (`#shorts`) stripped: similarity ≥ 0.8, or all words of the shorter title contained
     in the longer one (you shortened it in Studio), within ±2.5 s duration.
Read-only API calls only (playlistItems.list + videos.list), so it works for any API project.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import re

from clipforge.api import db
from clipforge.config import Config, load_config
from clipforge.publish import channels as chs
from clipforge.publish import queue as q
from clipforge.publish import youtube as yt
from clipforge.publish.timez import as_utc, now_utc

TITLE_MIN = 0.8
FILE_MATCH = 2.0  # score for an exact file-name match (beats any title score)
DURATION_TOL = 2.5  # seconds (Studio re-encodes; durations differ slightly)
WAITING = ("queued", "needs_manual", "missed", "failed")


def _norm(t: str) -> str:
    t = re.sub(r"#\w+", " ", (t or "").lower())  # hashtags, incl. #shorts
    return re.sub(r"\W+", " ", t).strip()


def _words(t: str) -> set[str]:
    return {w for w in _norm(t).split() if len(w) > 1 or w.isdigit()}


def title_score(a: str, b: str) -> float:
    ratio = difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()
    wa, wb = _words(a), _words(b)
    small, big = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    contained = 0.0
    if len(small) >= 2 and small <= big:
        contained = 0.85 + 0.1 * len(small) / max(1, len(big))  # all words of the shorter title appear
    return max(ratio, contained)


def file_names(it: db.QueueItem, cfg: Config) -> set[str]:
    """File names this clip could have been uploaded from."""
    names: set[str] = set()
    try:
        names.add(q.clip_file(it, cfg).name.lower())
    except Exception:
        pass
    if it.batch:
        try:
            manifest = json.loads((cfg.workspace / it.batch / "batch.json").read_text(encoding="utf-8"))
            names |= {e["file"].lower() for e in manifest.get("items", []) if e.get("id") == it.id and e.get("file")}
        except (OSError, ValueError, KeyError):
            pass
    return names


def _duration(video: dict) -> float:
    return yt.parse_duration(video.get("contentDetails", {}).get("duration", ""))


def match_score(item: db.QueueItem, video: dict, names: set[str] | None = None) -> float:
    fname = (video.get("fileDetails", {}).get("fileName") or "").lower()
    if names and fname and fname in names:
        return FILE_MATCH
    dur = _duration(video)
    if item.duration and dur and abs(item.duration - dur) > DURATION_TOL:
        return 0.0
    return title_score(item.title, video.get("snippet", {}).get("title", ""))


def match(items: list[db.QueueItem], videos: list[dict], names: dict[int, set[str]] | None = None,
          ) -> tuple[list[tuple[db.QueueItem, dict, float]], list[db.QueueItem], list[dict]]:
    """Greedy best-first matching. Returns (pairs, unmatched_items, unmatched_videos)."""
    names = names or {}
    scored = sorted(((match_score(it, v, names.get(it.id or -1)), i, j) for i, it in enumerate(items)
                     for j, v in enumerate(videos)), key=lambda x: x[0], reverse=True)
    used_i: set[int] = set()
    used_j: set[int] = set()
    pairs = []
    for s, i, j in scored:
        if s < TITLE_MIN:
            break
        if i in used_i or j in used_j:
            continue
        # ambiguous title match (another unused item/video scores the same): leave it for a manual Link
        if s < FILE_MATCH and any(s2 == s and ((i2 == i) != (j2 == j)) and i2 not in used_i and j2 not in used_j
                                  for s2, i2, j2 in scored if (i2, j2) != (i, j)):
            continue
        used_i.add(i)
        used_j.add(j)
        pairs.append((items[i], videos[j], round(s, 3)))
    return pairs, [it for i, it in enumerate(items) if i not in used_i], [v for j, v in enumerate(videos) if j not in used_j]


def apply_video(it: db.QueueItem, video: dict) -> db.QueueItem:
    """Link a queue item to a YouTube video and adopt YouTube's state (its publishAt wins over our slot)."""
    from clipforge.publish import runner

    st = video.get("status", {})
    publish_at = st.get("publishAt")
    if publish_at:
        it.slot_utc = as_utc(dt.datetime.fromisoformat(publish_at.replace("Z", "+00:00")))
    it.youtube_video_id = video["id"]
    it.batch = it.batch  # kept: shows where it came from
    status, reason, msg = runner.classify_ex(video, None, "scheduled")
    it.yt_state_json = json.dumps(runner.yt_state(video))
    if status == "published":
        pub = video.get("snippet", {}).get("publishedAt")
        if pub:
            it.published_at = as_utc(dt.datetime.fromisoformat(pub.replace("Z", "+00:00")))
    elif status == "scheduled" and not publish_at:
        if st.get("privacyStatus") in ("private", "unlisted"):
            status, reason = "needs_manual", "not_scheduled_in_studio"
            msg = "Uploaded, but not scheduled yet — in Studio set Visibility → Schedule (or Public), then Sync."
    it.status, it.reason, it.error = status, reason, msg
    return q.save(it)


def sync(channel_id: str, cfg: Config | None = None, client: yt.YouTubeClient | None = None) -> dict:
    from clipforge.publish import runner

    cfg = cfg or load_config()
    ch = chs.get(channel_id)
    if not ch:
        raise q.QueueError("Unknown channel")
    client = client or yt.client_for(channel_id, cfg)
    ids = client.uploads(ch.uploads_playlist, 100) if ch.uploads_playlist else []
    videos = client.videos(ids) if ids else []
    known = {it.youtube_video_id for it in q.items(channel_id) if it.youtube_video_id}
    fresh = [v for v in videos if v["id"] not in known]
    waiting = [it for it in q.items(channel_id, WAITING) if not it.youtube_video_id and it.slot_utc]
    names = {it.id: file_names(it, cfg) for it in waiting}  # type: ignore[misc]
    pairs, left_items, left_videos = match(waiting, fresh, names)  # type: ignore[arg-type]
    for it, v, _ in pairs:
        apply_video(it, v)
    # refresh items already linked (scheduled → published, locked, time changed in Studio)
    by_id = {v["id"]: v for v in videos}
    now = now_utc()
    for it in q.items(channel_id, ("scheduled", "locked_private", "needs_manual")):
        v = by_id.get(it.youtube_video_id)
        if v and it.youtube_video_id:
            if it.status == "needs_manual":
                apply_video(it, v)
            else:
                expected = as_utc(it.slot_utc) if it.slot_utc and (it.privacy or "scheduled") == "scheduled" else None
                runner.apply_state(it, v, expected, None, now)
    shorts = [v for v in left_videos if _duration(v) <= 180]
    return {
        "matched": [{"queue_id": it.id, "video_id": v["id"], "title": v["snippet"]["title"], "score": s,
                     "by": "file name" if s >= FILE_MATCH else "title"} for it, v, s in pairs],
        "unmatched_items": [{"queue_id": it.id, "title": it.title} for it in left_items],
        "unmatched_videos": [{
            "video_id": v["id"], "title": v.get("snippet", {}).get("title", ""), "duration": _duration(v),
            "file_name": v.get("fileDetails", {}).get("fileName", ""),
            "suggestions": [{"queue_id": it.id, "title": it.title, "score": round(sc, 2)} for sc, it in
                            sorted(((match_score(it, v, names.get(it.id)), it) for it in left_items),  # type: ignore[arg-type]
                                   key=lambda x: x[0], reverse=True)[:3] if sc > 0.3],
        } for v in shorts],
    }


def parse_video_id(text: str) -> str:
    """A YouTube video id from an id or any URL form: watch?v=, youtu.be/, shorts/, live/, embed/, studio …/video/<id>."""
    import re
    from urllib.parse import parse_qs, urlparse

    t = (text or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", t):
        return t
    u = urlparse(t if "://" in t else "https://" + t)
    v = parse_qs(u.query).get("v")
    if v and re.fullmatch(r"[A-Za-z0-9_-]{11}", v[0]):
        return v[0]
    for part in reversed([p for p in u.path.split("/") if p]):
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", part):
            return part
    raise q.QueueError("That doesn't look like a YouTube video link or id.")


def link(item_id: int, video_id: str, cfg: Config | None = None, client: yt.YouTubeClient | None = None) -> db.QueueItem:
    """Manual linking for anything the automatic match missed. The video must exist and belong to the item's channel."""
    video_id = parse_video_id(video_id)
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
