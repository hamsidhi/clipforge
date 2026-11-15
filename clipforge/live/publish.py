"""Posting live clips — ONLY to accounts switched on for live clips (Live page → Accounts; settings `content:
gaming`), never the podcast accounts.

Uses the regular queue (dedup pub_key, YouTube limits/ledger, Instagram resumable upload, the state machine). Every
caption credits the streamer; while the stream is live it says "🔴 <streamer> is LIVE on <platform> → link", after it
ends not-yet-uploaded clips get "Full stream: …" instead. At most one post per platform per `live.min_post_gap_min`
while live; the rest queue.
"""

from __future__ import annotations

import datetime as dt
import json

from clipforge.config import Config
from clipforge.live import store
from clipforge.utils.log import log

LIVE_PREFIX = "🔴 LIVE now"
VOD_PREFIX = "Full stream:"


def is_gaming(ch, cfg: Config | None = None) -> bool:
    return str((ch.settings or {}).get("content", "")).lower() == "gaming"


def gaming_accounts(platform: str = "both") -> list:
    from clipforge.services import publishing as pub

    return [c for c in pub.postable_accounts(platform) if is_gaming(c)]


def platform_name(url: str) -> str:
    u = url.lower()
    return "Twitch" if "twitch.tv" in u else "Kick" if "kick.com" in u else "YouTube" if "youtu" in u else "stream"


def link_line(sess: dict) -> str:
    url = sess.get("stream_url", "")
    if not url:
        return ""
    who = sess.get("streamer") or ""
    if sess.get("live", False):
        return f"{LIVE_PREFIX}: {who} on {platform_name(url)} → {url}" if who else f"{LIVE_PREFIX} on {platform_name(url)} → {url}"
    return f"{VOD_PREFIX} {sess.get('vod_url') or url}"


def caption(title: str, tags: list[str], sess: dict) -> str:
    line = link_line(sess)
    credit = f"🎥 Credit: {sess['streamer']}" if sess.get("streamer") else ""
    return "\n\n".join(x for x in (title, "\n".join(y for y in (line, credit) if y), " ".join(tags)) if x)


def swap_line(text: str, sess: dict) -> str:
    """Replace the LIVE line with the VOD line (stream ended)."""
    out = []
    for ln in text.splitlines():
        if ln.startswith(LIVE_PREFIX):
            new = link_line(sess)
            if new:
                out.append(new)
            continue
        out.append(ln)
    return "\n".join(out)


def _last_post(sess: dict, platform: str) -> dt.datetime | None:
    t = (sess.get("last_post") or {}).get(platform)
    return dt.datetime.fromisoformat(t) if t else None


def post_clip(cfg: Config, sid: str, rank: int, mode: str = "now", title: str | None = None,
              now: dt.datetime | None = None) -> list[str]:
    """Add the clip to every gaming account. mode now = publish now (respecting the live post gap), queue = the
    accounts' normal plan. Returns one human line per account."""
    from clipforge.publish import queue as q
    from clipforge.publish.timez import now_utc
    from clipforge.services import publishing as pub

    now = now or now_utc()
    sess = store.load_session(cfg, sid)
    clip = store.get_clip(cfg, sid, rank)
    if clip.get("status") not in ("ready", "posted", "queued"):
        raise ValueError(f"Clip #{rank} isn't ready ({clip.get('status')}).")
    accounts = gaming_accounts()
    if not accounts:
        raise ValueError("No account is switched on for live clips. Live page → Accounts: switch on the YouTube/"
                         "Instagram account(s) for these clips (podcast accounts never get them unless you do).")
    title = (title or clip.get("title") or "Live highlight")[:95]
    tags = clip.get("hashtags") or ["#gaming", "#shorts"]
    lines: list[str] = []
    posted: dict = dict(clip.get("posted") or {})
    for ch in accounts:
        plat = ch.platform or "youtube"
        if ch.id in posted:
            lines.append(f"{ch.title}: already added (queue #{posted[ch.id]})")
            continue
        try:
            items = q.add_clips(ch.id, sid, [rank], cfg)
        except Exception as exc:
            lines.append(f"{ch.title}: {exc}")
            continue
        if not items:
            lines.append(f"{ch.title}: skipped (duplicate)")
            continue
        it = items[0]
        cap = caption(title, tags, sess)
        it.title, it.description, it.ig_caption = title, cap, cap
        it.tags_json = json.dumps([t.lstrip("#") for t in tags])
        it.meta_status, it.status, it.category_id = "ready", "queued", "20"  # 20 = Gaming
        it.pin_comment = "Would you have clutched this? 👇"
        q.save(it)
        posted[ch.id] = it.id
        gap = dt.timedelta(minutes=cfg.live.min_post_gap_min)
        last = _last_post(sess, plat)
        try:
            if mode == "now" and sess.get("live") and last and now - last < gap:
                at = last + gap
                # posted AT that time (YouTube: uploaded public at the slot; Instagram: container before it) — the
                # normal "≥ 30 min ahead" rule of q.move is for private+publishAt uploads, not for live posts
                it = q.get(it.id) or it
                it.slot_utc, it.status, it.reason = at, "queued", ""
                if plat == "youtube":
                    it.privacy = "public"
                q.save(it)
                lines.append(f"{ch.title}: scheduled {at.astimezone().strftime('%H:%M')} (live post gap {cfg.live.min_post_gap_min} min)")
            elif mode == "now":
                pub.do_publish_now(it.id)
                lines.append(f"{ch.title}: posting now")
            else:
                q.autoschedule(ch.id, [it.id], cfg)
                lines.append(f"{ch.title}: queued for the next slot")
                continue
            sess.setdefault("last_post", {})[plat] = (last + gap if (last and now - last < gap and sess.get("live"))
                                                      else now).isoformat()
        except Exception as exc:  # e.g. YouTube unverified lock → stays queued, honestly reported
            lines.append(f"{ch.title}: {getattr(exc, 'detail', None) or exc} — left in the queue")
            try:
                q.autoschedule(ch.id, [it.id], cfg)
            except Exception as exc2:
                log.warning(f"[live] autoschedule after failed publish: {exc2}")
    store.update_clip(cfg, sid, rank, posted=posted, status="posted" if mode == "now" else "queued", title=title)
    s2 = store.load_session(cfg, sid)
    s2["last_post"] = {**(s2.get("last_post") or {}), **(sess.get("last_post") or {})}
    store.save_session(cfg, sid, s2)
    return lines


def stream_ended(cfg: Config, sid: str, vod_url: str = "") -> int:
    """Swap the LIVE line for the VOD line on clips not uploaded yet. Returns how many queue items changed."""
    from clipforge.publish import queue as q

    sess = store.load_session(cfg, sid)
    sess["live"] = False
    if vod_url:
        sess["vod_url"] = vod_url
    store.save_session(cfg, sid, sess)
    n = 0
    for c in sess.get("clips", []):
        for item_id in (c.get("posted") or {}).values():
            it = q.get(int(item_id))
            if not it or it.youtube_video_id or it.ig_media_id or it.ig_container_id:
                continue
            new_desc, new_cap = swap_line(it.description, sess), swap_line(it.ig_caption, sess)
            if new_desc != it.description or new_cap != it.ig_caption:
                it.description, it.ig_caption = new_desc, new_cap
                q.save(it)
                n += 1
    return n


def vod_url_for(stream_url: str, video_id: str = "") -> str:
    """Where the full stream lives afterwards: YouTube → the broadcast's watch URL; Twitch/Kick → the channel's videos."""
    u = stream_url.rstrip("/")
    if u.endswith("/live"):
        u = u[: -len("/live")]
    if "twitch.tv" in u:
        return f"https://www.twitch.tv/{u.rsplit('/', 1)[-1]}/videos"
    if "kick.com" in u:
        return f"https://kick.com/{u.rsplit('/', 1)[-1]}/videos"
    if video_id and ("youtube" in u or "youtu.be" in u):
        return f"https://www.youtube.com/watch?v={video_id}"
    return stream_url
