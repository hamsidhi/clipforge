"""Live clipping (2026-11-15): paste another streamer's live link → highlight clips while it's live.
Covers: governor, scorer (incl. time re-ordering of late signals), audio/chat detectors (Twitch, Kick, YouTube chat
budget), HUD packs, facecam detection, layout math, stream helpers, commands/status, posting rules (live-clip
accounts only, streamer credit, LIVE → VOD swap, post gap, auto-post), and an end-to-end simulated stream."""

from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pytest

from tests.test_publish import NOW, env  # noqa: F401


# ============================================================================ governor
def test_governor_waits_when_the_laptop_is_busy():
    from clipforge.live.governor import Governor

    t = [0.0]
    cpu = [50.0]
    g = Governor(cpu_ceiling=85, cpu_fn=lambda: cpu[0], clock=lambda: t[0])
    assert g.sample() == "ok"
    cpu[0] = 95
    for _ in range(4):
        t[0] += 1
        g.sample()
    assert g.state == "ok"  # not yet 5 s
    t[0] += 2
    assert g.sample() == "busy" and not g.allow_render() and not g.allow_detectors()
    cpu[0] = 40
    assert g.sample() == "ok" and g.allow_render()


# ============================================================================ scorer
def _scorer(**kw):
    from clipforge.live.scorer import MomentScorer, ScorerConfig

    return MomentScorer(ScorerConfig(**{"min_score": 4, "cooldown_sec": 90, "max_per_hour": 3, "pre_roll": 20,
                                        "post_roll": 10, "weights": {"manual": 10, "game_event": 5, "audio": 3.5,
                                                                     "chat": 3.5}, **kw}))


def test_scorer_combines_signals_merges_and_respects_cooldown():
    from clipforge.live.scorer import Signal

    s = _scorer()
    assert s.add(Signal(100, "audio", 0.9, "loud")) is None              # 3.15 < 4
    m = s.add(Signal(103, "game_event", 0.5, "kill", {"event": "kill"}))  # 3.15 + 2.5 → fires
    assert m and m.event == "kill" and m.start == 83 and m.end == 113
    assert s.add(Signal(108, "chat", 1.0, "chat spike")) is None           # merged into the open moment
    assert s.open.end == 118 and "chat" in s.open.kinds
    assert s.due(117) == [] and s.due(118)[0].id == m.id
    s.add(Signal(150, "game_event", 0.6, "kill", {"event": "kill"}))
    s.add(Signal(151, "audio", 1.0, "loud"))
    assert len(s.fired) == 1                                               # cooldown (90 s)
    big = s.add(Signal(160, "game_event", 1.0, "Winner Winner Chicken Dinner", {"event": "win"}))
    assert big is not None and big.event == "win"                          # big events skip the cooldown


def test_scorer_manual_always_fires_and_hourly_cap():
    from clipforge.live.scorer import Signal

    s = _scorer(cooldown_sec=0, max_per_hour=2)
    for t in (10, 100, 200):
        s.add(Signal(t, "game_event", 0.9, "x", {"event": "ace"}))
        s.due(t + 60)
    assert sum(1 for m in s.fired if not m.manual) == 2
    m = s.add(Signal(300, "manual", 1.0, "marked"))
    assert m and m.manual and m.score == 10


def test_scorer_adaptive_threshold():
    from clipforge.live.scorer import Signal

    s = _scorer(warmup_scores=10, threshold_pct=90, min_score=1, cooldown_sec=0, max_per_hour=99)
    for i in range(40):  # a loud streamer: lots of medium spikes
        s.add(Signal(i * 30, "audio", 0.8, "loud"))
        s.due(i * 30 + 25)
    assert s.threshold() >= 2.8
    before = len(s.fired)
    s.add(Signal(5000, "audio", 0.8, "loud"))
    assert len(s.fired) == before                    # ordinary for this streamer → no clip


def test_engine_reorders_late_signals_by_time(tmp_path, monkeypatch):
    """Chat arrives instantly, audio comes 10-20 s late from closed segments: with `lag` they still combine."""
    from clipforge.config import Config
    from clipforge.live import session as sess_mod
    from clipforge.live.games import PACKS
    from clipforge.live.render import Layout
    from clipforge.live.scorer import Signal
    from clipforge.live.sources import FileSource

    cfg = Config.model_validate({"output": {"dir": str(tmp_path)}, "live": {"captions": False}})
    saved = []
    monkeypatch.setattr(sess_mod.LiveEngine, "_save", lambda self, m: saved.append(m))
    t = [100.0]
    eng = sess_mod.LiveEngine(cfg, source=FileSource(tmp_path / "x.ts"), pack=PACKS["generic"], clock=lambda: t[0],
                              layout=Layout(1280, 720), notify=False, simulate=True, sid="live_test_lag", lag=25)
    eng.gov.cpu_fn = lambda: 10
    eng.signal(Signal(100, "chat", 1.0, "chat spike"))      # arrives at once
    t[0] = 118
    eng.signal(Signal(99, "audio", 1.0, "loud moment"))     # arrives 19 s late, happened first
    eng.tick()
    assert not eng.scorer.fired                              # still inside the lag
    t[0] = 140
    eng.tick()                                               # both fed in time order → 3.5 + 3.5 = 7
    assert eng.scorer.fired and set(eng.scorer.fired[0].kinds) == {"audio", "chat"}
    t[0] = 140 + 25 + 11
    eng.tick()
    assert saved and saved[0].id == 1


# ============================================================================ audio + chat
def test_hype_detector_fires_on_spike_only():
    from clipforge.live.audio import HypeDetector, obs_level_db

    assert obs_level_db([[0.1, 0.2, 0.2]]) == pytest.approx(-20, abs=0.1)
    d = HypeDetector("audio")
    fired = []
    for i in range(400):
        t = i * 0.2
        db = -35 + (i % 3)
        if 300 <= i <= 304:
            db = -8
        s = d.feed(t, db)
        if s:
            fired.append((round(t, 1), s.value))
    assert len(fired) == 1 and 60 <= fired[0][0] <= 61 and fired[0][1] > 0.6


def test_chat_spike_and_clip_command_permissions():
    from clipforge.live.chat import ChatMsg, ChatSpike, parse_irc

    c = ChatSpike(["w", "🔥", "clip it"], allowed=[])
    out = []
    for i in range(60):
        out += c.feed(ChatMsg(i * 6.0, f"u{i}", "hello"))
    assert not out
    for i in range(12):
        out += c.feed(ChatMsg(361 + i * 0.5, f"v{i}", "W W 🔥"))
    assert any(s.kind == "chat" for s in out)
    assert c.feed(ChatMsg(400, "randomviewer", "!clip lol")) == []           # viewers can't trigger clips
    assert c.feed(ChatMsg(402, "mod1", "!clip", is_mod=True))[0].kind == "manual"  # the streamer's mods can
    m = parse_irc("@badges=broadcaster/1;display-name=Me;mod=0 :me!me@me.tmi.twitch.tv PRIVMSG #me :!clip that", 5.0)
    assert m.is_owner and m.text == "!clip that" and m.user == "Me"
    assert parse_irc(":tmi.twitch.tv PING", 1) is None


def test_kick_chat_parsing_and_outdated_key_stops_quietly():
    from clipforge.live.chat import KickChat, parse_kick

    raw = json.dumps({"event": "App\\Events\\ChatMessageEvent", "channel": "chatrooms.668.v2",
                      "data": json.dumps({"content": "W OMEGALUL", "sender": {"username": "viewer1", "identity": {
                          "badges": [{"type": "moderator"}]}}})})
    m = parse_kick(raw, 12.0)
    assert m.user == "viewer1" and m.text == "W OMEGALUL" and m.is_mod and m.platform == "kick" and m.t == 12.0
    assert parse_kick(json.dumps({"event": "pusher:connection_established", "data": "{}"}), 1) is None
    tries = []

    def connect(url, **kw):
        tries.append(url)
        raise RuntimeError("received 4001 (private use) App key x not in this cluster")
    k = KickChat("xqc", lambda m: None, lambda: 0, connect=connect, room_id=668, key="x")
    k.run()
    assert len(tries) == 1 and "/app/x?" in tries[0]          # no endless retry loop


def test_youtube_chat_honours_polling_interval_budget_and_upload_quota():
    from clipforge.live.chat import YouTubeChat

    calls = []

    class Req:
        def __init__(self, res):
            self.res = res

        def execute(self):
            return self.res

    class Svc:
        def videos(self):
            return type("V", (), {"list": lambda s, **kw: Req({"items": [{"liveStreamingDetails": {"activeLiveChatId": "chat1"}}]})})()

        def liveChatMessages(self):
            def lst(s, **kw):
                calls.append(kw)
                return Req({"pollingIntervalMillis": 25000, "nextPageToken": f"p{len(calls)}",
                            "items": [{"snippet": {"displayMessage": "W"}, "authorDetails": {"displayName": "A"}}]})
            return type("M", (), {"list": lst})()

    got, waits = [], []
    y = YouTubeChat(Svc(), got.append, lambda: 1.0, min_sec=20, units_per_poll=5, max_units=11, video_id="abc")
    y.stop_ev.wait = lambda s: waits.append(s) or False
    y.run()
    assert len(calls) == 2 and waits == [25.0, 25.0] and len(got) == 1 and calls[1]["pageToken"] == "p1"
    calls.clear()
    y2 = YouTubeChat(Svc(), got.append, lambda: 1.0, min_sec=20, units_per_poll=5, max_units=999, video_id="abc",
                     quota_ok=lambda: False)                  # the day's quota is needed for uploads
    y2.stop_ev.wait = lambda s: False
    y2.run()
    assert calls == []


# ============================================================================ stream helpers
def test_stream_link_helpers():
    from clipforge.live.publish import vod_url_for
    from clipforge.live.sources import channel_of, live_url, platform_of
    from clipforge.notify.tg_live import is_live_link

    assert platform_of("https://www.twitch.tv/xqc") == "twitch" and channel_of("https://www.twitch.tv/xqc/") == "xqc"
    assert platform_of("https://kick.com/xqc") == "kick" and channel_of("https://kick.com/xqc?x=1") == "xqc"
    assert live_url("https://www.youtube.com/@LofiGirl") == "https://www.youtube.com/@LofiGirl/live"
    assert live_url("https://www.youtube.com/watch?v=abc") == "https://www.youtube.com/watch?v=abc"
    assert is_live_link("https://www.twitch.tv/xqc") and is_live_link("https://kick.com/xqc")
    assert not is_live_link("https://www.twitch.tv/xqc/videos") and not is_live_link("https://youtu.be/abc")
    assert vod_url_for("https://kick.com/xqc") == "https://kick.com/xqc/videos"
    assert vod_url_for("https://www.twitch.tv/xqc") == "https://www.twitch.tv/xqc/videos"
    assert vod_url_for("https://www.youtube.com/@x/live", "vid123") == "https://www.youtube.com/watch?v=vid123"


def test_hls_save_joins_the_segments_around_the_moment(tmp_path):
    import subprocess

    from clipforge.live import sources
    from clipforge.live.scorer import Moment
    from clipforge.utils.ffmpeg import ffmpeg_bin

    buf = tmp_path / "buf"
    buf.mkdir()
    t0 = dt.datetime(2026, 11, 15, 20, 0, 0).timestamp()
    for k in range(6):  # 6 × 10-s segments; the last one is "still being written"
        name = dt.datetime.fromtimestamp(t0 + 10 * k).strftime("%Y%m%d_%H%M%S") + ".ts"
        subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x36:r=10:d=10",
                        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "10", "-c:v", "libx264", "-c:a", "aac",
                        "-f", "mpegts", str(buf / name)], check=True)
    src = sources.HlsSource("https://kick.com/x", buf)
    src.t0_wall = t0
    path, start, dur = src.save(Moment(1, trigger=25, start=12, end=35, score=7, reasons=[], kinds=[]), now=60, wait=0)
    assert path.exists() and start == pytest.approx(2) and dur == pytest.approx(23)
    assert len(src.closed_segments()) == 5
    lv = sources.segment_levels(buf / sorted(p.name for p in buf.glob("*.ts"))[0])
    assert len(lv) == 50                                     # 10 s × 5 per second


# ============================================================================ HUD / facecam / game packs
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
    cv2.putText(win, "WINNER WINNER", (110, 90), cv2.FONT_HERSHEY_DUPLEX, 1.6, (0, 200, 255), 6)
    assert [s.meta["event"] for s in bgmi.feed(1.5, win)] == ["win"]
    assert bgmi.feed(3.0, win) == []
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
    assert detect_pack(["xQc (live) Valorant ranked", "Gaming"]).name == "valorant"
    assert detect_pack(["GTA VI first day"]).name == "gta6" and detect_pack(["gta v rp"]).name == "gta5"
    assert detect_pack(["Just chatting"]).name == "generic"
    assert detect_pack(["valorant"], "cs2").name == "cs2"


def test_facecam_is_the_face_that_stays_put():
    from clipforge.live.facecam import decide

    cam = (0.10, 0.42, 0.04, 0.09)
    frames = []
    for i in range(10):  # the streamer's cam never moves; a game character's face wanders around
        game_face = (0.3 + 0.05 * i, 0.1 + 0.03 * i, 0.12, 0.27)
        frames.append([game_face, cam] if i % 4 else [game_face])
    d = decide(frames)
    assert d.kind == "facecam" and d.rect[0] < 0.12 < d.rect[0] + d.rect[2] and d.rect[2] == pytest.approx(0.18)
    irl = decide([[(0.35, 0.2, 0.25, 0.45)]] * 10)
    assert irl.kind == "irl" and irl.focus_x == pytest.approx(0.475)
    assert decide([[]] * 10).kind == "none"
    assert decide([[(0.1 * i, 0.5, 0.05, 0.1)] for i in range(10)]).kind == "none"   # moving face = not a cam


def test_titles_credit_the_streamer_not_me():
    from clipforge.live.games import PACKS, hashtags_for, title_for

    a = title_for(PACKS["bgmi_pubg"], "win", ["game_event"], "s1:1", streamer="Mortal")
    assert a == title_for(PACKS["bgmi_pubg"], "win", ["game_event"], "s1:1", streamer="Mortal") and len(a) <= 95
    for ev, kinds in (("", ["audio"]), ("manual", ["manual"]), ("kill", ["game_event"]), ("win", ["game_event"])):
        for seed in range(6):
            t = title_for(PACKS["bgmi_pubg"], ev, kinds, f"x{seed}", 3, streamer="Mortal")
            assert not any(w in f" {t} " for w in (" I ", " My ", " me ", " We ")), t
    assert "{streamer}" not in title_for(PACKS["generic"], "", ["audio"], "q", streamer="")
    tags = hashtags_for(PACKS["valorant"], ["Tenz Live", "#gaming"])
    assert len(tags) <= 5 and all(t == t.lower() and t.startswith("#") for t in tags) and "#shorts" in tags


# ============================================================================ layout
def test_layout_graphs():
    from clipforge.live.render import Layout, video_graph

    g = video_graph(Layout(1920, 1080, (40, 770, 480, 270)))
    assert "vstack=inputs=2" in g and "scale=1080:672" in g and "scale=1080:1248" in g and g.endswith("[vout]")
    assert "crop=934:1080:493:0" in g
    crop = video_graph(Layout(1920, 1080, None, "crop", focus_x=0.3))
    assert "crop=606:1080:394:0,scale=1080:1920" in crop     # IRL: follows the face
    blur = video_graph(Layout(1280, 720, None, "fit_blur"), "c.ass", "../fonts")
    assert "boxblur" in blur and "ass=filename='c.ass':fontsdir='../fonts'" in blur
    assert Layout.scale_rect((20, 700, 480, 270), (1920, 1080), (1280, 720)) == (13, 466, 320, 180)


# ============================================================================ commands / status
def test_command_tail_and_status_staleness(tmp_path):
    from clipforge.config import Config
    from clipforge.live import store

    cfg = Config.model_validate({"output": {"dir": str(tmp_path)}})
    store.send_command(cfg, "clip", note="old")       # before the agent started: never replayed
    tail = store.CommandTail(cfg)
    store.send_command(cfg, "start", url="https://kick.com/x")
    store.send_command(cfg, "stop", sid="live_1")
    assert [c["cmd"] for c in tail.read()] == ["start", "stop"] and tail.read() == []
    store.write_status(cfg, {"note": "x", "streams": []})
    assert store.read_status(cfg)["engine_running"]
    st = json.loads((tmp_path / "live" / "status.json").read_text())
    st["heartbeat"] -= 60
    (tmp_path / "live" / "status.json").write_text(json.dumps(st))
    assert not store.read_status(cfg)["engine_running"]
    assert store.slug("xQc Live!! (Kick)") == "xqc-live-kick"


# ============================================================================ posting rules
def _live_clip(cfg, sid="live_20261115_2000_xqc", live=True, stream="https://www.twitch.tv/xqc", streamer="xQc"):
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
    store.save_session(cfg, sid, {"id": sid, "stream_url": stream, "live": live, "streamer": streamer, "clips": [
        {"rank": 1, "title": "xQc 1v4 CLUTCH 😱", "hashtags": ["#valorant", "#xqc", "#shorts"], "file": out.name,
         "status": "ready", "posted": {}, "score": 8, "duration": 30}], "last_post": {}})
    return sid


def test_live_clips_only_go_to_switched_on_accounts_with_credit_and_gap(env, monkeypatch):  # noqa: F811
    import shutil

    from clipforge import pipeline
    from clipforge.api import db
    from clipforge.config import load_config
    from clipforge.live import publish as lp, store
    from clipforge.models import ClipCandidate
    from clipforge.publish import queue as q
    from clipforge.services import publishing as pub
    from clipforge.workspace import Workspace

    cfg = load_config()
    sid = _live_clip(cfg)
    with db.session() as s:
        s.add(db.Channel(id="UCpod", title="Podcast", confirmed=True))
        s.commit()
    with pytest.raises(ValueError, match="switched on for live clips"):
        lp.post_clip(cfg, sid, 1, "now")
    with db.session() as s:
        s.add(db.Channel(id="UCclips", title="Clips", confirmed=True, settings_json=json.dumps({"content": "gaming"})))
        s.commit()
    published = []
    monkeypatch.setattr(pub, "do_publish_now", lambda i: published.append(i))
    assert lp.post_clip(cfg, sid, 1, "now", now=NOW) == ["Clips: posting now"] and len(published) == 1
    it = q.items()[0]
    assert [i.channel_id for i in q.items()] == ["UCclips"]              # never the podcast account
    assert "🔴 LIVE now: xQc on Twitch → https://www.twitch.tv/xqc" in it.description
    assert "🎥 Credit: xQc" in it.ig_caption and it.meta_status == "ready" and it.category_id == "20"
    sess = store.load_session(cfg, sid)
    sess["clips"].append({**sess["clips"][0], "rank": 2, "posted": {}, "status": "ready"})
    store.save_session(cfg, sid, sess)
    ws = Workspace(cfg.workspace, sid)
    shutil.copy(ws.find_clip(1), ws.clip_path(2, "second"))
    pipeline.save_candidates(ws, pipeline.load_candidates(ws) + [ClipCandidate(rank=2, start=100, end=130, title="b", score=7)])
    lines2 = lp.post_clip(cfg, sid, 2, "now", now=NOW + dt.timedelta(minutes=5))
    assert "live post gap" in lines2[0] and len(published) == 1
    second = q.get(store.get_clip(cfg, sid, 2)["posted"]["UCclips"])
    assert second.privacy == "public" and second.slot_utc is not None
    n = lp.stream_ended(cfg, sid, lp.vod_url_for("https://www.twitch.tv/xqc"))
    assert n == 2
    for i in q.items():
        assert "LIVE now" not in i.description and "Full stream: https://www.twitch.tv/xqc/videos" in i.ig_caption
        assert "🎥 Credit: xQc" in i.description


def test_caption_swap_pure():
    from clipforge.live.publish import caption, swap_line

    live = {"stream_url": "https://kick.com/xqc", "live": True, "streamer": "xQc"}
    c = caption("Title", ["#gaming", "#shorts"], live)
    assert "🔴 LIVE now: xQc on Kick → https://kick.com/xqc" in c and "🎥 Credit: xQc" in c
    after = swap_line(c, {**live, "live": False, "vod_url": "https://kick.com/xqc/videos"})
    assert "LIVE now" not in after and "Full stream: https://kick.com/xqc/videos" in after and after.startswith("Title")


def test_auto_post_rules(tmp_path, monkeypatch):
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
                              layout=Layout(1920, 1080), notify=False, simulate=True, sid="live_test_auto", streamer="Mortal")
    eng.gov.cpu_fn = lambda: 10
    eng.signal(Signal(30, "audio", 1.0, "loud"))             # audio only: clip, but no auto-post
    t[0] = 45
    eng.tick()
    t[0] = 200
    eng.signal(Signal(200, "game_event", 1.0, "win", {"event": "win"}))
    eng.signal(Signal(201, "audio", 1.0, "loud"))            # 5 + 3.5 = 8.5 ≥ 8 → auto-post
    t[0] = 220
    eng.tick()
    eng.finish(wait=30)
    assert posted == [2]


# ============================================================================ end to end (real render)
def test_simulated_stream_end_to_end(tmp_path):
    from clipforge.config import Config
    from clipforge.live.simulate import make_sample, simulate

    cfg = Config.model_validate({"output": {"dir": str(tmp_path)}, "live": {"captions": False}})
    p, fc = make_sample(tmp_path / "s.mp4", seconds=60)
    r = simulate(cfg, p, game="bgmi_pubg", facecam=fc, marks=[57], streamer="TestStreamer")
    reasons = " | ".join(c["reason"] for c in r["clips"])
    assert len(r["clips"]) == 2, r["moments"]                # kills (~40 s) + the manual mark (57 s)
    assert "kill" in reasons and "marked" in reasons
    from clipforge.utils.ffmpeg import probe_video

    info = probe_video(next((tmp_path / r["session"] / "clips").glob("01_*.mp4")))
    assert (info["width"], info["height"]) == (1080, 1920)
