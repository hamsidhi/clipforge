"""Pure slot planning: which free publish times exist, and which clip goes where.

Rules (auto-fill):
  1. Best clips first, but interleave episodes: never two clips of the same episode back-to-back when another
     episode is available, and never the same topic twice within 24 h.
  2. Never more than posts_per_day per channel per day, nor more than the daily capacity (upload quota).
  3. Peak slots get the strongest remaining clip; off-peak slots take a below-median clip when one is available.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from statistics import median
from typing import Callable, Iterator, Optional

from clipforge.publish.timez import as_utc, local_to_utc, utc_to_local

LEAD = dt.timedelta(minutes=30)  # never schedule closer than this to "now"


@dataclass
class Plan:
    zone: str = "Asia/Kolkata"
    slots: list[str] = field(default_factory=lambda: ["12:30", "18:30", "21:00"])
    peak_slots: list[str] = field(default_factory=lambda: ["18:30", "21:00"])
    posts_per_day: int = 3
    min_gap_hours: float = 2.5
    days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])  # Monday=0
    start_date: dt.date | None = None
    # Publishing v2 daily plan: day → that day's slots (HH:MM); slots before `cutoff` fall on the next date and
    # count for the evening before. None = the fixed `slots` list above.
    day_plan: Optional[Callable[[dt.date], list[str]]] = None
    cutoff: str = "05:00"

    @classmethod
    def from_settings(cls, s: dict) -> "Plan":
        sd = s.get("start_date")
        return cls(zone=s.get("timezone", "Asia/Kolkata"), slots=list(s.get("slots") or []),
                   peak_slots=list(s.get("peak_slots") or []), posts_per_day=int(s.get("posts_per_day", 3)),
                   min_gap_hours=float(s.get("min_gap_hours", 2.5)), days=list(s.get("days", range(7))),
                   start_date=dt.date.fromisoformat(sd) if sd else None)


@dataclass
class Occupied:
    """An already-planned post of this channel."""

    slot_utc: dt.datetime
    topic: str = ""
    episode: str = ""


@dataclass
class ClipRef:
    id: int
    score: float
    episode: str
    topic: str


def _topic(t: str) -> str:
    return " ".join(t.lower().split())


def free_slots(plan: Plan, taken: list[dt.datetime], now: dt.datetime, days_ahead: int = 60) -> Iterator[tuple[dt.datetime, bool]]:
    """Yield (slot_utc, is_peak) in time order that respect posts/day, min gap and allowed weekdays.

    `taken` is read live (UTC datetimes): the caller appends the slots it fills, skipped slots stay free."""
    now = as_utc(now)
    if plan.day_plan is not None:
        yield from _free_slots_daily(plan, taken, now, days_ahead)
        return
    today = utc_to_local(now, plan.zone).date()
    start = max(today, plan.start_date) if plan.start_date else today
    gap = dt.timedelta(hours=plan.min_gap_hours)
    for d in range(days_ahead):
        day = start + dt.timedelta(days=d)
        if day.weekday() not in plan.days:
            continue
        for hhmm in sorted(plan.slots):
            t = local_to_utc(day, hhmm, plan.zone)
            if t < now + LEAD:
                continue
            per_day = sum(1 for x in taken if utc_to_local(x, plan.zone).date() == day)
            if per_day >= plan.posts_per_day:
                break
            if any(abs(t - as_utc(x)) < gap for x in taken):
                continue
            yield t, hhmm in plan.peak_slots


def _free_slots_daily(plan: Plan, taken: list[dt.datetime], now: dt.datetime, days_ahead: int) -> Iterator[tuple[dt.datetime, bool]]:
    ch, cm = (int(x) for x in plan.cutoff.split(":"))
    cut = dt.timedelta(hours=ch, minutes=cm)

    def pday(t: dt.datetime) -> dt.date:
        return (utc_to_local(t, plan.zone) - cut).date()

    start = pday(now)
    if plan.start_date and plan.start_date > start:
        start = plan.start_date
    gap = dt.timedelta(hours=plan.min_gap_hours)
    for d in range(days_ahead):
        day = start + dt.timedelta(days=d)
        if day.weekday() not in plan.days:
            continue
        times = plan.day_plan(day)  # type: ignore[misc]
        for hhmm in times:
            h, m = (int(x) for x in hhmm.split(":"))
            date = day + dt.timedelta(days=1) if dt.timedelta(hours=h, minutes=m) < cut else day
            t = local_to_utc(date, hhmm, plan.zone)
            if t < now + LEAD:
                continue
            if sum(1 for x in taken if pday(as_utc(x)) == day) >= len(times):
                break
            if any(abs(t - as_utc(x)) < gap for x in taken):
                continue
            yield t, hhmm in plan.peak_slots


def fill(clips: list[ClipRef], plan: Plan, occupied: list[Occupied], now: dt.datetime,
         daily_capacity: Callable[[dt.date], int] | None = None, days_ahead: int = 60) -> list[tuple[int, dt.datetime]]:
    """Assign clips to free slots. Returns [(clip_id, slot_utc)]. `daily_capacity(local_date)` = how many more
    posts that day may take (e.g. remaining upload quota shared by all channels)."""
    remaining = sorted(clips, key=lambda c: c.score, reverse=True)
    if not remaining:
        return []
    weak_cut = median(c.score for c in remaining) if len(remaining) >= 3 else -1.0
    history: list[Occupied] = sorted(occupied, key=lambda o: as_utc(o.slot_utc))
    used_per_day: dict[dt.date, int] = {}
    result: list[tuple[int, dt.datetime]] = []
    taken = [as_utc(o.slot_utc) for o in occupied]
    for slot, peak in free_slots(plan, taken, now, days_ahead):
        if not remaining:
            break
        day = utc_to_local(slot, plan.zone).date()
        if daily_capacity is not None and used_per_day.get(day, 0) >= daily_capacity(day):
            continue  # quota full that day: roll over to the next day
        before = [h for h in history if as_utc(h.slot_utc) < slot]
        prev = before[-1] if before else None
        recent_topics = {_topic(h.topic) for h in history if h.topic and abs(as_utc(h.slot_utc) - slot) < dt.timedelta(hours=24)}
        topic_ok = [c for c in remaining if not c.topic or _topic(c.topic) not in recent_topics]
        both_ok = [c for c in topic_ok if not prev or c.episode != prev.episode]
        pool = both_ok or topic_ok
        if not pool:
            continue  # only same-topic clips left: leave this slot empty, try later
        weak = [c for c in pool if c.score < weak_cut]
        pick = min(weak, key=lambda c: c.score) if (not peak and weak) else pool[0]
        remaining.remove(pick)
        result.append((pick.id, slot))
        taken.append(slot)
        history.append(Occupied(slot, pick.topic, pick.episode))
        history.sort(key=lambda o: as_utc(o.slot_utc))
        used_per_day[day] = used_per_day.get(day, 0) + 1
    return result
