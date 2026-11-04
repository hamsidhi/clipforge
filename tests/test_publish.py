"""Phase 12 publishing logic: slots, time zones, quota, metadata, runner, reconcile, YPP, earnings."""

from __future__ import annotations

import datetime as dt
import json

import pytest

import clipforge.config as config_mod
from clipforge.models import CandidateFile, ClipCandidate, Segment, Transcript, VideoInfo, Word
from clipforge.publish import metadata_opt, slots
from clipforge.publish.timez import UTC, local_to_utc, parse_local, quota_day, utc_to_local
from clipforge.workspace import Workspace
from tests.fake_youtube import FakeYouTube

NOW = dt.datetime(2026, 11, 4, 4, 0, tzinfo=UTC)  # 09:30 IST, Wednesday


# ----------------------------------------------------------------------------- time zones
def test_ist_utc_roundtrip_and_date_boundary():
    # 02:00 IST on the 5th is still the 4th in UTC
    t = local_to_utc(dt.date(2026, 11, 5), "02:00")
    assert t == dt.datetime(2026, 11, 4, 20, 30, tzinfo=UTC)
    assert utc_to_local(t).strftime("%Y-%m-%d %H:%M") == "2026-11-05 02:00"
    assert parse_local("2026-11-05T21:00") == dt.datetime(2026, 11, 5, 15, 30, tzinfo=UTC)
    # quota day follows Pacific time: 06:00 UTC on 5 Nov is still 4 Nov in California
    assert quota_day(dt.datetime(2026, 11, 5, 6, 0, tzinfo=UTC)) == "2026-11-04"


# ----------------------------------------------------------------------------- slot filling
def _clips():
    return [slots.ClipRef(1, 9.0, "epA", "money"), slots.ClipRef(2, 8.5, "epA", "startup"),
            slots.ClipRef(3, 8.0, "epB", "health"), slots.ClipRef(4, 6.0, "epB", "money"),
            slots.ClipRef(5, 5.0, "epC", "love"), slots.ClipRef(6, 4.0, "epA", "fear")]


def test_fill_respects_posts_per_day_and_gap():
    plan = slots.Plan(slots=["12:30", "13:30", "18:30", "21:00"], posts_per_day=2, min_gap_hours=3)
    res = slots.fill(_clips(), plan, [], NOW)
    days: dict[dt.date, list[dt.datetime]] = {}
    for _, t in res:
        days.setdefault(utc_to_local(t).date(), []).append(t)
    assert all(len(v) <= 2 for v in days.values())
    for v in days.values():
        v.sort()
        assert all(b - a >= dt.timedelta(hours=3) for a, b in zip(v, v[1:]))
    assert len(res) == 6


def test_fill_interleaves_episodes_and_topics():
    plan = slots.Plan()
    res = slots.fill(_clips(), plan, [], NOW)
    by = {c.id: c for c in _clips()}
    ordered = [by[i] for i, _ in sorted(res, key=lambda r: r[1])]
    # never the same episode back-to-back while others were available
    assert all(a.episode != b.episode for a, b in zip(ordered, ordered[1:4]))
    # same topic ("money") at least 24 h apart
    money = sorted(t for i, t in res if by[i].topic == "money")
    assert money[1] - money[0] >= dt.timedelta(hours=24)
    # the best clip lands in a peak slot on the first day (off-peak 12:30 gets weaker clips; interleaving wins ties)
    slot_of = {i: t for i, t in res}
    assert utc_to_local(slot_of[1]).strftime("%H:%M") in plan.peak_slots
    assert utc_to_local(slot_of[1]).date() == utc_to_local(NOW).date()


def test_offpeak_gets_weaker_clips():
    plan = slots.Plan(slots=["12:30", "18:30", "21:00"], peak_slots=["18:30", "21:00"])
    res = slots.fill(_clips(), plan, [], NOW)
    by = {c.id: c for c in _clips()}
    offpeak = [by[i].score for i, t in res if utc_to_local(t).strftime("%H:%M") == "12:30"]
    peak = [by[i].score for i, t in res if utc_to_local(t).strftime("%H:%M") != "12:30"]
    assert max(offpeak) <= max(peak)


def test_fill_quota_rollover_and_skips_past_and_weekdays():
    plan = slots.Plan(days=[0, 1, 2, 3, 4])  # weekdays only
    res = slots.fill(_clips(), plan, [], NOW, daily_capacity=lambda d: 1)
    days = sorted(utc_to_local(t).date() for _, t in res)
    assert len(days) == len(set(days))  # 1 per day because of quota
    assert all(d.weekday() < 5 for d in days)
    assert all(t > NOW for _, t in res)


def test_fill_respects_existing_occupied():
    plan = slots.Plan(posts_per_day=1)
    occ = [slots.Occupied(local_to_utc(dt.date(2026, 11, 4), "18:30"), "x", "epZ")]
    res = slots.fill(_clips()[:1], plan, occ, NOW)
    assert utc_to_local(res[0][1]).date() == dt.date(2026, 11, 5)


# ----------------------------------------------------------------------------- metadata
def test_title_rubric_and_duplicates():
    good, _ = metadata_opt.score_title("Why 90% of startups fail in India?", "startup")
    bad, why = metadata_opt.score_title("SHOCKING TRUTH YOU WON'T BELIEVE!!!", "startup")
    assert good > bad and "clickbait phrase" in why
    assert metadata_opt.is_duplicate("Why startups fail", ["why startups FAIL!"])
    assert not metadata_opt.is_duplicate("Why startups fail", ["How I raised money"])


def test_tags_limit_and_hashtags():
    tags = metadata_opt.tags_within_limit([f"tag number {i}" for i in range(100)])
    assert sum(len(t) + 2 for t in tags) + len(tags) - 1 <= 500
    h = metadata_opt.final_hashtags(["#Money", "#Business"], "#podcast")
    assert h[0] == "#shorts" and "#podcast" in h and len(h) <= 5 and all(x == x.lower() for x in h)


def test_description_has_credit():
    info = VideoInfo(id="v", title="FO516 Liquor Business", channel="Raj Shamani", url="https://youtu.be/x")
    credit = metadata_opt.credit_line("🎙 Source: {episode} — {source_channel} {url}", info)
    d = metadata_opt.build_description("Hook line.", credit, "Follow!", ["#shorts", "#podcast"])
    assert "Source: FO516 Liquor Business — Raj Shamani https://youtu.be/x" in d and d.endswith("#shorts #podcast")


# ----------------------------------------------------------------------------- YPP + earnings
def test_ypp_targets_switch_by_date():
    from clipforge.config import YppConfig
    from clipforge.publish.analytics import eta, ypp_targets

    y = YppConfig()
    assert ypp_targets(y, dt.date(2027, 1, 31))["ads"]["shorts_views_90d"] == 10_000_000
    assert ypp_targets(y, dt.date(2027, 2, 1))["ads"]["shorts_views_90d"] == 20_000_000
    assert eta(500, 1000, 50, dt.date(2026, 11, 4)) == "2026-11-15"
    assert eta(1000, 1000, 0, dt.date(2026, 11, 4)) == "reached"
    assert eta(10, 1000, 0, dt.date(2026, 11, 4)) is None


def test_earnings_math():
    from clipforge.api.db import Post
    from clipforge.publish.analytics import earnings

    assert earnings(Post(views=120_000, rate_per_1k=50, campaign_fee_pct=10)) == 5400.0
    assert earnings(Post(views=120_000, approved_views=100_000, rate_per_1k=50)) == 5000.0


def test_classify_locked_and_rejected():
    from clipforge.publish.runner import classify

    slot = NOW + dt.timedelta(days=1)
    assert classify({"status": {"privacyStatus": "private"}}, slot)[0] == "locked_private"
    assert classify({"status": {"privacyStatus": "private", "publishAt": "x"}}, slot)[0] == "scheduled"
    assert classify({"status": {"uploadStatus": "rejected", "rejectionReason": "duplicate"}}, slot)[0] == "failed"
    assert classify({"status": {"privacyStatus": "public"}}, slot)[0] == "published"


def test_reconcile_matching():
    from clipforge.api.db import QueueItem
    from clipforge.publish.reconcile import match

    items = [QueueItem(id=1, channel_id="c", video_id="v", rank=1, clip_key="a", title="Why 90% startups fail", duration=30),
             QueueItem(id=2, channel_id="c", video_id="v", rank=2, clip_key="b", title="Liquor license costs 8 crore", duration=22)]
    fake = FakeYouTube()
    fake.add_video("x1", "Liquor License costs 8 Crore!", 23)
    fake.add_video("x2", "Why 90% startups fail", 45)   # title matches but duration doesn't
    fake.add_video("x3", "Unrelated vlog", 30)
    pairs, left_items, left_videos = match(items, fake.videos(["x1", "x2", "x3"]))
    assert [(it.id, v["id"]) for it, v, _ in pairs] == [(2, "x1")]
    assert [it.id for it in left_items] == [1]


# ----------------------------------------------------------------------------- OAuth / quota (isolated workspace)
@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"output: {{dir: '{(tmp_path / 'ws').as_posix()}'}}\nllm: {{provider: mock}}\n"
                        "publishing: {mode: api}\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", cfg_path)
    from clipforge.api import db
    from clipforge.publish import youtube as yt

    monkeypatch.setattr(db, "_engine", None)
    db.engine()
    fake = FakeYouTube()
    yt.set_client_factory(lambda cid, cfg: fake)
    yield tmp_path, fake
    yt.set_client_factory(None)
    db._engine = None


def test_invalid_grant_marks_channel(env, monkeypatch):
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    from clipforge.api import db
    from clipforge.publish import channels as chs, runner, youtube as yt

    tmp, _ = env
    with db.session() as s:
        s.add(db.Channel(id="UCx", title="X"))
        s.commit()
    p = yt.token_path("UCx")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"token": "old", "refresh_token": "r", "client_id": "id", "client_secret": "s",
                             "token_uri": "https://oauth2.googleapis.com/token", "expiry": "2020-01-01T00:00:00Z"}))

    def boom(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", boom)
    with pytest.raises(yt.NeedsReauth):
        yt.load_credentials("UCx")
    yt.set_client_factory(None)  # use the real loader -> NeedsReauth
    rep = runner.run_once()
    assert rep.skipped_channels["UCx"] == "needs re-login"
    assert chs.get("UCx").auth_status == "needs_reauth"


def test_quota_accounting(env):
    from clipforge.config import load_config
    from clipforge.publish import quota

    cfg = load_config()
    quota.charge(cfg, units=50, inserts=2)
    quota.charge(cfg, units=1)
    r = quota.remaining(cfg)
    assert r["inserts"] == 98 and r["units"] == 10_000 - 51
    quota.charge(cfg, inserts=1, day="2000-01-01")
    assert quota.remaining(cfg)["inserts"] == 98


# ----------------------------------------------------------------------------- end-to-end (fake YouTube, mock LLM)
def _seed_episode(ws_root, video_id: str, n: int = 3, topic_prefix: str = "t") -> None:
    from clipforge.utils import ffmpeg as ff

    ws = Workspace(ws_root, video_id)
    ws.write_model(ws.info, VideoInfo(id=video_id, title=f"Episode {video_id}", channel="Raj Shamani",
                                      url=f"https://youtu.be/{video_id}", duration=300, width=1920, height=1080))
    words = [Word(start=i * 0.5, end=i * 0.5 + 0.4, text=f"w{i}") for i in range(500)]
    ws.write_model(ws.transcript, Transcript(language="hi", model="t", duration=300,
                                             segments=[Segment(id=0, start=0, end=250, text="", words=words)]))
    cands = [ClipCandidate(rank=i + 1, start=10 + i * 40, end=40 + i * 40, title=f"{video_id} clip {i + 1} about money {i}",
                           hook=f"Hook {i}", topic=f"{topic_prefix}{i}", score=9 - i, approved=True) for i in range(n)]
    ws.write_model(ws.candidates, CandidateFile(video_id=video_id, candidates=cands))
    for c in cands:  # tiny real mp4 files
        ff.run_ffmpeg(["-f", "lavfi", "-i", "color=black:s=108x192:d=1", "-c:v", "libx264", "-preset", "ultrafast",
                       str(ws.clip_path(c.rank, c.title))])


def test_e2e_queue_schedule_upload_batch_reconcile_dashboard(env):
    from clipforge.api import db
    from clipforge.config import load_config
    from clipforge.publish import analytics, batch, channels as chs, queue as q, reconcile, runner

    tmp, fake = env
    cfg = load_config()
    _seed_episode(cfg.workspace, "epA", 3, "a")
    _seed_episode(cfg.workspace, "epB", 3, "b")
    with db.session() as s:
        s.add(db.Channel(id="UCtest", title="Test Channel", uploads_playlist="UUtest", confirmed=True))
        s.commit()
    added = q.add_clips("UCtest", "epA", None) + q.add_clips("UCtest", "epB", None)
    assert len(added) == 6
    assert q.add_clips("UCtest", "epA", None) == []  # duplicate guard
    for it in added:
        q.generate_metadata(it.id, cfg)
    ready = q.items("UCtest", ("draft",))
    assert all(it.meta_status == "ready" and "#shorts" in it.description and "Source:" in it.description for it in ready)
    scheduled = q.autoschedule("UCtest", now=NOW)
    assert len(scheduled) == 6
    days = {utc_to_local(it.slot_utc).date() for it in scheduled}
    assert len(days) == 2  # 3 posts/day over 2 days

    # API mode: uploads everything within the horizon as private + publishAt
    rep = runner.run_once(cfg, now=NOW)
    assert len(rep.uploaded) == 6 and not rep.failed
    body = fake.uploaded_bodies[0]
    assert body["status"]["privacyStatus"] == "private" and body["status"]["publishAt"].endswith("Z")
    assert body["status"]["selfDeclaredMadeForKids"] is False and body["snippet"]["defaultLanguage"] == "hi"
    # after the slots pass, YouTube publishes them; the next run notices
    later = NOW + dt.timedelta(days=3)
    fake.publish_due(later)
    rep2 = runner.run_once(cfg, now=later)
    assert len(rep2.published) == 6
    assert rep2.synced == ["UCtest"]
    dash = analytics.ypp_progress("UCtest", cfg.ypp, utc_to_local(later).date())
    assert dash["subscribers"] == 120 and dash["shorts_views_90d"] == 500_000

    # assisted mode flow on a second channel: batch + manual Studio upload + reconcile
    fake2 = FakeYouTube("UCtwo")
    from clipforge.publish import youtube as yt

    yt.set_client_factory(lambda cid, c: fake2 if cid == "UCtwo" else fake)
    with db.session() as s:
        s.add(db.Channel(id="UCtwo", title="Two", uploads_playlist="UUtwo"))
        s.commit()
    two = q.add_clips("UCtwo", "epA", [1, 2])
    for it in two:
        q.generate_metadata(it.id, cfg)
    q.autoschedule("UCtwo", now=NOW)
    b = batch.prepare("UCtwo", cfg=cfg.with_overrides({"publishing.mode": "assisted"}))
    assert b["count"] == 2 and (tmp / "ws" / b["relative"] / "batch.html").exists()
    for it in q.items("UCtwo", ("queued",)):  # "I uploaded them in Studio"
        fake2.add_video(f"s{it.id}", it.title, int(it.duration), privacy="private",
                        publish_at=it.slot_utc.strftime("%Y-%m-%dT%H:%M:%SZ"))
    res = reconcile.sync("UCtwo", cfg)
    assert len(res["matched"]) == 2
    assert all(it.status == "scheduled" and it.youtube_video_id for it in q.items("UCtwo"))
    assert chs.get("UCtwo").confirmed is False  # assisted mode never needs the upload confirmation


def test_locked_private_detected(env):
    from clipforge.api import db
    from clipforge.config import load_config
    from clipforge.publish import queue as q, runner

    tmp, fake = env
    fake.lock_private = True
    cfg = load_config()
    _seed_episode(cfg.workspace, "epL", 1)
    with db.session() as s:
        s.add(db.Channel(id="UCtest", title="T", uploads_playlist="UUtest", confirmed=True))
        s.commit()
    it = q.add_clips("UCtest", "epL", None)[0]
    q.generate_metadata(it.id, cfg)
    q.autoschedule("UCtest", now=NOW)
    rep = runner.run_once(cfg, now=NOW)
    assert rep.locked == [it.id] and q.get(it.id).status == "locked_private"


def test_retry_backoff_and_unconfirmed_channel(env):
    from clipforge.api import db
    from clipforge.config import load_config
    from clipforge.publish import queue as q, runner

    tmp, fake = env
    fake.fail_uploads = 1
    cfg = load_config()
    _seed_episode(cfg.workspace, "epR", 1)
    with db.session() as s:
        s.add(db.Channel(id="UCtest", title="T", uploads_playlist="UUtest", confirmed=False))
        s.commit()
    it = q.add_clips("UCtest", "epR", None)[0]
    q.generate_metadata(it.id, cfg)
    q.autoschedule("UCtest", now=NOW)
    runner.run_once(cfg, now=NOW)
    assert q.get(it.id).status == "queued" and "Confirm" in q.get(it.id).error  # never uploads unconfirmed
    with db.session() as s:
        ch = s.get(db.Channel, "UCtest")
        ch.confirmed = True
        s.add(ch)
        s.commit()
    runner.run_once(cfg, now=NOW)
    first = q.get(it.id)
    assert first.status == "queued" and first.attempts == 1 and first.next_attempt_at is not None
    runner.run_once(cfg, now=NOW + dt.timedelta(minutes=1))
    assert q.get(it.id).attempts == 1  # still backing off
    runner.run_once(cfg, now=NOW + dt.timedelta(hours=1))
    assert q.get(it.id).status == "scheduled"


def test_process_lock_and_stuck_recovery(env):
    from clipforge.api import db
    from clipforge.config import load_config
    from clipforge.publish import queue as q, runner

    tmp, fake = env
    cfg = load_config()
    with runner._ProcessLock(cfg) as got:
        assert got
        rep = runner.run_once(cfg, now=NOW)  # second "process" is refused
        assert "Another ClipForge process" in rep.messages[0]
    _seed_episode(cfg.workspace, "epS", 1)
    with db.session() as s:
        s.add(db.Channel(id="UCtest", title="T", uploads_playlist="UUtest", confirmed=True))
        s.commit()
    it = q.add_clips("UCtest", "epS", None)[0]
    q.generate_metadata(it.id, cfg)
    q.autoschedule("UCtest", now=NOW)
    stuck = q.get(it.id)
    stuck.status = "uploading"  # crash mid-upload
    q.save(stuck)
    rep = runner.run_once(cfg, now=NOW)
    # never re-upload blindly: YouTube may already have the file
    assert rep.uploaded == [] and q.get(it.id).status == "needs_manual" and "interrupted" in q.get(it.id).error


def _one_channel_two_items(env, confirmed=True):
    from clipforge.api import db
    from clipforge.config import load_config
    from clipforge.publish import queue as q

    tmp, fake = env
    cfg = load_config()
    _seed_episode(cfg.workspace, "epU", 2)
    with db.session() as s:
        s.add(db.Channel(id="UCtest", title="T", uploads_playlist="UUtest", confirmed=confirmed))
        s.commit()
    its = q.add_clips("UCtest", "epU", None)
    for it in its:
        q.generate_metadata(it.id, cfg)
    q.autoschedule("UCtest", now=NOW)
    return cfg, fake, [q.get(i.id) for i in its]


def test_unschedule_during_run_is_respected(env):
    from clipforge.publish import queue as q, runner

    cfg, fake, (a, b) = _one_channel_two_items(env)
    first = sorted([a, b], key=lambda x: x.slot_utc)
    orig = fake.upload

    def upload_and_user_unschedules(path, body, on_progress=None):
        vid = orig(path, body, on_progress)
        q.unschedule(first[1].id)  # user clicks Unschedule while the first upload runs
        return vid

    fake.upload = upload_and_user_unschedules
    rep = runner.run_once(cfg, now=NOW)
    assert rep.uploaded == [first[0].id]
    assert q.get(first[1].id).status == "draft" and not q.get(first[1].id).youtube_video_id


def test_linked_or_batched_items_never_reuploaded(env):
    from clipforge.publish import batch, queue as q, runner

    cfg, fake, (a, b) = _one_channel_two_items(env)
    a.youtube_video_id, a.status = "studio1", "needs_manual"  # linked from Studio, not scheduled yet
    q.save(a)
    with pytest.raises(q.QueueError):
        q.move(a.id, NOW + dt.timedelta(days=2))
    b.batch = "batches/x/y"
    q.save(b)
    rep = runner.run_once(cfg, now=NOW)
    assert rep.uploaded == []
    assert fake.uploaded_bodies == []
    with pytest.raises(q.QueueError, match="API mode"):
        batch.prepare("UCtest", cfg=cfg)  # env is in API mode: batches would duplicate uploads


def test_refresh_error_marks_channel_and_continues(env):
    from google.auth.exceptions import RefreshError

    from clipforge.api import db
    from clipforge.publish import channels as chs, runner

    tmp, fake = env
    from clipforge.config import load_config

    cfg = load_config()
    with db.session() as s:
        s.add(db.Channel(id="UCa", title="A", uploads_playlist="UUa"))
        s.add(db.Channel(id="UCb", title="B", uploads_playlist="UUb"))
        s.commit()

    class Broken(FakeYouTube):
        def uploads(self, playlist_id, max_items=100):
            raise RefreshError("invalid_grant: Token has been expired or revoked.")

    from clipforge.publish import youtube as yt

    yt.set_client_factory(lambda cid, c: Broken(cid) if cid == "UCa" else FakeYouTube(cid))
    rep = runner.run_once(cfg, now=NOW)
    assert chs.get("UCa").auth_status == "needs_reauth"
    assert "UCb" in rep.synced


def test_batch_paths_and_missed_slots(env):
    from clipforge.publish import batch, queue as q

    from clipforge.config import load_config

    cfg = load_config().with_overrides({"publishing.mode": "assisted"})
    tmp, fake = env
    _seed_episode(cfg.workspace, "epB2", 3)
    from clipforge.api import db

    with db.session() as s:
        s.add(db.Channel(id="UCtest", title="T", uploads_playlist="UUtest"))
        s.commit()
    its = q.add_clips("UCtest", "epB2", None, cfg)
    for it in its:
        q.generate_metadata(it.id, cfg)
    q.autoschedule("UCtest", cfg=cfg, now=NOW)
    b1 = batch.prepare("UCtest", 1, cfg, now=NOW)
    b2 = batch.prepare("UCtest", 1, cfg, now=NOW)  # same minute: must not overwrite b1
    assert b1["relative"] != b2["relative"]
    later = NOW + dt.timedelta(days=30)  # all remaining slots are in the past now
    with pytest.raises(q.QueueError):
        batch.prepare("UCtest", None, cfg, now=later)
    left = [it for it in q.items("UCtest") if not it.batch]
    assert all(it.status == "missed" and it.reason == "slot_passed" for it in left)
