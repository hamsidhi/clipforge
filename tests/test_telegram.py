"""Phase 15a: Telegram as a full remote control — every command / button path against the fake Bot API."""

from __future__ import annotations

import datetime as dt
import json
import time

import pytest

import clipforge.config as config_mod
from clipforge.models import CandidateFile, ClipCandidate, ClipMetadata, Segment, Transcript, VideoInfo, Word
from clipforge.workspace import Workspace
from tests.fake_telegram import TOKEN, FakeTelegram

SECRET_VARS = ("CLIPFORGE_PASSWORD_HASH", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_PIN_HASH")
OWNER, VIEWER, STRANGER = 42, 77, 999


@pytest.fixture
def tg(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"output: {{dir: '{(tmp_path / 'ws').as_posix()}'}}\nllm: {{provider: mock}}\n"
                        "telegram: {enabled: true, progress_every_sec: 5, clips_to_send: 2}\n"
                        "publishing: {mode: api}\n" "youtube: {min_gap_sec: 0, max_per_batch: 100, daily_safety_limit: 100}\nschedule: {enabled: false}\n", encoding="utf-8")
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

    monkeypatch.setattr(manager, "submit", lambda kind, job_id, payload=None: _submitted.append((kind, job_id, payload)) or 1)
    from clipforge.stages import preview

    monkeypatch.setattr(preview, "ensure_preview", lambda clip: clip)  # tests have no real video
    from clipforge.config import save_secret
    from clipforge.notify import telegram

    save_secret("TELEGRAM_BOT_TOKEN", TOKEN)
    fake = FakeTelegram()
    slept: list[float] = []
    b = telegram.Bot(transport=fake.transport(), sleep=slept.append)
    telegram.set_bot(b)
    from clipforge.notify import tg_store as st

    st.add_chat(OWNER, OWNER, "owner", "private", "me")
    _submitted.clear()
    yield b, fake, tmp_path
    telegram.set_bot(None)
    db._engine = None
    import os

    for v in SECRET_VARS:
        os.environ.pop(v, None)


_submitted: list = []


def seed_job(root, n_clips: int = 3, status: str = "done"):
    """A finished job with rendered clips, thumbnails, metadata and a transcript."""
    from clipforge.api import db

    ws = Workspace(root / "ws", "vidA")
    ws.write_model(ws.info, VideoInfo(id="vidA", title="Episode A", duration=300, width=1920, height=1080))
    words = [Word(start=i * 0.5, end=i * 0.5 + 0.4, text=f"w{i}" + ("." if i % 10 == 9 else "")) for i in range(560)]
    words[30] = Word(start=15.0, end=15.4, text="galti")
    ws.write_model(ws.transcript, Transcript(language="hi", model="t", duration=300,
                                             segments=[Segment(id=0, start=0, end=280, text="", words=words)]))
    cands = [ClipCandidate(rank=i, start=5 + 40 * (i - 1), end=35 + 40 * (i - 1), title=f"Clip {i} title", hook=f"hook {i}",
                           score=9 - i) for i in range(1, n_clips + 1)]
    ws.write_model(ws.candidates, CandidateFile(video_id="vidA", candidates=cands))
    metas = []
    for c in cands:
        f = ws.clips_dir / f"{c.rank:02d}_clip.mp4"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"\x00" * 2000)
        work = ws.work_dir(c.rank)
        (work / "thumb.jpg").write_bytes(b"\xff\xd8thumb")
        (work / "render.json").write_text(json.dumps({"candidate": c.model_dump()}))
        metas.append(ClipMetadata(rank=c.rank, file=f.name, start=c.start, end=c.end, duration=c.duration, score=c.score,
                                  title=c.title, hook=c.hook, yt_title=c.title, yt_description="desc #money",
                                  ig_caption="Caption text #Money #Biz", hashtags=["#Money", "#Biz"], credit="Credit"))
    ws.metadata_json.write_text(json.dumps([m.model_dump() for m in metas]), encoding="utf-8")
    with db.session() as s:
        job = db.Job(source="https://youtu.be/vidA", video_id="vidA", title="Episode A", status=status, n_clips=n_clips,
                     stage="done")
        s.add(job)
        s.commit()
        s.refresh(job)
    return job, ws


def send(b, fake, text, chat=OWNER, **kw):
    b.handle(fake.msg(text, chat=chat, **kw))


def press(b, fake, label, chat=OWNER, user=None):
    b.handle(fake.press(fake.button(label), chat=chat, user=user))


# ============================================================================ pairing, roles, silence
def test_pairing_roles_groups_and_unknown_silence(tg):
    from clipforge.api import db
    from clipforge.notify import tg_store as st

    b, fake, _ = tg
    send(b, fake, "/help", chat=STRANGER)
    send(b, fake, "/pair 000000", chat=STRANGER)
    assert fake.calls == []  # strangers get nothing at all — not even "unauthorized"
    with db.session() as s:
        from sqlmodel import select

        events = [e.detail for e in s.exec(select(db.AuditEvent)).all()]
    assert any("unknown Telegram user 999" in e for e in events)
    code = b.new_pair_code(role="viewer")
    send(b, fake, f"/pair {code}", chat=VIEWER)
    assert st.chat(VIEWER).role == "viewer" and "viewer" in fake.last_text()
    send(b, fake, f"/pair {code}", chat=1234)  # one-time
    assert st.chat(1234) is None
    code = b.new_pair_code()
    send(b, fake, f"/pair {code}", chat=-500, user=OWNER, kind="group")  # groups need a group code
    assert st.chat(-500) is None
    code = b.new_pair_code(allow_group=True)
    send(b, fake, f"/pair {code}", chat=-500, user=OWNER, kind="group")
    assert st.chat(-500).kind == "group"
    fake.clear()
    send(b, fake, "/jobs", chat=-500, user=555, kind="group")  # someone else in that group: ignored
    assert fake.calls == []
    send(b, fake, "/jobs", chat=-500, user=OWNER, kind="group")
    assert "No jobs yet" in fake.last_text()


def test_pair_code_burns_after_5_wrong(tg):
    from clipforge.notify import tg_store as st

    b, fake, _ = tg
    code = b.new_pair_code()
    for _ in range(5):
        send(b, fake, "/pair 111111", chat=STRANGER)
    send(b, fake, f"/pair {code}", chat=STRANGER)
    assert st.chat(STRANGER) is None and b.pair_code is None


def test_viewer_is_read_only(tg):
    from clipforge.notify import tg_store as st

    b, fake, tmp = tg
    job, _ = seed_job(tmp)
    st.add_chat(VIEWER, VIEWER, "viewer", "private", "friend")
    send(b, fake, f"/clips {job.id}", chat=VIEWER)
    labels = [x[0] for x in fake.buttons()]
    assert "▶ Preview" in labels and "✅ Approve" not in labels and "✏️ Edit" not in labels
    send(b, fake, f"/delete {job.id}", chat=VIEWER)
    assert "read-only" in fake.last_text()
    send(b, fake, f"/approve {job.id} 1", chat=VIEWER)
    assert "read-only" in fake.last_text()
    # an owner's button pressed in the viewer's chat doesn't exist there
    send(b, fake, f"/clip {job.id}-1")
    data = fake.button("✅ Approve")
    b.handle(fake.press(data, chat=VIEWER))
    assert "expired" in (fake.of("answerCallbackQuery")[-1].get("text") or "")


# ============================================================================ confirmations, PIN, type YES
def test_confirmation_bound_one_shot_and_expiring(tg, monkeypatch):
    from clipforge.api import db
    from clipforge.notify import tg_store as st

    b, fake, tmp = tg
    job, _ = seed_job(tmp)
    send(b, fake, f"/delete {job.id}")
    assert "can't be undone" in fake.last_text()
    yes = fake.button("✅ Yes")
    assert len(yes.encode()) <= 64
    b.handle(fake.press(yes, chat=OWNER, user=555))  # somebody else's tap (group): refused
    assert db.get_job(job.id) is not None
    b.handle(fake.press(yes))
    assert db.get_job(job.id) is None and "Deleted job" in fake.last_text() and "freed" in fake.last_text()
    b.handle(fake.press(yes))  # double tap: idempotent
    assert fake.of("answerCallbackQuery")[-1]["text"] == "Already done."
    job2, _ = seed_job(tmp)
    send(b, fake, f"/delete {job2.id}")
    yes = fake.button("✅ Yes")
    real_now = st.now
    monkeypatch.setattr(st, "now", lambda: real_now() + dt.timedelta(minutes=3))  # buttons expire after 2 min
    b.handle(fake.press(yes))
    assert db.get_job(job2.id) is not None and "expired" in fake.of("answerCallbackQuery")[-1]["text"]


def test_pin_with_deletion_and_lockout_and_type_yes(tg):
    from clipforge.api import db
    from clipforge.config import load_config, save_config
    from clipforge.notify import tg_core
    from clipforge.publish import channels as chs
    from clipforge.publish import queue as q

    b, fake, tmp = tg
    tg_core.set_pin("432199")
    save_config(load_config().with_overrides({"telegram.pin_required": True}))
    with db.session() as s:
        s.add(db.Channel(id="UCy", title="My Shorts", uploads_playlist="UUy", confirmed=True, paused=True))
        s.commit()
    send(b, fake, "/resume my shorts")
    press(b, fake, "✅ Yes")
    assert "PIN" in fake.last_text() and chs.get("UCy").paused
    for i in range(5):
        upd = fake.msg("111111")
        b.handle(upd)
        assert upd["message"]["message_id"] in fake.deleted  # the PIN message is deleted at once
    assert "locked" in fake.last_text() and chs.get("UCy").paused
    send(b, fake, "/resume my shorts")
    press(b, fake, "✅ Yes")
    assert "locked" in fake.last_text()
    # unlock (as if 15 min passed) and use the right PIN
    from clipforge.notify import tg_store as st

    st.kv_del("pin_fail")
    send(b, fake, "/resume my shorts")
    press(b, fake, "✅ Yes")
    send(b, fake, "432199")
    assert not chs.get("UCy").paused and "Resumed" in fake.last_text()
    # publish now needs "type YES" too
    item = q.save(db.QueueItem(channel_id="UCy", video_id="v", rank=1, clip_key="k1", title="Hot take", status="missed",
                               reason="slot_passed", slot_utc=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)))
    send(b, fake, "/needsyou")
    press(b, fake, "🚀 Publish now")
    press(b, fake, "✅ Yes")
    assert "YES</b>" in fake.last_text()
    send(b, fake, "yes please")
    assert "didn't type YES" in fake.last_text() and q.get(item.id).privacy != "public"


# ============================================================================ API client: limits, 429, splitting
def test_rate_limiter_and_retry_after(tg):
    from clipforge.notify.tg_api import BotAPI, Limiter

    t = [0.0]
    slept: list[float] = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    lim = Limiter(per_chat=1.0, burst=3, overall=25, edits_per_min=20, sleep=sleep, clock=lambda: t[0])
    for _ in range(3):
        assert lim.wait("42") == 0  # burst
    assert lim.wait("42") > 0.9  # then ~1 per second per chat
    assert lim.wait("43") == 0  # other chats are independent
    for _ in range(20):
        lim.wait("44", edit=True)
    assert lim.wait("44", edit=True) > 30  # edits: at most 20 a minute
    fake = FakeTelegram()
    fake.fail["sendMessage"] = [(429, {"ok": False, "error_code": 429, "description": "Too Many Requests",
                                       "parameters": {"retry_after": 7}})]
    api_slept: list[float] = []
    api = BotAPI(TOKEN, fake.transport(), sleep=api_slept.append, limiter=Limiter(sleep=api_slept.append))
    api.call("sendMessage", chat_id=1, text="hi")
    assert 7 in api_slept and len(fake.of("sendMessage")) == 2  # waited retry_after, then sent


def test_split_text_and_caption():
    from clipforge.notify.tg_api import split_caption, split_text

    words = " ".join(f"word{i}" for i in range(2000))  # ~16k chars
    parts = split_text(words)
    assert all(len(p) <= 4096 for p in parts) and len(parts) >= 4
    assert " ".join(parts).split() == words.split()  # nothing lost, nothing cut mid-word
    code = "<pre>" + "\n".join(f"line {i} " + "x" * 40 for i in range(200)) + "</pre>"
    parts = split_text("intro\n\n" + code)
    assert all(len(p) <= 4096 for p in parts) and all(p.count("<pre>") == p.count("</pre>") for p in parts)
    cap, rest = split_caption("a " * 900)
    assert len(cap) <= 1024 and rest and all(len(r) <= 4096 for r in rest)


def test_scrub_and_error_ids(tg):
    from clipforge.notify.tg_core import error_reply, scrub

    s = scrub(r"failed 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw at E:\youtube\clipforge\workspace\x\token.json "
              "from 100.101.102.103 /home/clipforge/.env password=hunter2")
    assert "AAHdq" not in s and "E:\\youtube" not in s and "100.101" not in s and "hunter2" not in s and "/home/" not in s
    msg = error_reply(RuntimeError("Traceback secret path C:\\Users\\me\\x.py"))
    assert "error <code>" in msg and "Users" not in msg


# ============================================================================ conversations
def test_conversation_timeout_and_cancel(tg, monkeypatch):
    from clipforge.notify import tg_store as st

    b, fake, tmp = tg
    job, _ = seed_job(tmp)
    send(b, fake, f"/edit {job.id}-1")
    press(b, fake, "📝 Title")
    assert fake.of("sendMessage")[-1]["reply_markup"]["force_reply"] is True
    send(b, fake, "/cancel")
    assert "Cancelled" in fake.last_text() and st.get_conversation(OWNER) is None
    press(b, fake, "📝 Title")
    real_now = st.now
    monkeypatch.setattr(st, "now", lambda: real_now() + dt.timedelta(minutes=11))
    send(b, fake, "A new title after the timeout")
    assert st.get_conversation(OWNER) is None and "I can show jobs" in fake.last_text()  # plain text → chat mode


# ============================================================================ files: fallback chain + file_id cache
def test_file_size_fallback_chain(tg, monkeypatch):
    from clipforge.notify import commands, tg_media

    b, fake, tmp = tg
    job, ws = seed_job(tmp)
    send(b, fake, f"/clip {job.id}-1")
    press(b, fake, "▶ Preview")
    assert fake.of("sendVideo")[-1]["supports_streaming"] == "true" and "YouTube title" in fake.last_text()
    uploads = fake.uploads
    press(b, fake, "▶ Preview")  # same file again: by file_id, no upload
    assert fake.uploads == uploads and fake.of("sendVideo")[-1]["video"].startswith("vid")
    press(b, fake, "⬇ Full quality")
    assert fake.of("sendDocument") and "YouTube title" in fake.last_text()
    # too big for the cloud API → Telegram-fit copy
    big = ws.find_clip(2)
    api = b.api()
    monkeypatch.setattr(type(api), "send_limit", property(lambda self: 1500))
    fit = big.parent / "_work" / "02" / "telegram_fit.mp4"
    monkeypatch.setattr(tg_media, "telegram_fit", lambda src, limit, duration: (fit.write_bytes(b"\x00" * 1000), fit)[1])
    send(b, fake, f"/clip {job.id}-2")
    press(b, fake, "⬇ Full quality")
    assert fake.of("sendDocument")[-1]["document"] == "<file telegram_fit.mp4>" and "Re-encoded" in fake.of("sendDocument")[-1]["caption"]
    # re-encoding impossible → private Tailscale link
    monkeypatch.setattr(tg_media, "telegram_fit", lambda *a: (_ for _ in ()).throw(RuntimeError("too long")))
    monkeypatch.setattr(commands, "app_url", lambda path: f"https://laptop.tail1234.ts.net{path}")
    send(b, fake, f"/clip {job.id}-3")
    press(b, fake, "⬇ Full quality")
    assert "ts.net/api/clips/" in fake.texts()[-2] and "Tailscale" in fake.texts()[-2]
    monkeypatch.setattr(commands, "app_url", lambda path: None)
    press(b, fake, "⬇ Full quality")
    assert "Too big for Telegram" in fake.texts()[-2] and "docs/TELEGRAM.md" in fake.texts()[-2]


def test_big_upload_explains_limit_and_small_one_starts_wizard(tg):
    from clipforge.services import jobs as job_svc

    b, fake, tmp = tg
    send(b, fake, "", video={"file_id": "F1", "file_unique_id": "u1", "file_size": 30 * 1024 * 1024})
    assert "20 MB" in fake.last_text()
    fake.files["videos/F2.mp4"] = b"\x00" * 5000
    send(b, fake, "", video={"file_id": "F2", "file_unique_id": "u2", "file_size": 5000, "file_name": "talk.mp4"})
    assert "New job" in fake.texts()[-2] and "talk.mp4" in fake.texts()[-2]
    press(b, fake, "Start with my defaults")
    assert _submitted and _submitted[-1][0] == "pipeline"
    job = job_svc.list_jobs()[0]
    assert job["source"].endswith("talk.mp4")


# ============================================================================ edits → re-render → resend → undo
def test_edit_rerender_resend_undo(tg):
    from clipforge.services import clips as clip_svc
    from clipforge.notify import telegram

    b, fake, tmp = tg
    job, ws = seed_job(tmp)
    cid = f"{job.id}-1"
    send(b, fake, f"/edit {cid}")
    press(b, fake, "📝 Title")
    send(b, fake, "Paisa kaise banta hai")
    assert "Paisa kaise banta hai" in fake.last_text()
    press(b, fake, "💾 Save")
    assert clip_svc.get_clip(cid)["title"] == "Paisa kaise banta hai"
    # trim with buttons: always a different word boundary, shows first/last words
    before = clip_svc.get_clip(cid)
    send(b, fake, f"/edit {cid}")
    press(b, fake, "+0.3")  # the newest "+0.3" is the end row's
    after = clip_svc.get_clip(cid)
    assert after["end"] > before["end"] and "Ends:" in fake.last_text()
    start_plus = [d for label, d, _ in fake.buttons() if label == "+0.3"][0]  # the start row's
    b.handle(fake.press(start_plus))
    assert clip_svc.get_clip(cid)["start"] > before["start"]
    send(b, fake, f"/trim 1 0:02-0:20")
    t = clip_svc.get_clip(cid)
    assert 16 <= t["duration"] <= 20 and "now" in fake.last_text()
    send(b, fake, "/words 1")
    assert "<code>" in fake.last_text()
    send(b, fake, '/word 1 "galti" "galat"')
    assert "Changed 1 word" in fake.last_text()
    assert "galat" in clip_svc.get_clip(cid)["word_edits"].values()
    press(b, fake, "🔁 Re-render")
    assert ("render", job.id, {"ranks": [1]}) in _submitted
    # the render finished: previous.mp4 exists, the new version is sent with Undo
    work = ws.work_dir(1)
    (work / "previous.mp4").write_bytes(b"\x01" * 1500)
    (work / "previous.json").write_text(json.dumps({"candidate": before | {"title": "Clip 1 title"}}))
    telegram.on_render(job.id, [1], True)
    assert "New version" in fake.of("sendVideo")[-1]["caption"] and "YouTube title" in fake.last_text()
    press(b, fake, "↩ Undo")
    assert clip_svc.get_clip(cid)["title"] == "Clip 1 title" and "Restored" in fake.texts()[-2]


# ============================================================================ times, poller, offset, modes, mini app
def test_ist_time_parsing():
    from clipforge.publish.timez import parse_when

    now = dt.datetime(2026, 11, 4, 10, 0, tzinfo=dt.timezone.utc)  # 15:30 IST
    ist = lambda s: parse_when(s, now=now).astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30))).strftime("%d %H:%M")  # noqa: E731
    assert ist("today 21:00") == "04 21:00" and ist("tomorrow 9pm") == "05 21:00"
    assert ist("10:00") == "05 10:00" and ist("5 Nov 18:30") == "05 18:30" and ist("nov 5 6:30 pm") == "05 18:30"
    with pytest.raises(ValueError):
        parse_when("today 9:00", now=now)  # passed
    with pytest.raises(ValueError):
        parse_when("someday", now=now)


def test_single_poller_lease_409_and_durable_offset(tg):
    from clipforge.notify import telegram
    from clipforge.notify import tg_store as st

    b, fake, _ = tg
    assert st.acquire_poller(120, owner="laptop") is True
    assert st.acquire_poller(120, owner="server") is False  # a second poller backs off
    assert b.poll_once(timeout=0) == -1 and fake.of("getUpdates") == []
    st.kv_del("poller")
    ups = [fake.msg("/help"), fake.msg("/jobs")]
    fake.updates = list(ups)
    assert b.poll_once(timeout=0) == 2
    last = ups[-1]["update_id"]
    assert st.kv_get("offset") == last + 1
    b2 = telegram.Bot(transport=fake.transport(), sleep=lambda s: None)  # a restart continues from the stored offset
    b2.poll_once(timeout=0)
    assert fake.of("getUpdates")[-1]["offset"] == last + 1
    fake.fail["getUpdates"] = [(409, {"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates"})]
    assert b2.poll_once(timeout=0) == -1 and st.poller_owner() is None  # released, backs off


def test_worker_mode_never_polls(tg):
    from clipforge.config import load_config, save_config

    b, fake, _ = tg
    save_config(load_config().with_overrides({"deployment.mode": "worker"}))
    b.start()
    assert b.thread is None


def test_mini_app_init_data():
    from clipforge.notify.webapp import InitDataError, sign, validate_init_data

    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": 42, "first_name": "H"}), "query_id": "AAA"}
    from urllib.parse import urlencode

    good = urlencode({**fields, "hash": sign(fields, TOKEN)})
    assert validate_init_data(good, TOKEN)["user"]["id"] == 42
    with pytest.raises(InitDataError):
        validate_init_data(good, "999:other-token")
    with pytest.raises(InitDataError):
        validate_init_data(good.replace("H%22", "X%22"), TOKEN)
    old = {**fields, "auth_date": str(int(time.time()) - 90000)}
    with pytest.raises(InitDataError):
        validate_init_data(urlencode({**old, "hash": sign(old, TOKEN)}), TOKEN)


# ============================================================================ end to end
def test_e2e_link_wizard_progress_clips_approve_schedule_queue(tg, monkeypatch):
    from clipforge.api import db
    from clipforge.notify import telegram, tg_jobs
    from clipforge.publish import queue as q

    b, fake, tmp = tg
    with db.session() as s:
        s.add(db.Channel(id="UCyt", title="Figuring Clips", uploads_playlist="UUyt", confirmed=True))
        s.add(db.Channel(id="ig:1", title="figuring.reels", platform="instagram", confirmed=True,
                         extra_json=json.dumps({"username": "figuring.reels", "manual": True})))
        s.commit()
    # 1. paste a link → options wizard → change clips to 15 → start
    send(b, fake, "look https://youtu.be/vidA?si=xyz")
    assert "New job" in fake.texts()[-2] and "Skip" in fake.last_text()
    press(b, fake, "15")
    assert "Clips: <b>15</b>" in fake.last_text()
    press(b, fake, "▶ Start")
    kind, job_id, _ = _submitted[-1]
    job = db.get_job(job_id)
    assert kind == "pipeline" and json.loads(job.settings_json)["clips"] == 15
    # 2. live progress: edited in place, throttled, only on change
    db.update_job(job_id, status="running", stage="transcribe", progress=0.31, eta_sec=600)
    b.tick()
    edits = fake.of("editMessageText")
    assert "Transcribing" in edits[-1]["text"] and "31%" in edits[-1]["text"]
    n = len(edits)
    b.tick()  # same text → no edit
    db.update_job(job_id, progress=0.5)
    b.tick()  # changed but < 5 s → no edit
    assert len(fake.of("editMessageText")) == n
    rec = tg_jobs.st.kv_get(f"prog:{job_id}")
    rec["at"] = 0
    tg_jobs.st.kv_set(f"prog:{job_id}", rec)
    b.tick()
    assert "50%" in fake.of("editMessageText")[-1]["text"]
    # 3. the job finishes (simulated) → summary, album, one card per top clip
    _, ws = seed_job(tmp, n_clips=4)
    with db.session() as s:
        j = s.get(db.Job, job_id)
        j.video_id, j.status, j.n_clips = "vidA", "done", 4
        s.add(j)
        s.commit()
    telegram.on_job_status(job_id, "done")
    assert "4 clips ready" in " ".join(fake.texts()) and fake.of("sendMediaGroup")
    b.tick()
    assert "done" in fake.of("editMessageText")[-1]["text"]
    # 4. approve 3 clips
    send(b, fake, f"/approve {job_id} 1-3")
    assert "Approved 3 clip(s)" in fake.last_text()
    # 5. schedule both platforms
    press(b, fake, "📅 Schedule approved")
    press(b, fake, "Both")
    assert "Scheduled" in fake.last_text()
    items = q.items()
    assert len(items) == 6 and {i.platform for i in items} == {"youtube", "instagram"}
    assert all(i.status == "queued" and i.slot_utc for i in items) and all(i.privacy == "scheduled" for i in items if i.platform == "youtube")
    # 6. /queue shows them, /needsyou is empty
    send(b, fake, "/queue")
    assert "6 posts" in fake.last_text() and "▶️" in fake.last_text() and "📸" in fake.last_text()
    send(b, fake, "/needsyou")
    # no clip needs anything; the only human action is the test channel's missing Google login (auto mode, v2)
    t = fake.last_text()
    assert "Nothing needs you" in t or ("<b>Accounts</b>" in t and "no saved Google login" in t)
    # every button sent in this whole chat fits Telegram's 64-byte callback_data
    assert all(len(d.encode()) <= 64 for _, d, _ in fake.buttons(len(fake.calls)))
    import os

    out = os.environ.get("CLIPFORGE_DOC_TRANSCRIPT")  # docs/TELEGRAM.md example chat is rendered from this test
    if out:
        from pathlib import Path

        Path(out).write_text(fake.transcript(), encoding="utf-8")


def test_running_bot_stops_polling_when_the_laptop_becomes_a_worker(tg):
    """Handover while `clipforge ui` runs: the running bot must stop polling by itself (no restart needed)."""
    import threading

    from clipforge.config import load_config, save_config
    from clipforge.notify import tg_store as st

    b, fake, _ = tg
    b.poll_once(timeout=0)
    assert st.poller_owner() is not None
    save_config(load_config().with_overrides({"deployment.mode": "worker"}))
    b.polling = True
    b.stop.clear()
    calls = len(fake.of("getUpdates"))
    t = threading.Thread(target=b._loop, daemon=True)
    t.start()
    import time

    for _ in range(50):
        if not b.polling:
            break
        time.sleep(0.05)
    b.stop.set()
    t.join(2)
    assert b.polling is False and st.poller_owner() is None and len(fake.of("getUpdates")) == calls


# ============================================================================ copy-ready metadata after every clip
def test_metadata_follows_each_clip_as_reply(tg):
    b, fake, tmp = tg
    job, _ = seed_job(tmp, n_clips=2)
    fake.clear()
    send(b, fake, f"/send {job.id} 1 preview")
    calls = [(m, p) for m, p in fake.calls if m in ("sendVideo", "sendMessage")]
    assert [m for m, _ in calls] == ["sendVideo", "sendMessage"]
    meta = calls[1][1]
    assert meta["parse_mode"] == "HTML" and meta["reply_parameters"]["message_id"] > 0
    t = meta["text"]
    for label in ("YouTube title", "YouTube description", "Instagram caption", "Hashtags"):
        assert f"<b>{label}</b>\n<code>" in t
    assert "<code>#money #biz</code>" in t and "Tags" not in t  # lowercase, no queue tags yet
    assert str(tmp) not in t and TOKEN not in t and ".mp4" not in t
    fake.clear()
    send(b, fake, f"/clip {job.id}-2")  # cards get it too
    assert "<b>YouTube title</b>" in fake.last_text() and fake.of("sendMessage")[-1]["reply_parameters"]


def test_metadata_splits_at_section_boundaries(tg):
    from clipforge.notify import tg_clips

    b, fake, tmp = tg
    job, ws = seed_job(tmp, n_clips=1)
    rows = json.loads(ws.metadata_json.read_text(encoding="utf-8"))
    rows[0]["yt_description"] = "d" * 3000
    rows[0]["ig_caption"] = "c" * 3000
    ws.metadata_json.write_text(json.dumps(rows), encoding="utf-8")
    msgs = tg_clips.meta_messages(f"{job.id}-1")
    assert len(msgs) >= 2 and all(len(m) <= 4096 for m in msgs)
    assert all(m.count("<code>") == m.count("</code>") for m in msgs)  # never cut inside a block
    assert "YouTube description" in msgs[0] and "Instagram caption" in msgs[1]


def test_metadata_not_ready_then_meta_generates(tg):
    b, fake, tmp = tg
    job, ws = seed_job(tmp, n_clips=1)
    ws.metadata_json.unlink()
    fake.clear()
    send(b, fake, f"/send {job.id} 1 preview")
    assert fake.last_text() == f"📋 Metadata not ready — /meta {job.id}-1 to generate"
    fake.clear()
    send(b, fake, f"/meta {job.id}-1")  # runs the normal metadata step (mock LLM)
    assert ws.metadata_json.exists() and "<b>YouTube title</b>" in fake.last_text()
    from clipforge.services import clips as clip_svc

    clip_svc.set_approved(job.id, [1], True)
    fake.clear()
    send(b, fake, f"/meta {job.id}")  # all approved clips of the job
    assert "<b>Instagram caption</b>" in fake.last_text() and not fake.of("sendVideo")


# ============================================================================ transcript after the link (skips full Whisper)
def test_link_then_pasted_transcript_then_start(tg):
    from clipforge.api import db
    from clipforge.notify import tg_store as st

    b, fake, tmp = tg
    send(b, fake, "/new https://youtu.be/vidT")
    assert "Skip" in fake.last_text() and st.get_conversation(OWNER)[0] == "wiz_transcript"
    send(b, fake, "0:00\naaj hum baat karenge\n0:05 paisa kaise banta hai")  # two messages, like a long paste
    send(b, fake, "1:02 aakhri line")
    assert "Got 3 timed lines" in fake.last_text()
    press(b, fake, "✅ Use it")
    assert "Transcript added: 3 lines" in fake.last_text() and st.get_conversation(OWNER) is None
    assert any("Transcript: <b>pasted (3 lines)</b>" in t for t in fake.texts())
    press(b, fake, "▶ Start with my defaults")
    _, job_id, _ = _submitted[-1]
    job = db.get_job(job_id)
    saved = json.loads((tmp / "ws" / job.video_id / "transcript_input.json").read_text(encoding="utf-8"))
    assert "paisa kaise banta hai" in saved["text"]


def test_transcript_errors_show_line_numbers_and_srt_file_works(tg):
    from clipforge.notify import tg_store as st

    b, fake, tmp = tg
    send(b, fake, "https://youtu.be/vidS")
    send(b, fake, "0:10 a\n0:20 b\n0:05 c")
    press(b, fake, "✅ Use it")
    assert "line 3" in fake.last_text() and st.get_conversation(OWNER)[0] == "wiz_transcript"
    fake.files["docs/T1.srt"] = b"1\n00:00:01,000 --> 00:00:03,000\nhello\n\n2\n00:00:04,000 --> 00:00:06,000\nworld\n"
    fake.files["videos/T1.mp4"] = fake.files["docs/T1.srt"]  # the fake's getFile answers videos/<id>.mp4
    send(b, fake, "", document={"file_id": "T1", "file_name": "ep.srt", "file_size": 80, "mime_type": "application/x-subrip"})
    assert "Transcript added: 2 lines (srt" in fake.last_text()
    send(b, fake, "https://youtu.be/vidS2", )
    press(b, fake, "⏭ Skip")
    assert st.get_conversation(OWNER) is None and "No transcript" in fake.last_text()


def test_transcript_warning_offers_whisper(tg, monkeypatch):
    from clipforge.api import db
    from clipforge.notify import telegram

    b, fake, tmp = tg
    job, ws = seed_job(tmp)
    ws.write_json(ws.dir / "transcript_report.json", {"source": "pasted", "label": "pasted", "warnings": ["The transcript ends at 4:00 but the video is 10:00 long"], "saved_sec": 3000})
    fake.clear()
    telegram.on_transcript_warning(job.id, ["The transcript ends at 4:00 but the video is 10:00 long"])
    assert "may not fit" in fake.last_text() and "4:00" in fake.last_text()
    press(b, fake, "🎙 Use Whisper instead")
    assert json.loads(db.get_job(job.id).settings_json)["transcript_source"] == "whisper"
    assert _submitted[-1][:2] == ("pipeline", job.id) and "Whisper instead" in fake.last_text()
    from clipforge.notify import tg_jobs

    assert "Transcript: pasted · ~50 min saved · ⚠️" in tg_jobs.transcript_line(db.get_job(job.id))
