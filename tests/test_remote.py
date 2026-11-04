"""Phase 14: auth + remote gating, Telegram bot (fake Bot API), watchdog, preview proxy, Tailscale parsing, watch folder."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import clipforge.config as config_mod

REMOTE = {"X-Forwarded-For": "100.101.102.103", "Host": "laptop.tail1234.ts.net"}
SECRET_VARS = ("CLIPFORGE_PASSWORD_HASH", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "META_APP_ID", "META_APP_SECRET")


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"output: {{dir: '{(tmp_path / 'ws').as_posix()}'}}\nllm: {{provider: mock}}\n"
                        "auth: {max_failures: 5, lockout_minutes: 15}\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", cfg_path)
    env_file = tmp_path / ".env"
    monkeypatch.setattr(config_mod, "ENV_PATH", env_file)
    monkeypatch.setitem(config_mod.Secrets.model_config, "env_file", str(env_file))
    for v in SECRET_VARS:
        monkeypatch.delenv(v, raising=False)
    from clipforge.api import auth, db

    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(auth, "limiter", auth._Limiter())
    from clipforge.api.server import create_app

    app = create_app()
    app.state.publish_ticker = False
    yield app, tmp_path
    for v in SECRET_VARS:
        import os

        os.environ.pop(v, None)


def _remote(app) -> TestClient:
    return TestClient(app, headers=REMOTE)


# ----------------------------------------------------------------------------- auth
def test_local_is_trusted_remote_needs_password_and_login(env):
    app, _ = env
    with TestClient(app) as local:
        assert local.get("/api/jobs").status_code == 200  # laptop: no login
        r = _remote(app)
        res = r.get("/api/jobs")
        assert res.status_code == 401 and res.json()["setup_required"]
        assert r.get("/api/health").json()["ok"] is True  # health never needs auth
        assert local.post("/api/auth/setup", json={"password": "short"}).status_code == 400
        assert local.post("/api/auth/setup", json={"password": "correct horse battery"}).status_code == 200
        assert r.get("/api/jobs").json()["login_required"]
        assert r.post("/api/auth/setup", json={"password": "attacker sets this"}).status_code == 403  # remote can't set it
        assert r.post("/api/auth/login", json={"password": "nope"}).status_code == 401
        ok = r.post("/api/auth/login", json={"password": "correct horse battery", "remember": True})
        assert ok.status_code == 200
        assert "cf_session" in r.cookies and "cf_csrf" in r.cookies
        sc = ok.headers.get("set-cookie", "")
        assert "httponly" in sc.lower() and "samesite=strict" in sc.lower()
        assert r.get("/api/jobs").status_code == 200
        st = r.get("/api/auth/status").json()
        assert st["authenticated"] and not st["local"]


def test_csrf_required_for_changes(env):
    app, _ = env
    with TestClient(app) as local:
        local.post("/api/auth/setup", json={"password": "correct horse battery"})
        r = _remote(app)
        r.post("/api/auth/login", json={"password": "correct horse battery"})
        body = {"platform": "youtube", "clip_title": "x"}
        assert r.post("/api/posts", json=body).status_code == 403
        assert r.post("/api/posts", json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
        assert r.post("/api/posts", json=body, headers={"X-CSRF-Token": r.cookies["cf_csrf"]}).status_code == 200


def test_lockout_after_5_wrong_passwords(env):
    app, _ = env
    with TestClient(app) as local:
        local.post("/api/auth/setup", json={"password": "correct horse battery"})
        r = _remote(app)
        codes = [r.post("/api/auth/login", json={"password": f"bad{i}"}).status_code for i in range(5)]
        assert codes[:4] == [401] * 4 and codes[4] == 429
        assert r.post("/api/auth/login", json={"password": "correct horse battery"}).status_code == 429  # even the right one
        kinds = [e["kind"] for e in local.get("/api/auth/audit").json()]
        assert "lockout" in kinds and "login_failed" in kinds


def test_session_expiry_revoke_and_password_change(env):
    from clipforge.api import db

    app, _ = env
    with TestClient(app) as local:
        local.post("/api/auth/setup", json={"password": "correct horse battery"})
        r = _remote(app)
        r.post("/api/auth/login", json={"password": "correct horse battery", "remember": False})
        assert r.get("/api/jobs").status_code == 200
        with db.session() as s:  # expire it
            row = s.exec(db.select(db.AuthSession)).first()
            row.expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
            s.add(row)
            s.commit()
        assert r.get("/api/jobs").status_code == 401
        r.post("/api/auth/login", json={"password": "correct horse battery"})
        sid = [s for s in local.get("/api/auth/sessions").json() if not s["current"]][0]["id"]
        assert local.delete(f"/api/auth/sessions/{sid}").status_code == 200
        assert r.get("/api/jobs").status_code == 401
        r.post("/api/auth/login", json={"password": "correct horse battery"})
        local.post("/api/auth/setup", json={"password": "a brand new password"})  # logs every device out
        assert r.get("/api/jobs").status_code == 401


def test_everything_behind_auth_when_remote(env):
    app, _ = env
    with TestClient(app) as local:
        local.post("/api/auth/setup", json={"password": "correct horse battery"})
        r = _remote(app)
        for path in ("/api/jobs/1/events", "/api/clips/1-1/video", "/api/jobs/1/export.zip", "/api/queue",
                     "/api/batches/files/batches/x/batch.html", "/api/settings", "/api/telegram", "/api/auth/audit"):
            assert r.get(path).status_code == 401, path
        assert r.get("/docs").status_code == 404 and r.get("/openapi.json").status_code == 404
        assert local.get("/openapi.json").status_code == 200
        h = r.get("/api/health").headers
        assert h["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["referrer-policy"] == "same-origin"


def test_trust_localhost_off_makes_the_laptop_log_in(env):
    app, tmp = env
    (tmp / "config.yaml").write_text((tmp / "config.yaml").read_text() + "", encoding="utf-8")
    with TestClient(app) as local:
        local.post("/api/auth/setup", json={"password": "correct horse battery"})
        txt = (tmp / "config.yaml").read_text().replace("auth: {", "auth: {trust_localhost: false, ")
        (tmp / "config.yaml").write_text(txt, encoding="utf-8")
        assert local.get("/api/jobs").status_code == 401


def test_password_hash_roundtrip():
    from clipforge.api.auth import hash_password, verify_password

    h = hash_password("correct horse battery", n=2 ** 12)
    assert h.startswith("scrypt$") and "correct" not in h
    assert verify_password("correct horse battery", h) and not verify_password("wrong", h)
    assert not verify_password("x", None) and not verify_password("x", "garbage")


def test_serve_refuses_lan_without_password(env, monkeypatch):
    from clipforge.api import server

    with pytest.raises(SystemExit) as e:
        server.serve(host="0.0.0.0", open_browser=False)
    assert "set-password" in str(e.value)


# ----------------------------------------------------------------------------- telegram (fake Bot API)
class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.updates: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if request.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(request.content or b"{}")
        else:
            body = {"multipart": True}
        self.sent.append((method, body))
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "clipforge_test_bot"}})
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": out})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    def texts(self) -> list[str]:
        return [b.get("text", "") for m, b in self.sent if m == "sendMessage"]


def _msg(uid: int, text: str, n: int = 1) -> dict:
    return {"update_id": n, "message": {"message_id": n, "from": {"id": uid}, "chat": {"id": uid, "type": "private"}, "text": text}}


@pytest.fixture
def tg(env, monkeypatch):
    from clipforge.notify import telegram

    fake = FakeTelegram()
    b = telegram.Bot(transport=httpx.MockTransport(fake.handler))
    telegram.set_bot(b)
    from clipforge.config import save_secret

    save_secret("TELEGRAM_BOT_TOKEN", "123:abc")
    app, tmp = env
    (tmp / "config.yaml").write_text((tmp / "config.yaml").read_text() + "telegram: {enabled: true}\n", encoding="utf-8")
    yield b, fake, app
    telegram.set_bot(None)


def test_pairing_only_my_user(tg):
    from clipforge.config import Secrets

    b, fake, app = tg
    b.handle(_msg(999, "/pair 000000"))  # nobody paired, wrong code
    assert not Secrets().telegram_chat_id
    code = b.new_pair_code()
    b.handle(_msg(42, f"/pair {code}"))
    assert Secrets().telegram_chat_id == "42"
    fake.sent.clear()
    b.handle(_msg(999, "/help"))  # a stranger: ignored
    assert fake.sent == []
    b.handle(_msg(42, "/help"))
    assert "/schedule" in fake.texts()[-1]


def test_commands_and_confirm_buttons(tg, monkeypatch):
    """Phase 14 commands still work on the Phase 15 bot: link → options wizard, /queue, /pause with a confirmation."""
    from clipforge.api import db
    from clipforge.config import save_secret

    b, fake, app = tg
    save_secret("TELEGRAM_CHAT_ID", "42")  # a Phase 14 pairing is migrated to an owner chat
    with TestClient(app):
        b.handle(_msg(42, "check this https://youtu.be/abc123"))
        assert any("New job" in t for t in fake.texts())
        b.handle(_msg(42, "/queue", 2))
        assert "Nothing scheduled" in fake.texts()[-1]
        with db.session() as s:
            s.add(db.Channel(id="UCx", title="Figuring Clips", uploads_playlist="UUx"))
            s.commit()
        b.handle(_msg(42, "/pause figuring", 3))
        last = [x for x in fake.sent if x[0] == "sendMessage"][-1][1]
        kb = last["reply_markup"]["inline_keyboard"][0]
        assert "Pause" in last["text"] and kb[0]["callback_data"].startswith("c:") and len(kb[0]["callback_data"]) <= 64
        from clipforge.publish import channels as chs

        assert not chs.get("UCx").paused  # not before the confirmation
        cq = {"id": "q1", "from": {"id": 42}, "data": kb[0]["callback_data"], "message": {"message_id": 5, "chat": {"id": 42, "type": "private"}}}
        b.handle({"update_id": 9, "callback_query": cq})
        assert chs.get("UCx").paused
        n = len(fake.sent)
        b.handle({"update_id": 10, "callback_query": {**cq, "id": "q2", "from": {"id": 7},
                                                       "message": {"message_id": 5, "chat": {"id": 7, "type": "private"}}}})
        assert len(fake.sent) == n  # a stranger's button press: no answer at all


def test_notifications_toggles_dedupe_and_daily_summary(tg):
    from clipforge.config import save_secret
    from clipforge.notify import telegram
    from clipforge.publish.runner import RunReport

    b, fake, app = tg
    save_secret("TELEGRAM_CHAT_ID", "42")
    with TestClient(app):
        rep = RunReport(skipped_channels={"UCx": "needs re-login"}, quota_exhausted=True)
        telegram.on_run_report(rep)
        telegram.on_run_report(rep)  # same alerts again: not repeated
        texts = fake.texts()
        assert sum("needs a new login" in t for t in texts) == 1 and sum("quota" in t for t in texts) == 1
        now = dt.datetime(2026, 11, 4, 17, 0, tzinfo=dt.timezone.utc)  # 22:30 IST
        assert b.daily_summary_if_due(now) is True
        assert b.daily_summary_if_due(now + dt.timedelta(minutes=5)) is False  # once a day
        assert "Posted today" in fake.texts()[-1]
        assert b.daily_summary_if_due(dt.datetime(2026, 11, 5, 10, 0, tzinfo=dt.timezone.utc)) is False  # 15:30 IST: too early


def test_telegram_settings_api(tg):
    b, fake, app = tg
    with TestClient(app) as c:
        r = c.post("/api/telegram/token", json={"token": "999:xyz"})
        assert r.status_code == 200 and r.json()["bot_username"] == "clipforge_test_bot"
        assert r.json()["token"] != "999:xyz"  # masked
        code = c.post("/api/telegram/pair").json()["code"]
        assert len(code) == 6
        assert c.post("/api/telegram/test").status_code == 400  # not paired yet


# ----------------------------------------------------------------------------- service / remote / preview / watch / PWA
def test_watchdog_restarts_with_backoff(env):
    from clipforge import service

    class P:
        def __init__(self, *a, **k):
            pass

        def wait(self):
            return 1

    sleeps, clock = [], iter(range(0, 1000, 1))
    n = service.run(max_restarts=4, popen=P, sleep=sleeps.append, clock=lambda: next(clock))
    assert n == 4 and sleeps == [10, 30, 60]
    xml = service.task_xml()
    assert "service run" in xml and "<LogonTrigger>" in xml and "RestartOnFailure" in xml and "<Hidden>true</Hidden>" in xml


def test_tailscale_status_parsing():
    from clipforge.remote import parse_status, serve_command

    ok = parse_status({"BackendState": "Running", "Self": {"DNSName": "laptop.tail1234.ts.net."},
                       "CurrentTailnet": {"MagicDNSEnabled": True}, "CertDomains": ["laptop.tail1234.ts.net"]})
    assert ok.running and ok.https_certs and ok.url == "https://laptop.tail1234.ts.net" and not ok.steps
    bad = parse_status({"BackendState": "NeedsLogin", "Self": {}})
    assert not bad.running and len(bad.steps) == 3 and "admin/dns" in " ".join(bad.steps)
    assert serve_command(8000) == ["serve", "--bg", "--https=443", "127.0.0.1:8000"]
    assert serve_command(8000, off=True)[-1] == "off"


def test_preview_proxy(tmp_path):
    from clipforge.stages.preview import ensure_preview, preview_path
    from clipforge.utils.ffmpeg import probe_video, run_ffmpeg

    clip = tmp_path / "03_some-title.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "color=red:s=1080x1920:d=1:r=30", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)])
    p = ensure_preview(clip)
    assert p == preview_path(clip) == tmp_path / "_work" / "03" / "preview.mp4"
    info = probe_video(p)
    assert info["width"] == 540 and info["height"] == 960
    mtime = p.stat().st_mtime
    assert ensure_preview(clip).stat().st_mtime == mtime  # reused, not re-encoded


def test_watch_folder_waits_for_stable_files(tmp_path):
    from clipforge.watch import scan

    f = tmp_path / "episode.mp4"
    f.write_bytes(b"x" * 10)
    (tmp_path / "notes.txt").write_text("hi")
    sizes: dict = {}
    assert scan(tmp_path, set(), sizes, now=0, settle=30) == []      # first sight
    assert scan(tmp_path, set(), sizes, now=10, settle=30) == []     # not settled yet
    f.write_bytes(b"x" * 20)                                          # still being synced
    assert scan(tmp_path, set(), sizes, now=40, settle=30) == []
    assert scan(tmp_path, set(), sizes, now=80, settle=30) == [f]
    assert scan(tmp_path, {str(f)}, sizes, now=200, settle=30) == []  # already started


def test_pwa_manifest_share_target_and_sw():
    root = Path(__file__).resolve().parent.parent / "frontend" / "public"
    m = json.loads((root / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert m["display"] == "standalone" and m["start_url"] == "/"
    assert {i["sizes"] for i in m["icons"]} >= {"192x192", "512x512"} and any(i.get("purpose") == "maskable" for i in m["icons"])
    assert m["share_target"]["action"] == "/share" and m["share_target"]["params"]["url"] == "url"
    sw = (root / "sw.js").read_text(encoding="utf-8")
    assert "/api/" in sw and "startsWith('/api/')" in sw  # the API is never cached
    for icon in m["icons"]:
        assert (root / icon["src"].lstrip("/")).exists()


# ----------------------------------------------------------------------------- security review regressions
def test_other_websites_cannot_post_to_localhost(env):
    app, _ = env
    with TestClient(app) as local:
        evil = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
        assert local.post("/api/publishing/run", headers=evil).status_code == 403
        assert local.post("/api/publishing/client-secret", content=b"{}", headers={**evil, "Content-Type": "text/plain"}).status_code == 403
        assert local.get("/api/probe?url=https://youtu.be/x", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert local.post("/api/publishing/run", headers={"Origin": "https://evil.example"}).status_code == 403  # no Sec-Fetch-*
        ok = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
        assert local.post("/api/publishing/run", headers=ok).status_code == 200
        assert local.post("/api/publishing/run", headers={"Origin": "http://localhost:5173"}).status_code == 200  # vite dev
        # Google / Meta sign-in redirects are cross-site navigations and must still work
        assert local.get("/api/oauth/callback?error=access_denied", headers={"Sec-Fetch-Site": "cross-site"},
                         follow_redirects=False).status_code in (302, 303, 307)


def test_dev_proxy_and_test_hosts_are_not_trusted(env, monkeypatch):
    app, _ = env
    with TestClient(app) as c:
        # Vite proxy (xfwd) forwarding a LAN phone: loopback peer, local Host, but X-Forwarded-For = phone
        assert c.get("/api/jobs", headers={"X-Forwarded-For": "192.168.1.50", "Host": "127.0.0.1:8000"}).status_code == 401
        # ... and forwarding the laptop's own browser stays trusted
        assert c.get("/api/jobs", headers={"X-Forwarded-For": "127.0.0.1", "Host": "127.0.0.1:8000"}).status_code == 200
        monkeypatch.delenv("CLIPFORGE_TESTING")
        assert c.get("/api/jobs").status_code == 401  # "testclient"/"testserver" only count under pytest


def test_global_flood_slows_but_never_locks_out_the_owner(env, monkeypatch):
    from clipforge.api import auth

    app, _ = env
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    with TestClient(app) as local:
        local.post("/api/auth/setup", json={"password": "correct horse battery"})
        for i in range(20):  # attacker rotates addresses
            TestClient(app, headers={"X-Forwarded-For": f"100.64.0.{i}"}).post("/api/auth/login", json={"password": "x"})
        owner = TestClient(app, headers={"X-Forwarded-For": "100.64.9.9", "Host": "laptop.ts.net"})
        assert owner.post("/api/auth/login", json={"password": "correct horse battery"}).status_code == 200


def test_pairing_code_burns_after_5_wrong_guesses(tg):
    from clipforge.config import Secrets

    b, fake, app = tg
    code = b.new_pair_code()
    wrong = f"{(int(code) + 1) % 10 ** 6:06d}"
    for _ in range(5):
        b.handle(_msg(666, f"/pair {wrong}"))
    b.handle(_msg(666, f"/pair {code}"))  # even the right code is dead now
    assert not Secrets().telegram_chat_id and b.pair_code is None


def test_remote_session_limits(env):
    app, _ = env
    with TestClient(app) as local:
        local.post("/api/auth/setup", json={"password": "correct horse battery"})
        r = _remote(app)
        r.post("/api/auth/login", json={"password": "correct horse battery"})
        h = {"X-CSRF-Token": r.cookies["cf_csrf"]}
        assert r.post("/api/jobs", json={"url": str(Path(__file__))}, headers=h).status_code == 400  # laptop files
        assert r.put("/api/settings", json={"config": {"auth": {"trust_localhost": False}}}, headers=h).status_code == 403
        assert r.put("/api/settings", json={"config": {"clips": {"count": 18}}}, headers=h).status_code == 200
        uid = local.post("/api/uploads", json={"filename": "a.mp4", "size": 10}).json()["upload_id"]
        assert local.put(f"/api/uploads/{uid}?offset=0", content=b"x" * 11).status_code == 400  # over the declared size
        assert local.put(f"/api/uploads/{uid}?offset=0", content=b"x" * 10).json()["received"] == 10
