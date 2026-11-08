"""Reach report for YouTube Shorts (2026-11-09): why do the Shorts get (no) views? Read-only, data first.

1. `collect()` — videos.list (status/statistics/contentDetails/processingDetails) for every upload of the channel and
   YouTube Analytics v2 (`reports.query`, ids=channel==MINE): per-video metrics (dimension `video` needs `sort` +
   `maxResults`), traffic sources per video (`insightTrafficSourceType`, filter `video==ID`), per day, country.
   Metric names checked against developers.google.com/youtube/analytics/metrics (engagedViews exists; "viewed vs
   swiped away" is NOT in the API — YouTube Studio → Content → a Short → Analytics → Engagement only).
   Analytics lags 2-3 days; lifetime views from videos.list are fresher.
2. `build()` (pure, unit-tested) — funnel, buckets, patterns in OUR data, compared with the channel's own median.
3. `render_md()` / `telegram_summary()`.

Bucket thresholds are ClipForge working rules (config `reach:`), not YouTube numbers.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import statistics
from pathlib import Path
from typing import Any

from clipforge.config import Config, load_config
from clipforge.utils.log import log

SHORTS_FEED = "SHORTS"
BUCKETS = ("invisible", "scheduled", "too_new", "no_test_traffic", "tested_rejected", "working")
BUCKET_TEXT = {
    "invisible": "invisible (private/locked/rejected/restricted)",
    "scheduled": "scheduled (private until its publish time — normal)",
    "too_new": "too new (< {h} h public)",
    "no_test_traffic": "no test traffic (< {v} views after {h} h)",
    "tested_rejected": "tested but rejected (feed views, weak retention)",
    "working": "working",
}
VIDEO_METRICS = ("views,engagedViews,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,comments,"
                 "shares,subscribersGained")


# ----------------------------------------------------------------------------- collect (Google, read-only)
def collect(channel_id: str, cfg: Config | None = None, now: dt.datetime | None = None, days: int = 28) -> dict:
    from googleapiclient.discovery import build as gbuild

    from clipforge.publish import channels as chs
    from clipforge.publish import youtube as yt

    cfg = cfg or load_config()
    now = now or dt.datetime.now(dt.timezone.utc)
    ch = chs.get(channel_id)
    c = yt.client_for(channel_id, cfg)
    ids = c.uploads(ch.uploads_playlist if ch and ch.uploads_playlist else "UU" + channel_id[2:], 500)
    raw: dict[str, Any] = {"channel_id": channel_id, "collected_at": now.isoformat(), "errors": {},
                           "videos": c.videos(ids) if ids else [], "channel": c.channel_stats()}
    ya = gbuild("youtubeAnalytics", "v2", credentials=yt.load_credentials(channel_id, cfg), cache_discovery=False)
    start, end = (now.date() - dt.timedelta(days=days)).isoformat(), now.date().isoformat()

    def q(name: str, **kw) -> dict | None:
        try:
            r = ya.reports().query(ids="channel==MINE", startDate=start, endDate=end, **kw).execute()
            raw[name] = {"headers": [h["name"] for h in r.get("columnHeaders", [])], "rows": r.get("rows") or []}
            return raw[name]
        except Exception as exc:  # a metric Google refuses for this channel: recorded, report goes on
            raw["errors"][name] = str(exc)[-300:]
            return None

    q("by_video", metrics=VIDEO_METRICS, dimensions="video", sort="-views", maxResults=200)
    q("by_day", metrics="views,engagedViews,averageViewPercentage,subscribersGained", dimensions="day", sort="day")
    q("traffic", metrics="views", dimensions="insightTrafficSourceType")
    q("country", metrics="views", dimensions="country", sort="-views", maxResults=10)
    raw["traffic_per_video"] = {}
    import time

    for v in raw["videos"]:
        if v.get("status", {}).get("privacyStatus") != "public":
            continue
        for attempt in range(3):  # Google answers "Internal error / backendError" now and then: retry, then skip
            try:
                r = ya.reports().query(ids="channel==MINE", startDate=start, endDate=end, metrics="views",
                                       dimensions="insightTrafficSourceType", filters=f"video=={v['id']}").execute()
                raw["traffic_per_video"][v["id"]] = r.get("rows") or []
                break
            except Exception as exc:
                if attempt == 2:
                    raw["errors"][f"traffic:{v['id']}"] = str(exc)[-200:]
                else:
                    time.sleep(2 * (attempt + 1))
    return raw


def queue_context(channel_id: str) -> dict[str, dict]:
    """youtube video id → what ClipForge knows about the clip (source episode, topic, score, duration, slot)."""
    from sqlmodel import select

    from clipforge.api import db

    out: dict[str, dict] = {}
    with db.session() as s:
        for it in s.exec(select(db.QueueItem).where(db.QueueItem.platform == "youtube")):
            if it.youtube_video_id:
                out[it.youtube_video_id] = {"episode": it.episode_title or it.video_id, "topic": it.topic or "",
                                            "score": it.score, "duration": it.duration, "queue_id": it.id}
    return out


# ----------------------------------------------------------------------------- build (pure)
def _dur(iso: str) -> float:
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", iso or "")
    return (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3) or 0)) if m else 0.0


def _t(s: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def _q(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    v = sorted(vals)
    k = (len(v) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 1)


def bucket_of(v: dict, now: dt.datetime, min_hours: float = 48, min_views: int = 50, min_view_pct: float = 70,
              min_feed_views: int = 20) -> str:
    st = v["status"]
    if st.get("uploadStatus") in ("rejected", "failed", "deleted") or v.get("blocked") or st.get("madeForKids"):
        return "invisible"
    if st.get("privacyStatus") != "public":
        pa = _t(st.get("publishAt"))
        return "scheduled" if pa and pa > now else "invisible"
    pub = v.get("public_at") or _t(v.get("publishedAt"))
    if pub and now - pub < dt.timedelta(hours=min_hours):
        return "too_new"
    feed, views = v.get("feed_views", 0), v.get("views", 0)
    if views < min_views and feed < min_feed_views:
        return "no_test_traffic"
    pct = v.get("avg_view_pct")
    if pct is not None and pct < min_view_pct:
        return "tested_rejected"
    return "working"


GUEST_WORDS = ("altman", "sam", "bryson", "huberman", "macron", "mel robbins", "raj", "tiger", "trump", "modi")


def title_features(title: str) -> dict[str, bool]:
    t = title.lower()
    return {"has_guest_name": any(w in t for w in GUEST_WORDS), "has_number": bool(re.search(r"\d", t)),
            "question": "?" in t, "has_#shorts": "#shorts" in t, "colon_style": ":" in t}


def _group(rows: list[dict], key, metric: str) -> list[dict]:
    groups: dict[str, list[float]] = {}
    for r in rows:
        k = key(r)
        if k is None or r.get(metric) is None:
            continue
        groups.setdefault(str(k), []).append(float(r[metric]))
    return [{"group": k, "n": len(v), "median": _q(v, 0.5), "enough": len(v) >= 5}
            for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))]


def dur_bucket(sec: float) -> str:
    return "≤25 s" if sec <= 25 else "26–35 s" if sec <= 35 else "36–45 s" if sec <= 45 else ">45 s"


def build(raw: dict, ctx: dict[str, dict] | None = None, now: dt.datetime | None = None,
          rules: dict | None = None) -> dict:
    """Pure: raw Google data (+ queue context) → report dict."""
    now = now or _t(raw.get("collected_at")) or dt.datetime.now(dt.timezone.utc)
    ctx = ctx or {}
    rules = {"min_hours": 48, "min_views": 50, "min_view_pct": 70.0, "min_feed_views": 20, **(rules or {})}
    an: dict[str, dict] = {}
    bv = raw.get("by_video") or {}
    for row in bv.get("rows", []):
        an[row[0]] = dict(zip(bv["headers"][1:], row[1:]))
    traffic = {vid: {r[0]: r[1] for r in rows} for vid, rows in (raw.get("traffic_per_video") or {}).items()}
    vids = []
    for v in raw.get("videos", []):
        st, stats, cd = v.get("status", {}), v.get("statistics", {}), v.get("contentDetails", {})
        a = an.get(v["id"], {})
        tr = traffic.get(v["id"], {})
        rr = cd.get("regionRestriction") or {}
        rec = {"id": v["id"], "title": v.get("snippet", {}).get("title", ""), "status": st,
               "publishedAt": v.get("snippet", {}).get("publishedAt"),
               "public_at": _t(st.get("publishAt")) if st.get("publishAt") else _t(v.get("snippet", {}).get("publishedAt")),
               "privacy": st.get("privacyStatus"), "upload": st.get("uploadStatus"),
               "reason": st.get("rejectionReason") or st.get("failureReason") or "",
               "license": st.get("license"), "made_for_kids": bool(st.get("madeForKids")),
               "self_declared_mfk": st.get("selfDeclaredMadeForKids"),
               "blocked": "IN" in (rr.get("blocked") or []) or (rr.get("allowed") is not None and "IN" not in rr["allowed"]),
               "region_restriction": rr, "content_rating": cd.get("contentRating") or {},
               "duration": _dur(cd.get("duration", "")), "views": int(stats.get("viewCount", 0)),
               "likes": int(stats.get("likeCount", 0)), "comments": int(stats.get("commentCount", 0)),
               "an_views": a.get("views"), "engaged_views": a.get("engagedViews"),
               "avg_view_pct": a.get("averageViewPercentage"), "avg_view_dur": a.get("averageViewDuration"),
               "shares": a.get("shares"), "subs": a.get("subscribersGained"),
               "feed_views": int(tr.get(SHORTS_FEED, 0)), "traffic": tr,
               "traffic_known": v["id"] in (raw.get("traffic_per_video") or {}), **ctx.get(v["id"], {})}
        rec["bucket"] = bucket_of(rec, now, **rules)
        pub = rec["public_at"]
        rec["hour_ist"] = ((pub + dt.timedelta(hours=5, minutes=30)).hour if pub else None)
        rec["age_h"] = round((now - pub).total_seconds() / 3600, 1) if pub else None
        rec.update(title_features(rec["title"]))
        vids.append(rec)
    public = [r for r in vids if r["privacy"] == "public"]
    old = [r for r in public if r["bucket"] != "too_new"]
    views_old = [r["views"] for r in old]
    buckets = {b: [r["id"] for r in vids if r["bucket"] == b] for b in BUCKETS}
    tr_all = {r[0]: r[1] for r in (raw.get("traffic") or {}).get("rows", [])}
    total_tr = sum(tr_all.values()) or 0
    days = (raw.get("by_day") or {}).get("rows", [])
    last_day = max((d[0] for d in days if d[1]), default=None)
    retention = [r for r in public if r["avg_view_pct"] is not None and (r["an_views"] or 0) > 0]
    funnel = {
        "uploaded": len(vids), "public": len(public), "scheduled": len(buckets["scheduled"]),
        "invisible": len(buckets["invisible"]),
        "with_feed_traffic": sum(1 for r in public if r["feed_views"] > 0),
        "traffic_unknown": sum(1 for r in public if not r["traffic_known"]),
        "feed_views": sum(r["feed_views"] for r in public),
        "views_total": sum(r["views"] for r in public),
        "views_per_short": {"median": _q(views_old, .5), "p25": _q(views_old, .25), "p75": _q(views_old, .75),
                            "n": len(views_old)},
        "avg_view_pct_median": _q([r["avg_view_pct"] for r in retention], .5),
        "engaged_ratio": (round(sum(r["engaged_views"] or 0 for r in retention) /
                                max(1, sum(r["an_views"] or 0 for r in retention)), 2) if retention else None),
        "likes": sum(r["likes"] for r in public), "comments": sum(r["comments"] for r in public),
        "traffic_share": {k: round(v / total_tr, 2) for k, v in sorted(tr_all.items(), key=lambda kv: -kv[1])} if total_tr else {},
        "analytics_through": last_day,
    }
    patterns = {
        "length_vs_view_pct": _group(retention, lambda r: dur_bucket(r["duration"]), "avg_view_pct"),
        "length_vs_views": _group(old, lambda r: dur_bucket(r["duration"]), "views"),
        "topic_vs_views": _group(old, lambda r: r.get("topic") or None, "views"),
        "episode_vs_views": _group(old, lambda r: (r.get("episode") or "")[:40] or None, "views"),
        "hour_ist_vs_views": _group(old, lambda r: r["hour_ist"], "views"),
        "guest_name_in_title_vs_views": _group(old, lambda r: r["has_guest_name"], "views"),
        "number_in_title_vs_views": _group(old, lambda r: r["has_number"], "views"),
    }
    checks = {
        "made_for_kids": [r["id"] for r in vids if r["made_for_kids"] or r["self_declared_mfk"]],
        "region_blocked_in_india": [r["id"] for r in vids if r["blocked"]],
        "rejected_or_failed": [r["id"] for r in vids if r["upload"] in ("rejected", "failed")],
        "private_past_publish_time": [r["id"] for r in vids if r["privacy"] != "public" and r["bucket"] == "invisible"],
    }
    return {"generated_at": now.isoformat(), "channel": raw.get("channel", {}), "rules": rules, "funnel": funnel,
            "buckets": {b: len(v) for b, v in buckets.items()}, "bucket_ids": buckets, "checks": checks,
            "patterns": patterns, "errors": raw.get("errors", {}),
            "videos": [{k: (v.isoformat() if isinstance(v, dt.datetime) else v) for k, v in r.items() if k != "status"}
                       for r in vids]}


# ----------------------------------------------------------------------------- output
def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:.0f}%"


def telegram_summary(rep: dict) -> str:
    f, b = rep["funnel"], rep["buckets"]
    vps = f["views_per_short"]
    feed = f["traffic_share"].get(SHORTS_FEED)
    lines = [
        "📊 YouTube reach report",
        f"{f['uploaded']} uploaded · {f['public']} public · {f['scheduled']} scheduled · {f['invisible']} invisible",
        f"Shorts feed tested {f['with_feed_traffic']}/{f['public']} public Shorts ({f['feed_views']} feed views"
        f"{'; ' + str(f['traffic_unknown']) + ' unknown' if f.get('traffic_unknown') else ''}); "
        f"feed = {_pct(feed)} of all views",
        f"Views per Short (≥48 h old): median {vps['median']}, p25 {vps['p25']}, p75 {vps['p75']} (n={vps['n']})",
        f"Buckets: no test traffic {b['no_test_traffic']}, tested-rejected {b['tested_rejected']}, working {b['working']}, "
        f"too new {b['too_new']}",
        f"Analytics data through {f['analytics_through'] or '–'} (Google lags 2–3 days).",
    ]
    return "\n".join(lines)


def render_md(rep: dict, findings: list[str] | None = None, changes: list[str] | None = None) -> str:
    f, b, r = rep["funnel"], rep["buckets"], rep["rules"]
    vps = f["views_per_short"]
    out = [f"# Reach report — YouTube Shorts ({rep['generated_at'][:16]} UTC)", "",
           f"Channel: {rep['channel'].get('subscribers', '?')} subscribers, {rep['channel'].get('views', '?')} lifetime views, "
           f"{rep['channel'].get('videos', '?')} public videos. Analytics data through **{f['analytics_through'] or '–'}** "
           "(YouTube Analytics lags 2–3 days; lifetime views below come from videos.list and are fresher).", ""]
    if findings:
        out += ["## Top findings", ""] + [f"{i}. {x}" for i, x in enumerate(findings, 1)] + [""]
    out += ["## Funnel", "", "| step | value |", "|---|---|",
            f"| uploaded (uploads playlist) | {f['uploaded']} |",
            f"| public | {f['public']} |",
            f"| scheduled (private until publishAt — normal) | {f['scheduled']} |",
            f"| invisible (private past publish time / rejected / blocked / made for kids) | {f['invisible']} |",
            f"| public Shorts with ANY Shorts-feed views | {f['with_feed_traffic']} |",
            f"| Shorts-feed views (all Shorts) | {f['feed_views']} |",
            f"| lifetime views (public Shorts) | {f['views_total']} |",
            f"| views per Short ≥{r['min_hours']} h old: median / p25 / p75 (n) | {vps['median']} / {vps['p25']} / {vps['p75']} ({vps['n']}) |",
            f"| average view % (median over Shorts with analytics) | {f['avg_view_pct_median']} |",
            f"| engaged views ÷ views | {f['engaged_ratio']} |",
            f"| likes / comments | {f['likes']} / {f['comments']} |", "",
            "Traffic sources (share of views, 28 days): " +
            (", ".join(f"{k} {_pct(v)}" for k, v in f["traffic_share"].items()) or "–"), "",
            "## Buckets", "",
            f"Rules (ClipForge working thresholds, `reach:` in config.yaml): too new < {r['min_hours']} h public; no test traffic "
            f"= < {r['min_views']} views and < {r['min_feed_views']} Shorts-feed views; tested-rejected = average view % "
            f"< {r['min_view_pct']}.", "", "| bucket | Shorts |", "|---|---|"]
    out += [f"| {BUCKET_TEXT[k].format(h=r['min_hours'], v=r['min_views'])} | {b[k]} |" for k in BUCKETS]
    out += ["", "## Visibility checks", ""]
    for k, ids in rep["checks"].items():
        out.append(f"- {k.replace('_', ' ')}: {len(ids)}" + (f" ({', '.join(ids[:10])})" if ids else ""))
    out += ["- copyright claims / restrictions: not exposed by the Data API for your own videos — check YouTube Studio → "
            "Content → **Restrictions** column.", "", "## Patterns in this channel's own data", "",
            "Groups with n < 5 are shown but marked — too few to conclude anything."]
    names = {"length_vs_view_pct": "clip length → average view %", "length_vs_views": "clip length → lifetime views",
             "topic_vs_views": "topic → views", "episode_vs_views": "source episode → views",
             "hour_ist_vs_views": "posting hour (IST) → views", "guest_name_in_title_vs_views": "guest name in title → views",
             "number_in_title_vs_views": "number in title → views"}
    for k, rows in rep["patterns"].items():
        out += ["", f"**{names.get(k, k)}**", "", "| group | n | median |", "|---|---|---|"]
        out += [f"| {x['group']} | {x['n']}{'' if x['enough'] else ' ⚠'} | {x['median']} |" for x in rows[:12]]
    if changes:
        out += ["", "## Changes applied (future clips only)", ""] + [f"- {c}" for c in changes]
    out += ["", "## Per Short", "", "| id | public (UTC) | len | views | feed | avg % | bucket | title |", "|---|---|---|---|---|---|---|---|"]
    for v in sorted(rep["videos"], key=lambda v: v.get("public_at") or ""):
        out.append(f"| {v['id']} | {(v.get('public_at') or '')[:16]} | {v['duration']:.0f} | {v['views']} | {v['feed_views']} | "
                   f"{'' if v['avg_view_pct'] is None else round(v['avg_view_pct'])} | {v['bucket']} | {v['title'][:45]} |")
    if rep.get("errors"):
        out += ["", "Queries Google refused: " + "; ".join(f"{k}: {v[:120]}" for k, v in rep["errors"].items())]
    return "\n".join(out) + "\n"


def reports_dir(cfg: Config | None = None) -> Path:
    p = (cfg or load_config()).workspace / "reports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def latest(cfg: Config | None = None) -> dict | None:
    p = reports_dir(cfg) / "reach_latest.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError):
        return None


def run(cfg: Config | None = None, now: dt.datetime | None = None, notify: bool = True) -> list[dict]:
    """Collect + build + save (workspace/reports/) for every confirmed YouTube channel; Telegram summary."""
    from clipforge.publish import channels as chs

    cfg = cfg or load_config()
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for ch in chs.all_channels():
        if ch.platform != "youtube" or ch.auth_status != "ok":
            continue
        rep = build(collect(ch.id, cfg, now), queue_context(ch.id), now, rules=cfg.reach.model_dump(include={
            "min_hours", "min_views", "min_view_pct", "min_feed_views"}))
        rep["channel_id"], rep["channel_title"] = ch.id, ch.title
        d = reports_dir(cfg)
        (d / f"reach_{now:%Y%m%d}.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
        (d / "reach_latest.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
        (d / "REACH_REPORT.md").write_text(render_md(rep), encoding="utf-8")
        out.append(rep)
        if notify:
            try:
                from clipforge.notify import telegram as tg

                tg.bot().notify("published", f"{ch.title}\n" + telegram_summary(rep), key=f"reach:{ch.id}:{now:%Y%m%d}")
            except Exception as exc:
                log.warning(f"[reach] Telegram summary not sent: {exc}")
    return out


def weekly_due(cfg: Config, now: dt.datetime) -> bool:
    if not cfg.reach.weekly_report:
        return False
    rep = latest(cfg)
    last = _t(rep.get("generated_at")) if rep else None
    return last is None or now - last >= dt.timedelta(days=7)
