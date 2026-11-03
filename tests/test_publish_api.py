"""Publishing API endpoints (fake YouTube, mock LLM, isolated workspace)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import clipforge.config as config_mod
from tests.fake_youtube import FakeYouTube
from tests.test_publish import _seed_episode


@pytest.fixture
def api(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"output: {{dir: '{(tmp_path / 'ws').as_posix()}'}}\nllm: {{provider: mock}}\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config_mod, "ROOT_DIR", tmp_path)
    from clipforge.api import db
    from clipforge.publish import youtube as yt

    monkeypatch.setattr(yt, "client_secret_path", lambda: tmp_path / "client_secret.json")
    monkeypatch.setattr(db, "_engine", None)
    fake = FakeYouTube("UCapi")
    yt.set_client_factory(lambda cid, cfg: fake)
    from clipforge.api.server import create_app

    app = create_app()
    app.state.publish_ticker = False
    with TestClient(app) as c:
        with db.session() as s:
            s.add(db.Channel(id="UCapi", title="API Channel", uploads_playlist="UUapi"))
            s.commit()
        from clipforge.config import load_config

        _seed_episode(load_config().workspace, "epX", 4, "x")
        yield c, fake
    yt.set_client_factory(None)
    db._engine = None


def _wait_meta(c, n, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        items = c.get("/api/queue?channel_id=UCapi").json()
        if len(items) >= n and all(i["meta_status"] != "pending" for i in items):
            return items
        time.sleep(0.2)
    raise AssertionError("metadata not generated")


def test_full_flow(api):
    c, fake = api
    st = c.get("/api/publishing/status").json()
    assert st["mode"] == "assisted" and st["uploads_left_today"] == 100 and st["banner"]
    assert c.post("/api/channels/oauth/start").status_code == 400  # no client_secret.json yet
    assert c.post("/api/publishing/client-secret", content=b'{"nope": 1}').status_code == 400
    ok = c.post("/api/publishing/client-secret", content=b'{"installed": {"client_id": "123.apps.googleusercontent.com"}}')
    assert ok.status_code == 200

    r = c.post("/api/queue", json={"channel_id": "UCapi", "video_id": "epX"})
    assert r.status_code == 200 and len(r.json()["added"]) == 4
    assert c.post("/api/queue", json={"channel_id": "UCapi", "video_id": "epX"}).json()["added"] == []  # duplicate guard
    items = _wait_meta(c, 4)
    assert all(i["title"] and "#shorts" in i["description"] for i in items)

    sch = c.post("/api/channels/UCapi/autoschedule", json={}).json()
    assert len(sch["scheduled"]) == 4
    first = sch["scheduled"][0]
    slots = c.get("/api/channels/UCapi/free-slots?n=3").json()
    moved = c.patch(f"/api/queue/{first['id']}", json={"slot_local": slots[0]["slot_local"], "title": "My own title 123"})
    assert moved.status_code == 200 and moved.json()["slot_local"] == slots[0]["slot_local"]
    assert c.patch(f"/api/queue/{first['id']}", json={"slot_local": "2020-01-01T10:00"}).status_code == 400
    bulk = c.post("/api/queue/bulk", json={"ids": [sch["scheduled"][1]["id"]], "action": "shift", "days": 7}).json()
    assert bulk["done"]

    b = c.post("/api/channels/UCapi/batch", json={"n": 2}).json()
    assert b["count"] == 2
    page = c.get(b["url"])
    assert page.status_code == 200 and "YouTube Studio" in page.text
    assert c.get("/api/batches/files/../config.yaml").status_code == 404
    assert c.get("/api/batches/files/%2F%2Fattacker%2Fshare%2Fx").status_code == 404  # UNC path: never resolved
    assert c.get("/api/batches/files/batches%2F..%2F..%2Fconfig.yaml").status_code == 404

    # "uploaded in Studio": reconcile links them
    for it in c.get("/api/queue?channel_id=UCapi&status=queued").json()[:2]:
        fake.add_video(f"v{it['id']}", it["title"], int(it["duration"]), privacy="private",
                       publish_at=it["slot_utc"].replace("+00:00", "Z"))
    sync = c.post("/api/channels/UCapi/sync").json()
    assert len(sync["matched"]) == 2
    dash = c.get("/api/channels/UCapi/dashboard").json()
    assert {"ypp", "working", "heatmap", "recommendation", "reminders"} <= set(dash)
    assert not dash["recommendation"]["ready"]
    assert c.post("/api/channels/UCapi/apply-slots", json={"slots": ["25:00"]}).status_code == 400

    p = c.post("/api/posts", json={"clip_title": "x", "platform": "youtube", "rate_per_1k": 100, "views": 50_000,
                                   "campaign_fee_pct": 20, "submission_status": "approved"}).json()
    assert p["earnings"] == 4000.0
    assert c.patch("/api/channels/UCapi", json={"paused": True}).json()["paused"] is True
    assert c.patch("/api/channels/UCapi", json={"settings": {"bogus": 1}}).status_code == 400
    assert c.get("/api/queue.csv").status_code == 200
