"""YouTube upload guard rails (no network: the Google client is faked)."""

from __future__ import annotations

import json
import sys
import types

import pytest

from clipforge.config import Config
from clipforge.models import ClipMetadata
from clipforge.stages import upload
from clipforge.workspace import Workspace


def _ws(tmp_path) -> tuple[Config, Workspace]:
    cfg = Config().with_overrides({"output.dir": str(tmp_path), "upload.enabled": True})
    ws = Workspace(cfg.workspace, "vid")
    (ws.clips_dir / "01_a.mp4").write_bytes(b"x")
    meta = ClipMetadata(rank=1, file="01_a.mp4", start=0, end=30, duration=30, score=8, title="A", hook="h",
                        yt_title="A #shorts", yt_description="d", ig_caption="c", hashtags=["#a", "#b"], credit="cr")
    ws.metadata_json.write_text(json.dumps([meta.model_dump()]), encoding="utf-8")
    return cfg, ws


def test_disabled_by_default(tmp_path):
    cfg, ws = _ws(tmp_path)
    with pytest.raises(upload.UploadError, match="disabled"):
        upload.upload_clips(ws, cfg.with_overrides({"upload.enabled": False}))


def test_uploads_private_scheduled_and_counts_quota(tmp_path, monkeypatch):
    cfg, ws = _ws(tmp_path)
    sent = []

    class Req:
        def __init__(self, body):
            self.body = body

        def next_chunk(self):
            return None, {"id": "yt123"}

    class Videos:
        def insert(self, part, body, media_body):
            sent.append(body)
            return Req(body)

    class Client:
        def videos(self):
            return Videos()

    disc = types.ModuleType("googleapiclient.discovery")
    disc.build = lambda *a, **k: Client()
    http = types.ModuleType("googleapiclient.http")
    http.MediaFileUpload = lambda *a, **k: object()
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", disc)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", http)
    monkeypatch.setattr(upload, "_credentials", lambda: None)

    ids = upload.upload_clips(ws, cfg)
    assert ids == ["yt123"]
    status = sent[0]["status"]
    assert status["privacyStatus"] == "private" and "publishAt" in status
    assert sent[0]["snippet"]["tags"] == ["a", "b"]
    assert upload.quota_used_today(cfg) == upload.INSERT_COST
