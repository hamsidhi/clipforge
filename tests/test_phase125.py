"""Phase 12.5: the "Needs you" bug, the explicit state machine, upload-ASAP, late policies, catch-up, health."""

from __future__ import annotations

import datetime as dt
import json

import pytest

import clipforge.config as config_mod
from clipforge.publish.timez import UTC
from tests.fake_youtube import FakeYouTube
from tests.test_publish import NOW, _seed_episode


def _env(tmp_path, monkeypatch, mode: str, extra: str = ""):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"output: {{dir: '{(tmp_path / 'ws').as_posix()}'}}\nllm: {{provider: mock}}\n"
                        f"publishing: {{mode: {mode}{extra}}}\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", cfg_path)
    from clipforge.api import db
    from clipforge.publish import youtube as yt

    monkeypatch.setattr(db, "_engine", None)
    db.engine()
    fake = FakeYouTube()
    yt.set_client_factory(lambda cid, cfg: fake)
    return fake


@pytest.fixture
def assisted(tmp_path, monkeypatch):
    yield _env(tmp_path, monkeypatch, "assisted")
    from clipforge.publish import youtube as yt

    yt.set_client_factory(None)


@pytest.fixture
def api(tmp_path, monkeypatch):
    yield _env(tmp_path, monkeypatch, "api")
    from clipforge.publish import youtube as yt

    yt.set_client_factory(None)


def _channel(cid="UCtest", confirmed=True):
    from clipforge.api import db

    with db.session() as s:
        s.add(db.Channel(id=cid, title="Test", uploads_playlist="UU" + cid[2:], confirmed=confirmed))
        s.commit()


def _schedule(n=3, video="ep1", now=NOW):
    from clipforge.config import load_config
    from clipforge.publish import queue as q

    cfg = load_config()
    _seed_episode(cfg.workspace, video, n, video)
    for it in q.add_clips("UCtest", video, None, cfg):
        q.generate_metadata(it.id, cfg)
    return q.autoschedule("UCtest", cfg=cfg, now=now)


# ----------------------------------------------------------------------------- the reported bug
def test_bug_studio_uploads_with_edited_titles_never_become_needs_you(assisted):
    """Assisted mode: the user uploads the clip files in Studio, edits titles AND times. Before 12.5, Sync matched
    only by title, so these stayed 'queued' and flipped to a reasonless 'Needs you' once ClipForge's slot passed."""
    from clipforge.config import load_config
    from clipforge.publish import batch, queue as q, runner

    cfg = load_config()
    _channel()
    items = _schedule(3)
    batch.prepare("UCtest", cfg=cfg, now=NOW)
    for k, it in enumerate(q.items("UCtest", ("queued",))):
        clip = q.clip_file(it, cfg)
        studio_time = (it.slot_utc + dt.timedelta(hours=5 + k)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assisted.add_video(f"v{k}", f"Totally different title {k} #shorts", int(it.duration), privacy="private",
                           publish_at=studio_time, file_name=clip.name)  # uploaded straight from clips/
    late = max(it.slot_utc for it in items) + dt.timedelta(minutes=5)  # every ClipForge slot has passed
    rep = runner.run_once(cfg, now=late.replace(tzinfo=UTC))
    after = q.items("UCtest")
    assert all(it.status == "scheduled" and it.youtube_video_id for it in after), [(it.status, it.reason) for it in after]
    assert not rep.missed
    # YouTube's (Studio) time wins
    assert all(it.slot_utc.strftime("%Y-%m-%dT%H:%M:%SZ") == assisted.store[it.youtube_video_id]["status"]["publishAt"]
               for it in after)


def test_assisted_passed_slots_get_reasons_not_bare_needs_you(assisted):
    from clipforge.config import load_config
    from clipforge.publish import batch, queue as q, runner, states

    cfg = load_config()
    _channel()
    items = _schedule(3)
    batch.prepare("UCtest", 1, cfg, now=NOW)  # only the first clip goes into a Studio batch
    late = max(it.slot_utc for it in items) + dt.timedelta(minutes=1)
    rep = runner.run_once(cfg, now=late.replace(tzinfo=UTC))
    got = {it.batch != "": (it.status, it.reason) for it in q.items("UCtest")}
    assert got[True] == ("needs_manual", "assisted_unlinked")
    assert got[False] == ("missed", "assisted_no_batch")
    assert len(rep.missed) == 3
    for it in q.items("UCtest"):
        assert it.error and states.label(it.reason) and states.actions(it.reason)
        assert "reschedule" in states.actions(it.reason)


def test_studio_upload_without_schedule_is_explained(assisted):
    from clipforge.config import load_config
    from clipforge.publish import batch, queue as q, reconcile

    cfg = load_config()
    _channel()
    _schedule(1)
    batch.prepare("UCtest", cfg=cfg, now=NOW)
    it = q.items("UCtest")[0]
    assisted.add_video("vx", "x", int(it.duration), privacy="private", file_name=q.clip_file(it, cfg).name)
    reconcile.sync("UCtest", cfg)
    it = q.get(it.id)
    assert (it.status, it.reason) == ("needs_manual", "not_scheduled_in_studio")


def test_reconcile_title_with_hashtags_and_shortened():
    from clipforge.publish.reconcile import title_score

    assert title_score("5 Friends, 5 Future: Success Secrets", "5 Friends, 5 Future #shorts") >= 0.8
    assert title_score("High Bar AI Products", "High Bar for AI Products #shorts") >= 0.8
    assert title_score("Why people do weird stuff", "Short-term pain for long-term gain") < 0.5


# ----------------------------------------------------------------------------- API mode
def test_api_uploads_as_soon_as_scheduled(api):
    from clipforge.config import load_config
    from clipforge.publish import queue as q, runner

    cfg = load_config()
    _channel()
    far = NOW + dt.timedelta(days=20)
    items = _schedule(2, now=far)  # slots ~3 weeks away
    rep = runner.run_once(cfg, now=NOW)
    assert len(rep.uploaded) == 2
    for body, it in zip(api.uploaded_bodies, items):
        assert body["status"]["privacyStatus"] == "private" and body["status"]["publishAt"]
    it = q.get(items[0].id)
    assert it.status == "scheduled" and it.yt_state["processingStatus"] == "processing"
    assert it.yt_state["privacyStatus"] == "private" and it.yt_state["publishAt"]


@pytest.mark.parametrize("policy", ["next_slot", "ask", "publish_now"])
def test_slot_too_close_policies(tmp_path, monkeypatch, policy):
    fake = _env(tmp_path, monkeypatch, "api", f", late_policy: {policy}")
    try:
        from clipforge.config import load_config
        from clipforge.publish import queue as q, runner

        cfg = load_config()
        _channel()
        items = _schedule(1)
        slot = items[0].slot_utc.replace(tzinfo=UTC)
        rep = runner.run_once(cfg, now=slot - dt.timedelta(minutes=5))  # laptop woke up 5 min before the slot
        it = q.get(items[0].id)
        if policy == "next_slot":
            assert it.slot_utc.replace(tzinfo=UTC) > slot and rep.rescheduled == [it.id]
            assert it.status == "scheduled" and "Moved automatically" in it.error or it.error == ""
            assert fake.uploaded_bodies and fake.uploaded_bodies[0]["status"]["publishAt"]
        elif policy == "ask":
            assert (it.status, it.reason) == ("missed", "slot_too_close") and not fake.uploaded_bodies
        else:
            assert fake.uploaded_bodies[0]["status"] == {**fake.uploaded_bodies[0]["status"], "privacyStatus": "public"}
            assert "publishAt" not in fake.uploaded_bodies[0]["status"]
    finally:
        from clipforge.publish import youtube as yt

        yt.set_client_factory(None)


def test_catch_up_after_laptop_was_off(api):
    """Off for 2 days: passed slots move to the next free slots (never a silent miss), future ones upload."""
    from clipforge.config import load_config
    from clipforge.publish import queue as q, runner

    cfg = load_config()
    _channel()
    items = _schedule(6)  # 2 days of slots
    back = NOW + dt.timedelta(days=2, hours=6)
    rep = runner.run_once(cfg, now=back)
    statuses = [q.get(it.id).status for it in items]
    assert all(s == "scheduled" for s in statuses), statuses
    assert all(q.get(it.id).slot_utc.replace(tzinfo=UTC) > back for it in items)
    assert rep.rescheduled


def test_batched_clip_in_api_mode_is_explained_and_can_be_unbatched(api, monkeypatch):
    from clipforge.config import load_config
    from clipforge.publish import batch, queue as q, runner

    cfg = load_config()
    _channel()
    _schedule(1, now=NOW + dt.timedelta(days=3))
    batch.prepare("UCtest", cfg=cfg, now=NOW, force=True, ids=[q.items("UCtest")[0].id])
    runner.run_once(cfg, now=NOW)
    it = q.items("UCtest")[0]
    assert it.status == "queued" and it.reason == "batched_in_api" and not api.uploaded_bodies
    from clipforge.api.publish_routes import _do_unbatch

    _do_unbatch(it.id)
    runner.run_once(cfg, now=NOW)
    assert q.get(it.id).status == "scheduled" and api.uploaded_bodies


def test_locked_private_and_studio_time_change_followed(api):
    from clipforge.config import load_config
    from clipforge.publish import queue as q, runner

    cfg = load_config()
    _channel()
    items = _schedule(2, now=NOW + dt.timedelta(days=1))
    runner.run_once(cfg, now=NOW)
    a, b = (q.get(it.id) for it in items)
    api.store[a.youtube_video_id]["status"].pop("publishAt")  # YouTube dropped the schedule
    new_time = "2026-12-01T13:00:00Z"
    api.store[b.youtube_video_id]["status"]["publishAt"] = new_time  # user changed the time in Studio
    runner.run_once(cfg, now=NOW + dt.timedelta(minutes=11))
    a, b = q.get(a.id), q.get(b.id)
    assert (a.status, a.reason) == ("locked_private", "locked_private")
    assert b.slot_utc.strftime("%Y-%m-%dT%H:%M:%SZ") == new_time


def test_private_and_unlisted_by_choice(api):
    from clipforge.config import load_config
    from clipforge.publish import queue as q, runner

    cfg = load_config()
    _channel()
    items = _schedule(1, now=NOW + dt.timedelta(days=2))
    it = q.get(items[0].id)
    it.privacy = "unlisted"
    q.save(it)
    runner.run_once(cfg, now=NOW)
    body = api.uploaded_bodies[0]["status"]
    assert body["privacyStatus"] == "unlisted" and "publishAt" not in body
    assert q.get(it.id).status == "uploaded"


def test_quota_rollover_and_login_expired(api, monkeypatch):
    from clipforge.config import load_config
    from clipforge.publish import channels as chs, queue as q, runner
    from clipforge.publish import youtube as yt

    cfg = load_config()
    _channel()
    items = _schedule(3, now=NOW + dt.timedelta(days=1))
    calls = {"n": 0}
    real = api.upload

    def limited(path, body, on_progress=None):
        calls["n"] += 1
        if calls["n"] > 1:
            raise yt.QuotaExceeded("Daily upload quota used up")
        return real(path, body, on_progress)

    monkeypatch.setattr(api, "upload", limited)
    rep = runner.run_once(cfg, now=NOW)
    assert rep.quota_exhausted and len(rep.uploaded) == 1
    waiting = [q.get(it.id) for it in items if q.get(it.id).status == "queued"]
    assert waiting and all(it.reason == "quota" for it in waiting)
    chs.mark_reauth("UCtest", "invalid_grant")
    runner.run_once(cfg, now=NOW + dt.timedelta(hours=1))
    assert all(q.get(it.id).reason == "login_expired" for it in waiting)


def test_scheduler_state_file_written(api):
    from clipforge.config import load_config
    from clipforge.publish import runner

    cfg = load_config()
    _channel()
    runner.run_once(cfg, now=NOW, source="task")
    st = runner.read_state(cfg)
    assert st["last_source"] == "task" and st["runs"]["task"].startswith("2026-11-04")


def test_task_xml_has_logon_interval_and_wake_triggers():
    from clipforge.cli_publish import scheduler_task_xml

    xml = scheduler_task_xml()
    assert "<LogonTrigger>" in xml and "PT30M" in xml and "Power-Troubleshooter" in xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml and "<Hidden>true</Hidden>" in xml
    assert "scheduler --once" in xml
    d = scheduler_task_xml(daemon=True)
    assert "scheduler --daemon" in d and "RestartOnFailure" in d


def test_keep_awake_state_machine(monkeypatch):
    from clipforge.utils import power

    calls = []
    k = power.KeepAwake(call=lambda flags: calls.append(flags) or 1)
    monkeypatch.setattr(power, "keeper", k)
    monkeypatch.setattr(power, "busy_reason", lambda now=None: "")
    assert power.update("when_busy") is False and calls == []
    monkeypatch.setattr(power, "busy_reason", lambda now=None: "a job")
    assert power.update("when_busy") is True
    assert calls[-1] == power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED
    power.update("when_busy")
    assert len(calls) == 1  # no repeated calls while holding
    assert power.update("never") is False and calls[-1] == power.ES_CONTINUOUS
    assert power.update("always") is True


# ----------------------------------------------------------------------------- API endpoints
def test_api_one_click_fixes_health_and_audit(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    _env(tmp_path, monkeypatch, "assisted")
    from clipforge.api.server import create_app
    from clipforge.publish import queue as q, runner

    app = create_app()
    app.state.publish_ticker = False
    try:
        with TestClient(app) as c:
            from clipforge.config import load_config

            cfg = load_config()
            _channel()
            items = _schedule(3)
            late = max(it.slot_utc for it in items) + dt.timedelta(minutes=1)
            runner.run_once(cfg, now=late.replace(tzinfo=UTC))
            rows = c.get("/api/queue?channel_id=UCtest").json()
            assert all(r["status"] == "missed" and r["reason_label"] and r["actions"] for r in rows)
            r = c.post("/api/queue/bulk", json={"action": "reschedule_missed", "channel_id": "UCtest"}).json()
            assert len(r["done"]) == 3 and not r["errors"]
            assert all(it.status == "queued" for it in q.items("UCtest"))
            # publish now is API-only
            assert c.post(f"/api/queue/{items[0].id}/publish-now").status_code == 400
            # editable title limits + privacy selector
            assert c.patch(f"/api/queue/{items[0].id}", json={"title": "x" * 101}).status_code == 400
            assert c.patch(f"/api/queue/{items[0].id}", json={"privacy": "unlisted"}).json()["privacy"] == "unlisted"
            h = c.get("/api/publishing/health").json()
            assert h["accounts"][0]["token"]["ok"] is False and "last_run_at" in h and "quota" in h
            a = c.post("/api/publishing/audit", json={"status": "approved", "applied_on": "2026-11-04"}).json()
            assert a["api_audited"] is True and a["audit_status"] == "approved"
            assert c.post("/api/publishing/audit", json={"status": "applied", "applied_on": "4/11"}).status_code == 400
            d = c.delete("/api/channels/UCtest").json()
            assert d["ok"] and "test mode" in d["revoked"]
    finally:
        from clipforge.publish import youtube as yt

        yt.set_client_factory(None)
