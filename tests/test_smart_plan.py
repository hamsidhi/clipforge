"""Smart daily plan (publish/smart.py): times + posts per day learned from the account's own results."""

from __future__ import annotations

import datetime as dt

import pytest

from clipforge.publish.timez import UTC
from tests.test_publishing_v2 import _channel as _add_channel
from tests.test_publishing_v2 import _env

NOW = dt.datetime(2026, 11, 20, 6, 0, tzinfo=UTC)


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = _env(tmp_path, monkeypatch, "schedule: {smart: true, ig_min_per_day: 8, ig_max_per_day: 10}\n"
                                       "youtube: {daily_safety_limit: 8, min_gap_sec: 0}\n")
    from clipforge.publish import smart

    smart.clear_cache()
    yield fake
    from clipforge.publish import youtube as yt

    yt.set_client_factory(None)
    smart.clear_cache()


def _perf(cid, i, at_utc, views, likes=0, shares=0, pct=None, platform="youtube"):
    from clipforge.api import db

    with db.session() as s:
        s.add(db.VideoPerf(youtube_video_id=f"v{cid[-3:]}{i}", channel_id=cid, published_at=at_utc, views=views,
                           likes=likes, shares=shares, avg_view_pct=pct, platform=platform))
        s.commit()


def _ist(day: dt.date, hh: int, mm: int = 0) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=UTC) - dt.timedelta(hours=5, minutes=30)


def _m(hhmm: str) -> int:
    h, m = (int(x) for x in hhmm.split(":"))
    return h * 60 + m


# ----------------------------------------------------------------------------- pure parts
def test_pick_times_gap_window_morning_and_evening():
    from clipforge.publish import smart

    vals = [1.0] * smart.NBINS
    for b in range(20 * 4, 22 * 4):  # a strong evening
        vals[b] = 3.0
    t = smart.pick_times(vals, 10, 7 * 60, 25 * 60 + 30, 60)
    assert len(t) == 10
    assert all(b - a >= 60 for a, b in zip(t, t[1:]))
    assert any(420 <= m <= 660 for m in t)                       # at least one morning slot
    assert sum(1 for m in t if 1080 <= m <= 1470) >= 2           # at least two evening slots
    assert any(1200 <= m < 1320 for m in t) and min(t) >= 420 and max(t) <= 1530


def test_curve_moves_toward_good_hours_and_explores_empty_ones():
    from clipforge.publish import smart

    pts = [{"minute": 13 * 60, "rel": 3.0, "weight": 1.0}] * 5 + [{"minute": 21 * 60, "rel": 0.3, "weight": 1.0}] * 5
    c = smart.curve(pts)["value"]
    prior = smart.curve([])["value"]
    assert c[13 * 4] > prior[13 * 4] and c[21 * 4] < prior[21 * 4]   # data beats the assumed curve
    assert c[13 * 4] > c[21 * 4]
    no = smart.curve([])
    assert no["value"][3 * 4] > 0 and max(no["value"]) == pytest.approx(no["value"][20 * 4], rel=0.05)


def test_daily_count_max_by_default_and_lower_when_recent_posts_drop(env):
    from clipforge.config import load_config
    from clipforge.publish import smart

    cfg = load_config()
    assert smart.daily_count("youtube", [], cfg)[0] == 8
    assert smart.daily_count("instagram", [], cfg)[0] == 10
    good = [{"raw": 2.0}] * 5 + [{"raw": 2.0}] * 10
    assert smart.daily_count("youtube", good, cfg)[0] == 8
    bad = [{"raw": 0.5}] * 5 + [{"raw": 2.0}] * 10          # last 5 at 25% of before → maximum steps down
    n, why = smart.daily_count("youtube", bad, cfg)
    assert n == 6 and "dropped" in why
    capped = cfg.with_overrides({"youtube.daily_safety_limit": 5})
    assert smart.daily_count("youtube", [], capped)[0] == 5     # never above the upload safety limit


# ----------------------------------------------------------------------------- with data
def test_slots_follow_what_worked_for_this_account(env):
    from clipforge.config import load_config
    from clipforge.publish import channels as chs, plan, smart
    from clipforge.publish import channels as chs

    _add_channel()
    ch = chs.get("UCtest")
    base = dt.date(2026, 11, 10)
    i = 0
    for d in range(8):  # 1 pm posts do great, 9 pm posts do badly — against the assumed evening peak
        day = base + dt.timedelta(days=d)
        _perf(ch.id, i, _ist(day, 13), views=900, likes=60, shares=10, pct=95.0); i += 1
        _perf(ch.id, i, _ist(day, 21), views=5, pct=30.0); i += 1
    cfg = load_config()
    sl = plan.day_slots(chs.get(ch.id), cfg, dt.date(2026, 11, 21), NOW)
    assert 6 <= len(sl) <= 8 and any(abs(_m(h) - 13 * 60) <= 30 for h in sl)  # 3 of the last 5 were weak → fewer
    ex = smart.explain(chs.get(ch.id), cfg, NOW)
    assert ex["posts_used"] == 16 and "13:00" in ex["best_hours_ist"][:2]
    worse = smart.day_plan(chs.get(ch.id), cfg, dt.date(2026, 11, 21), NOW)
    assert worse["peak"] and any(abs(_m(h) - 13 * 60) <= 30 for h in worse["peak"])


def test_instagram_own_fixed_slots_are_replaced_and_more_than_5_per_day(env):
    from clipforge.config import load_config
    from clipforge.publish import channels as chs, queue as q
    from clipforge.publish import plan as daily

    ch = chs.add_manual_instagram("figuringclips.in")
    chs.update(ch.id, settings_json='{"slots": ["13:00", "19:00", "21:30"], "posts_per_day": 10}')  # like the live one
    p = q.plan_for(chs.get(ch.id), load_config(), NOW)
    day = dt.date(2026, 11, 21)
    assert p.day_plan is not None and len(p.day_plan(day)) == 10 and p.min_gap_hours == 1
    off = chs.get(ch.id)
    chs.update(ch.id, settings_json='{"slots": ["13:00"], "smart": false}')
    assert q.plan_for(chs.get(ch.id), load_config(), NOW).day_plan is None  # opt-out per account still works
    assert daily.min_gap_hours("youtube", load_config()) == 1.25
    assert off


def test_autoschedule_fills_the_smart_plan(env):
    from clipforge.config import load_config
    from clipforge.publish import queue as q
    from tests.test_publish import _seed_episode
    from clipforge.publish import channels as chs

    _add_channel()
    ch = chs.get("UCtest")
    cfg = load_config()
    _seed_episode(cfg.workspace, "ep1", 12, "ep1")
    for it in q.add_clips(ch.id, "ep1", None, cfg):
        q.generate_metadata(it.id, cfg)
    got = q.autoschedule(ch.id, cfg=cfg, now=NOW)
    days: dict = {}
    for it in got:
        local = it.slot_utc.replace(tzinfo=UTC) + dt.timedelta(hours=5, minutes=30)
        days.setdefault((local - dt.timedelta(hours=5)).date(), []).append(local)
    assert max(len(v) for v in days.values()) > 5
    for v in days.values():
        v.sort()
        assert all((b - a) >= dt.timedelta(minutes=75) for a, b in zip(v, v[1:]))


def test_explore_shift_changes_one_slot_per_day_within_rules():
    from clipforge.publish import smart

    times = [480, 600, 780, 1110, 1200, 1290]
    w = [5.0] * smart.NBINS
    w[600 // smart.BIN] = 0.0                   # least data around 10:00 → that one moves
    d1, d2 = dt.date(2026, 11, 21), dt.date(2026, 11, 22)
    a, b = smart.explore_shift(times, w, d1, 420, 1530, 60), smart.explore_shift(times, w, d2, 420, 1530, 60)
    assert a != b and a != times and len(a) == len(times)
    for t in (a, b):
        assert sum(1 for x, y in zip(sorted(t), times) if x != y) == 1
        assert all(y - x >= 60 for x, y in zip(sorted(t), sorted(t)[1:]))


# ----------------------------------------------------------------------------- 2026-11-19: fair scores, shared learning
def test_post_score_is_fair_between_old_and_new_posts():
    from clipforge.api import db
    from clipforge.publish import smart

    now = NOW
    old_yt = db.VideoPerf(youtube_video_id="a", channel_id="c", views=240, platform="youtube",
                          published_at=now - dt.timedelta(days=10))
    new_yt = db.VideoPerf(youtube_video_id="b", channel_id="c", views=100, platform="youtube",
                          published_at=now - dt.timedelta(hours=20))
    # YouTube: 240 views after 10 days ≈ 100 on day 1 (×2.4 growth) — the same as a new post with 100
    assert smart.post_score(old_yt, now) == pytest.approx(smart.post_score(new_yt, now), rel=0.05)
    old_ig = db.VideoPerf(youtube_video_id="c", channel_id="c", views=100, platform="instagram",
                          published_at=now - dt.timedelta(days=10))
    assert smart.post_score(old_ig, now) == pytest.approx(smart.post_score(new_yt, now), rel=0.01)  # Reels: day 1 = all
    stored = db.VideoPerf(youtube_video_id="d", channel_id="c", views=999, views_24h=100, platform="youtube",
                          published_at=now - dt.timedelta(days=30))
    assert smart.post_score(stored, now) == pytest.approx(smart.post_score(new_yt, now), rel=0.01)  # views_24h wins


def test_gaming_prior_and_shared_learning_between_same_content_accounts(env):
    import json

    from clipforge.publish import channels as chs, smart

    assert smart.prior_bins("gaming")[23 * 4] > smart.prior_bins("podcast")[23 * 4]  # gamers: later nights
    for cid, content in (("UCgameA", "gaming"), ("UCgameB", "gaming"), ("UCpod", "podcast")):
        _add_channel(cid)
        chs.update(cid, settings_json=json.dumps({"content": content}))
    for i in range(6):
        _perf("UCgameB", i, NOW - dt.timedelta(days=2 + i), 100)
        _perf("UCpod", i, NOW - dt.timedelta(days=2 + i), 100)
    sib = smart.sibling_posts(chs.get("UCgameA"), NOW)
    assert {x["sibling"] for x in sib} == {"UCgameB"}                       # same content only
    assert all(x["weight"] <= smart.SIBLING_SAME_PLATFORM for x in sib)
    assert smart.content_kind(chs.get("UCgameA")) == "gaming" and smart.content_kind(chs.get("UCpod")) == "podcast"
    assert smart.account_curve(chs.get("UCgameA"), NOW)["sibling_posts"] == 6
