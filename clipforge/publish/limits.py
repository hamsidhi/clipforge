"""YouTube upload eligibility per channel (Publishing v2). Deterministic: ledger + known quota costs only — Google
doesn't expose "uploads left", so nothing here invents that number.

Before each upload `check()` answers ok / why not / until when:
  1. channel cooldown (after uploadLimitExceeded) — after it ends, exactly ONE probe upload; the batch resumes only
     if that probe succeeds (`probe` flag on the channel)
  2. project quota: videos.insert calls left today + API units below `project_units_safe_pct`
  3. successful uploads in the last 24 h < `youtube.daily_safety_limit`
  4. at most 1 active upload per channel
  5. ≥ `min_gap_sec` since the channel's last attempt
  6. ≤ `max_per_batch` uploads per scheduler run
`youtube.override_until` (ISO time) skips 1 and 3 until then.
Channel state lives in Channel.extra_json: yt_cooldown_until, yt_probe, yt_last_limit_reason.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

from sqlmodel import select

from clipforge.api import db
from clipforge.config import Config
from clipforge.publish.timez import as_utc, iso_utc

WINDOW = dt.timedelta(hours=24)


@dataclass
class Verdict:
    ok: bool
    code: str = ""          # cooldown | project_quota | safety_limit | busy | gap | batch
    why: str = ""
    until: dt.datetime | None = None
    probe: bool = False     # this upload is the single probe after a cooldown


# ----------------------------------------------------------------------------- ledger
def record(platform: str, channel_id: str, *, queue_id: int | None, pub_key: str = "", video_id: str = "",
           result: str, error_type: str = "", error_reason: str = "", privacy: str = "",
           publish_at: dt.datetime | None = None, at: dt.datetime | None = None) -> db.UploadLedger:
    from clipforge.publish.timez import now_utc

    at = as_utc(at or now_utc())
    row = db.UploadLedger(platform=platform, channel_id=channel_id, queue_id=queue_id, pub_key=pub_key,
                          video_id=video_id, attempted_at=at, succeeded_at=at if result == "ok" else None,
                          result=result, error_type=error_type, error_reason=error_reason[:500], privacy=privacy,
                          publish_at=publish_at)
    with db.session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
    return row


def rows(channel_id: str, since: dt.datetime, platform: str = "youtube") -> list[db.UploadLedger]:
    with db.session() as s:
        out = s.exec(select(db.UploadLedger).where(db.UploadLedger.channel_id == channel_id,
                                                   db.UploadLedger.platform == platform)).all()
    return [r for r in out if as_utc(r.attempted_at) >= as_utc(since)]


def successes_24h(channel_id: str, now: dt.datetime, platform: str = "youtube") -> list[db.UploadLedger]:
    return sorted((r for r in rows(channel_id, now - WINDOW, platform) if r.succeeded_at),
                  key=lambda r: as_utc(r.succeeded_at))  # type: ignore[arg-type]


def last_attempt(channel_id: str, platform: str = "youtube") -> dt.datetime | None:
    with db.session() as s:
        r = s.exec(select(db.UploadLedger).where(db.UploadLedger.channel_id == channel_id,
                                                 db.UploadLedger.platform == platform)
                   .order_by(db.UploadLedger.attempted_at.desc())).first()  # type: ignore[attr-defined]
    return as_utc(r.attempted_at) if r else None


# ----------------------------------------------------------------------------- channel state
def _extra(channel_id: str) -> dict:
    from clipforge.publish import channels as chs

    ch = chs.get(channel_id)
    return dict(ch.extra) if ch else {}


def _set_extra(channel_id: str, **kw) -> None:
    from clipforge.publish import channels as chs

    ex = _extra(channel_id)
    for k, v in kw.items():
        if v is None:
            ex.pop(k, None)
        else:
            ex[k] = v
    chs.update(channel_id, extra_json=json.dumps(ex))


def cooldown_until(channel_id: str) -> dt.datetime | None:
    v = _extra(channel_id).get("yt_cooldown_until")
    return as_utc(dt.datetime.fromisoformat(v.replace("Z", "+00:00"))) if v else None


def start_cooldown(channel_id: str, now: dt.datetime, cfg: Config, reason: str) -> dt.datetime:
    """uploadLimitExceeded: the window started with the first successful upload of the last 24 h (fallback: now)."""
    ok = successes_24h(channel_id, now)
    base = as_utc(ok[0].succeeded_at) if ok else as_utc(now)  # type: ignore[arg-type]
    until = max(base + dt.timedelta(hours=cfg.youtube.cooldown_hours), as_utc(now) + dt.timedelta(minutes=30))
    _set_extra(channel_id, yt_cooldown_until=iso_utc(until), yt_probe=True, yt_last_limit_reason=reason[:300])
    return until


def probe_succeeded(channel_id: str) -> None:
    _set_extra(channel_id, yt_cooldown_until=None, yt_probe=None, yt_last_limit_reason=None)


def _override(cfg: Config, now: dt.datetime) -> bool:
    v = (cfg.youtube.override_until or "").strip()
    if not v:
        return False
    try:
        t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return False
    if t.tzinfo is None:  # a local (IST) time typed by the user
        from clipforge.publish.timez import tz

        t = t.replace(tzinfo=tz(cfg.publishing.timezone))
    return as_utc(now) < as_utc(t)


# ----------------------------------------------------------------------------- the controller
def check(channel_id: str, cfg: Config, now: dt.datetime, done_this_run: int = 0) -> Verdict:
    from clipforge.publish import queue as q
    from clipforge.publish import quota

    now = as_utc(now)
    y = cfg.youtube
    override = _override(cfg, now)
    probe = False
    cd = cooldown_until(channel_id)
    if cd and not override:
        if now < cd:
            return Verdict(False, "cooldown", "YouTube's upload limit for this channel was reached; waiting for it to "
                           "reset.", cd)
        probe = True  # cooldown over: one probe upload, nothing more in this run
        if done_this_run >= 1:
            return Verdict(False, "batch", "Waiting for the probe upload to succeed first.", None)
    rem = quota.remaining(cfg)
    if rem["inserts"] <= 0:
        return Verdict(False, "project_quota", "The API project's daily upload calls are used up (resets at midnight "
                       "Pacific).", None)
    if rem["units_limit"] and rem["units"] < rem["units_limit"] * (100 - y.project_units_safe_pct) / 100:
        return Verdict(False, "project_quota", "The API project's daily quota units are nearly used up.", None)
    ok = successes_24h(channel_id, now)
    if len(ok) >= y.daily_safety_limit and not override:
        until = as_utc(ok[len(ok) - y.daily_safety_limit].succeeded_at) + WINDOW  # type: ignore[arg-type]
        return Verdict(False, "safety_limit", f"{len(ok)} uploads in the last 24 h (safety limit "
                       f"{y.daily_safety_limit}).", until)
    if any(it.platform == "youtube" for it in q.items(channel_id, ("uploading",))):
        return Verdict(False, "busy", "Another upload of this channel is running.", None)
    last = last_attempt(channel_id)
    if last and now - last < dt.timedelta(seconds=y.min_gap_sec):
        return Verdict(False, "gap", "Short pause between uploads.", last + dt.timedelta(seconds=y.min_gap_sec))
    if done_this_run >= y.max_per_batch:
        return Verdict(False, "batch", "Batch size reached for this run.", None)
    return Verdict(True, probe=probe)


def status(channel_id: str, cfg: Config, now: dt.datetime) -> dict:
    """For /health: ledger count, cooldown, last failure reasons (no secrets)."""
    ok = successes_24h(channel_id, now)
    recent = sorted(rows(channel_id, now - dt.timedelta(days=2)), key=lambda r: as_utc(r.attempted_at), reverse=True)
    cd = cooldown_until(channel_id)
    return {"uploads_24h": len(ok), "safety_limit": cfg.youtube.daily_safety_limit,
            "cooldown_until": iso_utc(cd) if cd and cd > as_utc(now) else None,
            "probe_pending": bool(_extra(channel_id).get("yt_probe")),
            "recent_failures": [f"{r.error_type}: {r.error_reason[:120]}" for r in recent if r.result != "ok"][:3]}
