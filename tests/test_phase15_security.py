"""Regression tests for the Phase 15 security review (one test per finding)."""

from __future__ import annotations

import datetime as dt
import hashlib

import pytest
from fastapi.testclient import TestClient

from tests.fake_telegram import FakeTelegram
from tests.test_server import _job, _seed_downloaded, srv  # noqa: F401  (fixture)
from tests.test_telegram import OWNER, send, tg  # noqa: F401  (fixture)

REMOTE = {"X-Forwarded-For": "100.101.102.103", "Host": "laptop.tail1234.ts.net"}


@pytest.mark.parametrize("secret_text, must_vanish", [
    ("https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/getMe", "AAHdqTcv"),
    ("access_token=ya29.a0AfH6SMabcdef123456 rest", "a0AfH6"),
    ("refresh_token=1//0gabcdefghijklmn", "0gabcdef"),
    ("client_secret=GOCSPX-abcdefghij", "GOCSPX"),
    ("ig token IGQVJWabcdefghijklmnop failed", "IGQVJW"),
    ("groq gsk_abcdefghijklmnopqrstu", "gsk_abc"),
    ("worker key cfw_abcdefghijklmnopqrstuvwx", "cfw_abc"),
    ("Authorization: Bearer abcdefghijkl12345", "abcdefghijkl12345"),
    ("E:/youtube/clipforge/workspace/x/token.json", "youtube/clipforge"),
    (r"C:\Program Files\My App\secret.txt", "Program Files"),
    ("/Users/me/private/notes.txt and /data/a/b.mp4", "/Users/me"),
    ("from fe80::1:2:3:4 and 100.101.1.2", "100.101"),
    ("at laptop.tail1234.ts.net", "tail1234"),
])
def test_scrub_removes_secrets_paths_and_addresses(secret_text, must_vanish):
    from clipforge.notify.tg_core import scrub

    assert must_vanish not in scrub(secret_text)


def test_scrub_replaces_loaded_secret_values(tg):
    from clipforge.config import save_secret
    from clipforge.notify.tg_core import scrub

    save_secret("GEMINI_API_KEY", "my-very-own-gemini-key-123")
    assert "my-very-own" not in scrub("Gemini said: invalid key my-very-own-gemini-key-123")


def test_phone_cannot_change_security_settings(srv):  # noqa: F811
    app, tmp, _ = srv
    with TestClient(app) as local:
        local.post("/api/auth/setup", json={"password": "correct horse battery"})
        r = TestClient(app, headers=REMOTE)
        r.post("/api/auth/login", json={"password": "correct horse battery"})
        h = {"X-CSRF-Token": r.cookies["cf_csrf"]}
        for cfg in ({"telegram": {"api_base_url": "http://127.0.0.1:9999"}}, {"telegram": {"pin_required": True}},
                    {"deployment": {"mode": "standalone"}}, {"server": {"backup_dir": "/tmp/x"}}):
            assert r.put("/api/settings", json={"config": cfg}, headers=h).status_code == 403, cfg
        assert r.put("/api/settings", json={"config": {"telegram": {"notify_published": False}}}, headers=h).status_code == 200
    from clipforge.config import TelegramConfig

    with pytest.raises(ValueError):
        TelegramConfig(api_base_url="https://evil.example.com")  # the bot token is in every URL
    assert TelegramConfig(api_base_url="http://127.0.0.1:8081").api_base_url


def test_import_refuses_dot_ids_and_existing_videos(srv):  # noqa: F811
    from clipforge.server import leases

    app, tmp, _ = srv
    _job("existing")
    w, key = leases.add_worker("x")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    for vid in ("..", ".", "../x", ".hidden"):
        assert c.post("/api/worker/import", json={"video_id": vid, "source": "s"}).status_code == 400, vid
    assert c.post("/api/worker/import", json={"video_id": "existing", "source": "s"}).status_code == 409


def test_commit_is_atomic_with_the_fence(srv, monkeypatch):  # noqa: F811
    """The reaper can't re-queue (and another worker can't claim) a task while its results are being committed."""
    from clipforge.server import artifacts, leases

    app, tmp, _ = srv
    job = _job()
    ws = _seed_downloaded(tmp / "ws")
    w, key = leases.add_worker("a")
    leases.create(job.id, "transcribe")
    t = leases.claim(w, ["transcribe"])
    data = b'{"x": 1}'
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    c.put(f"/api/worker/artifacts/{t.id}/transcript.json", params={"fence": t.fence, "offset": 0}, content=data)
    real = artifacts.sha256
    raced = []

    def slow_sha(p):  # hashing takes longer than the lease (but less than a stuck commit): the reaper runs, another worker asks
        leases.reap(at=leases.now() + dt.timedelta(minutes=5))
        other, _ = leases.add_worker("b")
        raced.append(leases.claim(other, ["transcribe"]))
        return real(p)

    monkeypatch.setattr(artifacts, "sha256", slow_sha)
    from clipforge import pipeline
    from clipforge.api.jobs import job_config

    fp = pipeline.fp_transcribe(ws, job_config({}))
    r = c.post("/api/worker/complete", json={"task_id": t.id, "fence": t.fence, "files": [
        {"path": "transcript.json", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}],
        "state": {"transcribe": {"fingerprint": fp, "done_at": 1.0}}})
    assert r.status_code == 200 and raced == [None] and ws.transcript.read_bytes() == data
    assert leases.get(t.id).status == "done"


def test_pin_never_asked_in_groups_and_anonymous_admins_ignored(tg):  # noqa: F811
    from clipforge.api import db
    from clipforge.config import load_config, save_config
    from clipforge.notify import tg_core
    from clipforge.notify import tg_store as st

    b, fake, tmp = tg
    tg_core.set_pin("482913")
    save_config(load_config().with_overrides({"telegram.pin_required": True}))
    with db.session() as s:
        s.add(db.Channel(id="UCg", title="Grp", uploads_playlist="UUg", confirmed=True, paused=True))
        s.commit()
    st.add_chat(-900, OWNER, "owner", "group", "team")
    send(b, fake, "/resume grp", chat=-900, user=OWNER, kind="group")
    b.handle(fake.press(fake.button("✅ Yes"), chat=-900, user=OWNER, kind="group"))
    assert "private chat" in fake.last_text() and "Send your PIN" not in fake.last_text()
    fake.clear()
    send(b, fake, "/jobs", chat=-900, user=1087968824, kind="group")  # GroupAnonymousBot
    assert fake.calls == []
    with pytest.raises(ValueError):
        tg_core.set_pin("1234")  # 6–8 digits now


def test_pin_lockouts_escalate(tg):  # noqa: F811
    from clipforge.notify import tg_core
    from clipforge.notify import tg_store as st

    tg_core.set_pin("482913")
    for _ in range(5):
        tg_core.check_pin("000000")
    first = st.kv_get("pin_fail")
    st.kv_set("pin_fail", {**first, "locked_until": 0})
    for _ in range(5):
        tg_core.check_pin("000000")
    second = st.kv_get("pin_fail")
    assert second["strikes"] == 2 and second["locked_until"] - first["locked_until"] > 10 * 60


def test_artifact_paths_reject_trailing_newline():
    from clipforge.server import artifacts

    with pytest.raises(artifacts.ArtifactError):
        artifacts.check_path("candidates.json\n")


def test_handover_rejects_hostile_hosts_and_quotes_names():
    from clipforge.server import handover as ho

    for bad in ("-oProxyCommand=calc.exe", "host;rm -rf /", "a b", "h\nx", "$(id)"):
        with pytest.raises(ho.HandoverError):
            ho.SshTransport(bad)
    assert ho.remote_quote("lap$(id)`x`") == "'lap$(id)`x`'"


def _unused() -> FakeTelegram:
    return FakeTelegram()
