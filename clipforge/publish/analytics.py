"""Stats sync (Data API + Analytics API), best-time learner, YPP progress and "what's working"."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from statistics import mean, median

from sqlmodel import select

from clipforge.api import db
from clipforge.config import Config, YppConfig
from clipforge.publish import channels as chs
from clipforge.publish import youtube as yt
from clipforge.publish.timez import PACIFIC, as_utc, utc_to_local
from clipforge.utils.log import log

SHORTS_MAX_SEC = 180  # YouTube Shorts can be up to 3 minutes
MIN_PUBLISHED_FOR_LEARNING = 20
MIN_SAMPLES_PER_HOUR = 4
WIN_MARGIN = 1.25  # a slot must beat the median hour by 25% to be recommended


def _parse_time(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ----------------------------------------------------------------------------- sync
def sync_channel(channel_id: str, client: yt.YouTubeClient, cfg: Config, now: dt.datetime, max_analytics: int = 30) -> dict:
    """Pull the latest uploads + stats into SQLite. Read-only — works for any API project."""
    ch = chs.get(channel_id)
    if not ch:
        return {}
    ids = client.uploads(ch.uploads_playlist, 200) if ch.uploads_playlist else []
    videos = client.videos(ids) if ids else []
    with db.session() as s:
        queue_by_vid = {it.youtube_video_id: it for it in s.exec(
            select(db.QueueItem).where(db.QueueItem.channel_id == channel_id)).all() if it.youtube_video_id}
        n_shorts = 0
        for v in videos:
            dur = yt.parse_duration(v.get("contentDetails", {}).get("duration", ""))
            if dur > SHORTS_MAX_SEC:
                continue
            n_shorts += 1
            st = v.get("statistics", {})
            sn = v.get("snippet", {})
            perf = s.get(db.VideoPerf, v["id"]) or db.VideoPerf(youtube_video_id=v["id"], channel_id=channel_id)
            perf.title = sn.get("title", "")
            # public: snippet.publishedAt is the publish time; scheduled: status.publishAt; plain private: none
            status = v.get("status", {})
            perf.published_at = (_parse_time(sn.get("publishedAt")) if status.get("privacyStatus") == "public"
                                 else _parse_time(status.get("publishAt")))
            perf.duration = dur
            perf.views, perf.likes = int(st.get("viewCount", 0)), int(st.get("likeCount", 0))
            perf.comments = int(st.get("commentCount", 0))
            perf.updated_at = db.now()
            if v["id"] in queue_by_vid:
                perf.queue_id = queue_by_vid[v["id"]].id
            s.add(perf)
            s.add(db.StatSnapshot(youtube_video_id=v["id"], taken_at=now, views=perf.views))
        stats = client.channel_stats()
        shorts_90 = shorts_28 = None
        try:
            shorts_28 = _shorts_views(client, now, 28)
            shorts_90 = _shorts_views(client, now, 90)
        except Exception as exc:  # Analytics API not enabled / not yet authorized: fall back to local numbers
            log.warning(f"YouTube Analytics unavailable ({exc}); using per-video totals instead")
        today = utc_to_local(now, chs.settings_of(ch, cfg)["timezone"]).date()
        row = s.exec(select(db.ChannelDaily).where(db.ChannelDaily.channel_id == channel_id,
                                                   db.ChannelDaily.day == today)).first() or \
            db.ChannelDaily(channel_id=channel_id, day=today)
        row.subscribers, row.total_views = stats["subscribers"], stats["views"]
        row.shorts_views_28d, row.shorts_views_90d = shorts_28, shorts_90
        row.uploads_90d = sum(1 for p in s.exec(select(db.VideoPerf).where(db.VideoPerf.channel_id == channel_id)).all()
                              if p.published_at and now - dt.timedelta(days=90) <= as_utc(p.published_at) <= now)
        s.add(row)
        s.commit()
    _per_video_analytics(channel_id, client, now, max_analytics)
    chs.update(channel_id, last_sync_at=now)
    return {"videos": len(videos), "shorts": n_shorts, "subscribers": stats["subscribers"]}


def _shorts_views(client: yt.YouTubeClient, now: dt.datetime, days: int) -> int:
    end = now.astimezone(PACIFIC).date()
    start = end - dt.timedelta(days=days - 1)
    rows = client.analytics(start.isoformat(), end.isoformat(), "views", dimensions="creatorContentType")
    return int(sum(r[1] for r in rows if str(r[0]).upper() == "SHORTS"))


def _per_video_analytics(channel_id: str, client: yt.YouTubeClient, now: dt.datetime, limit: int) -> None:
    """views in the first 24 h / 72 h (Analytics days ≈ Pacific days), average view %, subscribers gained."""
    with db.session() as s:
        perfs = [p for p in s.exec(select(db.VideoPerf).where(db.VideoPerf.channel_id == channel_id)).all()
                 # Analytics data lags 1–3 days: wait 5 days so the first 3 days are complete
                 if p.published_at and as_utc(p.published_at) <= now - dt.timedelta(days=5) and p.views_72h is None]
        for p in perfs[:limit]:
            pub_day = as_utc(p.published_at).astimezone(PACIFIC).date()
            try:
                rows = client.analytics(pub_day.isoformat(), (pub_day + dt.timedelta(days=2)).isoformat(), "views",
                                        dimensions="day", filters=f"video=={p.youtube_video_id}")
                daily = [int(r[1]) for r in sorted(rows, key=lambda r: r[0])]
                if len(daily) < 3:
                    continue  # incomplete data: leave None so the next sync retries
                p.views_24h = daily[0]
                p.views_72h = sum(daily[:3])
                tot = client.analytics(pub_day.isoformat(), now.astimezone(PACIFIC).date().isoformat(),
                                       "averageViewPercentage,subscribersGained", filters=f"video=={p.youtube_video_id}")
                if tot:
                    p.avg_view_pct, p.subs_gained = float(tot[0][0]), int(tot[0][1])
            except Exception as exc:
                log.debug(f"analytics for {p.youtube_video_id} failed: {exc}")
                _fallback_from_snapshots(s, p)
            s.add(p)
        s.commit()


def _fallback_from_snapshots(s, p: db.VideoPerf) -> None:
    snaps = list(s.exec(select(db.StatSnapshot).where(db.StatSnapshot.youtube_video_id == p.youtube_video_id)).all())
    pub = as_utc(p.published_at)
    for hours, attr in ((24, "views_24h"), (72, "views_72h")):
        target = pub + dt.timedelta(hours=hours)
        near = [x for x in snaps if abs(as_utc(x.taken_at) - target) <= dt.timedelta(hours=8)]
        if near:
            setattr(p, attr, min(near, key=lambda x: abs(as_utc(x.taken_at) - target)).views)


# ----------------------------------------------------------------------------- best time
def heatmap(channel_id: str, zone: str = "Asia/Kolkata") -> dict:
    """Average views at 24 h by (weekday, hour) of publishing, in local time."""
    with db.session() as s:
        perfs = [p for p in s.exec(select(db.VideoPerf).where(db.VideoPerf.channel_id == channel_id)).all()
                 if p.published_at and p.views_24h is not None]
    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    hours: dict[int, list[int]] = defaultdict(list)
    for p in perfs:
        t = utc_to_local(p.published_at, zone)
        cells[(t.weekday(), t.hour)].append(p.views_24h or 0)
        hours[t.hour].append(p.views_24h or 0)
    return {
        "samples": len(perfs),
        "cells": [{"weekday": w, "hour": h, "avg": round(mean(v)), "n": len(v)} for (w, h), v in sorted(cells.items())],
        "hours": [{"hour": h, "avg": round(mean(v)), "n": len(v)} for h, v in sorted(hours.items())],
    }


def recommend_slots(hm: dict, current: list[str], posts_per_day: int) -> dict:
    """Recommend publish hours only with enough evidence. Rule (also shown in the UI):
    ≥20 published Shorts with 24 h views; an hour needs ≥4 Shorts; recommended hours beat the median hour
    by ≥25%. Never applied automatically."""
    rule = (f"Needs {MIN_PUBLISHED_FOR_LEARNING}+ published Shorts with 24-hour views; an hour counts only with "
            f"{MIN_SAMPLES_PER_HOUR}+ Shorts; a recommended hour must beat the typical hour by {int((WIN_MARGIN - 1) * 100)}%.")
    if hm["samples"] < MIN_PUBLISHED_FOR_LEARNING:
        return {"ready": False, "rule": rule, "message": f"{hm['samples']}/{MIN_PUBLISHED_FOR_LEARNING} Shorts measured so far.",
                "slots": []}
    eligible = [h for h in hm["hours"] if h["n"] >= MIN_SAMPLES_PER_HOUR]
    if len(eligible) < 2:
        return {"ready": False, "rule": rule, "message": "Not enough Shorts per hour yet — keep posting at varied times.",
                "slots": []}
    typical = median(h["avg"] for h in eligible)
    winners = sorted((h for h in eligible if h["avg"] >= typical * WIN_MARGIN), key=lambda h: -h["avg"])[:posts_per_day]
    slots = sorted(f"{h['hour']:02d}:00" for h in winners)
    current_hours = {int(c.split(":")[0]) for c in current}
    if not slots or {int(s[:2]) for s in slots} <= current_hours:
        return {"ready": True, "rule": rule, "message": "Your current slots are already among the best times.", "slots": []}
    return {"ready": True, "rule": rule, "slots": slots,
            "message": "These hours clearly beat the rest: " + ", ".join(
                f"{h['hour']:02d}:00 ({h['avg']} views/24h, {h['n']} Shorts)" for h in winners)}


# ----------------------------------------------------------------------------- YPP
def ypp_targets(y: YppConfig, today: dt.date) -> dict:
    new_rule = today >= dt.date.fromisoformat(y.new_rule_date)
    return {
        "ads": {"label": "Shorts ad revenue (YPP)", "subs": y.ad_subs,
                "shorts_views_90d": y.new_ad_shorts_views_90d if new_rule else y.ad_shorts_views_90d},
        "fan": {"label": "Fan funding (lower tier)", "subs": y.fan_subs, "shorts_views_90d": y.fan_shorts_views_90d,
                "uploads_90d": y.fan_uploads_90d},
        "rule_note": (f"New applicants need {y.new_ad_shorts_views_90d:,} Shorts views in 90 days from {y.new_rule_date}."
                      if not new_rule else f"Rule in force since {y.new_rule_date}."),
    }


def eta(current: float, target: float, per_day: float, today: dt.date) -> str | None:
    if current >= target:
        return "reached"
    if per_day <= 0:
        return None
    return (today + dt.timedelta(days=int((target - current) / per_day) + 1)).isoformat()


def ypp_progress(channel_id: str, y: YppConfig, today: dt.date) -> dict:
    with db.session() as s:
        rows = sorted(s.exec(select(db.ChannelDaily).where(db.ChannelDaily.channel_id == channel_id)).all(),
                      key=lambda r: r.day)
        perfs = list(s.exec(select(db.VideoPerf).where(db.VideoPerf.channel_id == channel_id)).all())
    targets = ypp_targets(y, today)
    last = rows[-1] if rows else None
    subs = last.subscribers if last else 0
    since = dt.datetime.combine(today - dt.timedelta(days=90), dt.time(), tzinfo=dt.timezone.utc)
    local90 = sum(p.views for p in perfs if p.published_at and as_utc(p.published_at) >= since)
    views90 = last.shorts_views_90d if last and last.shorts_views_90d is not None else local90
    uploads90 = last.uploads_90d if last and last.uploads_90d is not None else 0
    # pace: subscribers gained per day over the last 14 days of syncs; Shorts views per day from the 28-day window
    window = [r for r in rows if r.day >= today - dt.timedelta(days=14)]
    subs_rate = ((window[-1].subscribers - window[0].subscribers) / max(1, (window[-1].day - window[0].day).days)
                 if len(window) >= 2 else 0.0)
    views_rate = (last.shorts_views_28d / 28) if last and last.shorts_views_28d else views90 / 90
    # the 90-day window also drops old days: net growth ≈ new daily views − average daily views in the window
    net_views_rate = views_rate - views90 / 90
    out = {"subscribers": subs, "shorts_views_90d": views90, "uploads_90d": uploads90, "targets": targets,
           "subs_per_day": round(subs_rate, 1), "shorts_views_per_day": round(views_rate)}
    for key in ("ads", "fan"):
        t = targets[key]
        out[key] = {
            "subs_pct": min(100, round(100 * subs / t["subs"], 1)) if t["subs"] else 100,
            "views_pct": min(100, round(100 * views90 / t["shorts_views_90d"], 1)),
            "subs_eta": eta(subs, t["subs"], subs_rate, today),
            "views_eta": eta(views90, t["shorts_views_90d"], net_views_rate, today),
            "needed_views_per_day": round(t["shorts_views_90d"] / 90),
        }
    return out


# ----------------------------------------------------------------------------- what's working
def whats_working(channel_id: str) -> dict:
    with db.session() as s:
        perfs = list(s.exec(select(db.VideoPerf).where(db.VideoPerf.channel_id == channel_id)).all())
        qmap = {it.id: it for it in s.exec(select(db.QueueItem).where(db.QueueItem.channel_id == channel_id)).all()}

    def group(key) -> list[dict]:
        g: dict[str, list[int]] = defaultdict(list)
        for p in perfs:
            k = key(p)
            if k:
                g[k].append(p.views)
        return sorted(({"key": k, "avg_views": round(mean(v)), "n": len(v)} for k, v in g.items()),
                      key=lambda x: -x["avg_views"])

    def length_bucket(p: db.VideoPerf) -> str:
        return "≤25 s" if p.duration <= 25 else "26–40 s" if p.duration <= 40 else "41–60 s" if p.duration <= 60 else "60 s+"

    def title_pattern(p: db.VideoPerf) -> str:
        t = p.title
        if any(ch.isdigit() for ch in t):
            return "has a number"
        if "?" in t:
            return "question"
        return "statement"

    return {
        "top": [{"title": p.title, "views": p.views, "id": p.youtube_video_id}
                for p in sorted(perfs, key=lambda p: -p.views)[:10]],
        "by_topic": group(lambda p: qmap[p.queue_id].topic if p.queue_id in qmap else "")[:8],
        "by_episode": group(lambda p: qmap[p.queue_id].episode_title if p.queue_id in qmap else "")[:8],
        "by_length": group(length_bucket),
        "by_title": group(title_pattern),
        "avg_views": round(mean(p.views for p in perfs)) if perfs else 0,
        "shorts": len(perfs),
    }


def earnings(post: db.Post) -> float:
    views = post.approved_views if post.approved_views is not None else post.views
    return round(views / 1000 * post.rate_per_1k * (1 - post.campaign_fee_pct / 100), 2)
