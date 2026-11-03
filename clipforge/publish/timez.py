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
