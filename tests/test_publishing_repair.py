"""Publishing repair (2026-11-05): state machine, per-account mode resolution, scheduler → platform client, YouTube
error mapping, leases, IST/UTC, migration, Instagram recovery, shared status wording. Fakes only (no network)."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

from clipforge.publish.timez import UTC
from tests.fake_instagram import FakeInstagram
from tests.fake_youtube import FakeYouTube
from tests.test_phase125 import _channel, _env, _schedule
from tests.test_publish import NOW


@pytest.fixture
def api(tmp_path, monkeypatch):
    yield _env(tmp_path, monkeypatch, "api")
    from clipforge.publish import youtube as yt

    yt.set_client_factory(None)


@pytest.fixture
def assisted(tmp_path, monkeypatch):
    yield _env(tmp_path, monkeypatch, "assisted")
    from clipforge.publish import youtube as yt

    yt.set_client_factory(None)


def _cfg():
    from clipforge.config import load_config

    return load_config()


# ============================================================================ state machine
def test_transitions_legal_and_illegal(api):
    from clipforge.publish import queue as q
    from clipforge.publish import states

    _channel()
    it = _schedule(1, now=NOW + dt.timedelta(days=20))[0]
    assert it.status == "queued"
    it.status = "published"  # no media id
    with pytest.raises(states.IllegalTransition, match="without a platform media id"):
        q.save(it)
    it = q.get(it.id)
    it.status, it.youtube_video_id = "scheduled", "ytX"
    q.save(it)  # queued → scheduled (link) is legal
    it.status = "draft"
    with pytest.raises(states.IllegalTransition, match="scheduled → draft"):
        q.save(it)
    it = q.get(it.id)
    it.status = "published"
    q.save(it)
    it.status = "queued"
    with pytest.raises(states.IllegalTransition, match="published → queued"):  # published is final
        q.save(it)
    with pytest.raises(states.IllegalTransition, match="unknown status"):
        states.check("queued", "bogus")


def test_canonical_names_cover_every_status():
    from clipforge.publish import states

    assert set(states.CANONICAL) == set(states.STATUSES)
    assert set(states.TRANSITIONS) == set(states.STATUSES)
    for to in states.TRANSITIONS.values():
        assert to <= set(states.STATUSES)


# ============================================================================ mode resolution
def test_mode_resolution_account_beats_default(assisted):
    from clipforge.publish import channels as chs
    from clipforge.publish import modes
    from clipforge.services import publishing as pub

    _channel()
    ch = chs.get("UCtest")
    assert modes.resolve(ch).mode == "assisted" and modes.resolve(ch).source == "default"
    pub.set_account_mode("UCtest", "auto")
    m = modes.resolve(chs.get("UCtest"))
    assert (m.mode, m.source, m.problem) == ("auto", "account", "")
    pub.set_account_mode("UCtest", "")
    assert modes.resolve(chs.get("UCtest")).source == "default"


def test_account_in_auto_uploads_even_when_global_default_is_assisted(assisted):
    """The real bug: the global default (assisted) routed every item to Studio. An explicit per-account auto wins."""
    from clipforge.publish import runner
    from clipforge.services import publishing as pub

    _channel()
    items = _schedule(2, now=NOW + dt.timedelta(days=2))
    assert runner.run_once(_cfg(), now=NOW).uploaded == []  # assisted: nothing uploads
    pub.set_account_mode("UCtest", "auto")
    rep = runner.run_once(_cfg(), now=NOW)
    assert sorted(rep.uploaded) == sorted(i.id for i in items)
    assert all(b["status"]["privacyStatus"] == "private" and b["status"]["publishAt"] for b in assisted.uploaded_bodies)


def test_auto_never_silently_downgraded(api, monkeypatch):
    """Auto mode without a usable login → FAILED auto_unavailable with Reconnect / Switch to assisted (not Studio)."""
    from clipforge.publish import modes, queue as q, runner
    from clipforge.publish import youtube as yt

    _channel()
    items = _schedule(2, now=NOW + dt.timedelta(days=2))
    monkeypatch.setattr(yt, "_factory", yt._default_factory)  # the real client: needs a token file, none exists
    rep = runner.run_once(_cfg(), now=NOW)
    assert "auto mode blocked" in rep.skipped_channels["UCtest"]
    for it in items:
        it = q.get(it.id)
        assert (it.status, it.reason) == ("failed", "auto_unavailable")
        assert "switch_assisted" in q.public(it)["actions"] and "no saved Google login" in it.error
    # a token without the upload permission is reported too
    p = yt.token_path("UCtest")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"scopes": ["https://www.googleapis.com/auth/youtube.readonly"]}), encoding="utf-8")
    from clipforge.publish import channels as chs

    assert "youtube.upload" in modes.resolve(chs.get("UCtest")).problem
    # once it works again (fake client back), the items return to the queue and upload
    monkeypatch.setattr(yt, "_factory", lambda cid, cfg: api)
    rep = runner.run_once(_cfg(), now=NOW)
    assert sorted(rep.uploaded) == sorted(i.id for i in items)


def test_switch_assisted_fix(api, monkeypatch):
    from clipforge.publish import channels as chs, modes, queue as q, runner
    from clipforge.publish import youtube as yt
    from clipforge.services import publishing as pub

    _channel()
    it = _schedule(1, now=NOW + dt.timedelta(days=2))[0]
    monkeypatch.setattr(yt, "_factory", yt._default_factory)
    runner.run_once(_cfg(), now=NOW)
    assert q.get(it.id).reason == "auto_unavailable"
    out = pub.fix(it.id, "switch_assisted")
    assert out.status == "queued" and modes.resolve(chs.get("UCtest")).mode == "assisted"


def test_manual_instagram_account_is_assisted_and_cannot_be_switched_to_auto(api):
    from clipforge.publish import channels as chs, modes
    from clipforge.services import publishing as pub
    from clipforge.services.errors import ServiceError

    ch = chs.add_manual_instagram("someone")
    m = modes.resolve(ch)
    assert (m.mode, m.source) == ("assisted", "manual_account")
    with pytest.raises(ServiceError, match="added manually"):
        pub.set_account_mode(ch.id, "auto")


def test_instagram_auto_without_meta_app_fails_loudly(tmp_path, monkeypatch):
    """An API Instagram account whose Meta app is missing: FAILED auto_unavailable, never 'post it yourself'."""
    from clipforge.publish import channels as chs, instagram as ig, queue as q, runner
    from clipforge.config import Secrets

    fake_yt = _env(tmp_path, monkeypatch, "api")
    try:
        fake = FakeInstagram()
        ig.set_client_factory(lambda cfg: fake)
        monkeypatch.setattr(ig, "preflight", lambda path: [])
        from tests.test_instagram import _connect, _schedule_ig

        ch = _connect()
        items = _schedule_ig(ch.id, 1)
        ig.set_client_factory(None)  # real client again …
        monkeypatch.setattr(Secrets, "meta_app_id", "", raising=False)
        monkeypatch.setattr(ig, "configured", lambda: False)  # … and no Meta app
        runner.run_once(_cfg(), now=items[0].slot_utc.replace(tzinfo=UTC) - dt.timedelta(minutes=10))
        it = q.get(items[0].id)
        assert (it.status, it.reason) == ("failed", "auto_unavailable") and "Meta app" in it.error
        assert fake_yt.uploaded_bodies == []
    finally:
        from clipforge.publish import youtube as yt

        ig.set_client_factory(None)
        yt.set_client_factory(None)


# ============================================================================ scheduler tick → platform client
def test_scheduler_tick_calls_the_platform_client(api):
    """The worker's tick function runs the scheduler, which calls client.upload for a due item (not just a status
    flip), and the stored status comes from the platform's answer."""
    from clipforge.publish import queue as q
    from clipforge.publish import worker as w

    _channel()
    it = _schedule(1, now=dt.datetime.now(UTC) + dt.timedelta(days=1))[0]
    assert api.uploaded_bodies == []
    w.run_scheduler()  # exactly what the ticker / `clipforge scheduler --once` run
    assert len(api.uploaded_bodies) == 1
    it = q.get(it.id)
    assert it.youtube_video_id == "yt1" and it.status in ("processing", "scheduled")
    assert it.yt_state.get("uploadStatus") in ("uploaded", "processed")  # read back from videos.list
    assert it.lease_owner == "" and it.last_attempt_at is not None  # lease taken, then released
    assert it.idempotency_key == f"youtube:UCtest:{it.clip_key}"
    w.run_scheduler()
    assert len(api.uploaded_bodies) == 1  # idempotent: never uploaded twice


def test_lease_is_taken_by_the_claim(api):
    from clipforge.publish import queue as q

    _channel()
    it = _schedule(1, now=NOW + dt.timedelta(days=2))[0]
    assert q.claim_for_upload(it.id, it.slot_utc)
    got = q.get(it.id)
    assert got.status == "uploading" and got.lease_owner == q.lease_owner() and got.lease_expires_at is not None
    assert not q.claim_for_upload(it.id, it.slot_utc)  # a second claimer loses


# ============================================================================ YouTube request + errors
def test_upload_body_fields(api, monkeypatch):
    from clipforge.publish import queue as q, runner

    _channel()
    it = _schedule(1, now=NOW + dt.timedelta(days=2))[0]
    it = q.get(it.id)
    it.title = "x" * 150
    body = runner.upload_body(it, {"language": "hi"}, it.slot_utc, synthetic=True)
    assert len(body["snippet"]["title"]) == 100 and body["snippet"]["defaultAudioLanguage"] == "hi"
    assert body["snippet"]["categoryId"] and isinstance(body["snippet"]["tags"], list)
    st = body["status"]
    assert st["privacyStatus"] == "private" and st["publishAt"].endswith("Z")
    assert st["selfDeclaredMadeForKids"] is False and st["containsSyntheticMedia"] is True


class _Resp(dict):
    def __init__(self, status: int):
        super().__init__()
        self.status, self.reason = status, ""


def _http_error(status: int, reason: str, message: str = "boom"):
    from googleapiclient.errors import HttpError

    content = json.dumps({"error": {"code": status, "message": message, "errors": [{"reason": reason}]}}).encode()
    return HttpError(_Resp(status), content)


@pytest.mark.parametrize("status,reason,expect_status,expect_reason", [
    (400, "uploadLimitExceeded", "queued", "upload_limit"),
    (403, "forbidden", "failed", "upload_failed"),
    (400, "invalidMetadata", "failed", "upload_failed"),
    (500, "backendError", "queued", "retrying"),
    (403, "quotaExceeded", "queued", "quota"),
])
def test_upload_error_mapping(api, status, reason, expect_status, expect_reason):
    from clipforge.publish import queue as q, runner

    _channel()
    it = _schedule(1, now=NOW + dt.timedelta(days=2))[0]

    def boom(path, body, on_progress=None):
        raise _http_error(status, reason, "the real message")

    api.upload = boom
    runner.run_once(_cfg(), now=NOW)
    got = q.get(it.id)
    assert (got.status, got.reason) == (expect_status, expect_reason), got.error
    assert got.lease_owner == ""
    if expect_status == "failed":
        assert "the real message" in got.error
    if expect_reason == "retrying":
        assert got.next_attempt_at is not None
        from clipforge.publish import states

        assert states.canonical(got) == "RETRYING"


def test_invalid_grant_marks_reauth(api):
    from clipforge.publish import channels as chs, queue as q, runner, states
    from clipforge.publish import youtube as yt

    _channel()
    it = _schedule(1, now=NOW + dt.timedelta(days=1))[0]

    def boom(path, body, on_progress=None):
        raise yt.NeedsReauth("Google rejected the saved login (invalid_grant).")

    api.upload = boom
    rep = runner.run_once(_cfg(), now=NOW)
    assert rep.skipped_channels["UCtest"] == "needs re-login"
    assert chs.get("UCtest").auth_status == "needs_reauth"
    got = q.get(it.id)
    assert got.reason == "login_expired" and states.canonical(got) == "REQUIRES_REAUTHENTICATION"
    assert got.attempts == 0  # not the clip's fault


def test_token_refresh_invalid_grant_raises_needs_reauth(tmp_path, monkeypatch):
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    from clipforge.publish import youtube as yt

    _env(tmp_path, monkeypatch, "api")
    p = yt.token_path("UCx")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"token": "a", "refresh_token": "r", "token_uri": "https://oauth2.googleapis.com/token",
                             "client_id": "c", "client_secret": "s", "scopes": yt.SCOPES,
                             "expiry": "2000-01-01T00:00:00Z"}), encoding="utf-8")

    def refresh(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", refresh)
    with pytest.raises(yt.NeedsReauth, match="invalid_grant"):
        yt.load_credentials("UCx")
    yt.set_client_factory(None)


def test_processing_then_scheduled_then_published(api):
    from clipforge.publish import queue as q, runner

    _channel()
    api.process_after_reads = 5  # YouTube is slow today
    it = _schedule(1, now=NOW + dt.timedelta(days=2))[0]
    runner.run_once(_cfg(), now=NOW)
    assert q.get(it.id).status == "processing"
    for k in range(1, 8):
        runner.run_once(_cfg(), now=NOW + dt.timedelta(minutes=11 * k))
    assert q.get(it.id).status == "scheduled"
    api.publish_due(it.slot_utc.replace(tzinfo=UTC) + dt.timedelta(minutes=1))
    rep = runner.run_once(_cfg(), now=it.slot_utc.replace(tzinfo=UTC) + dt.timedelta(minutes=30))
    got = q.get(it.id)
    assert got.status == "published" and it.id in rep.published and got.youtube_video_id


def test_studio_instead_for_locked_private_keeps_history(api, tmp_path):
    from clipforge.publish import queue as q, runner
    from clipforge.services import publishing as pub

    _channel()
    api.lock_private = True
    it = _schedule(1, now=NOW + dt.timedelta(days=2))[0]
    runner.run_once(_cfg(), now=NOW)
    got = q.get(it.id)
    assert got.status == "locked_private"
    old = got.youtube_video_id
    out = pub.fix(it.id, "studio_instead")
    assert (out.status, out.reason) == ("needs_manual", "assisted_batch") and out.batch and not out.youtube_video_id
    assert out.yt_state["replaced_video_id"] == old


# ============================================================================ Instagram
@pytest.fixture
def ig_env(tmp_path, monkeypatch):
    fyt = _env(tmp_path, monkeypatch, "api")
    from clipforge.publish import instagram as ig

    fake = FakeInstagram()
    ig.set_client_factory(lambda cfg: fake)
    monkeypatch.setattr(ig, "preflight", lambda path: [])
    yield fake, fyt
    from clipforge.publish import youtube as yt

    ig.set_client_factory(None)
    yt.set_client_factory(None)


def test_ig_flow_states_and_publish_once(ig_env):
    from clipforge.publish import queue as q
    from tests.test_instagram import _connect, _run, _schedule_ig

    fake, _ = ig_env
    ch = _connect()
    it = _schedule_ig(ch.id, 1)[0]
    slot = it.slot_utc.replace(tzinfo=UTC)
    fake.process_ticks = 3
    _run(slot - dt.timedelta(minutes=20))
    got = q.get(it.id)
    assert got.status == "processing" and got.ig_container_id  # uploaded, Meta processing
    _run(slot - dt.timedelta(minutes=10))
    _run(slot - dt.timedelta(minutes=5))
    assert q.get(it.id).status == "ready"
    _run(slot)
    got = q.get(it.id)
    assert got.status == "published" and got.ig_media_id and got.permalink
    _run(slot + dt.timedelta(minutes=1))
    assert len(fake.published) == 1  # never published twice


def test_ig_published_container_without_media_is_not_marked_published(ig_env):
    from clipforge.publish import queue as q
    from tests.test_instagram import _connect, _run, _schedule_ig

    fake, _ = ig_env
    ch = _connect()
    it = _schedule_ig(ch.id, 1)[0]
    slot = it.slot_utc.replace(tzinfo=UTC)
    _run(slot - dt.timedelta(minutes=20))
    _run(slot - dt.timedelta(minutes=10))
    fake.lose_publish_response = True
    real_recent = fake.recent_media
    fake.recent_media = lambda *a, **k: []  # Meta published it, but the Reel isn't listed yet
    _run(slot)
    _run(slot + dt.timedelta(minutes=1))  # container says PUBLISHED, Reel not found: NOT published, keeps looking
    got = q.get(it.id)
    assert got.status != "published" and not got.ig_media_id and got.reason == "ig_find_media"
    assert len(fake.published) == 1
    fake.recent_media = real_recent
    _run(slot + dt.timedelta(minutes=2))
    got = q.get(it.id)
    assert got.status == "published" and got.ig_media_id == "m1" and len(fake.published) == 1


def test_ig_format_error_is_not_retried(ig_env):
    from clipforge.publish import instagram as ig, queue as q
    from tests.test_instagram import _connect, _run, _schedule_ig

    fake, _ = ig_env
    ch = _connect()
    it = _schedule_ig(ch.id, 1)[0]

    def bad(*a, **k):
        raise ig.IGError("Meta API error 9004/2207026: Unsupported video format", code=9004, subcode=2207026, format=True)

    fake.create_reel = bad
    _run(it.slot_utc.replace(tzinfo=UTC) - dt.timedelta(minutes=20))
    got = q.get(it.id)
    assert (got.status, got.reason) == ("failed", "ig_format") and "2207026" in got.error


def test_graph_error_parsing_and_token_redaction():
    import httpx

    from clipforge.publish import instagram as ig

    def handler(request):
        return httpx.Response(400, json={"error": {"code": 100, "error_subcode": 2207026, "message": "bad",
                                                   "error_user_title": "Unsupported format",
                                                   "error_user_msg": "The video format is not supported."}})

    c = ig.GraphIG.__new__(ig.GraphIG)
    c.base, c.http = "https://graph.facebook.com/v26.0", httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ig.IGError) as e:
        c._do("POST", "/1/media", data={"access_token": "SECRET"})
    assert e.value.subcode == 2207026 and e.value.format and "2207026" in str(e.value)
    assert "Unsupported format" in str(e.value)
    assert ig._redact("GET https://x/?access_token=EAAB123abc&x=1 OAuth EAAzzz") == \
        "GET https://x/?access_token=<redacted>&x=1 OAuth <redacted>"


# ============================================================================ IST ↔ UTC
def test_ist_utc_date_boundary():
    from clipforge.publish.timez import local_to_utc, parse_local, quota_day, utc_to_local

    # 01:15 IST on 6 Nov is still 5 Nov in UTC (19:45)
    u = local_to_utc(dt.date(2026, 11, 6), "1:15")
    assert u == dt.datetime(2026, 11, 5, 19, 45, tzinfo=UTC)
    assert utc_to_local(u).date() == dt.date(2026, 11, 6)
    assert parse_local("2026-11-06T01:15") == u
    # 23:59 UTC on 5 Nov is 6 Nov 05:29 IST; YouTube's quota day is Pacific (still 5 Nov)
    late = dt.datetime(2026, 11, 5, 23, 59, tzinfo=UTC)
    assert utc_to_local(late).strftime("%d %H:%M") == "06 05:29"
    assert quota_day(late) == "2026-11-05"


# ============================================================================ migration
def test_migration_backs_up_and_keeps_old_rows(tmp_path, monkeypatch):
    import clipforge.config as config_mod

    ws = tmp_path / "ws"
    ws.mkdir()
    db_path = ws / "clipforge.db"
    con = sqlite3.connect(db_path)  # a v15 queueitem table without the new columns, with old statuses
    con.execute("CREATE TABLE queueitem (id INTEGER PRIMARY KEY, channel_id VARCHAR, video_id VARCHAR, rank INTEGER, "
                "clip_key VARCHAR, status VARCHAR, platform VARCHAR, youtube_video_id VARCHAR, ig_media_id VARCHAR)")
    con.executemany("INSERT INTO queueitem (channel_id, video_id, rank, clip_key, status, platform, youtube_video_id, "
                    "ig_media_id) VALUES (?,?,?,?,?,?,?,?)",
                    [("UC", "v", 1, "k1", "scheduled", "youtube", "yt1", ""),
                     ("UC", "v", 2, "k2", "needs_manual", "youtube", "", "")])
    con.execute("PRAGMA user_version=15")
    con.commit()
    con.close()
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"output: {{dir: '{ws.as_posix()}'}}\nllm: {{provider: mock}}\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", cfg_path)
    from clipforge.api import db

    monkeypatch.setattr(db, "_engine", None)
    db.engine()
    backups = list((ws / "backups").glob("clipforge-*-v15.db"))
    assert len(backups) == 1
    con = sqlite3.connect(db_path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(queueitem)")}
    assert {"lease_owner", "lease_expires_at", "last_attempt_at", "idempotency_key"} <= cols
    assert [r[0] for r in con.execute("SELECT status FROM queueitem ORDER BY id")] == ["scheduled", "needs_manual"]
    idx = {r[1] for r in con.execute("PRAGMA index_list(queueitem)")}
    assert {"ux_queue_clip_platform_account", "ux_queue_youtube_media", "ux_queue_instagram_media"} <= idx
    with pytest.raises(sqlite3.IntegrityError):  # one publication per (clip, platform, account)
        con.execute("INSERT INTO queueitem (channel_id, video_id, rank, clip_key, status, platform, youtube_video_id, "
                    "ig_media_id) VALUES ('UC','v',1,'k1','draft','youtube','','')")
    with pytest.raises(sqlite3.IntegrityError):  # a YouTube id belongs to one publication
        con.execute("INSERT INTO queueitem (channel_id, video_id, rank, clip_key, status, platform, youtube_video_id, "
                    "ig_media_id) VALUES ('UC','v',3,'k3','scheduled','youtube','yt1','')")
    con.close()
    snap = sqlite3.connect(backups[0])
    assert snap.execute("SELECT count(*) FROM queueitem").fetchone()[0] == 2
    snap.close()
    db._engine.dispose()
    monkeypatch.setattr(db, "_engine", None)


# ============================================================================ wording (web app + Telegram)
def test_status_text_is_honest(api):
    from clipforge.publish import queue as q, runner, states
    from clipforge.services import publishing as pub

    _channel()
    it = _schedule(1, now=NOW + dt.timedelta(days=2))[0]
    t = pub.status_text(q.get(it.id))
    assert t["state"] == "SCHEDULED" and "not uploaded yet" in t["label"] and "Published" not in t["label"]
    pub.set_account_mode("UCtest", "assisted")
    assert "assisted" in pub.status_text(q.get(it.id))["label"]
    pub.set_account_mode("UCtest", "auto")
    runner.run_once(_cfg(), now=NOW)
    for st in ("processing", "scheduled"):
        x = q.get(it.id)
        x.status = st
        lab = states.status_text(x)["label"]
        assert "Published" not in lab and "publish" not in lab.split()[0].lower()
    x = q.get(it.id)
    x.status = "published"
    assert states.status_text(x)["label"] == "Published"
    out = pub.item_out(q.get(it.id))
    assert out["status_text"]["label"] and out["status_text"]["state"]


def test_telegram_uses_the_same_status_words(api):
    from clipforge.notify import tg_publish
    from clipforge.publish import queue as q
    from clipforge.services import publishing as pub

    _channel()
    it = q.get(_schedule(1, now=NOW + dt.timedelta(days=2))[0].id)
    assert pub.status_text(it)["label"] in tg_publish.status_line(it)


def test_health_has_heartbeat_and_account_mode(api):
    from clipforge.publish import runner
    from clipforge.services import publishing as pub

    _channel()
    _schedule(1, now=dt.datetime.now(UTC) + dt.timedelta(days=1))
    runner.run_once(_cfg(), source="task")
    h = pub.health()
    hb = h["heartbeat"]
    assert hb["last_run"].endswith("IST") and hb["source"] == "task" and hb["items_processed"] >= 1
    acc = h["accounts"][0]
    assert acc["mode"] == "auto" and acc["mode_problem"] == "" and acc["audit_status"]
    assert acc["upcoming"] >= 0 and "last_published" in acc


def test_link_accepts_every_youtube_url_form():
    from clipforge.publish import queue as q
    from clipforge.publish.reconcile import parse_video_id

    for t in ("Cx093Vl1V7M", "https://www.youtube.com/watch?v=Cx093Vl1V7M&t=3", "https://youtu.be/Cx093Vl1V7M",
              "youtube.com/shorts/Cx093Vl1V7M?feature=share", "https://studio.youtube.com/video/Cx093Vl1V7M/edit"):
        assert parse_video_id(t) == "Cx093Vl1V7M", t
    with pytest.raises(q.QueueError):
        parse_video_id("https://www.youtube.com/watch")


def test_upload_path_confined_to_workspace(api, tmp_path, monkeypatch):
    from clipforge.publish import queue as q

    _channel()
    it = _schedule(1, now=NOW + dt.timedelta(days=2))[0]
    assert q.clip_file(it, _cfg()).resolve().is_relative_to(_cfg().workspace.resolve())
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"x")
    ws, _c = q.candidate_for(it, _cfg())
    monkeypatch.setattr(type(ws), "find_clip", lambda self, rank: outside)
    with pytest.raises(q.QueueError, match="outside the workspace"):
        q.clip_file(it, _cfg())


def test_api_routes_smoke(api):
    from fastapi.testclient import TestClient

    from clipforge.api.server import create_app

    _channel()
    it = _schedule(1, now=NOW + dt.timedelta(days=2))[0]
    app = create_app()
    app.state.publish_ticker = False
    with TestClient(app) as c:
        ch = c.get("/api/channels").json()[0]
        assert ch["mode"] == "auto" and ch["mode_setting"] == "default" and ch["mode_problem"] == ""
        r = c.patch("/api/channels/UCtest", json={"mode": "assisted"})
        assert r.status_code == 200 and r.json()["mode"] == "assisted" and r.json()["mode_setting"] == "assisted"
        row = next(x for x in c.get("/api/queue").json() if x["id"] == it.id)
        assert row["status_text"]["label"] == "Planned (assisted)" and row["status_text"]["state"] == "SCHEDULED"
        assert c.post(f"/api/queue/{it.id}/fix", json={"action": "switch_auto"}).status_code == 200
        assert c.get("/api/channels").json()[0]["mode"] == "auto"
        assert c.post(f"/api/queue/{it.id}/fix", json={"action": "bogus"}).status_code == 422
        h = c.get("/api/publishing/health").json()
        assert h["heartbeat"]["text"] and h["accounts"][0]["mode"] == "auto"
        assert c.get("/api/publishing/status").status_code == 200
        assert c.post(f"/api/queue/{it.id}/link", json={"video_id": "https://www.youtube.com/watch"}).status_code == 400


def test_existing_titles_with_undated_video(api):
    """Regression (5 Nov 2026): a synced video without publishedAt (a private upload) next to dated ones crashed
    the queue list and the scheduler: naive datetime.min vs UTC-aware DB datetimes."""
    from clipforge.api import db
    from clipforge.publish import queue as q

    _channel()
    it = _schedule(1, now=NOW + dt.timedelta(days=2))[0]
    with db.session() as s:
        s.add(db.VideoPerf(youtube_video_id="a1", channel_id="UCtest", title="Dated", published_at=NOW))
        s.add(db.VideoPerf(youtube_video_id="a2", channel_id="UCtest", title="Private test", published_at=None))
        s.commit()
    assert {"Dated", "Private test"} <= set(q.existing_titles("UCtest", it.id))
    q.problems(q.get(it.id))  # what the queue list and the uploader call


def test_upload_limit_pauses_the_channel_and_resumes(api):
    """Live 5 Nov 2026: after ~10 uploads YouTube answered 400 uploadLimitExceeded for every further clip. One refused
    call must pause the channel (not 29 calls, not FAILED), and uploads resume by themselves later."""
    from clipforge.publish import queue as q, runner

    _channel()
    items = _schedule(3, now=NOW + dt.timedelta(days=2))
    real, calls = api.upload, []

    def limited(path, body, on_progress=None):
        calls.append(1)
        raise _http_error(400, "uploadLimitExceeded", "The user has exceeded the number of videos they may upload.")

    api.upload = limited
    rep = runner.run_once(_cfg(), now=NOW)
    assert len(calls) == 1 and rep.failed == []
    for it in items:
        got = q.get(it.id)
        assert (got.status, got.reason) == ("queued", "upload_limit") and got.next_attempt_at and got.attempts == 0
        assert "limit" in runner_states_label(got)
    runner.run_once(_cfg(), now=NOW + dt.timedelta(hours=1))
    assert len(calls) == 1  # paused: no new call
    api.upload = real
    rep = runner.run_once(_cfg(), now=NOW + runner.UPLOAD_LIMIT_PAUSE + dt.timedelta(minutes=1))
    assert sorted(rep.uploaded) == sorted(i.id for i in items)


def runner_states_label(it):
    from clipforge.publish import states

    return states.status_text(it)["label"].lower()
