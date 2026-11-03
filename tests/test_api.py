"""API smoke tests with an isolated workspace + SQLite DB."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import clipforge.config as config_mod
from clipforge.models import CandidateFile, ClipCandidate, Segment, Transcript, VideoInfo, Word
from clipforge.workspace import Workspace


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"output: {{dir: '{(tmp_path / 'ws').as_posix()}'}}\nllm: {{provider: mock}}\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config_mod, "ENV_PATH", tmp_path / ".env")
    from clipforge.api import db

    monkeypatch.setattr(db, "_engine", None)
    from clipforge.api.server import create_app

    with TestClient(create_app()) as c:
        yield c, tmp_path / "ws"
    db._engine = None


def _seed(ws_root, video_id="vid1"):
    ws = Workspace(ws_root, video_id)
    ws.write_model(ws.info, VideoInfo(id=video_id, title="Test", duration=120, width=1920, height=1080))
    words = [Word(start=i * 0.5, end=i * 0.5 + 0.4, text=f"w{i}" + ("." if i % 10 == 9 else "")) for i in range(200)]
    ws.write_model(ws.transcript, Transcript(language="hi", model="t", duration=120,
                                             segments=[Segment(id=0, start=0, end=100, text="", words=words)]))
    ws.write_model(ws.candidates, CandidateFile(video_id=video_id, candidates=[
        ClipCandidate(rank=1, start=5, end=35, title="First", score=8.2),
        ClipCandidate(rank=2, start=50, end=80, title="Second", score=6.1),
    ]))
    return ws


def test_settings_masks_keys(client):
    c, _ = client
    r = c.put("/api/settings", json={"secrets": {"GEMINI_API_KEY": "AIzaSyTESTKEY123456"}})
    assert r.status_code == 200
    masked = r.json()["secrets"]["GEMINI_API_KEY"]
    assert masked and "TESTKEY" not in masked
    r = c.put("/api/settings", json={"config": {"clips": {"count": 17}}})
    assert r.json()["config"]["clips"]["count"] == 17
    assert c.put("/api/settings", json={"config": {"clips": {"min_sec": 90}}}).status_code == 400


def test_job_clips_patch_and_export(client):
    c, root = client
    _seed(root)
    from clipforge.api import db

    with db.session() as s:
        job = db.Job(source="x.mp4", video_id="vid1", title="Test", status="done")
        s.add(job)
        s.commit()
        s.refresh(job)
    clips = c.get(f"/api/jobs/{job.id}/clips").json()
    assert [x["rank"] for x in clips] == [1, 2] and clips[0]["id"] == f"{job.id}-1"

    # trim: snaps to word boundaries, never mid-word
    r = c.patch(f"/api/clips/{job.id}-1", json={"start": 6.2, "end": 30.1, "title": "New title"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["title"] == "New title"
    assert d["start"] <= 6.2 and d["end"] >= 29.9

    r = c.patch(f"/api/clips/{job.id}-1", json={"word_edits": {"0": "Hello"}, "approved": True})
    assert r.json()["approved"] is True
    detail = c.get(f"/api/clips/{job.id}-1").json()
    assert detail["words"][0]["edited"] == "Hello"

    # trimming the start keeps the edit on the same word (indices shift)
    first = detail["words"][0]
    r = c.patch(f"/api/clips/{job.id}-1", json={"start": detail["start"] - 1.05, "snap": False})
    moved = c.get(f"/api/clips/{job.id}-1").json()
    edited = [w for w in moved["words"] if w["edited"] == "Hello"]
    assert len(edited) == 1 and edited[0]["start"] == first["start"] and edited[0]["index"] > 0

    assert c.post(f"/api/jobs/{job.id}/approve", json={"min_score": 6}).json()["changed"] == 2
    z = c.get(f"/api/jobs/{job.id}/export.zip")
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"


def test_tracker_earnings(client):
    c, _ = client
    p = c.post("/api/posts", json={"clip_title": "c", "platform": "youtube", "rate_per_1k": 50, "views": 12000}).json()
    assert p["earnings"] == 600
    c.patch(f"/api/posts/{p['id']}", json={"views": 20000})
    data = c.get("/api/posts").json()
    assert data["totals"]["earnings"] == 1000 and data["totals"]["views"] == 20000
    assert "clipforge_tracker.csv" in c.get("/api/posts.csv").headers["content-disposition"]


def test_bad_inputs(client):
    c, _ = client
    assert c.post("/api/jobs", json={"url": "not a url"}).status_code == 400
    assert c.get("/api/jobs/999").status_code == 404
    assert c.get("/api/clips/abc").status_code == 400
    assert c.get("/api/presets").status_code == 200
    assert json.loads(c.get("/api/estimate?duration=3600").text)["total_sec"] > 0
