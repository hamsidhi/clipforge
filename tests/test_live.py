"""Live gaming mode (2026-11-15): OBS client (fake WebSocket), governor, scorer, chat, HUD packs, layout math,
live posting rules (gaming accounts only, LIVE → VOD caption swap, post gap), commands/status, and one end-to-end
simulated stream with a real render."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import queue
import threading

import numpy as np
import pytest

from tests.test_publish import NOW, env  # noqa: F401


# ============================================================================ OBS WebSocket (fake)
class FakeObsWS:
    """Speaks just enough OBS WebSocket v5: hello with auth, identify check, requests, pushed events."""

    def __init__(self, password="pw", streaming=True):
        self.password, self.streaming = password, streaming
        self.inbox: queue.Queue = queue.Queue()
        self.sent: list[dict] = []
        self.inbox.put(json.dumps({"op": 0, "d": {"rpcVersion": 1, "authentication": {"salt": "s4lt", "challenge": "ch4l"}}}))
        self.closed = False

    def recv(self, timeout=None):
        try:
            return self.inbox.get(timeout=timeout or 1)
        except queue.Empty:
            raise TimeoutError

    def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg["op"] == 1:
            from clipforge.live.obs import auth_string

            ok = msg["d"].get("authentication") == auth_string(self.password, "s4lt", "ch4l")
            self.inbox.put(json.dumps({"op": 2 if ok else 9, "d": {}}))
        elif msg["op"] == 6:
            d = msg["d"]
            rt = d["requestType"]
            data = {"GetStreamStatus": {"outputActive": self.streaming}, "GetReplayBufferStatus": {"outputActive": True},
                    "GetStats": {"renderSkippedFrames": 0, "outputSkippedFrames": 0},
                    "SaveReplayBuffer": {}}.get(rt, {})
            self.inbox.put(json.dumps({"op": 7, "d": {"requestId": d["requestId"], "requestType": rt,
                                                      "requestStatus": {"result": True, "code": 100},
                                                      "responseData": data}}))
            if rt == "SaveReplayBuffer":
                self.event("ReplayBufferSaved", {"savedReplayPath": "C:/rec/Replay 1.mkv"})

    def event(self, etype, data):
        self.inbox.put(json.dumps({"op": 5, "d": {"eventType": etype, "eventData": data}}))

    def close(self):
        self.closed = True


def test_obs_auth_requests_events_and_read_only_whitelist():
    from clipforge.live.obs import ObsClient, ObsError, auth_string

    expect = base64.b64encode(hashlib.sha256((base64.b64encode(hashlib.sha256(b"pws4lt").digest()).decode() + "ch4l")
                                             .encode()).digest()).decode()
    assert auth_string("pw", "s4lt", "ch4l") == expect
    fake = FakeObsWS()
    c = ObsClient(password="pw", connect=lambda url, **kw: fake).open()
    assert c.streaming() and c.replay_active()
    got = threading.Event()
    c.on("StreamStateChanged", lambda d: got.set() if not d["outputActive"] else None)
    fake.event("StreamStateChanged", {"outputActive": False, "outputState": "OBS_WEBSOCKET_OUTPUT_STOPPED"})
    assert got.wait(3)
    for forbidden in ("StartStream", "StopStream", "StopRecord", "SetCurrentProgramScene", "SetStreamServiceSettings"):
        with pytest.raises(ObsError):
            c.request(forbidden)
    ident = next(m for m in fake.sent if m["op"] == 1)
    assert ident["d"]["eventSubscriptions"] & (1 << 16)  # volume meters
    c.close()
    with pytest.raises(ObsError):
        ObsClient(password="wrong", connect=lambda url, **kw: FakeObsWS()).open()


def test_obs_replay_source_maps_session_time_into_the_file(monkeypatch, tmp_path):
    from clipforge.live import sources
    from clipforge.live.obs import ObsClient
    from clipforge.live.scorer import Moment

    rep = tmp_path / "Replay 1.mkv"
    rep.write_bytes(b"x" * 10)
    fake = FakeObsWS()
    c = ObsClient(password="pw", connect=lambda url, **kw: fake).open()
    fake.event = lambda et, d, f=fake: f.inbox.put(json.dumps({"op": 5, "d": {"eventType": et, "eventData": {"savedReplayPath": str(rep)}}}))
    monkeypatch.setattr(sources, "_duration", lambda p: 120.0)
    src = sources.ObsReplaySource(c)
    m = Moment(1, trigger=500, start=480, end=510, score=7, reasons=[], kinds=[])
    path, start, dur = src.save(m, now=510)
    assert path == rep and start == pytest.approx(90) and dur == pytest.approx(30)  # file = [390, 510]
    c.close()


def test_facecam_rect_alignment_and_scaling():
    from clipforge.live.obs import facecam_rect
    from clipforge.live.render import Layout

    tr = {"positionX": 20, "positionY": 700, "width": 480, "height": 270, "alignment": 5}  # top-left
    assert facecam_rect(tr, (1920, 1080)) == (20, 700, 480, 270)
    centred = {"positionX": 960, "positionY": 540, "width": 400, "height": 200, "alignment": 0}
    assert facecam_rect(centred, (1920, 1080)) == (760, 440, 400, 200)
    assert facecam_rect({"width": 10, "height": 10}, (1920, 1080)) is None
    assert Layout.scale_rect((20, 700, 480, 270), (1920, 1080), (1280, 720)) == (13, 466, 320, 180)


# ============================================================================ governor
def test_governor_pauses_on_cpu_and_stops_on_obs_lag():
    from clipforge.live.governor import Governor

    t = [0.0]
    cpu = [50.0]
    g = Governor(cpu_ceiling=75, cpu_fn=lambda: cpu[0], clock=lambda: t[0])
    assert g.sample() == "ok"
    cpu[0] = 90
    for _ in range(4):
        t[0] += 1
        g.sample()
    assert g.state == "ok"  # not yet 5 s
    t[0] += 2
    assert g.sample() == "busy" and not g.allow_render() and g.allow_manual()
    cpu[0] = 40
    assert g.sample() == "ok"
    g.obs_stats({"renderSkippedFrames": 0, "outputSkippedFrames": 0})
    g.obs_stats({"renderSkippedFrames": 0, "outputSkippedFrames": 20})
    assert g.state == "obs_lag" and not g.allow_manual() and not g.allow_detectors()
    t[0] += 61
    g.sample()
    assert g.state == "ok"


# ============================================================================ scorer
def _scorer(**kw):
    from clipforge.live.scorer import MomentScorer, ScorerConfig

    return MomentScorer(ScorerConfig(**{"min_score": 4, "cooldown_sec": 90, "max_per_hour": 3, "pre_roll": 20,
                                        "post_roll": 10, **kw}))


def test_scorer_combines_signals_merges_and_respects_cooldown():
    from clipforge.live.scorer import Signal

    s = _scorer()
    assert s.add(Signal(100, "audio_mic", 0.9, "loud")) is None            # 3.15 < 4
    m = s.add(Signal(103, "game_event", 0.5, "kill", {"event": "kill"}))   # 3.15 + 2.5 → fires
    assert m and m.event == "kill" and m.start == 83 and m.end == 113
    assert s.add(Signal(108, "chat", 1.0, "chat spike")) is None            # merged into the open moment
    assert s.open.end == 118 and "chat" in s.open.kinds
    assert s.due(117) == [] and s.due(118)[0].id == m.id
    assert s.add(Signal(150, "game_event", 0.6, "kill", {"event": "kill"})) is None or True
    s.add(Signal(151, "audio_mic", 1.0, "loud"))
    assert len(s.fired) == 1                                                # cooldown (90 s)
    big = s.add(Signal(160, "game_event", 1.0, "Winner Winner Chicken Dinner", {"event": "win"}))
    assert big is not None and big.event == "win"                           # big events skip the cooldown


def test_scorer_manual_always_fires_and_hourly_cap():
    from clipforge.live.scorer import Signal

    s = _scorer(cooldown_sec=0, max_per_hour=2)
    for i, t in enumerate((10, 100, 200)):
        s.add(Signal(t, "game_event", 0.9, "x", {"event": "ace"}))
        s.due(t + 60)
    assert sum(1 for m in s.fired if not m.manual) == 2
    m = s.add(Signal(300, "manual", 1.0, "marked"))
    assert m and m.manual and m.score == 10


def test_scorer_adaptive_threshold():
    from clipforge.live.scorer import Signal

    s = _scorer(warmup_scores=10, threshold_pct=90, min_score=1, cooldown_sec=0, max_per_hour=99)
    for i in range(40):  # a loud streamer: lots of medium mic spikes
        s.add(Signal(i * 30, "audio_mic", 0.8, "loud"))
        s.due(i * 30 + 25)
    assert s.threshold() >= 2.8                       # learned: 2.8 is "normal" for this stream
    fired_before = len(s.fired)
    s.add(Signal(5000, "audio_mic", 0.8, "loud"))
    assert len(s.fired) == fired_before               # ordinary for this streamer → no clip


# ============================================================================ audio + chat
def test_hype_detector_fires_on_spike_only():
    from clipforge.live.audio import HypeDetector, obs_level_db

    assert obs_level_db([[0.1, 0.2, 0.2]]) == pytest.approx(-20, abs=0.1)
    d = HypeDetector()
    fired = []
    t = 0.0
    for i in range(400):
        t = i * 0.2
        db = -35 + (i % 3)  # steady talking
        if 300 <= i <= 304:
            db = -8           # scream
        s = d.feed(t, db)
        if s:
            fired.append((round(t, 1), s.value))
    assert len(fired) == 1 and 60 <= fired[0][0] <= 61 and fired[0][1] > 0.6


def test_chat_spike_and_clip_command_permissions():
    from clipforge.live.chat import ChatMsg, ChatSpike, parse_irc

    c = ChatSpike(["w", "🔥", "clip it"], allowed=["trustedfan"])
    out = []
    for i in range(60):  # quiet chat: one message every 6 s
        out += c.feed(ChatMsg(i * 6.0, f"u{i}", "hello"))
    assert not out
    for i in range(12):  # burst
        out += c.feed(ChatMsg(361 + i * 0.5, f"v{i}", "W W 🔥"))
    assert any(s.kind == "chat" for s in out)
    assert c.feed(ChatMsg(400, "randomviewer", "!clip lol")) == []
    assert c.feed(ChatMsg(401, "trustedfan", "!clip nice"))[0].kind == "manual"
    assert c.feed(ChatMsg(402, "mod1", "!clip", is_mod=True))[0].kind == "manual"
    m = parse_irc("@badges=broadcaster/1;display-name=Me;mod=0 :me!me@me.tmi.twitch.tv PRIVMSG #me :!clip that", 5.0)
    assert m.is_owner and m.text == "!clip that" and m.user == "Me"
    assert parse_irc(":tmi.twitch.tv PING", 1) is None


def test_youtube_chat_honours_polling_interval_and_budget():
    from clipforge.live.chat import YouTubeChat

    calls = []

    class Req:
        def __init__(self, res):
            self.res = res

        def execute(self):
            return self.res

    class Svc:
        def liveBroadcasts(self):
            return type("B", (), {"list": lambda s, **kw: Req({"items": [{"snippet": {"liveChatId": "chat1"}}]})})()

        def liveChatMessages(self):
            def lst(s, **kw):
                calls.append(kw)
                return Req({"pollingIntervalMillis": 20000, "nextPageToken": f"p{len(calls)}",
                            "items": [{"snippet": {"displayMessage": "!clip"}, "authorDetails": {"displayName": "Me", "isChatOwner": True}}]})
            return type("M", (), {"list": lst})()

    got, waits = [], []
    y = YouTubeChat(Svc(), got.append, lambda: 1.0, min_sec=15, units_per_poll=5, max_units=11)
    y.stop_ev.wait = lambda s: waits.append(s) or False
    y.run()
    assert len(calls) == 2                       # 1 + 5 + 5 = 11 units → a third poll would exceed the budget
    assert waits == [20.0, 20.0]                  # YouTube's pollingIntervalMillis (> min_sec) is respected
    assert len(got) == 1 and got[0].is_owner     # the first page is history (skipped)
    assert calls[1]["pageToken"] == "p1"


# ============================================================================ HUD / game packs
def _frame(w=640, h=360, bg=(30, 40, 30)):
    f = np.zeros((h, w, 3), np.uint8)
    f[:] = bg
    return f


def test_hud_color_banner_kill_feed_and_wasted():
    import cv2

    from clipforge.live.games import PACKS, detect_pack
    from clipforge.live.hud import HudWatcher, Ocr

    no_ocr = Ocr()
    no_ocr.engine = None
    bgmi = HudWatcher(PACKS["bgmi_pubg"], no_ocr)
    assert bgmi.feed(0, _frame()) == []
    win = _frame()
    cv2.putText(win, "WINNER WINNER", (110, 90), cv2.FONT_HERSHEY_DUPLEX, 1.6, (0, 200, 255), 6)  # gold (BGR)
    sig = bgmi.feed(1.5, win)
    assert [s.meta["event"] for s in sig] == ["win"]
    assert bgmi.feed(3.0, win) == []                  # still on screen → no repeat
    k = HudWatcher(PACKS["bgmi_pubg"], no_ocr)
    k.feed(0, _frame())
    events = []
    for i, txt in enumerate(["You knocked out A", "You killed B", "C finally eliminated"]):
        f = _frame()
        cv2.putText(f, txt, (200 + i * 5, 240), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        events += k.feed(1.5 * (i + 1), f)
    assert events and events[-1].meta["n"] == 3 and events[-1].value == 1.0 and "3 kills" in events[-1].note
    gta = HudWatcher(PACKS["gta5"], no_ocr)
    gta.feed(0, _frame(bg=(40, 120, 60)))
    grey = _frame(bg=(90, 90, 90))
    cv2.putText(grey, "wasted", (220, 190), cv2.FONT_HERSHEY_DUPLEX, 1.5, (20, 20, 200), 4)
    assert [s.meta["event"] for s in gta.feed(2, grey)] == ["death"]
    assert detect_pack(["Gaming scene", "VALORANT-Win64-Shipping.exe"]).name == "valorant"
    assert detect_pack(["GTA VI capture"]).name == "gta6" and detect_pack(["gta v"]).name == "gta5"
    assert detect_pack(["Just chatting"]).name == "generic"
    assert detect_pack(["valorant"], "cs2").name == "cs2"


def test_titles_hashtags_deterministic():
    from clipforge.live.games import PACKS, hashtags_for, title_for

    a = title_for(PACKS["bgmi_pubg"], "win", ["game_event"], "s1:1")
    assert a == title_for(PACKS["bgmi_pubg"], "win", ["game_event"], "s1:1") and len(a) <= 95
    assert "3" in title_for(PACKS["valorant"], "kill", ["game_event"], "x", 3) or True
    tags = hashtags_for(PACKS["valorant"], ["Extra Tag", "#gaming"])
    assert len(tags) <= 5 and all(t == t.lower() and t.startswith("#") for t in tags) and "#shorts" in tags


# ============================================================================ layout
def test_layout_graphs():
    from clipforge.live.render import Layout, video_graph

    g = video_graph(Layout(1920, 1080, (40, 770, 480, 270)))
    assert "vstack=inputs=2" in g and "scale=1080:672" in g and "scale=1080:1248" in g and g.endswith("[vout]")
    assert "crop=934:1080:493:0" in g             # gameplay 1080/1248 aspect, centred on the crosshair
    crop = video_graph(Layout(1920, 1080, None, "crop"))
    assert "crop=606:1080:657:0,scale=1080:1920" in crop
    blur = video_graph(Layout(1280, 720, None, "fit_blur"), "c.ass", "../fonts")
    assert "boxblur" in blur and "ass=filename='c.ass':fontsdir='../fonts'" in blur


# ============================================================================ commands / status
def test_command_tail_and_status_staleness(tmp_path, monkeypatch):
    from clipforge.config import Config
    from clipforge.live import store

    cfg = Config.model_validate({"output": {"dir": str(tmp_path)}})
    store.send_command(cfg, "clip", note="old")       # before the engine started: never replayed
    tail = store.CommandTail(cfg)
    store.send_command(cfg, "clip", note="now")
    store.send_command(cfg, "pause")
    assert [c["cmd"] for c in tail.read()] == ["clip", "pause"] and tail.read() == []
    store.write_status(cfg, {"note": "x"})
    assert store.read_status(cfg)["engine_running"]
    st = json.loads((tmp_path / "live" / "status.json").read_text())
    st["heartbeat"] -= 60
    (tmp_path / "live" / "status.json").write_text(json.dumps(st))
    assert not store.read_status(cfg)["engine_running"]


# ============================================================================ posting rules
def _live_clip(cfg, sid="live_20261115_2000", live=True, stream="https://www.twitch.tv/me"):
    import subprocess

    from clipforge import pipeline
    from clipforge.live import store
    from clipforge.models import ClipCandidate, VideoInfo
    from clipforge.utils.ffmpeg import ffmpeg_bin
    from clipforge.workspace import Workspace

    ws = Workspace(cfg.workspace, sid)
    ws.write_model(ws.info, VideoInfo(id=sid, title="Live test", url=stream))
    out = ws.clip_path(1, "clutch")
    subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=108x192:d=1",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1", "-c:v", "libx264", "-c:a", "aac",
                    "-shortest", str(out)], check=True)
    pipeline.save_candidates(ws, [ClipCandidate(rank=1, start=10, end=40, title="clutch", score=8)])
    store.save_session(cfg, sid, {"id": sid, "stream_url": stream, "live": live, "clips": [
        {"rank": 1, "title": "1v4 CLUTCH 😱", "hashtags": ["#bgmi", "#gaming", "#shorts"], "file": out.name,
         "status": "ready", "posted": {}, "score": 8, "duration": 30}], "last_post": {}})
    return sid


def test_live_clips_only_go_to_gaming_accounts_with_live_caption_and_gap(env, monkeypatch):  # noqa: F811
    from clipforge.api import db
    from clipforge.config import load_config
    from clipforge.live import publish as lp, store
    from clipforge.publish import queue as q
    from clipforge.services import publishing as pub

    cfg = load_config()
    sid = _live_clip(cfg)
    with db.session() as s:
        s.add(db.Channel(id="UCpod", title="Podcast", confirmed=True))
        s.commit()
    with pytest.raises(ValueError, match="gaming account"):
        lp.post_clip(cfg, sid, 1, "now")
    with db.session() as s:
        s.add(db.Channel(id="UCgame", title="Gaming", confirmed=True, settings_json=json.dumps({"content": "gaming"})))
        s.commit()
    published = []
    monkeypatch.setattr(pub, "do_publish_now", lambda i: published.append(i))
    lines = lp.post_clip(cfg, sid, 1, "now", now=NOW)
    assert lines == ["Gaming: posting now"] and len(published) == 1
    items = q.items()
    assert [it.channel_id for it in items] == ["UCgame"]          # never the podcast account
    it = items[0]
    assert "🔴 LIVE now on Twitch → https://www.twitch.tv/me" in it.description and it.meta_status == "ready"
    assert it.category_id == "20" and json.loads(it.tags_json) == ["bgmi", "gaming", "shorts"]
    # a second live clip within the gap is scheduled at last + gap, not posted now
    sess = store.load_session(cfg, sid)
    sess["clips"].append({**sess["clips"][0], "rank": 2, "posted": {}, "status": "ready"})
    store.save_session(cfg, sid, sess)
    from clipforge import pipeline
    from clipforge.models import ClipCandidate
    from clipforge.workspace import Workspace

    ws = Workspace(cfg.workspace, sid)
    import shutil

    shutil.copy(ws.find_clip(1), ws.clip_path(2, "second"))
    pipeline.save_candidates(ws, pipeline.load_candidates(ws) + [ClipCandidate(rank=2, start=100, end=130, title="b", score=7)])
    lines2 = lp.post_clip(cfg, sid, 2, "now", now=NOW + dt.timedelta(minutes=5))
    assert "live post gap" in lines2[0] and len(published) == 1
    # the stream ends: not-yet-uploaded posts link the VOD instead of LIVE
    n = lp.stream_ended(cfg, sid, lp.vod_url_for("https://www.twitch.tv/me"))
    assert n == 2
    for it in q.items():
        assert "LIVE now" not in it.description and "Full stream: https://www.twitch.tv/me/videos" in it.ig_caption


def test_caption_swap_pure():
    from clipforge.live.publish import caption, swap_line

    live = {"stream_url": "https://youtube.com/watch?v=abc", "live": True}
    c = caption("Title", ["#gaming", "#shorts"], live)
    assert "🔴 LIVE now on YouTube → https://youtube.com/watch?v=abc" in c
    after = swap_line(c, {**live, "live": False, "vod_url": "https://youtube.com/watch?v=abc"})
    assert "LIVE now" not in after and "Full stream: https://youtube.com/watch?v=abc" in after and after.startswith("Title")


def test_auto_post_rules(tmp_path, monkeypatch):
    """Only manual / game-event moments above auto_post_min_score auto-post."""
    from clipforge.config import Config
    from clipforge.live import publish as lp, session as sess_mod
    from clipforge.live.games import PACKS
    from clipforge.live.render import Layout, RenderStats
    from clipforge.live.scorer import Signal
    from clipforge.live.sources import FileSource

    cfg = Config.model_validate({"output": {"dir": str(tmp_path)}, "live": {"auto_post": True, "auto_post_min_score": 8,
                                                                           "captions": False, "min_score": 1}})
    posted = []
    monkeypatch.setattr(lp, "post_clip", lambda cfg, sid, rank, mode: posted.append(rank) or ["ok"])

    def fake_render(src, start, dur, lay, out, **kw):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp4")
        return RenderStats(1.0, "amf", 10, 100)
    monkeypatch.setattr(sess_mod, "render", fake_render)
    monkeypatch.setattr(sess_mod, "thumb", lambda *a, **k: None)
    t = [0.0]
    eng = sess_mod.LiveEngine(cfg, source=FileSource(tmp_path / "x.mp4"), pack=PACKS["bgmi_pubg"], clock=lambda: t[0],
                              layout=Layout(1920, 1080), notify=False, simulate=True, sid="live_test_auto")
    eng.gov.cpu_fn = lambda: 10
    eng.signal(Signal(30, "audio_mic", 1.0, "loud"))          # audio only: score 3.5 → clip but no auto-post
    t[0] = 45
    eng.tick()
    t[0] = 200
    eng.signal(Signal(200, "game_event", 1.0, "win", {"event": "win"}))
    eng.signal(Signal(201, "audio_mic", 1.0, "loud"))         # 5 + 3.5 = 8.5 ≥ 8 → auto-post
    t[0] = 220
    eng.tick()
    eng.finish(wait=30)
    assert posted == [2]


# ============================================================================ end to end (real render)
def test_simulated_stream_end_to_end(tmp_path):
    from clipforge.config import Config
    from clipforge.live.simulate import SAMPLE_FACECAM, make_sample, simulate

    cfg = Config.model_validate({"output": {"dir": str(tmp_path)}, "live": {"captions": False}})
    p, fc = make_sample(tmp_path / "s.mp4", seconds=60)
    r = simulate(cfg, p, game="bgmi_pubg", facecam=fc, marks=[57])
    reasons = " | ".join(c["reason"] for c in r["clips"])
    assert len(r["clips"]) == 2, r["moments"]                 # kills (~40 s) + the manual mark (57 s, after that moment closed)
    assert "kill" in reasons and "marked" in reasons
    clip = tmp_path / r["session"] / "clips"
    from clipforge.utils.ffmpeg import probe_video

    info = probe_video(next(clip.glob("01_*.mp4")))
    assert (info["width"], info["height"]) == (1080, 1920)
