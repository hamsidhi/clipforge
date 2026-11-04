"""Phase 15 reliability drills as regression tests (the reliability review ran them against real processes too).

Each test names the failure it guards against: lost hand-ins, endless loops, stuck jobs, poison tasks, interrupted
commits, a fence that fails open, and Telegram updates handled twice."""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from tests.fake_telegram import TOKEN, FakeTelegram
from tests.test_server import _job, _NullRep, _seed_downloaded, _worker_client, srv  # noqa: F401  (fixture)


def _fake_transcribe(pipeline):
    from clipforge.models import Segment, Transcript, Word

    def tr(wws, cfg, rep):
        if pipeline.transcribe_done(wws, cfg):
            return  # "transcribe: skipped" — already on this laptop
        words = [Word(start=i * 0.5, end=i * 0.5 + 0.4, text=f"w{i}") for i in range(50)]
        wws.write_model(wws.transcript, Transcript(language="en", model="t", duration=120,
                                                   segments=[Segment(id=0, start=0, end=30, text="", words=words)]))
        wws.mark_done("transcribe", pipeline.fp_transcribe(wws, cfg))
    return tr


def test_lost_hand_in_then_reclaim_still_delivers_results(srv, monkeypatch):  # noqa: F811
    """Drill 2: the server restarted while the laptop handed in; the task is re-claimed by a laptop that already has
    the transcript cached. It must still send it — before, it sent nothing and transcribe looped forever."""
    from clipforge import pipeline
    from clipforge.api.jobs import job_config
    from clipforge.server import dispatch, leases
    from clipforge.server import worker as wk

    app, tmp, _ = srv
    job = _job()
    ws = _seed_downloaded(tmp / "ws")
    client, w, _ = _worker_client(app, tmp)
    client.hello()
    monkeypatch.setattr(pipeline, "run_transcribe", _fake_transcribe(pipeline))
    monkeypatch.setattr(pipeline, "run_analyze", lambda ws, rep: None)
    leases.create(job.id, "transcribe")
    real = client.call
    monkeypatch.setattr(client, "call", lambda m, p, **kw: (_ for _ in ()).throw(wk.ServerError("server unreachable"))
                        if p == "/api/worker/complete" else real(m, p, **kw))
    monkeypatch.setattr(client, "sleep", lambda s: None)
    with pytest.raises(wk.ServerError):
        client.run_once()  # done on the laptop, hand-in lost (after retries)
    monkeypatch.setattr(client, "call", real)
    leases.reap(at=leases.now() + dt.timedelta(seconds=999))
    assert client.run_once() is True
    assert pipeline.transcribe_done(ws, job_config({}))  # the cached result arrived
    assert dispatch.delegate(job, job_config({}), _NullRep(), {}) is True
    assert leases.active(job.id).stage == "select"  # moved on — no endless transcribe loop


def test_empty_hand_in_is_rejected_not_marked_done(srv):  # noqa: F811
    from clipforge.server import leases

    app, tmp, _ = srv
    job = _job()
    _seed_downloaded(tmp / "ws")
    w, key = leases.add_worker("x")
    leases.create(job.id, "transcribe")
    t = leases.claim(w, ["transcribe"])
    from fastapi.testclient import TestClient

    r = TestClient(app, headers={"Authorization": f"Bearer {key}"}).post(
        "/api/worker/complete", json={"task_id": t.id, "fence": t.fence, "files": []})
    assert r.status_code == 422 and "missing" in r.json()["detail"]
    assert leases.get(t.id).status == "leased"  # the worker can still hand in properly


def test_worker_retries_a_lost_hand_in(srv, monkeypatch):  # noqa: F811
    from clipforge import pipeline
    from clipforge.server import leases
    from clipforge.server import worker as wk

    app, tmp, _ = srv
    job = _job()
    _seed_downloaded(tmp / "ws")
    client, w, _ = _worker_client(app, tmp)
    client.hello()
    leases.create(job.id, "transcribe")
    monkeypatch.setattr(pipeline, "run_transcribe", _fake_transcribe(pipeline))
    real = client.call
    n = {"c": 0}

    def flaky(method, path, **kw):
        if path == "/api/worker/complete" and n["c"] < 2:
            n["c"] += 1
            raise wk.ServerError("server unreachable: ReadTimeout")
        return real(method, path, **kw)

    monkeypatch.setattr(client, "call", flaky)
    monkeypatch.setattr(client, "sleep", lambda s: None)
    assert client.run_once() is True
    assert leases.tasks_for(job.id)[0].status == "done"


def test_no_job_is_left_running_with_nothing_working_on_it(srv, monkeypatch):  # noqa: F811
    from clipforge.api import db
    from clipforge.api.jobs import JobManager
    from clipforge.server import dispatch, leases

    app, tmp, submitted = srv
    job = _job()
    w, _ = leases.add_worker("a")
    leases.create(job.id, "transcribe")
    t = leases.claim(w, ["transcribe"])
    leases.complete(w.id, t.id, t.fence)  # the server died before continuing the job
    m = JobManager()
    m.thread.start = lambda: None
    m.submit = lambda kind, jid, payload=None: submitted.append((kind, jid, payload or {}))
    m.start()  # server mode: resumed, not "failed"
    assert db.get_job(job.id).status != "failed" and ("pipeline", job.id, {}) in submitted
    submitted.clear()
    db.update_job(job.id, updated_at=db.now() - dt.timedelta(minutes=10))
    with db.session() as s:  # updated_at is set by update_job — push it back
        j = s.get(db.Job, job.id)
        j.updated_at = db.now() - dt.timedelta(minutes=10)
        s.add(j)
        s.commit()
    assert dispatch.repair_stuck_jobs() == [job.id]  # the periodic repair finds it too


def test_poison_task_stops_after_three_vanished_workers(srv):  # noqa: F811
    from clipforge.server import leases

    app, tmp, _ = srv
    job, held = _job("p1"), _job("p2")
    w, _ = leases.add_worker("a")
    leases.create(job.id, "render")
    leases.create(held.id, "download", hold=True)
    for _ in range(3):
        for stage in ("render", "download"):
            assert leases.claim(w, [stage]) is not None
        out = leases.reap(at=leases.now() + dt.timedelta(seconds=999))
    assert leases.tasks_for(job.id)[0].status == "fallback" and job.id in out["fallback"]
    assert leases.tasks_for(held.id)[0].status == "failed" and out["failed"]


def test_interrupted_commit_is_requeued(srv):  # noqa: F811
    from clipforge.server import leases

    app, tmp, _ = srv
    job = _job()
    w, _ = leases.add_worker("a")
    leases.create(job.id, "transcribe")
    t = leases.claim(w, ["transcribe"])
    leases.begin_commit(w.id, t.id, t.fence)
    assert leases.reset_after_restart() == 1 and leases.get(t.id).status == "queued"
    t2 = leases.claim(w, ["transcribe"])
    leases.begin_commit(w.id, t2.id, t2.fence)
    leases.reap(at=leases.now() + dt.timedelta(seconds=leases.COMMIT_TIMEOUT + 5))
    assert leases.get(t2.id).status in ("queued", "fallback")  # out of 'committing' (here also past its grace time)
    job2 = _job("v2")
    leases.create(job2.id, "render")
    t3 = leases.claim(w, ["render"])
    leases.begin_commit(w.id, t3.id, t3.fence)
    assert leases.cancel_job(job2.id) == 1  # a committing task can be cancelled too


def test_fence_fails_closed(srv, monkeypatch):  # noqa: F811
    from clipforge.notify import tg_store as st
    from clipforge.server import dispatch

    dispatch.set_fenced("laptop is standalone")
    monkeypatch.setattr(st, "kv_get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("database is locked")))
    ok, why = dispatch.publishing_allowed()
    assert ok is False and "fence" in why


class OffsetFake(FakeTelegram):
    """getUpdates honours `offset` like the real API and can drop the network."""

    def __init__(self) -> None:
        super().__init__()
        self.pending: list[dict] = []
        self.drop_get = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            if self.drop_get:
                self.drop_get -= 1
                raise httpx.ConnectError("network down")
            p = json.loads(request.content or b"{}")
            off = int(p.get("offset") or 0)
            self.pending = [u for u in self.pending if u["update_id"] >= off]
            self.calls.append(("getUpdates", p))
            if self.fail.get("getUpdates"):
                status, body = self.fail["getUpdates"].pop(0)
                return httpx.Response(status, json=body)
            return httpx.Response(200, json={"ok": True, "result": list(self.pending)})
        return super().handler(request)


@pytest.fixture
def bots(srv, monkeypatch):  # noqa: F811
    from clipforge.config import save_secret
    from clipforge.notify import telegram
    from clipforge.notify import tg_store as st

    app, tmp, _ = srv
    p = tmp / "config.yaml"
    p.write_text(p.read_text(encoding="utf-8") + "telegram: {enabled: true}\n", encoding="utf-8")
    save_secret("TELEGRAM_BOT_TOKEN", TOKEN)
    st.add_chat(42, 42, "owner", "private", "me")
    fake = OffsetFake()
    yield fake, telegram, st
    telegram.set_bot(None)


def test_429s_and_network_drops_lose_and_repeat_nothing(bots):
    fake, telegram, st = bots
    b = telegram.Bot(transport=fake.transport(), sleep=lambda s: None)
    telegram.set_bot(b)
    handled: list[int] = []
    real = b.handle
    b.handle = lambda u: (handled.append(u["update_id"]), real(u))[-1]
    fake.pending += [fake.msg("/help") for _ in range(3)]
    fake.drop_get = 2
    for _ in range(2):
        with pytest.raises(telegram.TgError):
            b.poll_once(timeout=0)
    assert handled == []
    fake.fail["sendMessage"] = [(429, {"ok": False, "error_code": 429, "description": "Too Many Requests",
                                       "parameters": {"retry_after": 3}})] * 3
    assert b.poll_once(timeout=0) == 3 and handled == [1, 2, 3]
    assert b.poll_once(timeout=0) == 0 and handled == [1, 2, 3]


def test_two_pollers_never_handle_an_update_twice(bots, monkeypatch):
    """A slow batch outlives the lease, a second poller takes over: no update is handled by both."""
    fake, telegram, st = bots
    real_acq, real_rel = st.acquire_poller, st.release_poller
    who = {"cur": "A"}
    monkeypatch.setattr(st, "acquire_poller", lambda secs=120, owner=None: real_acq(secs, owner=who["cur"]))
    monkeypatch.setattr(st, "release_poller", lambda owner=None: real_rel(owner=who["cur"]))
    a = telegram.Bot(transport=fake.transport(), sleep=lambda s: None)
    b = telegram.Bot(transport=fake.transport(), sleep=lambda s: None)
    handled: list[tuple[str, int]] = []
    b.handle = lambda u: handled.append(("B", u["update_id"]))
    fake.pending += [fake.msg("/help") for _ in range(4)]

    def slow(u):
        handled.append(("A", u["update_id"]))
        if u["update_id"] == fake.pending[0]["update_id"]:  # A is slow on its first update: its lease runs out
            from sqlalchemy import text

            from clipforge.api import db

            with db.engine().begin() as conn:
                conn.execute(text("UPDATE tgkv SET value=:v WHERE key='poller'"),
                             {"v": json.dumps({"owner": "A", "until": "2000-01-01T00:00:00+00:00"})})
            who["cur"] = "B"
            b.poll_once(timeout=0)
            who["cur"] = "A"

    a.handle = slow
    a.poll_once(timeout=0)
    ids = [u for _, u in handled]
    assert sorted(ids) == sorted(set(ids)) and len(ids) == 4, handled
