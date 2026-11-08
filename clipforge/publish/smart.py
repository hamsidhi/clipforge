"""Smart daily plan (2026-11-09): slot times and the number of posts per day come from this account's own results.

Why this way:
- Neither platform's API gives "when my audience is online" in a usable form. YouTube Analytics has no hour
  dimension. Instagram `online_followers` isn't in Meta's current metric table, its time zone isn't documented and it
  needs ≥100 followers (checked 2026-11-09). So "when people are active" is learned from WHEN OUR POSTS DID WELL.
- Start: an assumed India curve (morning 07–10, lunch 12–14, evening 18–23 peak), a starting assumption, not data.
  As posts come in, each quarter-hour's value moves toward the performance of posts published near it.
- Per post: score = log(1 + views per day over its first 3 days) × engagement × retention, divided by the account's
  median, so 1.0 = a typical post. Recency: the last 5 posts weigh 1.0, older ones decay (half-life 7 days).
- Curve(t) = (1 − w)·prior(t) + w·perf(t) + κ/√(1 + n(t)), with perf(t) = kernel mean (σ = 60 min) of the
  post scores, n(t) = how much data is near t, and w = n/(n + 3). The κ term is exploration: times with little data
  get a bonus, so the plan keeps testing new hours instead of locking in early.
- Posts per day: as many as allowed (`*_max_per_day`). One step down per 25% drop of the last 5 posts' median
  score vs the 10 before (never below `*_min_per_day`). YouTube is also capped by `youtube.daily_safety_limit`.
- Slots: best quarter-hours inside the window, with a minimum gap, at least one in the morning and two in the
  evening. Days that already have posts keep them (the planner fits new posts around them), and a new day is planned
  with the newest data, so slots move from day to day.
"""

from __future__ import annotations

import datetime as dt
import math
import time
from statistics import median

from sqlmodel import select

from clipforge.api import db
from clipforge.config import Config
from clipforge.publish.timez import as_utc, utc_to_local

ZONE = "Asia/Kolkata"
BIN = 15                      # minutes per curve bin
NBINS = 1440 // BIN
SIGMA = 60.0                  # kernel width (minutes)
KAPPA = 0.15                  # exploration bonus
MORNING = (7 * 60, 11 * 60)   # IST minutes
EVENING = (18 * 60, 24 * 60 + 30)
# Assumed India activity by hour (relative). A starting point only — replaced by the account's data over time.
PRIOR_HOURLY = [0.35, 0.2, 0.1, 0.1, 0.1, 0.15, 0.35, 0.65, 0.85, 0.85, 0.7, 0.6,
                0.7, 0.75, 0.65, 0.6, 0.6, 0.7, 0.85, 0.95, 1.0, 1.0, 0.95, 0.7]
_cache: dict[str, tuple[float, dict]] = {}


def _min(hhmm: str) -> int:
    h, m = (int(x) for x in str(hhmm).split(":"))
    return h * 60 + m


def _fmt(m: int) -> str:
    m %= 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def prior_bins() -> list[float]:
    out = []
    for b in range(NBINS):
        t = b * BIN / 60
        h0 = int(t) % 24
        h1 = (h0 + 1) % 24
        f = t - int(t)
        out.append(PRIOR_HOURLY[h0] * (1 - f) + PRIOR_HOURLY[h1] * f)
    return out


# ----------------------------------------------------------------------------- per-post scores
def post_score(p: db.VideoPerf, now: dt.datetime) -> float | None:
    """Raw performance of one post; None while it's younger than 12 h (no signal yet)."""
    if not p.published_at:
        return None
    age_h = (as_utc(now) - as_utc(p.published_at)).total_seconds() / 3600
    if age_h < 12:
        return None
    views = max(int(p.views or 0), 0)
    per_day = views / max(min(age_h, 72) / 24, 0.5)
    eng = (int(p.likes or 0) + 2 * int(p.comments or 0) + 3 * int(p.shares or 0) + 3 * int(p.saves or 0)) / max(views, 1)
    eng_f = 1 + min(eng * 10, 1.0)                 # 0 → ×1, ≥10% engagement → ×2
    ret_f = 1.0 if p.avg_view_pct is None else 0.5 + min(float(p.avg_view_pct), 150) / 100
    return math.log1p(per_day) * eng_f * ret_f


def scored_posts(channel_id: str, now: dt.datetime, days: int = 45) -> list[dict]:
    """[{minute (IST, 0–1439), rel (1 = typical), weight, at}] newest first."""
    with db.session() as s:
        rows = s.exec(select(db.VideoPerf).where(db.VideoPerf.channel_id == channel_id)).all()
    pts = []
    for p in rows:
        sc = post_score(p, now)
        if sc is None or as_utc(now) - as_utc(p.published_at) > dt.timedelta(days=days):  # type: ignore[arg-type]
            continue
        loc = utc_to_local(as_utc(p.published_at), ZONE)  # type: ignore[arg-type]
        pts.append({"minute": loc.hour * 60 + loc.minute, "raw": sc, "at": as_utc(p.published_at)})
    pts.sort(key=lambda x: x["at"], reverse=True)
    med = median([x["raw"] for x in pts]) if pts else 0
    for i, x in enumerate(pts):
        x["rel"] = x["raw"] / med if med > 0 else (1.0 if x["raw"] == 0 else 2.0)
        age_d = (as_utc(now) - x["at"]).total_seconds() / 86400
        x["weight"] = 1.0 if i < 5 else 0.6 * 0.5 ** (age_d / 7)
    return pts


# ----------------------------------------------------------------------------- curve
def curve(pts: list[dict]) -> dict:
    """{value: [NBINS], data_weight: [NBINS], perf: [NBINS|None]} — pure."""
    prior = prior_bins()
    pmean = sum(prior) / len(prior)
    prior = [p / pmean for p in prior]                    # mean 1, same scale as relative post scores
    val, nw, perf = [], [], []
    for b in range(NBINS):
        m = b * BIN
        num = den = 0.0
        for x in pts:
            d = abs(x["minute"] - m)
            d = min(d, 1440 - d)
            k = math.exp(-0.5 * (d / SIGMA) ** 2) * x["weight"]
            num += k * x["rel"]
            den += k
        pm = num / den if den > 1e-9 else None
        w = den / (den + 3.0)
        v = (1 - w) * prior[b] + w * (pm if pm is not None else prior[b]) + KAPPA / math.sqrt(1 + den)
        val.append(v)
        nw.append(den)
        perf.append(pm)
    return {"value": val, "data_weight": nw, "perf": perf}


def account_curve(ch: db.Channel, now: dt.datetime) -> dict:
    key = ch.id
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    pts = scored_posts(ch.id, now)
    c = curve(pts)
    c["posts"] = pts
    _cache[key] = (time.time(), c)
    return c


def clear_cache() -> None:
    _cache.clear()


# ----------------------------------------------------------------------------- how many
def daily_count(platform: str, pts: list[dict], cfg: Config) -> tuple[int, str]:
    s = cfg.schedule
    lo, hi = (s.ig_min_per_day, s.ig_max_per_day) if platform == "instagram" else (s.yt_min_per_day, s.yt_max_per_day)
    if platform != "instagram":
        hi = min(hi, cfg.youtube.daily_safety_limit)
        lo = min(lo, hi)
    recent = [x["raw"] for x in pts[:5]]
    before = [x["raw"] for x in pts[5:15]]
    if len(recent) < 5 or len(before) < 5:
        return hi, f"{hi}/day: the maximum (not enough posts yet to judge a trend)"
    a, b = median(recent), median(before)
    if b <= 0 or a >= b * 0.75:
        return hi, f"{hi}/day: last 5 posts hold up (median score {a:.2f} vs {b:.2f} before)"
    steps = min(hi - lo, int((1 - a / b) / 0.25))
    n = max(lo, hi - max(1, steps))
    return n, f"{n}/day: last 5 posts dropped to {a / b:.0%} of the 10 before — posting a bit less"


# ----------------------------------------------------------------------------- which times
def window(platform: str, cfg: Config) -> tuple[int, int]:
    """(start, end) in minutes after midnight IST; end may be > 1440 (after midnight)."""
    s = cfg.schedule
    a, b = _min(s.smart_window[0]), _min(s.smart_window[1])
    if platform != "instagram":
        a, b = _min(s.smart_window_yt[0]), _min(s.smart_window_yt[1])
    return a, (b + 1440 if b <= a else b)


def pick_times(values: list[float], count: int, start: int, end: int, gap: int,
               morning: int = 1, evening: int = 2) -> list[int]:
    """Best `count` times (minutes, may exceed 1440) inside [start, end] at least `gap` apart, with at least
    `morning` slots in 07–11 and `evening` in 18–00:30 when the window allows. Pure."""
    cands = [(values[(m % 1440) // BIN], m) for m in range(start, end + 1, BIN)]

    def ok(m: int, chosen: list[int]) -> bool:
        return all(abs(m - c) >= gap for c in chosen)

    chosen: list[int] = []
    for zone, need in ((MORNING, morning), (EVENING, evening)):
        zc = sorted([c for c in cands if zone[0] <= c[1] <= zone[1]], reverse=True)
        for _, m in zc:
            if sum(1 for c in chosen if zone[0] <= c <= zone[1]) >= min(need, count - len(chosen)):
                break
            if ok(m, chosen):
                chosen.append(m)
    for _, m in sorted(cands, reverse=True):
        if len(chosen) >= count:
            break
        if m not in chosen and ok(m, chosen):
            chosen.append(m)
    return sorted(chosen)


def explore_shift(times: list[int], data_weight: list[float], day: dt.date, start: int, end: int, gap: int) -> list[int]:
    """Every day, move the slot with the least data around it by ±15/30 min (offset depends on the date), so the
    plan keeps testing nearby times and no two days are identical. Pure; respects window + gap."""
    if not times:
        return times
    i = min(range(len(times)), key=lambda k: (data_weight[(times[k] % 1440) // BIN], k))
    for off in ((15, -30, 30, -15), (-15, 30, -30, 15))[day.toordinal() % 2]:
        cand = times[i] + off
        if start <= cand <= end and all(abs(cand - t) >= gap for k, t in enumerate(times) if k != i):
            return sorted(times[:i] + [cand] + times[i + 1:])
    return times


def day_plan(ch: db.Channel, cfg: Config, day: dt.date, now: dt.datetime) -> dict:
    platform = ch.platform or "youtube"
    c = account_curve(ch, now)
    n, why = daily_count(platform, c["posts"], cfg)
    a, b = window(platform, cfg)
    gap = cfg.schedule.ig_min_gap_minutes if platform == "instagram" else cfg.schedule.yt_min_gap_minutes
    times = explore_shift(pick_times(c["value"], n, a, b, gap), c["data_weight"], day, a, b, gap)
    slots = [_fmt(m) for m in times]
    ranked = sorted(times, key=lambda m: c["value"][(m % 1440) // BIN], reverse=True)
    return {"slots": slots, "count": n, "why_count": why, "peak": [_fmt(m) for m in ranked[:max(2, n // 3)]],
            "posts_used": len(c["posts"])}


def day_slots(ch: db.Channel, cfg: Config, day: dt.date, now: dt.datetime) -> list[str]:
    return day_plan(ch, cfg, day, now)["slots"]


def explain(ch: db.Channel, cfg: Config, now: dt.datetime) -> dict:
    """For Health / Telegram: best hours by data, how many posts the plan is based on, and the count rule."""
    c = account_curve(ch, now)
    hourly = []
    for h in range(24):
        bins = range(h * 60 // BIN, (h + 1) * 60 // BIN)
        hourly.append({"hour": h, "value": round(sum(c["value"][b] for b in bins) / len(bins), 3),
                       "data": round(sum(c["data_weight"][b] for b in bins) / len(bins), 2)})
    plan = day_plan(ch, cfg, now.date(), now)
    top = sorted(hourly, key=lambda x: x["value"], reverse=True)[:5]
    return {"posts_used": len(c["posts"]), "best_hours_ist": [f"{x['hour']:02d}:00" for x in top],
            "count": plan["count"], "why_count": plan["why_count"], "slots_today": plan["slots"],
            "hourly": hourly, "rule": "slots = best quarter-hours of (assumed India curve → your posts' results "
                                      "by hour) + exploration bonus where data is thin"}
