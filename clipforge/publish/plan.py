"""Daily publishing plan (Publishing v2). Deterministic: config + DB numbers only, never the LLM.

Instagram: 3 morning slots + 4–6 evening slots (18:00 → 01:30), 7 a day by default, up to 9 when enough approved
clips are ready to keep a day of buffer. YouTube: 5 slots a day, 6 (adds `yt_sixth_slot`) when the last 7 days'
median 24-h views per Short ≥ the previous 7 days'; never more than `youtube.daily_safety_limit`.

Times are IST in config/UI and UTC in the DB. A slot earlier than `schedule.day_cutoff` (05:00) belongs to the
evening before: the "Thu" plan's 00:45 is stored as Fri 00:45 IST (= Thu 19:15 UTC).

Adaptation (after `adapt_min_posts` posts with 24-h views): score(t) = 0.7·median(views_24h of posts published
within ±45 min of t in the last 14 days) + 0.3·median(older ones). Each slot moves ±30 or ±60 min only toward a
time that scores ≥10% better, stays inside its window and keeps the minimum gap. One slot a day is "exploration"
(±30 min). Applied weekly when `schedule.auto_adapt` (else shown as a suggestion only).
"""

from __future__ import annotations

import datetime as dt
from statistics import median

from sqlmodel import select

from clipforge.api import db
from clipforge.config import Config
from clipforge.publish.timez import as_utc, local_to_utc, utc_to_local

ZONE = "Asia/Kolkata"
# windows in "minutes after the day cutoff" (05:00 → 0): handles slots after midnight
IG_WINDOWS = [("07:00", "12:00"), ("18:00", "01:30")]
YT_WINDOWS = [("11:30", "00:30")]
NEAR = 45  # minutes: a past post counts for a slot when published this close to it


def _min(hhmm: str) -> int:
    h, m = (int(x) for x in hhmm.split(":"))
    return h * 60 + m


def _fmt(m: int) -> str:
    m %= 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _rel(hhmm: str, cfg: Config) -> int:
    """Minutes after the day cutoff (so 00:45 sorts after 23:30)."""
    return (_min(hhmm) - _min(cfg.schedule.day_cutoff)) % 1440


def slot_datetime(day: dt.date, hhmm: str, cfg: Config, zone: str = ZONE) -> dt.datetime:
    """UTC time of slot `hhmm` of plan day `day` (after-midnight slots fall on the next calendar date)."""
    date = day + dt.timedelta(days=1) if _min(hhmm) < _min(cfg.schedule.day_cutoff) else day
    return local_to_utc(date, hhmm, zone)


def plan_day(t: dt.datetime, cfg: Config, zone: str = ZONE) -> dt.date:
    """Which plan day a UTC time belongs to (00:45 IST Friday → Thursday's plan)."""
    local = utc_to_local(as_utc(t), zone)
    return (local - dt.timedelta(minutes=_min(cfg.schedule.day_cutoff))).date()


def _windows(platform: str, cfg: Config) -> list[tuple[int, int]]:
    return [(_rel(a, cfg), _rel(b, cfg)) for a, b in (IG_WINDOWS if platform == "instagram" else YT_WINDOWS)]


def _inside(m: int, platform: str, cfg: Config) -> bool:
    return any(a <= m <= b for a, b in _windows(platform, cfg))


# ----------------------------------------------------------------------------- data
def _perfs(channel_id: str) -> list[db.VideoPerf]:
    with db.session() as s:
        rows = s.exec(select(db.VideoPerf).where(db.VideoPerf.channel_id == channel_id)).all()
    return [p for p in rows if p.published_at and p.views_24h is not None]


def youtube_daily_count(ch: db.Channel, cfg: Config, now: dt.datetime) -> int:
    """6 when the last 7 days' median 24-h views ≥ the previous 7 days' (both need ≥3 Shorts), else 5; capped by
    the daily safety limit."""
    now = as_utc(now)
    perfs = _perfs(ch.id)
    last = [p.views_24h for p in perfs if now - dt.timedelta(days=7) <= as_utc(p.published_at) < now]  # type: ignore[arg-type]
    prev = [p.views_24h for p in perfs
            if now - dt.timedelta(days=14) <= as_utc(p.published_at) < now - dt.timedelta(days=7)]  # type: ignore[arg-type]
    n = 6 if len(last) >= 3 and len(prev) >= 3 and median(last) >= median(prev) else 5  # type: ignore[type-var]
    return min(n, cfg.youtube.daily_safety_limit)


def instagram_daily_count(ch: db.Channel, cfg: Config, now: dt.datetime) -> int:
    """7 by default; 8 or 9 when the approved clips waiting (drafts ready + not yet posted) cover today AND one more
    day at that rate."""
    from clipforge.publish import queue as q

    waiting = [it for it in q.items(ch.id, ("draft", "queued", "waiting_capacity"))
               if it.status != "draft" or it.meta_status == "ready"]
    s = cfg.schedule
    n = s.ig_min_per_day
    for k in range(s.ig_max_per_day, s.ig_min_per_day, -1):
        if len(waiting) >= 2 * k:
            n = k
            break
    return n


# ----------------------------------------------------------------------------- slots of a day
def base_slots(platform: str, cfg: Config, count: int) -> list[str]:
    s = cfg.schedule
    if platform == "instagram":
        evening = max(0, min(len(s.ig_evening), count - len(s.ig_morning)))
        return list(s.ig_morning[:count]) + list(s.ig_evening[:evening])
    out = list(s.yt_slots[:count])
    if count > len(s.yt_slots) and s.yt_sixth_slot:
        out.append(s.yt_sixth_slot)
    return out


def day_slots(ch: db.Channel, cfg: Config, day: dt.date, now: dt.datetime) -> list[str]:
    """Slots (HH:MM IST) of one plan day for this account, adapted + one exploration slot when there's data."""
    platform = ch.platform or "youtube"
    count = instagram_daily_count(ch, cfg, now) if platform == "instagram" else youtube_daily_count(ch, cfg, now)
    slots = base_slots(platform, cfg, count)
    adapted = (ch.extra.get("adapted_slots") or {}) if cfg.schedule.auto_adapt else {}
    for old, new in adapted.items():
        if old in slots and new not in slots:
            slots[slots.index(old)] = new
    if ch.extra.get("adapt_ready"):
        slots = explore(slots, platform, cfg, day)
    return sorted(slots, key=lambda h: _rel(h, cfg))


def explore(slots: list[str], platform: str, cfg: Config, day: dt.date) -> list[str]:
    """One slot a day tries a nearby time (±30 min, alternating by date), if it stays inside its window + gap."""
    if not slots:
        return slots
    i = day.toordinal() % len(slots)
    step = 30 if day.toordinal() % 2 else -30
    cand = _rel(slots[i], cfg) + step
    others = [_rel(h, cfg) for j, h in enumerate(slots) if j != i]
    gap = round(min_gap_hours(platform, cfg) * 60)
    if _inside(cand, platform, cfg) and all(abs(cand - o) >= gap for o in others):
        out = list(slots)
        out[i] = _fmt(cand + _min(cfg.schedule.day_cutoff))
        return out
    return slots


def min_gap_hours(platform: str, cfg: Config) -> float:
    return cfg.schedule.ig_min_gap_minutes / 60 if platform == "instagram" else 1.25


def preview(ch: db.Channel, cfg: Config, now: dt.datetime, days: int = 2) -> list[dict]:
    """Today's and tomorrow's plan, for /health and the report: [{day, slots: [HH:MM], count}]."""
    today = plan_day(now, cfg)
    out = []
    for d in range(days):
        day = today + dt.timedelta(days=d)
        sl = day_slots(ch, cfg, day, now)
        out.append({"day": day.isoformat(), "label": day.strftime("%a %d %b"), "slots": sl, "count": len(sl)})
    return out


# ----------------------------------------------------------------------------- engagement adaptation
def score_at(m_rel: int, perfs: list[db.VideoPerf], cfg: Config, now: dt.datetime) -> float | None:
    recent, older = [], []
    for p in perfs:
        t = utc_to_local(as_utc(p.published_at), ZONE)  # type: ignore[arg-type]
        pm = _rel(f"{t.hour:02d}:{t.minute:02d}", cfg)
        d = min(abs(pm - m_rel), 1440 - abs(pm - m_rel))
        if d <= NEAR:
            (recent if as_utc(now) - as_utc(p.published_at) <= dt.timedelta(days=14) else older).append(p.views_24h)  # type: ignore[arg-type]
    if not recent and not older:
        return None
    if recent and older:
        return 0.7 * median(recent) + 0.3 * median(older)  # type: ignore[type-var]
    return float(median(recent or older))  # type: ignore[type-var]


def adapt(ch: db.Channel, cfg: Config, now: dt.datetime) -> dict:
    """Suggested slot moves for this account: {ready, samples, changes: {old: new}, rule}."""
    platform = ch.platform or "youtube"
    perfs = _perfs(ch.id)
    rule = (f"After {cfg.schedule.adapt_min_posts}+ posts with 24-h views: a slot moves ±30/60 min toward a time "
            "with ≥10% better median 24-h views (last 14 days weigh 70%), inside its window.")
    if len(perfs) < cfg.schedule.adapt_min_posts:
        return {"ready": False, "samples": len(perfs), "changes": {}, "rule": rule}
    count = cfg.schedule.ig_max_per_day if platform == "instagram" else 6
    slots = base_slots(platform, cfg, count)
    gap = round(min_gap_hours(platform, cfg) * 60)
    changes: dict[str, str] = {}
    taken = [_rel(h, cfg) for h in slots]
    for i, h in enumerate(slots):
        cur = _rel(h, cfg)
        base = score_at(cur, perfs, cfg, now)
        best, best_score = cur, base
        for step in (-60, -30, 30, 60):
            c = cur + step
            if not _inside(c, platform, cfg) or any(abs(c - o) < gap for j, o in enumerate(taken) if j != i):
                continue
            sc = score_at(c, perfs, cfg, now)
            if sc is not None and (best_score is None or sc >= (best_score or 0) * 1.10) and (base is None or sc > base * 1.10):
                best, best_score = c, sc
        if best != cur:
            changes[h] = _fmt(best + _min(cfg.schedule.day_cutoff))
            taken[i] = best
    return {"ready": True, "samples": len(perfs), "changes": changes, "rule": rule}


def weekly_apply(ch: db.Channel, cfg: Config, now: dt.datetime) -> dict | None:
    """Once a week: store the suggestion; apply it (adapted_slots) unless schedule.auto_adapt is false."""
    import json

    from clipforge.publish import channels as chs
    from clipforge.publish.timez import iso_utc

    ex = ch.extra
    last = ex.get("adapt_checked_at")
    if last and as_utc(now) - as_utc(dt.datetime.fromisoformat(last.replace("Z", "+00:00"))) < dt.timedelta(days=7):
        return None
    res = adapt(ch, cfg, now)
    ex = {**ex, "adapt_checked_at": iso_utc(now), "adapt_ready": res["ready"], "adapt_suggestion": res["changes"]}
    if res["ready"] and cfg.schedule.auto_adapt:
        ex["adapted_slots"] = res["changes"]
    chs.update(ch.id, extra_json=json.dumps(ex))
    return res
