"""Time helpers. Everything is stored in UTC; the user sees and types IST (or the channel's timezone)."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
PACIFIC = ZoneInfo("America/Los_Angeles")  # YouTube API quotas reset at midnight Pacific Time


def tz(name: str = "Asia/Kolkata") -> ZoneInfo:
    return ZoneInfo(name)


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC).replace(microsecond=0)


def as_utc(d: dt.datetime) -> dt.datetime:
    """Naive datetimes coming back from SQLite are UTC."""
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)


def local_to_utc(day: dt.date, hhmm: str, zone: str = "Asia/Kolkata") -> dt.datetime:
    """(2026-11-05, "21:00") IST -> 2026-11-05T15:30Z."""
    h, m = (int(x) for x in hhmm.split(":"))
    return dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz(zone)).astimezone(UTC)


def utc_to_local(d: dt.datetime, zone: str = "Asia/Kolkata") -> dt.datetime:
    return as_utc(d).astimezone(tz(zone))


def parse_local(text: str, zone: str = "Asia/Kolkata") -> dt.datetime:
    """'2026-11-05T21:00' or '2026-11-05 21:00' in local time -> aware UTC."""
    d = dt.datetime.fromisoformat(text.replace(" ", "T"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz(zone))
    return d.astimezone(UTC)


def fmt_local(d: dt.datetime | None, zone: str = "Asia/Kolkata") -> str:
    if d is None:
        return ""
    return utc_to_local(d, zone).strftime("%a %d %b %Y, %I:%M %p")


def iso_utc(d: dt.datetime | None) -> str | None:
    """RFC 3339 'Z' format used by the YouTube API (publishAt)."""
    if d is None:
        return None
    return as_utc(d).strftime("%Y-%m-%dT%H:%M:%SZ")


def quota_day(d: dt.datetime | None = None) -> str:
    """The quota day (Pacific date) a UTC instant belongs to."""
    return as_utc(d or now_utc()).astimezone(PACIFIC).date().isoformat()


MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def parse_when(text: str, zone: str = "Asia/Kolkata", now: dt.datetime | None = None) -> dt.datetime:
    """Phone-friendly local times → aware UTC. Accepts: 'today 21:00', 'tomorrow 9pm', 'tmrw 7:30 am', '21:00'
    (today, or tomorrow if that time has passed), '5 Nov 18:30', 'Nov 5 6:30pm', '2026-11-05 18:30', 'mon 18:00'.
    Raises ValueError with an example when it can't understand or the time is in the past."""
    import re

    zi = tz(zone)
    local_now = as_utc(now or now_utc()).astimezone(zi)
    s = " ".join(text.lower().replace(",", " ").split())
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if not m or (m.group(2) is None and m.group(3) is None):
        raise ValueError("Send a time like 'today 21:00', 'tomorrow 9pm' or '5 Nov 18:30' (IST).")
    hh, mm = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        if not 1 <= hh <= 12:
            raise ValueError("With am/pm the hour must be 1–12.")
        hh = hh % 12 + (12 if m.group(3) == "pm" else 0)
    if hh > 23 or mm > 59:
        raise ValueError("That isn't a valid time of day.")
    day_part = s[:m.start()].strip()
    today = local_now.date()
    weekdays = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    if day_part in ("", "at"):
        day = today
        if (hh, mm) <= (local_now.hour, local_now.minute):
            day = today + dt.timedelta(days=1)
    elif day_part in ("today", "tonight"):
        day = today
    elif day_part in ("tomorrow", "tmrw", "tmr", "kal"):
        day = today + dt.timedelta(days=1)
    elif day_part[:3] in weekdays:
        ahead = (weekdays.index(day_part[:3]) - today.weekday()) % 7
        day = today + dt.timedelta(days=ahead or (7 if (hh, mm) <= (local_now.hour, local_now.minute) else 0))
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_part):
        day = dt.date.fromisoformat(day_part)
    else:
        dm = re.fullmatch(r"(\d{1,2})\s*([a-z]{3})[a-z]*|([a-z]{3})[a-z]*\s*(\d{1,2})", day_part)
        if not dm:
            raise ValueError("Send a day like 'today', 'tomorrow', 'fri' or '5 Nov'.")
        d, mon = (dm.group(1), dm.group(2)) if dm.group(1) else (dm.group(4), dm.group(3))
        if mon not in MONTHS:
            raise ValueError(f"'{mon}' isn't a month.")
        year = today.year
        day = dt.date(year, MONTHS[mon], int(d))
        if day < today - dt.timedelta(days=1):  # "5 Jan" in December means next year
            day = dt.date(year + 1, MONTHS[mon], int(d))
    when = dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=zi)
    if when <= local_now:
        raise ValueError(f"{when:%a %d %b %H:%M} has already passed.")
    return when.astimezone(UTC)
