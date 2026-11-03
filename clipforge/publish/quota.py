"""Local YouTube API quota ledger. Days are Pacific-time days (when Google resets quotas).

The quota belongs to the Google Cloud project, so it is shared by all connected channels.
"""

from __future__ import annotations

import json
import threading

from clipforge.config import Config
from clipforge.publish.timez import quota_day

_lock = threading.Lock()


def _file(cfg: Config):
    return cfg.workspace / "youtube_quota.json"


def _load(cfg: Config) -> dict:
    f = _file(cfg)
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    # old format (Phase 10): {"2026-11-03": 1600}
    return {k: (v if isinstance(v, dict) else {"units": int(v), "inserts": 0}) for k, v in data.items()}


def charge(cfg: Config, units: int = 0, inserts: int = 0, day: str | None = None) -> dict:
    with _lock:
        data = _load(cfg)
        d = day or quota_day()
        row = data.setdefault(d, {"units": 0, "inserts": 0})
        row["units"] += units
        row["inserts"] += inserts
        # keep 60 days of history
        for k in sorted(data)[:-60]:
            data.pop(k, None)
        f = _file(cfg)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return row


def used(cfg: Config, day: str | None = None) -> dict:
    return _load(cfg).get(day or quota_day(), {"units": 0, "inserts": 0})


def remaining(cfg: Config, day: str | None = None) -> dict:
    u = used(cfg, day)
    return {
        "inserts": max(0, cfg.publishing.insert_calls_per_day - u["inserts"]),
        "units": max(0, cfg.publishing.units_per_day - u["units"]),
        "inserts_limit": cfg.publishing.insert_calls_per_day,
        "units_limit": cfg.publishing.units_per_day,
        "day": day or quota_day(),
    }
