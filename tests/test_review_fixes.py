"""Regression tests for the Phase 12.5/13 review findings (each failed before its fix)."""

from __future__ import annotations

import datetime as dt

import pytest

from clipforge.publish.timez import UTC
from tests.test_instagram import _connect, _run, _schedule_ig, env  # noqa: F401  (fixture)
from tests.test_phase125 import _channel, _schedule, api  # noqa: F401  (fixture)
from tests.test_publish import NOW


def test_bulk_reschedule_never_touches_possibly_uploaded_clips(api):  # noqa: F811
    from clipforge.api.publish_routes import BulkIn, queue_bulk
    from clipforge.publish import queue as q

    _channel()
    a, b, c = _schedule(3, now=NOW + dt.timedelta(days=1))
    for it, (status, reason) in zip((a, b, c), (("needs_manual", "interrupted"), ("needs_manual", "batched_in_api"),
                                                 ("missed", "slot_passed"))):
        x = q.get(it.id)
        x.status, x.reason = status, reason
        q.save(x)
    r = queue_bulk(BulkIn(action="reschedule_missed", channel_id="UCtest"))
    assert r["done"] == [c.id]
    assert q.get(a.id).reason == "interrupted" and q.get(b.id).reason == "batched_in_api"


def test_quota_exhaustion_does_not_burn_attempts(api, monkeypatch):  # noqa: F811
    from clipforge.config import load_config
    from clipforge.publish import queue as q, runner
    from clipforge.publish import youtube as yt

    cfg = load_config()
    _channel()
    items = _schedule(2, now=NOW + dt.timedelta(days=1))
    monkeypatch.setattr(api, "upload", lambda *a, **k: (_ for _ in ()).throw(yt.QuotaExceeded("used up")))
    for m in range(5):
        runner.run_once(cfg, now=NOW + dt.timedelta(minutes=m))
    assert all(q.get(it.id).attempts == 0 and q.get(it.id).reason == "quota" for it in items)


def test_old_bare_needs_manual_rows_get_a_reason(api):  # noqa: F811
    from clipforge.config import load_config
    from clipforge.publish import queue as q, runner

    _channel()
    it = q.get(_schedule(1)[0].id)
    it.status, it.reason, it.error = "needs_manual", "", "The publish time passed."
    q.save(it)
    runner.run_once(load_config(), now=NOW)
    it = q.get(it.id)
    assert it.reason and it.status in ("missed", "needs_manual")


def test_reel_published_but_answer_lost_is_recorded_not_reposted(env):  # noqa: F811
    from clipforge.publish import queue as q

    fake, _ = env
    ch = _connect()
    it = _schedule_ig(ch.id, 1)[0]
    slot = it.slot_utc.replace(tzinfo=UTC)
    _run(slot - dt.timedelta(minutes=20))
    _run(slot - dt.timedelta(minutes=19))
    fake.lose_publish_response = True
    _run(slot + dt.timedelta(minutes=1))  # published at Meta, but we never heard back
    for m in range(2, 6):
        _run(slot + dt.timedelta(minutes=m))
    got = q.get(it.id)
    assert len(fake.published) == 1 and got.status == "published" and got.ig_media_id == "m1" and got.permalink


def test_instagram_publish_now_after_missed_with_container(env):  # noqa: F811
    from clipforge.api.publish_routes import _do_publish_now
    from clipforge.publish import queue as q

    fake, _ = env
    ch = _connect()
    it = _schedule_ig(ch.id, 1)[0]
    slot = it.slot_utc.replace(tzinfo=UTC)
    _run(slot - dt.timedelta(minutes=20))
    _run(slot + dt.timedelta(hours=3))  # laptop off → missed while a container existed
    assert q.get(it.id).status == "missed"
    _do_publish_now(it.id)
    from clipforge.publish.timez import now_utc

    t = now_utc()
    for m in range(3):
        _run(t + dt.timedelta(minutes=m))
    assert q.get(it.id).status == "published" and len(fake.published) == 1


def test_crash_mid_upload_resumes_next_run(env):  # noqa: F811
    from clipforge.publish import queue as q

    fake, _ = env
    fake.upload_crash_at = 200
    ch = _connect()
    it = _schedule_ig(ch.id, 1)[0]
    slot = it.slot_utc.replace(tzinfo=UTC)
    with pytest.raises(KeyboardInterrupt):
        _run(slot - dt.timedelta(minutes=20))
    assert q.get(it.id).status == "uploading"
    _run(slot - dt.timedelta(minutes=19))
    assert "upload@200" in fake.calls
    _run(slot - dt.timedelta(minutes=18))
    _run(slot + dt.timedelta(minutes=1))
    assert q.get(it.id).status == "published"


def test_keep_awake_ignores_past_paused_and_assisted(env, monkeypatch):  # noqa: F811
    from clipforge.publish import channels as chs
    from clipforge.utils import power

    ch = _connect()
    it = _schedule_ig(ch.id, 1)[0]
    slot = it.slot_utc.replace(tzinfo=UTC)
    assert power.busy_reason(slot - dt.timedelta(minutes=30))
    assert not power.busy_reason(slot + dt.timedelta(days=3))  # long past: no reason to stay awake
    chs.update(ch.id, paused=True)
    assert not power.busy_reason(slot - dt.timedelta(minutes=30))


def test_revoke_keeps_shared_facebook_login(env):  # noqa: F811
    from clipforge.publish import channels as chs
    from clipforge.publish import instagram as ig

    fake, _ = env
    ch = _connect()
    tok = ig.load_tokens("1789")
    other = chs.register_instagram({"ig_user_id": "2000", "username": "second"}, tok["user_token_expires_at"])
    ig.save_tokens("2000", tok)
    msg = ig.revoke(ch)
    assert "kept" in msg and fake.revoked == []
    msg2 = ig.revoke(other)
    assert fake.revoked == [tok["user_token"]] and "revoked" in msg2


def test_permalink_must_be_an_instagram_https_url(env):  # noqa: F811
    from fastapi.testclient import TestClient

    from clipforge.api.server import create_app
    from clipforge.publish import channels as chs

    ch = chs.add_manual_instagram("me.reels")
    it = _schedule_ig(ch.id, 1)[0]
    app = create_app()
    app.state.publish_ticker = False
    with TestClient(app) as c:
        for bad in ("javascript:alert(1)//instagram.com/x", "https://evil.com/instagram.com/reel/x", "https://www.instagram.com/"):
            assert c.post(f"/api/queue/{it.id}/permalink", json={"permalink": bad}).status_code == 400
