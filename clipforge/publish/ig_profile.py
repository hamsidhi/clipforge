"""Instagram profile duplicate guard (2026-11-09): is this clip ALREADY on the profile?

Inside ClipForge, `dedup` already makes one clip = one publication per account. This module also checks what is
really on the profile, including Reels you posted by hand in assisted mode with your own caption. A caption check
alone can't catch those, so the check compares the VIDEO:

- fingerprint = duration + dHash (64-bit) of 3 frames at 25 / 50 / 75 % of the duration;
- same video if the durations differ by ≤ 0.6 s and the 3 frames differ by ≤ 12 bits on average (robust to
  Instagram's re-encode/rescale; different clips of the same podcast differ by 25+ bits);
- profile Reels are downloaded once (media_url, own media only) and their fingerprints cached per media id in
  workspace/accounts/instagram/<ig id>/profile_fp.json; clip fingerprints are cached by path + size + mtime.

Used right before every container upload (`ig_runner._start_upload`): a clip found on the profile becomes
SKIPPED_DUPLICATE with the Reel's link. It is never posted twice, and nothing is deleted. `scan()` is the report
behind `clipforge ig-dupes` and GET /api/accounts/{id}/ig-duplicates.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from clipforge.api import db
from clipforge.config import Config, load_config
from clipforge.utils.log import log

POSITIONS = (0.25, 0.5, 0.75)
MAX_DUR_DIFF = 0.6
MAX_BITS = 12


# ----------------------------------------------------------------------------- fingerprints
def _dhash(gray9x8: bytes) -> int:
    bits = 0
    for r in range(8):
        row = gray9x8[r * 9:(r + 1) * 9]
        for c in range(8):
            bits = (bits << 1) | (1 if row[c] > row[c + 1] else 0)
    return bits


def fingerprint_file(path: Path) -> dict:
    """{"dur": seconds, "hashes": [int, int, int]} — needs ffmpeg/ffprobe."""
    from clipforge.utils.ffmpeg import ffmpeg_bin, ffprobe_bin

    out = subprocess.run([ffprobe_bin(), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, timeout=60)
    dur = float((out.stdout or "0").strip() or 0)
    hashes = []
    for pos in POSITIONS:
        r = subprocess.run([ffmpeg_bin(), "-v", "error", "-ss", f"{dur * pos:.3f}", "-i", str(path), "-frames:v", "1",
                            "-vf", "scale=9:8:flags=area,format=gray", "-f", "rawvideo", "-"],
                           capture_output=True, timeout=60)
        if len(r.stdout) >= 72:
            hashes.append(_dhash(r.stdout[:72]))
    return {"dur": round(dur, 2), "hashes": hashes}


def same_video(a: dict, b: dict) -> bool:
    if not a or not b or not a.get("hashes") or len(a["hashes"]) != len(b.get("hashes") or []):
        return False
    if abs(float(a["dur"]) - float(b["dur"])) > MAX_DUR_DIFF:
        return False
    dist = [bin(x ^ y).count("1") for x, y in zip(a["hashes"], b["hashes"])]
    return sum(dist) / len(dist) <= MAX_BITS


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def same_caption(a: str, b: str) -> bool:
    """Exact same caption (normalized), only when long enough to be specific (a CTA alone never matches)."""
    na, nb = _norm(a), _norm(b)
    return len(na) >= 40 and na == nb


def download(url: str) -> Path:
    import httpx

    fd, name = tempfile.mkstemp(suffix=".mp4")
    import os

    os.close(fd)
    with httpx.stream("GET", url, timeout=httpx.Timeout(120, connect=20), follow_redirects=True) as r:
        r.raise_for_status()
        with open(name, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return Path(name)


# test hooks
_fingerprint: Callable[[Path], dict] = fingerprint_file
_download: Callable[[str], Path] = download


def set_hooks(fingerprint: Callable[[Path], dict] | None = None, downloader: Callable[[str], Path] | None = None) -> None:
    global _fingerprint, _download
    _fingerprint = fingerprint or fingerprint_file
    _download = downloader or download


# ----------------------------------------------------------------------------- caches
def _cache_path(ig_id: str, cfg: Config) -> Path:
    return cfg.workspace / "accounts" / "instagram" / ig_id / "profile_fp.json"


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def clip_fp(path: Path, cfg: Config) -> dict | None:
    p = cfg.workspace / "accounts" / "instagram" / "_clip_fp.json"
    cache = _load(p)
    st = path.stat()
    key = f"{path}|{st.st_size}|{int(st.st_mtime)}"
    if key not in cache:
        try:
            cache[key] = _fingerprint(path)
        except Exception as exc:
            log.warning(f"[ig-dupes] couldn't fingerprint {path.name}: {exc}")
            return None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache), encoding="utf-8")
    return cache[key]


def profile_fps(ig_id: str, media: list[dict], cfg: Config) -> dict[str, dict]:
    """media id → fingerprint for the profile's Reels/videos (downloads only media not seen before)."""
    p = _cache_path(ig_id, cfg)
    cache = _load(p)
    changed = False
    for m in media:
        mid = m.get("id")
        if not mid or mid in cache or not m.get("media_url"):
            continue
        if (m.get("media_type") or "VIDEO") not in ("VIDEO", "REELS") and m.get("media_product_type") != "REELS":
            continue
        tmp = None
        try:
            tmp = _download(m["media_url"])
            cache[mid] = _fingerprint(tmp)
            changed = True
        except Exception as exc:  # one unreadable Reel must not block the others
            log.warning(f"[ig-dupes] couldn't check Reel {mid}: {exc}")
        finally:
            if tmp is not None and tmp.exists() and tmp.parent == Path(tempfile.gettempdir()):
                tmp.unlink(missing_ok=True)
    if changed:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache), encoding="utf-8")
    return {mid: fp for mid, fp in cache.items() if fp}


# ----------------------------------------------------------------------------- checks
def find_on_profile(it: db.QueueItem, clip: Path, media: list[dict], fps: dict[str, dict], cfg: Config) -> dict | None:
    """The profile Reel that IS this clip (same video, or the very same caption), else None."""
    mine = clip_fp(clip, cfg)
    for m in media:
        if mine and same_video(mine, fps.get(m.get("id", ""), {})):
            return {**m, "why": "same video"}
    for m in media:
        if same_caption(m.get("caption", ""), it.ig_caption or ""):
            return {**m, "why": "same caption"}
    return None


def scan(ch: db.Channel, cfg: Config | None = None, limit: int = 50) -> dict:
    """Report: Reels on the profile, Reels posted more than once, and queue items already on the profile."""
    from clipforge.publish import instagram as ig
    from clipforge.publish import queue as q

    cfg = cfg or load_config()
    c, token, ig_id = ig.client(cfg), ig.page_token(ch, cfg), ig.ig_id_of(ch)
    media = c.recent_media(ig_id, token, limit)
    fps = profile_fps(ig_id, media, cfg)
    groups: list[list[dict]] = []
    for m in media:
        for g in groups:
            if same_video(fps.get(g[0]["id"], {}), fps.get(m["id"], {})) or same_caption(g[0].get("caption", ""), m.get("caption", "")):
                g.append(m)
                break
        else:
            groups.append([m])
    dup_groups = [[{"id": x["id"], "permalink": x.get("permalink", ""), "timestamp": x.get("timestamp", ""),
                    "caption": (x.get("caption") or "")[:80]} for x in g] for g in groups if len(g) > 1]
    on_profile, pending = [], []
    for it in q.items(ch.id, ("draft", "queued", "waiting_capacity", "needs_manual", "missed", "failed")):
        try:
            path = q.clip_file(it, cfg)
        except q.QueueError:
            continue
        hit = find_on_profile(it, path, media, fps, cfg)
        row = {"id": it.id, "title": it.title, "status": it.status,
               "slot_utc": it.slot_utc.isoformat() if it.slot_utc else None}
        if hit:
            on_profile.append({**row, "permalink": hit.get("permalink", ""), "why": hit["why"]})
        else:
            pending.append(row)
    posted = q.items(ch.id, ("published",))
    return {"profile_reels": len(media), "checked": len(fps), "duplicates_on_profile": dup_groups,
            "queue_already_on_profile": on_profile, "queue_not_on_profile": len(pending),
            "published_by_clipforge": len(posted)}
