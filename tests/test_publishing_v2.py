"""Publishing v2: duplicates, YouTube limit controller + ledger, Instagram login/eligibility, independent platform
queues, daily schedule, LLM budget/cache, /needsyou. Fakes only — no network."""

from __future__ import annotations

import datetime as dt
import json
import threading

import pytest

import clipforge.config as config_mod
from clipforge.publish.timez import UTC
from tests.fake_instagram import FakeInstagram
from tests.fake_youtube import FakeYouTube
from tests.test_publish import NOW, _seed_episode


def _env(tmp_path, monkeypatch, extra: str = "", mode: str = "api"):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"output: {{dir: '{(tmp_path / 'ws').as_posix()}'}}\nllm: {{provider: mock}}\n"
                        f"publishing: {{mode: {mode}}}\n{extra}"
                        # these tests cover the fixed daily lists; the smart plan has its own tests
                        + ("" if "schedule:" in extra else "schedule: {smart: false}\n"), encoding="utf-8")
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", cfg_path)
    from clipforge.api import db
    from clipforge.publish import runner
    from clipforge.publish import youtube as yt

    monkeypatch.setattr(db, "_engine", None)
    runner._keys_backfilled.clear()
    db.engine()
    fake = FakeYouTube()
    yt.set_client_factory(lambda cid, cfg: fake)
    return fake


LEGACY = "schedule: {enabled: false}\n"


@pytest.fixture
def yt_env(tmp_path, monkeypatch):
    fake = _env(tmp_path, monkeypatch, LEGACY)
    yield fake
    from clipforge.publish import youtube as yt

    yt.set_client_factory(None)


def _cfg():
    from clipforge.config import load_config

    return load_config()


def _channel(cid="UCtest"):
    from clipforge.api import db

    with db.session() as s:
        s.add(db.Channel(id=cid, title="Test", uploads_playlist="UU" + cid[2:], confirmed=True))
        s.commit()


def _add(n=3, video="ep1", cid="UCtest"):
    from clipforge.publish import queue as q

    cfg = _cfg()
    if not (cfg.workspace / video).exists():
        _seed_episode(cfg.workspace, video, n, video)
    items = q.add_clips(cid, video, None, cfg)
    for it in items:
        q.generate_metadata(it.id, cfg)
    return items


def _schedule(n=3, video="ep1", now=NOW, cid="UCtest"):
    from clipforge.publish import queue as q

    _add(n, video, cid)
    return q.autoschedule(cid, cfg=_cfg(), now=now)


def _run(now):
    from clipforge.publish import runner

    return runner.run_once(_cfg(), now=now)


# ============================================================================ B. duplicates
def test_pub_key_and_add_twice_creates_nothing(yt_env):
    from clipforge.publish import dedup, queue as q

    _channel()
    first = _add(2)
    assert len(first) == 2 and all(len(it.pub_key) == 64 for it in first)
    assert first[0].pub_key != first[1].pub_key
    assert _add(2) == []  # same clips again: drafts exist → nothing new
    path = q.clip_file(first[0], _cfg())
    assert dedup.make_key("ep1", 1, dedup.file_sha256(path), "youtube", "UCtest") == first[0].pub_key
    cache = json.loads((path.parent / "_work" / "sha256.json").read_text())
    assert cache[path.name]["sha256"] == dedup.file_sha256(path)  # computed once per render


@pytest.mark.parametrize("status,media,expect", [
    ("queued", "", "skipped_duplicate"), ("uploading", "", "skipped_duplicate"),
    ("scheduled", "yt9", "skipped_duplicate"), ("published", "yt9", "skipped_duplicate"),
    ("waiting_capacity", "", "skipped_duplicate"), ("failed:retrying", "", "resumed"),
    ("failed:upload_failed", "", "permanent_failure"), ("missed", "", "resumed"),
])
def test_duplicate_table(yt_env, status, media, expect):
    from clipforge.api import db
    from clipforge.publish import dedup, queue as q

    _channel()
    it = _add(1)[0]
    st, _, reason = status.partition(":")
    with db.session() as s:  # put the existing row into the state under test (direct write: test setup)
        row = s.get(db.QueueItem, it.id)
        row.status, row.reason, row.youtube_video_id = st, reason, media
        row.slot_utc = NOW + dt.timedelta(days=3)
        s.add(row)
        s.commit()
    new = db.QueueItem(channel_id="UCtest", video_id="ep1", rank=1, clip_key=it.clip_key, platform="youtube",
                       pub_key="x" * 64)
    row, outcome = dedup.add_or_skip(new)
    assert outcome == expect and row.id == it.id
    assert len(q.items("UCtest")) == 1  # never a second row


def test_concurrent_adds_make_exactly_one_job(yt_env):
    from clipforge.api import db
    from clipforge.publish import dedup

    _channel()
    _seed_episode(_cfg().workspace, "ep1", 1, "ep1")
    key = dedup.make_key("ep1", 1, "abc", "youtube", "UCtest")
    outcomes, barrier = [], threading.Barrier(2)

    def worker():
        barrier.wait()
        new = db.QueueItem(channel_id="UCtest", video_id="ep1", rank=1, clip_key="k", platform="youtube", pub_key=key)
        outcomes.append(dedup.add_or_skip(new)[1])

    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    from clipforge.publish import queue as q

    assert len(q.items("UCtest")) == 1 and sorted(outcomes) in (["created", "skipped_duplicate"],
                                                                  ["created", "exists"])


def test_duplicate_at_upload_time_is_skipped_never_uploaded(yt_env):
    """A legacy row (no key) for a clip that is already on YouTube: the runner marks it skipped, uploads nothing."""
    from clipforge.api import db
    from clipforge.publish import queue as q

    _channel()
    it = _schedule(1, now=NOW)[0]
    with db.session() as s:
        s.add(db.QueueItem(channel_id="UCtest", video_id="ep1", rank=1, clip_key=it.clip_key, platform="youtube",
                           status="scheduled", youtube_video_id="ytOLD", slot_utc=NOW + dt.timedelta(days=1)))
        s.commit()
    _run(NOW)
    assert yt_env.uploaded_bodies == []
    got = q.get(it.id)
    assert got.status == "skipped_duplicate" and "ytOLD" not in got.error and "#" in got.error


def test_republish_new_intent_new_key(yt_env):
    from clipforge.api import db
    from clipforge.publish import dedup, queue as q

    _channel()
    it = _add(1)[0]
    with pytest.raises(q.QueueError):
        dedup.republish(it.id)
    with db.session() as s:
        row = s.get(db.QueueItem, it.id)
        row.status, row.youtube_video_id = "published", "yt1"
        s.add(row)
        s.commit()
    new = dedup.republish(it.id)
    assert new.intent == "republish:1" and new.pub_key != it.pub_key and new.status == "draft"


def test_backfill_marks_legacy_duplicates_skipped(yt_env):
    from clipforge.api import db
    from clipforge.publish import dedup, queue as q

    _channel()
    it = _add(1)[0]
    with db.session() as s:
        a = s.get(db.QueueItem, it.id)
        a.pub_key = ""
        s.add(a)
        s.add(db.QueueItem(channel_id="UCtest", video_id="ep1", rank=1, clip_key=it.clip_key, platform="youtube",
                           status="queued", slot_utc=NOW + dt.timedelta(days=2)))
        s.commit()
    res = dedup.backfill(_cfg())
    assert res["skipped"] == 1 and res["keyed"] == 1
    sts = sorted(r.status for r in q.items("UCtest"))
    assert sts == ["draft", "skipped_duplicate"]  # kept, never deleted


# ============================================================================ C. errors, ledger, eligibility
def _http_error(status: int, reason: str, message: str = "boom"):
    from googleapiclient.errors import HttpError

    class R(dict):
        def __init__(self, st):
            super().__init__()
            self.status, self.reason = st, ""

    return HttpError(R(status), json.dumps({"error": {"code": status, "message": message,
                                                      "errors": [{"reason": reason}]}}).encode())


@pytest.mark.parametrize("exc,expect", [
    (lambda: _http_error(403, "quotaExceeded"), "project_quota"),
    (lambda: _http_error(400, "uploadLimitExceeded"), "channel_limit"),
    (lambda: _http_error(403, "insufficientPermissions"), "auth"),
    (lambda: _http_error(401, "authError"), "auth"),
    (lambda: _http_error(503, "backendError"), "temporary"),
    (lambda: _http_error(403, "rateLimitExceeded"), "temporary"),
    (lambda: ConnectionResetError("reset"), "temporary"),
    (lambda: _http_error(400, "invalidTitle"), "permanent"),
    (lambda: _http_error(403, "forbidden"), "permanent"),
    (lambda: _http_error(400, "somethingNew"), "permanent"),
])
def test_error_classification(exc, expect):
    from clipforge.publish import yt_errors as ye

    assert ye.classify(exc()).type == expect


def test_channel_limit_cooldown_probe_and_resume(yt_env):
    from clipforge.publish import limits, queue as q

    _channel()
    items = _schedule(4, now=NOW + dt.timedelta(days=3))
    real, calls = yt_env.upload, []

    def limited(path, body, on_progress=None):
        calls.append(1)
        raise _http_error(400, "uploadLimitExceeded", "The user has exceeded the number of videos they may upload.")

    yt_env.upload = limited
    rep = _run(NOW)
    assert len(calls) == 1 and rep.failed == [] and rep.notices
    cd = limits.cooldown_until("UCtest")
    assert cd and cd >= NOW + dt.timedelta(hours=23)
    for it in items:
        got = q.get(it.id)
        assert (got.status, got.reason) == ("waiting_capacity", "channel_limit") and got.attempts == 0
        assert got.slot_utc  # slot kept
    led = limits.rows("UCtest", NOW - dt.timedelta(hours=1))
    assert [(r.result, r.error_type) for r in led] == [("refused", "channel_limit")]
    _run(NOW + dt.timedelta(hours=5))
    assert len(calls) == 1  # cooldown: no calls at all
    # after the cooldown: ONE probe; it works → the batch resumes in later runs
    yt_env.upload = real
    t = cd + dt.timedelta(minutes=1)
    rep = _run(t)
    assert len(rep.uploaded) == 1 and limits.cooldown_until("UCtest") is None
    rep2 = _run(t + dt.timedelta(minutes=3))  # min_gap_sec (120 s) also spaces uploads inside a run
    rep3 = _run(t + dt.timedelta(minutes=6))
    assert len(rep2.uploaded) == 1 and len(rep3.uploaded) == 1


def test_probe_fails_again_starts_a_new_cooldown(yt_env):
    from clipforge.publish import limits

    _channel()
    _schedule(2, now=NOW + dt.timedelta(days=3))
    calls = []

    def limited(path, body, on_progress=None):
        calls.append(1)
        raise _http_error(400, "uploadLimitExceeded")

    yt_env.upload = limited
    _run(NOW)
    cd = limits.cooldown_until("UCtest")
    _run(cd + dt.timedelta(minutes=1))
    assert len(calls) == 2 and limits.cooldown_until("UCtest") > cd


def test_safety_limit_counts_only_successful_uploads(yt_env):
    from clipforge.publish import limits

    _channel()
    for k in range(8):
        limits.record("youtube", "UCtest", queue_id=None, result="ok", at=NOW - dt.timedelta(hours=k + 1))
    limits.record("youtube", "UCtest", queue_id=None, result="refused", error_type="channel_limit",
                  at=NOW - dt.timedelta(minutes=30))
    v = limits.check("UCtest", _cfg(), NOW)
    assert not v.ok and v.code == "safety_limit" and v.until == NOW - dt.timedelta(hours=8) + dt.timedelta(hours=24)
    assert limits.check("UCtest", _cfg(), NOW + dt.timedelta(hours=17)).ok


def test_gap_and_batch(yt_env):
    from clipforge.publish import limits

    _channel()
    limits.record("youtube", "UCtest", queue_id=None, result="ok", at=NOW - dt.timedelta(seconds=30))
    assert limits.check("UCtest", _cfg(), NOW).code == "gap"
    assert limits.check("UCtest", _cfg(), NOW + dt.timedelta(minutes=5), done_this_run=2).code == "batch"


def test_override_until_skips_cooldown(tmp_path, monkeypatch):
    fake = _env(tmp_path, monkeypatch, LEGACY + "youtube: {override_until: '2030-01-01T00:00'}\n")
    try:
        from clipforge.publish import limits

        _channel()
        limits.start_cooldown("UCtest", NOW, _cfg(), "x")
        assert limits.check("UCtest", _cfg(), NOW + dt.timedelta(hours=1)).ok
    finally:
        from clipforge.publish import youtube as yt

        yt.set_client_factory(None)
        assert fake


def test_private_with_publish_at_counts_as_uploaded_never_reuploaded(yt_env):
    from clipforge.publish import limits, queue as q

    _channel()
    it = _schedule(1, now=NOW + dt.timedelta(days=2))[0]
    _run(NOW)
    got = q.get(it.id)
    assert got.youtube_video_id and got.status in ("processing", "scheduled")
    assert [r.result for r in limits.rows("UCtest", NOW - dt.timedelta(minutes=1))] == ["ok"]
    assert limits.rows("UCtest", NOW - dt.timedelta(minutes=1))[0].publish_at is not None
    for k in range(1, 4):
        _run(NOW + dt.timedelta(minutes=5 * k))
    assert len(yt_env.uploaded_bodies) == 1


def test_project_quota_waits_without_retry_storm(yt_env):
    _channel()
    _schedule(3, now=NOW + dt.timedelta(days=2))
    calls = []

    def quota(path, body, on_progress=None):
        calls.append(1)
        raise _http_error(403, "quotaExceeded")

    yt_env.upload = quota
    rep = _run(NOW)
    assert rep.quota_exhausted and len(calls) == 1


# ============================================================================ A. Instagram login + eligibility
@pytest.fixture
def ig_env(tmp_path, monkeypatch):
    fyt = _env(tmp_path, monkeypatch, LEGACY)
    from clipforge.publish import instagram as ig

    fake = FakeInstagram()
    ig.set_client_factory(lambda cfg: fake)
    monkeypatch.setattr(ig, "preflight", lambda path: [])
    yield fake, fyt
    from clipforge.publish import youtube as yt

    ig.set_client_factory(None)
    yt.set_client_factory(None)


def _login(fake, username="figuring.clips"):
    from clipforge.publish import instagram as ig

    ig._flows["s1"] = (ig.redirect_uri(8000), 0)
    res = ig.finish_oauth("good-code", "s1", now=NOW)
    return ig.pick(res["pick"], res["accounts"][0]["ig_user_id"])


def test_redirect_uri_is_exact():
    from clipforge.publish import instagram as ig

    assert ig.redirect_uri(8000) == "http://localhost:8000/api/instagram/oauth/callback"


def test_manual_account_converted_in_place_and_auto_only_when_checks_pass(ig_env):
    from clipforge.api import db
    from clipforge.publish import channels as chs, instagram as ig, modes

    fake, _ = ig_env
    manual = chs.add_manual_instagram("Figuring.Clips")
    with db.session() as s:  # history attached to the manual account
        s.add(db.QueueItem(channel_id=manual.id, video_id="ep0", rank=1, clip_key="old", platform="instagram",
                           status="published", permalink="https://www.instagram.com/reel/abc/"))
        s.commit()
    ch = _login(fake)
    assert ch.id == manual.id  # same row: queue items and tracked Reels stay attached
    assert not ig.is_manual(ch) and ch.extra["ig_user_id"] == fake.ig_id and ch.extra["converted_from_manual"]
    m = modes.resolve(ch)
    assert (m.mode, m.problem) == ("auto", "")
    e = ig.eligibility(ch)
    assert e["auto_ready"] and e["connected"] and e["checks"]["permissions"]["ok"]
    from clipforge.publish import queue as q

    assert [it.status for it in q.items(ch.id)] == ["published"]


def test_missing_permission_keeps_account_assisted(ig_env):
    from clipforge.publish import instagram as ig, modes

    fake, _ = ig_env
    fake.granted = ["instagram_basic", "pages_show_list"]
    ch = _login(fake)
    assert modes.resolve(ch).mode == "assisted"
    e = ig.eligibility(ch)
    assert not e["auto_ready"] and "instagram_content_publish" in e["summary"] and e["connected"]


def test_ig_live_eligibility_reads_limit(ig_env):
    from clipforge.publish import channels as chs, instagram as ig

    fake, _ = ig_env
    ch = _login(fake)
    fake.quota_usage = 95
    e = ig.eligibility(ch, live=True)
    assert e["checks"]["publish_limit"]["ok"] is False and not e["auto_ready"]
    assert chs.get(ch.id).extra["ig_eligibility"]["auto_ready"] is False


def test_ig_publish_once_confirmed_by_read_back_and_ledger(ig_env):
    from clipforge.publish import limits, queue as q
    from tests.test_instagram import _schedule_ig

    fake, _ = ig_env
    ch = _login(fake)
    from clipforge.publish import channels as chs

    chs.update(ch.id, confirmed=True)
    it = _schedule_ig(ch.id, 1)[0]
    slot = it.slot_utc.replace(tzinfo=UTC)
    _run(slot - dt.timedelta(minutes=20))
    _run(slot - dt.timedelta(minutes=10))
    real_media = fake.media
    fake.media = lambda mid, tok: (_ for _ in ()).throw(__import__("clipforge.publish.instagram", fromlist=["x"]).IGError("temp", transient=True))
    _run(slot)
    got = q.get(it.id)
    assert got.ig_media_id and got.status == "processing"  # not PUBLISHED before the API confirms it
    fake.media = real_media
    _run(slot + dt.timedelta(minutes=1))
    got = q.get(it.id)
    assert got.status == "published" and got.permalink and len(fake.published) == 1
    assert [r.result for r in limits.rows(ch.id, slot - dt.timedelta(hours=1), "instagram")] == ["ok"]


def test_ig_auth_failure_marks_reauth(ig_env):
    from clipforge.publish import channels as chs
    from tests.test_instagram import _schedule_ig

    fake, _ = ig_env
    ch = _login(fake)
    chs.update(ch.id, confirmed=True)
    it = _schedule_ig(ch.id, 1)[0]
    fake.auth_broken = True
    rep = _run(it.slot_utc.replace(tzinfo=UTC) - dt.timedelta(minutes=20))
    assert chs.get(ch.id).auth_status == "needs_reauth" and rep.skipped_channels


# ============================================================================ E. independent pipelines
def test_youtube_cooldown_never_stops_instagram(ig_env):
    from clipforge.publish import channels as chs, limits, queue as q
    from tests.test_instagram import _schedule_ig

    fake, fyt = ig_env
    _channel()
    ch = _login(fake)
    chs.update(ch.id, confirmed=True)
    ig_item = _schedule_ig(ch.id, 1)[0]
    _schedule(2, video="ep1")
    limits.start_cooldown("UCtest", NOW, _cfg(), "uploadLimitExceeded")
    slot = ig_item.slot_utc.replace(tzinfo=UTC)
    for m in (-20, -10, 0):
        _run(slot + dt.timedelta(minutes=m))
    assert q.get(ig_item.id).status == "published"
    assert fyt.uploaded_bodies == []  # YouTube held by its cooldown


# ============================================================================ D. daily schedule
@pytest.fixture
def plan_env(tmp_path, monkeypatch):
    fake = _env(tmp_path, monkeypatch, "")
    yield fake
    from clipforge.publish import youtube as yt

    yt.set_client_factory(None)


def test_ig_plan_3_morning_4_evening_and_midnight_rollover(plan_env):
    from clipforge.publish import channels as chs, plan

    ch = chs.add_manual_instagram("someone")
    cfg = _cfg()
    day = dt.date(2026, 11, 5)
    sl = plan.day_slots(ch, cfg, day, NOW)
    assert sl == ["08:00", "09:30", "11:00", "18:00", "19:30", "21:00", "22:15"]
    assert plan.base_slots("instagram", cfg, 9)[-2:] == ["23:30", "00:45"]
    t = plan.slot_datetime(day, "00:45", cfg)  # Thursday's 00:45 = Friday 00:45 IST = Thursday 19:15 UTC
    assert t == dt.datetime(2026, 11, 5, 19, 15, tzinfo=UTC)
    assert plan.plan_day(t, cfg) == day


def test_ig_daily_count_grows_with_buffer(plan_env):
    from clipforge.api import db
    from clipforge.publish import channels as chs, plan

    ch = chs.add_manual_instagram("someone")
    cfg = _cfg()
    assert plan.instagram_daily_count(ch, cfg, NOW) == 7
    with db.session() as s:
        for i in range(18):
            s.add(db.QueueItem(channel_id=ch.id, video_id="v", rank=i, clip_key=f"k{i}", platform="instagram",
                               status="draft", meta_status="ready"))
        s.commit()
    assert plan.instagram_daily_count(ch, cfg, NOW) == 9


def test_youtube_5_or_6_and_safety_cap(plan_env):
    from clipforge.api import db
    from clipforge.publish import plan

    _channel()
    from clipforge.publish import channels as chs

    ch = chs.get("UCtest")
    cfg = _cfg()
    assert plan.youtube_daily_count(ch, cfg, NOW) == 5
    assert plan.base_slots("youtube", cfg, 6) == ["12:30", "15:00", "18:30", "20:30", "22:00", "23:30"]
    with db.session() as s:
        for d in range(1, 14):
            views = 1000 + (500 if d <= 6 else 0)  # last 7 days better than the 7 before
            s.add(db.VideoPerf(youtube_video_id=f"v{d}", channel_id="UCtest", published_at=NOW - dt.timedelta(days=d),
                               views_24h=views))
        s.commit()
    assert plan.youtube_daily_count(ch, cfg, NOW) == 6
    cfg2 = cfg.with_overrides({"youtube.daily_safety_limit": 4})
    assert plan.youtube_daily_count(ch, cfg2, NOW) == 4


def test_autoschedule_uses_the_daily_plan_and_topic_gap(plan_env):
    from clipforge.publish import queue as q
    from clipforge.publish.timez import utc_to_local

    _channel()
    _add(6, "ep1")
    out = q.autoschedule("UCtest", cfg=_cfg(), now=NOW)
    times = sorted(utc_to_local(it.slot_utc).strftime("%H:%M") for it in out)
    assert set(times) <= {"12:30", "15:00", "18:30", "20:30", "22:00", "23:30"}
    per_day: dict = {}
    for it in out:
        d = utc_to_local(it.slot_utc).date()
        per_day[d] = per_day.get(d, 0) + 1
    assert max(per_day.values()) <= 5


def test_adaptation_moves_slots_only_inside_windows(plan_env):
    from clipforge.api import db
    from clipforge.publish import channels as chs, plan
    from clipforge.publish.timez import local_to_utc

    ch = chs.add_manual_instagram("someone")
    cfg = _cfg()
    with db.session() as s:
        for d in range(1, 25):  # 08:30 posts do much better than 08:00 ones
            day = (NOW - dt.timedelta(days=d)).date()
            s.add(db.VideoPerf(youtube_video_id=f"a{d}", channel_id=ch.id, platform="instagram",
                               published_at=local_to_utc(day, "08:30"), views_24h=5000))
            s.add(db.VideoPerf(youtube_video_id=f"b{d}", channel_id=ch.id, platform="instagram",
                               published_at=local_to_utc(day, "07:40"), views_24h=500))
        s.commit()
    res = plan.adapt(ch, cfg, NOW)
    assert res["ready"]
    for old, new in res["changes"].items():
        m = plan._rel(new, cfg)
        assert plan._inside(m, "instagram", cfg)
        assert abs(plan._rel(new, cfg) - plan._rel(old, cfg)) in (30, 60)
    plan.weekly_apply(ch, cfg, NOW)
    ch = chs.get(ch.id)
    assert ch.extra["adapt_ready"] and ch.extra["adapted_slots"] == res["changes"]
    assert plan.weekly_apply(ch, cfg, NOW + dt.timedelta(days=1)) is None  # weekly


def test_exploration_stays_in_window():
    from clipforge.config import Config
    from clipforge.publish import plan

    cfg = Config()
    base = plan.base_slots("instagram", cfg, 7)
    for k in range(14):
        sl = plan.explore(base, "instagram", cfg, dt.date(2026, 11, 1) + dt.timedelta(days=k))
        assert sum(a != b for a, b in zip(sl, base)) <= 1
        assert all(plan._inside(plan._rel(h, cfg), "instagram", cfg) for h in sl)


# ============================================================================ E. LLM budget + cache
def test_retry_makes_zero_llm_calls(yt_env, monkeypatch):
    from clipforge import pipeline
    from clipforge.publish import queue as q

    _channel()
    it = _add(1)[0]
    calls = []
    real = pipeline.get_llm
    monkeypatch.setattr(pipeline, "get_llm", lambda *a, **k: calls.append(1) or real(*a, **k))
    q.generate_metadata(it.id)  # metadata exists → no LLM
    assert calls == []
    _schedule(1)
    _run(NOW)
    assert calls == []


def test_llm_budget_pauses_only_metadata(tmp_path, monkeypatch):
    fake = _env(tmp_path, monkeypatch, LEGACY.replace("schedule", "llm: {provider: mock, max_calls_per_day: 1}\nschedule"))
    try:
        from clipforge.publish import llm_budget, queue as q

        _channel()
        cfg = _cfg()
        _seed_episode(cfg.workspace, "ep1", 2, "ep1")
        items = q.add_clips("UCtest", "ep1", None, cfg)
        q.generate_metadata(items[0].id, cfg)
        q.generate_metadata(items[1].id, cfg)
        assert llm_budget.used_today(cfg) == 1
        assert q.get(items[0].id).meta_status == "ready"
        b = q.get(items[1].id)
        assert b.meta_status == "pending" and "budget" in b.error
    finally:
        from clipforge.publish import youtube as yt

        yt.set_client_factory(None)
        assert fake


# ============================================================================ F. /needsyou + /health
def test_needs_you_lists_only_human_actions(yt_env):
    from clipforge.api import db
    from clipforge.services import publishing as pub

    _channel()
    items = _add(3)
    with db.session() as s:
        for it, (st, reason) in zip(items, [("waiting_capacity", "channel_limit"), ("failed", "upload_failed"),
                                            ("missed", "slot_passed")]):
            row = s.get(db.QueueItem, it.id)
            row.status, row.reason = st, reason
            s.add(row)
        s.commit()
    reasons = sorted(r["item"].reason for r in pub.needs_you())
    assert reasons == ["slot_passed", "upload_failed"]


def test_health_has_plan_ledger_and_auto_ready(plan_env):
    from clipforge.publish import limits
    from clipforge.services import publishing as pub

    _channel()
    limits.record("youtube", "UCtest", queue_id=None, result="ok")
    h = pub.health()
    a = h["accounts"][0]
    assert a["limits"]["uploads_24h"] == 1 and a["auto_ready"] is True and a["plan"][0]["count"] == 5
    assert h["plan_today"] == "YT 5" and h["db"]["ok"]


# ============================================================================ review regressions (Publishing v2)
def test_review_ig_published_unconfirmed_never_missed(ig_env):
    from clipforge.publish import queue as q, channels as chs, instagram as ig
    from tests.test_instagram import _schedule_ig
    fake, _ = ig_env
    ch = _login(fake)
    chs.update(ch.id, confirmed=True)
    it = _schedule_ig(ch.id, 1)[0]
    slot = it.slot_utc.replace(tzinfo=UTC)
    _run(slot - dt.timedelta(minutes=20)); _run(slot - dt.timedelta(minutes=10))
    fake.media = lambda mid, tok: (_ for _ in ()).throw(ig.IGError("temp", transient=True))
    _run(slot)
    assert q.get(it.id).ig_media_id
    _run(slot + dt.timedelta(minutes=120))  # read-back still failing / app was off
    got = q.get(it.id)
    assert got.status != "missed"


def test_review_resumed_clip_never_reuses_a_taken_slot(yt_env, monkeypatch):
    import clipforge.publish.timez as tz; monkeypatch.setattr(tz, "now_utc", lambda: NOW)
    from clipforge.publish import queue as q
    _channel()
    items = _schedule(1)
    a = q.get(items[0].id)
    slot = a.slot_utc
    a.status, a.reason = "failed", "auto_unavailable"; q.save(a)
    # another clip takes the freed slot
    _add(2, "ep2")
    new = q.autoschedule("UCtest", cfg=_cfg(), now=NOW)
    res = q.add_clips("UCtest", "ep1", None, _cfg())
    assert all(r.status == 'draft' and r.slot_utc is None for r in res)
    planned = [x.slot_utc for x in q.items("UCtest", q.PLANNED)]
    assert len(planned) == len(set(planned)), planned


def test_review_reconnect_keeps_assisted(ig_env):
    from clipforge.publish import channels as chs, modes
    import json
    fake, _ = ig_env
    ch = _login(fake)
    s = dict(ch.settings); s["mode"] = "assisted"
    chs.update(ch.id, settings_json=json.dumps(s))
    assert modes.resolve(chs.get(ch.id)).mode == "assisted"
    ch = _login(fake)  # reconnect after token expiry
    assert modes.resolve(ch).mode == "assisted", modes.resolve(ch).mode


def test_review_converted_account_needs_a_media_id(ig_env):
    from clipforge.api import db
    from clipforge.publish import channels as chs, queue as q, states

    fake, _ = ig_env
    manual = chs.add_manual_instagram("figuring.clips")
    with db.session() as s_:
        s_.add(db.QueueItem(channel_id=manual.id, video_id="v", rank=1, clip_key="k", platform="instagram", status="queued"))
        s_.commit()
    it = q.items(manual.id)[0]
    it.permalink = "https://www.instagram.com/reel/x/"
    assert states._has_media(it)  # manual account: a pasted link is the only reference
    _login(fake)  # converted in place (same id)
    assert not states._has_media(it)  # API account now: needs a real media id
