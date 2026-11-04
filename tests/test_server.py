"""Phase 15b: server + laptop worker — leases, fencing, worker protocol, artifacts, publish guard, blocked downloads,
housekeeping, handover, backups. Two "machines" in one process: the server's workspace and the worker's own."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import clipforge.config as config_mod
from clipforge.models import CandidateFile, ClipCandidate, Segment, Transcript, VideoInfo, Word
from clipforge.workspace import Workspace

SECRET_VARS = ("CLIPFORGE_PASSWORD_HASH", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "CLIPFORGE_WORKER_KEY")
UTC = dt.timezone.utc


@pytest.fixture
def srv(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"output: {{dir: '{(tmp_path / 'ws').as_posix()}'}}\nllm: {{provider: mock}}\n"
                        "deployment: {mode: server, worker_grace_minutes: 10, lease_seconds: 120}\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", cfg_path)
    env_file = tmp_path / ".env"
    monkeypatch.setattr(config_mod, "ENV_PATH", env_file)
    monkeypatch.setitem(config_mod.Secrets.model_config, "env_file", str(env_file))
    for v in SECRET_VARS:
        monkeypatch.delenv(v, raising=False)
    from clipforge.api import auth, db

    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(auth, "limiter", auth._Limiter())
    from clipforge.api.jobs import manager

    submitted: list = []
    monkeypatch.setattr(manager, "submit", lambda kind, job_id, payload=None: submitted.append((kind, job_id, payload or {})) or 1)
    from clipforge.api.server import create_app

    app = create_app()
    yield app, tmp_path, submitted
    db._engine = None
    import os

    for v in SECRET_VARS:
        os.environ.pop(v, None)


def _job(video_id="vidS", source="https://youtu.be/vidS", status="running"):
    from clipforge.api import db

    with db.session() as s:
        j = db.Job(source=source, video_id=video_id, title="Ep", status=status)
        s.add(j)
        s.commit()
        s.refresh(j)
        return j


def _seed_downloaded(root: Path, vid="vidS") -> Workspace:
    from clipforge import pipeline

    ws = Workspace(root, vid)
    ws.write_model(ws.info, VideoInfo(id=vid, title="Ep", duration=120, width=1920, height=1080))
    ws.source.write_bytes(b"\x01" * 50_000)
    ws.audio.write_bytes(b"\x02" * 30_000)
    ws.mark_done("download", pipeline.fp_download(ws, None))
    return ws


def _worker_client(app, tmp: Path, name="laptop"):
    from clipforge.config import Config, load_config
    from clipforge.server import leases, worker as wk

    w, key = leases.add_worker(name)
    wcfg = Config.model_validate({**load_config().model_dump(mode="json"), "output": {"dir": (tmp / f"{name}_ws").as_posix()}})
    http = TestClient(app, base_url="http://server", headers={"Authorization": f"Bearer {key}"})
    return wk.WorkerClient("http://server", key, wcfg, http=http), w, key


# ============================================================================ leases + fencing
def test_lease_heartbeat_requeue_fencing_idempotent(srv):
    from clipforge.server import leases

    app, tmp, _ = srv
    job = _job()
    a, _ = leases.add_worker("a")
    b, _ = leases.add_worker("b")
    t = leases.create(job.id, "transcribe")
    assert leases.create(job.id, "transcribe").id == t.id  # never two live tasks for one job + stage
    t1 = leases.claim(a, ["transcribe"])
    assert t1.fence == 1 and t1.worker_id == a.id
    assert leases.claim(b, ["transcribe"]) is None
    assert leases.heartbeat(a.id, t1.id, 1, 0.4) == "ok"
    # worker a goes to sleep: its lease expires, the task is re-queued and b gets it with a new fence
    out = leases.reap(at=leases.now() + dt.timedelta(seconds=500))
    assert t1.id in out["requeued"]
    t2 = leases.claim(b, ["transcribe"])
    assert t2.id == t1.id and t2.fence == 2
    with pytest.raises(leases.StaleLease):
        leases.complete(a.id, t1.id, 1)  # the stale worker wakes up: rejected
    with pytest.raises(leases.StaleLease):
        leases.heartbeat(a.id, t1.id, 1)
    assert leases.complete(b.id, t2.id, 2) is True
    assert leases.complete(b.id, t2.id, 2) is False  # retried request: no double work
    assert leases.get(t1.id).status == "done"


def test_unclaimed_tasks_fall_back_but_hold_waits(srv):
    from clipforge.server import dispatch, leases

    app, tmp, submitted = srv
    j1, j2 = _job("v1"), _job("v2")
    leases.create(j1.id, "render")
    leases.create(j2.id, "download", hold=True)
    out = leases.reap(at=leases.now() + dt.timedelta(minutes=11))
    assert out["fallback"] == [j1.id]
    assert leases.active(j2.id).status == "queued"  # waits for the laptop however long it takes
    leases.create(j1.id, "select")
    import clipforge.server.leases as L

    real = L.now
    L.now = lambda: real() + dt.timedelta(minutes=30)
    try:
        dispatch.tick()
    finally:
        L.now = real
    assert any(k == "pipeline" and jid == j1.id and p.get("local_all") for k, jid, p in submitted)


def test_cancel_reaches_the_worker_and_restart_keeps_delegated_jobs(srv):
    from clipforge.api import db
    from clipforge.api.jobs import JobManager
    from clipforge.server import leases

    app, tmp, _ = srv
    job = _job()
    w, _ = leases.add_worker("a")
    leases.create(job.id, "render")
    t = leases.claim(w, ["render"])
    JobManager.start.__wrapped__ if hasattr(JobManager.start, "__wrapped__") else None
    m = JobManager()
    m.thread.start = lambda: None  # don't start a real worker thread
    m.start()  # server restart: the job is still with the worker — not marked failed
    assert db.get_job(job.id).status == "running"
    m.cancel(job.id)
    assert leases.heartbeat(w.id, t.id, t.fence) == "cancel"
    assert db.get_job(job.id).status == "cancelled"


# ============================================================================ protocol end to end (in process)
def test_worker_protocol_transcribe_roundtrip(srv, monkeypatch):
    from clipforge import pipeline
    from clipforge.api.jobs import job_config
    from clipforge.server import dispatch, leases

    app, tmp, submitted = srv
    job = _job()
    ws = _seed_downloaded(tmp / "ws")
    client, w, _ = _worker_client(app, tmp)
    client.hello()
    assert leases.online_for("transcribe")
    # the server's pipeline asks: transcription goes to the online laptop, the job thread is released at once
    assert dispatch.delegate(job, job_config({}), _NullRep(), {}) is True
    task = leases.active(job.id)
    assert task.stage == "transcribe" and task.status == "queued"

    def fake_transcribe(wws, cfg, rep):
        assert wws.audio.read_bytes() == b"\x02" * 30_000  # pulled from the server, checksum-verified
        words = [Word(start=i * 0.5, end=i * 0.5 + 0.4, text=f"w{i}") for i in range(100)]
        wws.write_model(wws.transcript, Transcript(language="hi", model="t", duration=120,
                                                   segments=[Segment(id=0, start=0, end=50, text="", words=words)]))
        wws.mark_done("transcribe", pipeline.fp_transcribe(wws, cfg))
        rep.progress(1.0)

    monkeypatch.setattr(pipeline, "run_transcribe", fake_transcribe)
    assert client.run_once() is True
    assert leases.get(task.id).status == "done"
    assert ws.transcript.exists() and pipeline.transcribe_done(ws, job_config({}))  # cache works unchanged on the server
    assert ("pipeline", job.id, {}) in submitted  # …and the job continues
    assert not (tmp / "ws" / "_incoming").exists() or not any((tmp / "ws" / "_incoming").iterdir())


def test_worker_render_results_are_adopted(srv, monkeypatch):
    from clipforge import pipeline
    from clipforge.api.jobs import job_config
    from clipforge.server import leases

    app, tmp, submitted = srv
    job = _job()
    ws = _seed_downloaded(tmp / "ws")
    words = [Word(start=i * 0.5, end=i * 0.5 + 0.4, text=f"w{i}") for i in range(200)]
    ws.write_model(ws.transcript, Transcript(language="hi", model="t", duration=120,
                                             segments=[Segment(id=0, start=0, end=100, text="", words=words)]))
    ws.write_model(ws.candidates, CandidateFile(video_id="vidS", candidates=[ClipCandidate(rank=1, start=5, end=35, title="One", score=8)]))
    client, w, _ = _worker_client(app, tmp)
    client.hello()
    leases.create(job.id, "render", {"ranks": [1]})

    def fake_render(wws, cfg, rep, only=None, force=False):
        c = pipeline.load_candidates(wws)[0]
        out = wws.clip_path(1, c.title)
        out.write_bytes(b"\x03" * 7000)
        wws.write_json(wws.work_dir(1) / "render.json", {"fingerprint": "laptop-fp", "file": out.name, "candidate": c.model_dump()})
        return [out]

    monkeypatch.setattr(pipeline, "render", fake_render)
    monkeypatch.setattr(pipeline, "metadata", lambda wws, cfg, rep=None: [])
    assert client.run_once()
    assert ws.find_clip(1) and ws.find_clip(1).read_bytes() == b"\x03" * 7000
    stamped = json.loads((ws.work_dir(1) / "render.json").read_text())["fingerprint"]
    assert stamped != "laptop-fp" and pipeline.render_todo(ws) == []  # re-stamped: the server won't render it again


def test_stale_worker_results_are_dropped(srv, monkeypatch):
    from clipforge import pipeline
    from clipforge.server import leases

    app, tmp, _ = srv
    job = _job()
    ws = _seed_downloaded(tmp / "ws")
    client, w, _ = _worker_client(app, tmp)
    client.hello()
    leases.create(job.id, "transcribe")

    def slow(wws, cfg, rep):  # while "transcribing", the lease expires and another worker takes it
        wws.transcript.write_text("{}")
        leases.reap(at=leases.now() + dt.timedelta(seconds=999))
        other, _ = leases.add_worker("other")
        leases.claim(other, ["transcribe"])

    monkeypatch.setattr(pipeline, "run_transcribe", slow)
    assert client.run_once() is True
    assert not ws.transcript.exists()  # nothing from the stale worker landed
    assert leases.active(job.id).fence == 2


def test_artifact_checksum_resume_and_path_traversal(srv):
    from clipforge.server import artifacts, leases

    app, tmp, _ = srv
    root = tmp / "ws"
    ws = Workspace(root, "vidS")
    data = b"hello world " * 1000
    artifacts.write_chunk(root, 7, 1, "transcript.json", 0, [data[:5000]])
    assert artifacts.staged_size(root, 7, 1, "transcript.json") == 5000  # resume from here
    artifacts.write_chunk(root, 7, 1, "transcript.json", 5000, [data[5000:]])
    with pytest.raises(artifacts.ArtifactError):
        artifacts.write_chunk(root, 7, 1, "transcript.json", 99999, [b"x"])  # gap: refused
    import hashlib

    good = {"path": "transcript.json", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    with pytest.raises(artifacts.ArtifactError):
        artifacts.commit(ws, root, 7, 1, [{**good, "sha256": "0" * 64}])
    assert not ws.transcript.exists()  # all-or-nothing
    artifacts.write_chunk(root, 7, 1, "transcript.json", 0, [data])
    assert artifacts.commit(ws, root, 7, 1, [good]) == 1 and ws.transcript.read_bytes() == data
    for bad in ("../x.json", "/etc/passwd", "clips/../../.env", "C:/Windows/x", ".env", "clipforge.db",
                "workspace/clipforge.db", "clips/_work/01/../../../../.env", "select_cache/../../x.json"):
        with pytest.raises(artifacts.ArtifactError):
            artifacts.check_path(bad)
    # over HTTP too, with a valid lease
    job = _job()
    w, key = leases.add_worker("x")
    leases.create(job.id, "render")
    t = leases.claim(w, ["render"])
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    r = c.put(f"/api/worker/artifacts/{t.id}/..%2F..%2F.env", params={"fence": t.fence}, content=b"PWNED=1")
    assert r.status_code in (400, 404)
    assert not (tmp / ".env").exists() or b"PWNED" not in (tmp / ".env").read_bytes()
    r = c.get(f"/api/worker/artifacts/{t.id}/clips/..%2F..%2Fclipforge.db", params={"fence": t.fence})
    assert r.status_code in (400, 404)


def test_worker_auth_and_mode(srv):
    from clipforge.config import load_config, save_config
    from clipforge.server import leases

    app, tmp, _ = srv
    c = TestClient(app)
    assert c.post("/api/worker/claim", json={}).status_code == 401
    w, key = leases.add_worker("x")
    ok = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    assert ok.post("/api/worker/claim", json={"capabilities": ["render"]}).json() == {"task": None}
    leases.remove_worker(w.id)
    assert ok.post("/api/worker/claim", json={}).status_code == 401  # revoked
    w2, key2 = leases.add_worker("y")
    save_config(load_config().with_overrides({"deployment.mode": "standalone"}))
    assert TestClient(app, headers={"Authorization": f"Bearer {key2}"}).post("/api/worker/claim", json={}).status_code == 409
    # adding a worker (creates a secret) is refused remotely
    r = TestClient(app, headers={"X-Forwarded-For": "100.1.2.3", "Host": "srv.tail.ts.net"}).post("/api/server/workers", json={"name": "z"})
    assert r.status_code in (401, 403)


def test_import_of_a_job_made_offline(srv):
    from clipforge.api import db

    app, tmp, _ = srv
    client, w, _ = _worker_client(app, tmp)
    client.hello()
    lws = Workspace(client.cfg.workspace, "offline1")
    lws.write_model(lws.info, VideoInfo(id="offline1", title="Offline", duration=60, width=1920, height=1080))
    lws.write_model(lws.candidates, CandidateFile(video_id="offline1", candidates=[ClipCandidate(rank=1, start=1, end=30, title="T", score=7)]))
    (lws.clips_dir / "01_t.mp4").write_bytes(b"\x05" * 3000)

    class J:
        id, video_id, source, title, settings = 1, "offline1", "https://youtu.be/offline1", "Offline", {}

    jid = client.push_local_job(J())
    job = db.get_job(jid)
    assert job.status == "done" and job.n_clips == 1
    assert (tmp / "ws" / "offline1" / "clips" / "01_t.mp4").read_bytes() == b"\x05" * 3000


# ============================================================================ exactly one publisher
def test_publishing_guard_worker_and_fenced(srv, monkeypatch):
    from clipforge.config import load_config, save_config
    from clipforge.publish import runner
    from clipforge.server import dispatch, leases
    from clipforge.services import publishing as pub
    from clipforge.services.errors import Conflict

    app, tmp, _ = srv
    called = []
    monkeypatch.setattr(runner, "_run_locked", lambda *a, **k: called.append(1) or runner.RunReport())
    save_config(load_config().with_overrides({"deployment.mode": "worker"}))
    rep = runner.run_once()
    assert not called and "worker mode" in rep.messages[0]
    with pytest.raises(Conflict):
        pub.require_publisher()
    save_config(load_config().with_overrides({"deployment.mode": "server"}))
    w, key = leases.add_worker("laptop")
    TestClient(app, headers={"Authorization": f"Bearer {key}"}).post("/api/server/fence", json={"reason": "emergency"})
    rep = runner.run_once()
    assert not called and "fenced" in rep.messages[0]
    dispatch.unfence()
    runner.run_once()
    assert called == [1]


def test_unique_queue_claim_prevents_double_upload(srv):
    """Two schedulers racing for the same clip: only one claim wins (publish/queue.claim_for_upload)."""
    from clipforge.api import db
    from clipforge.publish import queue as q

    app, tmp, _ = srv
    it = q.save(db.QueueItem(channel_id="UC1", video_id="v", rank=1, clip_key="k", title="t", status="queued",
                             slot_utc=dt.datetime(2030, 1, 1, tzinfo=UTC)))
    assert q.claim_for_upload(it.id, it.slot_utc) is True
    assert q.claim_for_upload(it.id, it.slot_utc) is False


# ============================================================================ blocked downloads
def test_download_blocked_detection_backoff_and_routing(srv, monkeypatch):
    from clipforge import pipeline
    from clipforge.api.jobs import job_config
    from clipforge.server import dispatch, leases
    from clipforge.stages import download

    app, tmp, _ = srv
    assert download.is_blocked("ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies…")
    assert download.is_blocked("HTTP Error 429: Too Many Requests")
    assert not download.is_blocked("Video unavailable. This video is private")

    class DLErr(Exception):
        pass

    class FakeYDL:
        utils = type("u", (), {"DownloadError": DLErr})
        calls = 0

        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            FakeYDL.calls += 1
            raise DLErr("Sign in to confirm you're not a bot")

    FakeYDL.YoutubeDL = FakeYDL
    slept: list = []
    with pytest.raises(download.DownloadBlocked):
        download._extract_with_backoff(FakeYDL, {}, "https://youtu.be/x", 3, _NullRep(), sleep=slept.append)
    assert FakeYDL.calls == 3 and len(slept) == 60 + 120  # 60 s then 120 s — capped, never hammering
    # on the server with no worker online: the job parks on a `hold` download task for the laptop
    job = _job()

    def blocked(*a, **k):
        raise download.DownloadBlocked("bot check")

    monkeypatch.setattr(pipeline, "run_download", blocked)
    assert dispatch.delegate(job, job_config({}), _NullRep(), {}) is True
    t = leases.active(job.id)
    assert t.stage == "download" and t.hold
    client, w, _ = _worker_client(app, tmp)
    client.hello()
    assert leases.claim(w, ["download"]).id == t.id  # the laptop picks it up when it comes online


def test_downloads_go_to_the_laptop_when_it_is_online(srv):
    from clipforge.api.jobs import job_config
    from clipforge.server import dispatch, leases

    app, tmp, _ = srv
    client, w, _ = _worker_client(app, tmp)
    client.hello()
    job = _job(video_id=None)
    assert dispatch.delegate(job, job_config({}), _NullRep(), {}) is True
    assert leases.active(job.id).stage == "download"  # home internet first (fewer bot blocks)


# ============================================================================ housekeeping
def test_cleanup_never_deletes_what_is_still_needed(srv):
    from clipforge.api import db
    from clipforge.publish import queue as q
    from clipforge.server import housekeeping

    app, tmp, _ = srv
    old = dt.datetime.now(UTC) - dt.timedelta(days=30)
    for vid in ("a", "b", "c"):
        ws = _seed_downloaded(tmp / "ws", vid)
        (ws.clips_dir / "01_x.mp4").write_bytes(b"1")
        with db.session() as s:
            s.add(db.Job(source=vid, video_id=vid, status="done", finished_at=old))
            s.commit()
    q.save(db.QueueItem(channel_id="UC", video_id="a", rank=1, clip_key="ka", status="queued"))  # waiting → keep all
    q.save(db.QueueItem(channel_id="UC", video_id="b", rank=1, clip_key="kb", status="published", published_at=old))
    p = housekeeping.plan()
    names = {f.relative_to(tmp / "ws").as_posix() for f in p.delete}
    assert not any(n.startswith("a/") for n in names) and "not published" in p.kept["a"]
    assert {"b/source.mp4", "b/audio.wav", "b/clips/01_x.mp4"} <= names
    assert {"c/source.mp4", "c/audio.wav"} <= names and "c/clips/01_x.mp4" not in names  # never queued: clips kept
    housekeeping.run()
    assert (tmp / "ws" / "a" / "source.mp4").exists() and not (tmp / "ws" / "b" / "source.mp4").exists()


# ============================================================================ handover
def test_handover_dry_run_run_and_rollback(srv, monkeypatch):
    from clipforge.config import Secrets, load_config, save_config
    from clipforge.server import handover as ho

    app, tmp, _ = srv
    save_config(load_config().with_overrides({"deployment.mode": "standalone"}))
    monkeypatch.setattr(ho, "_set_scheduler_task", lambda enabled: None)
    from clipforge.api import db

    db.engine()
    ws_root = tmp / "ws"
    _seed_downloaded(ws_root, "epi")
    tok = ws_root / "channels" / "UCabc" / "token.json"
    tok.parent.mkdir(parents=True)
    tok.write_text('{"refresh_token": "r"}')
    (tmp / ".env").write_text("TELEGRAM_BOT_TOKEN=1:x\n")
    remote = ho.DirTransport(tmp / "server")
    lines = ho.dry_run("dir:x", transport=remote)
    assert any("Will copy" in ln for ln in lines) and load_config().deployment.mode == "standalone"  # dry run changes nothing
    res = ho.run("dir:x", transport=remote, worker_name="hamza-laptop", server_url="http://clipforge-server:8000")
    app_dir = tmp / "server" / "app"
    assert (app_dir / "workspace" / "clipforge.db").exists() and (app_dir / "workspace" / "epi" / "source.mp4").exists()
    assert (app_dir / "workspace" / "channels" / "UCabc" / "token.json").exists()
    assert "mode: server" in (app_dir / "config.yaml").read_text()
    cfg = load_config()
    assert cfg.deployment.mode == "worker" and cfg.deployment.server_url == "http://clipforge-server:8000"
    assert Secrets().clipforge_worker_key.startswith("cfw_") and not tok.exists() and res["tokens_moved"] == 1
    out = ho.rollback(transport=remote)
    assert remote.stopped and load_config().deployment.mode == "standalone" and tok.exists()
    assert any("standalone again" in ln for ln in out)


# ============================================================================ backups
def test_backup_restore_roundtrip_and_retention(srv):
    from clipforge.config import load_config, save_config
    from clipforge.server import backup

    app, tmp, _ = srv
    from clipforge.api import db

    db.engine()
    pub, sec = backup.keygen()
    with pytest.raises(backup.BackupError):
        backup.make()  # no key → refused (backups contain logins)
    save_config(load_config().with_overrides({"server.backup_recipient": pub}))
    ws = _seed_downloaded(tmp / "ws", "e1")
    ws.write_model(ws.candidates, CandidateFile(video_id="e1", candidates=[ClipCandidate(rank=1, start=1, end=20, title="Mine", score=9)]))
    (tmp / ".env").write_text("TELEGRAM_BOT_TOKEN=1:secret\n")
    f = backup.make()
    raw = f.read_bytes()
    assert b"secret" not in raw and b"Mine" not in raw  # encrypted
    names = backup.restore(f, sec, tmp / "r1")
    assert {"workspace/clipforge.db", ".env", "workspace/e1/candidates.json", "config.yaml"} <= set(names)
    assert (tmp / "r1" / "unpacked" / ".env").read_text() == "TELEGRAM_BOT_TOKEN=1:secret\n"
    _, other = backup.keygen()
    with pytest.raises(backup.BackupError):
        backup.restore(f, other, tmp / "r2")
    cut = tmp / "cut.cfbk"
    cut.write_bytes(raw[:-40])
    with pytest.raises(backup.BackupError):
        backup.restore(cut, sec, tmp / "r3")
    # --apply puts files back and keeps the current ones
    ws.candidates.write_text("broken")
    db.engine().dispose()  # like a stopped ClipForge
    backup.restore(f, sec, tmp / "r4", apply=True)
    assert "Mine" in ws.candidates.read_text() and (tmp / "r4" / "before-restore" / "workspace" / "e1" / "candidates.json").exists()
    # retention: 7 daily + 4 weekly
    d = backup.backup_dir()
    for p in d.iterdir():
        p.unlink()
    start = dt.datetime(2026, 11, 4, 3, 30, tzinfo=UTC)
    for i in range(60):
        t = start - dt.timedelta(days=i)
        (d / f"clipforge-{t:%Y%m%d-%H%M%S}.tar.gz.cfbk").write_bytes(b"x")
    backup.prune()
    left = backup.list_backups()
    assert len(left) == 7 + 4


class _NullRep:
    def stage(self, *a, **k):
        pass

    def progress(self, *a, **k):
        pass

    def log(self, *a, **k):
        pass

    def cancelled(self):
        return False
