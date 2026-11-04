"""Phase 13: Instagram Reels — fake Graph API, no network."""

from __future__ import annotations

import datetime as dt
import json

import pytest

import clipforge.config as config_mod
from clipforge.publish.timez import UTC
from tests.fake_instagram import FakeInstagram
from tests.fake_youtube import FakeYouTube
from tests.test_publish import NOW, _seed_episode


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"output: {{dir: '{(tmp_path / 'ws').as_posix()}'}}\nllm: {{provider: mock}}\n"
                        "publishing: {mode: api}\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", cfg_path)
    from clipforge.api import db
    from clipforge.publish import instagram as ig
    from clipforge.publish import youtube as yt

    monkeypatch.setattr(db, "_engine", None)
    db.engine()
    fake, fyt = FakeInstagram(), FakeYouTube()
    ig.set_client_factory(lambda cfg: fake)
    yt.set_client_factory(lambda cid, cfg: fyt)
    monkeypatch.setattr(ig, "preflight", lambda path: [])  # seeded clips are 1-second stubs
    yield fake, fyt
    ig.set_client_factory(None)
    yt.set_client_factory(None)


def _connect(now=NOW, confirm=True):
    from clipforge.publish import channels as chs
    from clipforge.publish import instagram as ig

    state = "s1"
    ig._flows[state] = ("http://localhost:8000/api/instagram/oauth/callback", 0)
    res = ig.finish_oauth("good-code", state, now=now)
    assert [a["username"] for a in res["accounts"]] == ["figuring.clips"]  # the Page without IG is ignored
    assert "page_token" not in res["accounts"][0]  # tokens never leave the server
    ch = ig.pick(res["pick"], res["accounts"][0]["ig_user_id"])
    if confirm:
        chs.update(ch.id, confirmed=True)
    return chs.get(ch.id)


def _schedule_ig(ch_id: str, n=2, video="ep1", now=NOW):
    from clipforge.config import load_config
    from clipforge.publish import queue as q

    cfg = load_config()
    if not (cfg.workspace / video).exists():
        _seed_episode(cfg.workspace, video, n, video)
    for it in q.add_clips(ch_id, video, None, cfg):
        q.generate_metadata(it.id, cfg)
    return q.autoschedule(ch_id, cfg=cfg, now=now)


def _run(now):
    from clipforge.config import load_config
    from clipforge.publish import runner

    return runner.run_once(load_config(), now=now)


# ----------------------------------------------------------------------------- accounts / tokens
def test_oauth_exchange_page_discovery_and_long_lived_refresh(env):
    from clipforge.config import load_config
    from clipforge.publish import instagram as ig

    fake, _ = env
    ch = _connect()
    assert ch.platform == "instagram" and ch.id == "ig:1789" and ch.extra["page_name"] == "Figuring Clips Page"
    tok = ig.load_tokens("1789")
    assert tok["page_token"] == "page-token" and tok["user_token"].startswith("long-token")
    cfg = load_config()
    assert ig.refresh_if_needed(ch, cfg, NOW, fake) is False  # 60 days left
    assert ig.refresh_if_needed(ch, cfg, NOW + dt.timedelta(days=55), fake) is True
    assert ig.load_tokens("1789")["user_token"] == "long-token-2"
    assert ig.token_health(ch)["ok"]


def test_expired_or_used_state_is_refused(env):
    from clipforge.publish import instagram as ig

    with pytest.raises(ValueError):
        ig.finish_oauth("good-code", "never-issued")


# ----------------------------------------------------------------------------- publishing timing
def test_upload_early_publish_at_slot(env):
    from clipforge.publish import queue as q

    fake, _ = env
    ch = _connect()
    items = _schedule_ig(ch.id, 1)
    it = items[0]
    slot = it.slot_utc.replace(tzinfo=UTC)
    _run(slot - dt.timedelta(minutes=40))  # too early: nothing
    assert q.get(it.id).status == "queued" and not fake.containers
    _run(slot - dt.timedelta(minutes=20))  # lead time: container + upload
    got = q.get(it.id)
    assert got.status in ("uploading", "ready") and got.ig_container_id and not fake.published
    _run(slot - dt.timedelta(minutes=19))
    assert q.get(it.id).status == "ready" and not fake.published  # processed, waits for the slot
    rep = _run(slot + dt.timedelta(seconds=30))
    got = q.get(it.id)
    assert got.status == "published" and got.permalink.startswith("https://www.instagram.com/reel/")
    assert rep.published == [it.id]
    cap = fake.published[0]["caption"]
    tags = [w for w in cap.split() if w.startswith("#")]
    assert 1 <= len(tags) <= 5 and all(t == t.lower() for t in tags) and "#shorts" not in tags


def test_resumable_upload_resumes_after_drop(env):
    from clipforge.publish import queue as q

    fake, _ = env
    fake.fail_upload_once_at = 100
    ch = _connect()
    it = _schedule_ig(ch.id, 1)[0]
    _run(it.slot_utc.replace(tzinfo=UTC) - dt.timedelta(minutes=20))
    assert fake.calls[-1] == "upload@100"  # second attempt continued at the byte Meta reported
    assert q.get(it.id).status in ("uploading", "ready")


def test_container_error_and_expired(env):
    from clipforge.publish import queue as q

    fake, _ = env
    ch = _connect()
    a, b = _schedule_ig(ch.id, 2)
    fake.error_containers = True
    _run(a.slot_utc.replace(tzinfo=UTC) - dt.timedelta(minutes=15))
    assert (q.get(a.id).status, q.get(a.id).reason) == ("failed", "ig_container_failed")
    fake.error_containers = False
    t = b.slot_utc.replace(tzinfo=UTC) - dt.timedelta(minutes=15)
    _run(t)
    cid = q.get(b.id).ig_container_id
    fake.containers[cid]["status"] = "EXPIRED"
    _run(t + dt.timedelta(minutes=1))
    got = q.get(b.id)
    assert got.status == "queued" and not got.ig_container_id
    _run(t + dt.timedelta(minutes=2))
    assert q.get(b.id).ig_container_id and q.get(b.id).ig_container_id != cid


def test_missed_slot_is_never_posted_late(env):
    from clipforge.publish import queue as q, states

    fake, _ = env
    ch = _connect()
    it = _schedule_ig(ch.id, 1)[0]
    rep = _run(it.slot_utc.replace(tzinfo=UTC) + dt.timedelta(minutes=120))  # laptop was off
    got = q.get(it.id)
    assert (got.status, got.reason) == ("missed", "ig_missed") and not fake.published and rep.missed == [it.id]
    assert "reschedule" in states.actions(got.reason) and "publish_now" in states.actions(got.reason)


def test_publishing_limit_respected(env):
    from clipforge.publish import queue as q

    fake, _ = env
    fake.quota_usage = 95  # 100 limit − 10 margin
    ch = _connect()
    it = _schedule_ig(ch.id, 1)[0]
    slot = it.slot_utc.replace(tzinfo=UTC)
    _run(slot - dt.timedelta(minutes=20))
    _run(slot - dt.timedelta(minutes=19))
    _run(slot + dt.timedelta(minutes=1))
    got = q.get(it.id)
    assert got.status == "ready" and got.reason == "ig_limit" and not fake.published


def test_auth_failure_marks_reauth_and_never_crashes(env):
    from clipforge.publish import channels as chs, queue as q

    fake, _ = env
    ch = _connect()
    it = _schedule_ig(ch.id, 1)[0]
    fake.auth_broken = True
    rep = _run(it.slot_utc.replace(tzinfo=UTC) - dt.timedelta(minutes=20))
    assert chs.get(ch.id).auth_status == "needs_reauth" and rep.skipped_channels[ch.id] == "needs re-login"
    _run(it.slot_utc.replace(tzinfo=UTC) - dt.timedelta(minutes=19))
    assert q.get(it.id).reason == "login_expired"


def test_insights_sync(env):
    from clipforge.api import db

    fake, _ = env
    ch = _connect()
    it = _schedule_ig(ch.id, 1)[0]
    slot = it.slot_utc.replace(tzinfo=UTC)
    for m in (-20, -19, 1):
        _run(slot + dt.timedelta(minutes=m))
    _run(slot + dt.timedelta(hours=25))
    with db.session() as s:
        perf = s.get(db.VideoPerf, f"ig:{fake.published[0]['id']}")
    assert perf and perf.views == 1500 and perf.saves == 14 and perf.platform == "instagram"


# ----------------------------------------------------------------------------- captions
def test_caption_rules():
    from clipforge.publish import ig_meta

    c = ig_meta.clean_caption("Big idea\n\n#Money #STARTUP #money #a #b #c #d #e")
    assert ig_meta.hashtags(c) == ["#money", "#startup", "#a", "#b", "#c"]
    assert ig_meta.problems(c) == []
    assert ig_meta.problems("x #A") == ["Hashtags must be lowercase."]
    assert len(ig_meta.clean_caption("y" * 3000)) == 2200
    built = ig_meta.build("Hook!", "Value line.", "🎙 Source: X", ["#Reels", "podcast"])
    assert "Follow for more" in built and built.endswith("#reels #podcast")


# ----------------------------------------------------------------------------- cross-platform + assisted
def test_cross_platform_hours_after_youtube(env):
    from clipforge.api import db
    from clipforge.config import load_config
    from clipforge.publish import channels as chs, queue as q

    ch = _connect()
    chs.update(ch.id, settings_json=json.dumps({"hours_after_youtube": 3}))
    with db.session() as s:
        s.add(db.Channel(id="UCtest", title="YT", uploads_playlist="UUtest", confirmed=True))
        s.commit()
    cfg = load_config()
    _seed_episode(cfg.workspace, "ep9", 3, "x")
    for cid in ("UCtest", ch.id):
        for it in q.add_clips(cid, "ep9", None, cfg):
            q.generate_metadata(it.id, cfg)
    yt_items = {it.clip_key: it for it in q.autoschedule("UCtest", cfg=cfg, now=NOW)}
    ig_items = q.autoschedule(ch.id, cfg=cfg, now=NOW)
    assert len(ig_items) == 3
    for it in ig_items:
        assert it.platform == "instagram"
        assert it.slot_utc.replace(tzinfo=UTC) >= yt_items[it.clip_key].slot_utc.replace(tzinfo=UTC) + dt.timedelta(hours=3)


def test_manual_account_assisted_kit_and_permalink(env, tmp_path):
    from fastapi.testclient import TestClient

    from clipforge.api.server import create_app
    from clipforge.publish import batch, channels as chs, queue as q

    ch = chs.add_manual_instagram("@my.reels")
    items = _schedule_ig(ch.id, 2)
    from clipforge.config import load_config

    b = batch.prepare(ch.id, cfg=load_config(), now=NOW)
    html = (load_config().workspace / b["relative"] / "batch.html").read_text(encoding="utf-8")
    assert "Instagram kit" in html and "Meta Business Suite" in html
    _run(items[0].slot_utc.replace(tzinfo=UTC) + dt.timedelta(minutes=1))
    assert (q.get(items[0].id).status, q.get(items[0].id).reason) == ("needs_manual", "ig_assisted")
    app = create_app()
    app.state.publish_ticker = False
    with TestClient(app) as c:
        r = c.post(f"/api/queue/{items[0].id}/permalink", json={"permalink": "https://www.instagram.com/reel/AbC/"})
        assert r.status_code == 200 and r.json()["status"] == "published"
        assert c.post(f"/api/queue/{items[1].id}/permalink", json={"permalink": "https://www.instagram.com/reel/AbC/"}).status_code == 409
        assert c.post(f"/api/queue/{items[1].id}/permalink", json={"permalink": "nope"}).status_code == 400


def test_preflight_real_files(tmp_path):
    from clipforge.publish import instagram as ig
    from clipforge.utils.ffmpeg import run_ffmpeg

    good = tmp_path / "good.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "color=black:s=108x192:d=4:r=30", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(good)])
    assert ig.preflight(good) == []
    short = tmp_path / "short.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "color=black:s=108x192:d=1:r=30", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(short)])
    probs = " ".join(ig.preflight(short))
    assert "3 s to 15 min" in probs and "moov" in probs
    assert ig.preflight(tmp_path / "missing.mp4")


# ----------------------------------------------------------------------------- API e2e
def test_api_e2e_both_platforms(env):
    from fastapi.testclient import TestClient

    from clipforge.api import db
    from clipforge.api.server import create_app
    from clipforge.publish import queue as q
    from clipforge.publish.timez import now_utc

    fake, fyt = env
    app = create_app()
    app.state.publish_ticker = False
    with TestClient(app) as c:
        ch = _connect()
        with db.session() as s:
            s.add(db.Channel(id="UCtest", title="YT", uploads_playlist="UUtest", confirmed=True))
            s.add(db.Job(source="x", video_id="epE", title="E", status="done"))
            s.commit()
        from clipforge.config import load_config

        _seed_episode(load_config().workspace, "epE", 4, "e")
        job_id = c.get("/api/jobs").json()[0]["id"] if c.get("/api/jobs").status_code == 200 else 1
        r = c.post("/api/queue", json={"channel_ids": ["UCtest", ch.id], "video_id": "epE"})
        assert r.status_code == 200, r.text
        assert len(r.json()["added"]) == 8
        for _ in range(100):  # metadata is written by the publish worker thread
            if all(it.meta_status == "ready" for it in q.items()):
                break
            import time

            time.sleep(0.05)
        now = now_utc()
        for cid in ("UCtest", ch.id):
            assert len(c.post(f"/api/channels/{cid}/autoschedule", json={}).json()["scheduled"]) == 4
        rows = c.get("/api/queue").json()
        assert {r["platform"] for r in rows} == {"youtube", "instagram"}
        # walk the clock through every slot: YouTube uploads at once, Instagram at slot − 20 min → slot
        slots = sorted({it.slot_utc.replace(tzinfo=UTC) for it in q.items()})
        from clipforge.config import load_config as lc
        from clipforge.publish import runner

        runner.run_once(lc(), now=now)
        for s_ in slots:
            for m in (-20, -19, 1):
                runner.run_once(lc(), now=s_ + dt.timedelta(minutes=m))
        fyt.publish_due(slots[-1] + dt.timedelta(hours=1))
        runner.run_once(lc(), now=slots[-1] + dt.timedelta(hours=1))
        st = {(it.platform, it.status) for it in q.items()}
        assert st == {("youtube", "published"), ("instagram", "published")}, st
        ov = c.get("/api/publishing/overview").json()
        assert ov["instagram"]["posts"] == 4 and ov["youtube"]["posts"] == 4
        d = c.get(f"/api/accounts/{ch.id}/ig-dashboard").json()
        assert d["reels"] == 4
        rem = c.get("/api/posts/reminders").json()
        assert any(r["platform"] == "instagram" and "instagram.com/reel" in r["url"] for r in rem)
        assert job_id
