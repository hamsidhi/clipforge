"""The live agent: `clipforge live run` — its own process at BELOW_NORMAL priority pinned to `live.max_cores` cores
(the app's `LiveManager` keeps it running while `live.enabled`). It watches up to `live.max_streams` live streams
(links added from the app / Telegram, or `live.watch_channels` going live), runs one LiveEngine per stream, writes
workspace/live/status.json and obeys workspace/live/commands.jsonl. Active streams survive a restart (streams.json).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from clipforge.config import Config, load_config
from clipforge.live import store
from clipforge.live.games import detect_pack
from clipforge.live.governor import Governor, make_low_priority
from clipforge.live.render import Layout
from clipforge.live.sources import HlsSource, SegmentReader, StreamInfo, channel_of, is_live, live_url, stream_info
from clipforge.utils.ffmpeg import probe_video
from clipforge.utils.log import log


@dataclass
class Watch:
    url: str
    info: StreamInfo
    src: HlsSource
    eng: object
    reader: SegmentReader
    chats: list = field(default_factory=list)
    started: float = field(default_factory=time.time)
    last_check: float = field(default_factory=time.monotonic)
    ending: bool = False
    folder: str = ""


class Agent:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.gov = Governor(cpu_ceiling=cfg.live.cpu_ceiling)
        self.watches: dict[str, Watch] = {}
        self.starting: set[str] = set()
        self.tail = store.CommandTail(cfg)
        self.note = "Idle — paste a live stream link (app → Live, or send it on Telegram)."
        self.quit = threading.Event()
        self.last_watch_poll = 0.0
        self.errors: list[str] = []

    # ------------------------------------------------------------------ persistence
    def _streams_file(self):
        return store.live_dir(self.cfg) / "streams.json"

    def _persist(self) -> None:
        data = [{"url": w.url, "sid": sid, "folder": w.folder} for sid, w in self.watches.items() if not w.ending]
        store._atomic_write(self._streams_file(), data)

    def _resume(self) -> None:
        try:
            data = json.loads(self._streams_file().read_text(encoding="utf-8"))
        except Exception:
            return
        for d in data:
            threading.Thread(target=self.start_stream, args=(d["url"], d.get("sid"), d.get("folder", "")),
                             daemon=True).start()

    # ------------------------------------------------------------------ status / commands
    def write_status(self) -> None:
        st = {"pid": os.getpid(), "note": self.note,
              "governor": {"state": self.gov.state, "cpu": round(self.gov.cpu, 1), "reason": self.gov.reason},
              "streams": [w.eng.status() | {"ending": w.ending, "started": w.started} for w in list(self.watches.values())],
              "starting": sorted(self.starting), "errors": self.errors[-5:]}
        try:
            store.write_status(self.cfg, st)
        except Exception as exc:
            log.warning(f"[live] status write failed: {exc}")

    def _target(self, c: dict) -> list[Watch]:
        sid = c.get("sid")
        if sid:
            return [w for s, w in self.watches.items() if s == sid]
        return list(self.watches.values())

    def handle_commands(self) -> None:
        for c in self.tail.read():
            cmd = c.get("cmd")
            if cmd == "quit":
                self.quit.set()
            elif cmd == "start" and c.get("url"):
                threading.Thread(target=self.start_stream, args=(c["url"], None, c.get("folder", "")),
                                 daemon=True).start()
            elif cmd == "stop":
                for w in self._target(c):
                    self.end_stream(w, "stopped")
            elif cmd in ("clip", "pause", "resume", "auto"):
                ws = self._target(c)
                if cmd == "clip" and not c.get("sid"):
                    ws = ws[:1]  # the app/Telegram pick a stream when several run; a bare /clip marks the first
                for w in ws:
                    w.eng.command(c)

    # ------------------------------------------------------------------ one stream
    def start_stream(self, url: str, sid: str | None = None, folder: str = "") -> None:
        from clipforge.live.session import LiveEngine

        url = live_url(url)
        if url in self.starting or any(w.url == url for w in self.watches.values()):
            return
        if len(self.watches) >= self.cfg.live.max_streams:
            self._error(f"Already watching {len(self.watches)} streams (live.max_streams) — stop one first.")
            return
        self.starting.add(url)
        try:
            info = stream_info(url)
            if not info.is_live:
                self._error(f"{info.streamer or url} isn't live right now.")
                return
            lc = self.cfg.live
            pack = detect_pack([info.title, info.categories], lc.game_pack)
            sid = sid or store.session_id() + "_" + store.slug(info.streamer or channel_of(url) or info.video_id or "stream")
            src = HlsSource(url, self.cfg.workspace / "live" / "buffers" / sid, lc.hls_buffer_min, lc.max_cores,
                            lc.stream_height)
            t0w, t0 = time.time(), time.monotonic()
            src.start(t0w)
            eng = LiveEngine(self.cfg, source=src, pack=pack, clock=lambda: time.monotonic() - t0,
                             layout=Layout(1280, 720, None, pack.gameplay_only), stream_url=url, sid=sid,
                             governor=self.gov, title=info.title, streamer=info.streamer, platform=info.platform,
                             lag=25.0, detect_cam=True)
            first = threading.Event()

            def on_frame(t, frame, eng=eng):
                if not first.is_set():
                    first.set()
                    self._size_from_segment(src, eng)
                eng.frame(t, frame)
            sess = store.load_session(self.cfg, sid)
            sess["video_id"] = info.video_id
            sess["folder"] = folder or sess.get("folder") or store.LIVE_FOLDER
            store.save_session(self.cfg, sid, sess)
            reader = SegmentReader(src, eng.audio_level, on_frame, self.gov.allow_detectors)
            reader.start()
            w = Watch(url, info, src, eng, reader)
            w.folder = sess["folder"]
            w.chats = self._start_chat(w)
            self.watches[sid] = w
            self._persist()
            _tell(f"🔴 Watching <b>{_esc(info.streamer)}</b> on {info.platform.title()} · {_esc(pack.label)}\n"
                  f"{_esc(info.title[:120])}\nClips arrive here as drafts. /live shows all streams.")
            log.info(f"[live] watching {url} ({pack.name}) as {sid}")
        except Exception as exc:
            self._error(f"Couldn't start {url}: {exc}")
        finally:
            self.starting.discard(url)

    def _size_from_segment(self, src: HlsSource, eng) -> None:
        segs = src.closed_segments()
        if not segs:
            return
        try:
            info = probe_video(segs[-1][1])
            w, h = int(info.get("width") or 0), int(info.get("height") or 0)
            if w and h:
                eng.layout.src_w, eng.layout.src_h = w, h
        except Exception:
            pass

    def _start_chat(self, w: Watch) -> list:
        from clipforge.live.chat import KickChat, TwitchChat, YouTubeChat

        cc, eng = self.cfg.live.chat, w.eng
        out: list = []
        try:
            if w.info.platform == "twitch" and cc.twitch:
                out.append(TwitchChat(channel_of(w.url), eng.chat_msg, eng.clock))
            elif w.info.platform == "kick" and cc.kick:
                out.append(KickChat(channel_of(w.url), eng.chat_msg, eng.clock, key=cc.kick_pusher_key,
                                    cluster=cc.kick_pusher_cluster))
            elif w.info.platform == "youtube" and cc.youtube and w.info.video_id:
                svc = _youtube_reader()
                if svc is not None:
                    from clipforge.publish import quota

                    out.append(YouTubeChat(svc, eng.chat_msg, eng.clock, min_sec=cc.youtube_poll_min_sec,
                                           units_per_poll=cc.youtube_units_per_poll, max_units=cc.youtube_max_units,
                                           charge=lambda n: quota.charge(self.cfg, units=n), video_id=w.info.video_id,
                                           quota_ok=lambda: quota.remaining(self.cfg)["units"] > cc.youtube_keep_units))
        except Exception as exc:
            log.warning(f"[live] chat not started for {w.url}: {exc}")
        for c in out:
            c.start()
        return out

    def end_stream(self, w: Watch, why: str) -> None:
        if w.ending:
            return
        w.ending = True
        self._persist()

        def run() -> None:
            for c in w.chats:
                c.stop()
            w.reader.stop()
            w.src.stop()
            try:
                w.eng.finish()
            finally:
                self.watches.pop(w.eng.sid, None)
                log.info(f"[live] {w.url}: {why}")
        threading.Thread(target=run, daemon=True, name="live-finish").start()

    def check_streams(self) -> None:
        for w in list(self.watches.values()):
            if w.ending:
                continue
            if w.eng.stop_ev.is_set():
                self.end_stream(w, "stopped")
                continue
            quiet = time.time() - w.reader.last_seg_at
            if w.src.dead or (quiet > 90 and time.monotonic() - w.last_check > 60):
                w.last_check = time.monotonic()
                if w.src.dead or not is_live(w.url):
                    self.end_stream(w, "stream ended")

    def poll_watch_channels(self) -> None:
        lc = self.cfg.live
        if not lc.watch_channels or time.monotonic() - self.last_watch_poll < lc.poll_live_min * 60:
            return
        self.last_watch_poll = time.monotonic()
        for url in lc.watch_channels:
            u = live_url(url)
            if any(w.url == u for w in self.watches.values()) or len(self.watches) >= lc.max_streams:
                continue
            if is_live(u):
                threading.Thread(target=self.start_stream, args=(u,), daemon=True).start()

    def _error(self, msg: str) -> None:
        log.warning(f"[live] {msg}")
        self.errors.append(f"{time.strftime('%H:%M')} {msg}")
        _tell(f"⚠️ {_esc(msg)}")

    # ------------------------------------------------------------------ main loop
    def run(self) -> None:
        make_low_priority(None, self.cfg.live.max_cores, idle=False)
        owner = store.live_dir(self.cfg) / "agent.pid"
        owner.write_text(str(os.getpid()), encoding="utf-8")  # newest agent wins: older copies (left behind when
        self._resume()                                           # the app was killed) see this and exit
        last = 0.0
        while not self.quit.is_set():
            try:
                if owner.read_text(encoding="utf-8").strip() != str(os.getpid()):
                    log.info("[live] a newer live agent took over; this one exits")
                    break
            except OSError:
                pass
            self.handle_commands()
            for w in list(self.watches.values()):
                w.eng.tick()
            now = time.monotonic()
            if now - last >= 1:
                last = now
                self.gov.sample()
                self.check_streams()
                self.poll_watch_channels()
                n = len(self.watches)
                self.note = (f"Watching {n} stream(s)." if n else
                             "Idle — paste a live stream link (app → Live, or send it on Telegram).")
                self.write_status()
            time.sleep(0.5)
        # agent stopping (app restart / update): keep streams.json so the next agent resumes them; no recap, no
        # "stream ended" caption swap — just stop pulling and let renders in progress finish
        for w in list(self.watches.values()):
            for c in w.chats:
                c.stop()
            w.reader.stop()
            w.src.stop()
        deadline = time.monotonic() + 120
        while any(w.eng.busy or w.eng.render_q.unfinished_tasks for w in self.watches.values()) and time.monotonic() < deadline:
            time.sleep(1)
        self.write_status()


def _youtube_reader():
    """A YouTube API client from any connected YouTube account (readonly is enough to read public live chat)."""
    try:
        from googleapiclient.discovery import build

        from clipforge.publish import channels as chs
        from clipforge.publish.youtube import load_credentials

        ch = next((c for c in chs.all_channels() if (c.platform or "youtube") == "youtube" and c.auth_status == "ok"), None)
        if ch is None:
            log.warning("[live] YouTube chat: no connected YouTube account to read it with")
            return None
        return build("youtube", "v3", credentials=load_credentials(ch.id), cache_discovery=False)
    except Exception as exc:
        log.warning(f"[live] YouTube chat unavailable: {exc}")
        return None


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tell(text: str) -> None:
    try:
        from clipforge.notify.telegram import bot

        b = bot()
        if b.token:
            b.send_text(text, html=True)
    except Exception as exc:
        log.warning(f"[live] Telegram: {exc}")


def run_agent() -> None:
    Agent(load_config()).run()


# ----------------------------------------------------------------------------- inside the app: keep the agent alive
class LiveManager(threading.Thread):
    """Runs in `clipforge ui` / the service: keeps `clipforge live run` alive while live.enabled (restarts it if it
    dies, stops it when disabled). Separate process: the web app and the live agent never slow each other down."""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="live-manager")
        self.proc: subprocess.Popen | None = None
        self.stop_ev = threading.Event()

    def run(self) -> None:
        while not self.stop_ev.wait(10):
            try:
                cfg = load_config()
            except Exception:
                continue
            alive = self.proc is not None and self.proc.poll() is None
            if cfg.live.enabled and not alive:
                self.start_agent()
            elif not cfg.live.enabled and alive:
                self.stop_agent(cfg)

    def start_agent(self) -> None:
        flags = 0x4000 | 0x08000000 if sys.platform == "win32" else 0  # BELOW_NORMAL + no window
        log.info("[live] starting the live agent")
        self.proc = subprocess.Popen([sys.executable, "-m", "clipforge", "live", "run"], creationflags=flags,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop_agent(self, cfg: Config) -> None:
        store.send_command(cfg, "quit")
        try:
            self.proc.wait(60)  # type: ignore[union-attr]
        except Exception:
            self.proc.terminate()  # type: ignore[union-attr]
        self.proc = None

    def shutdown(self) -> None:
        self.stop_ev.set()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()


_manager: LiveManager | None = None


def start_manager() -> None:
    global _manager
    if _manager is None and os.environ.get("CLIPFORGE_NO_LIVE") != "1":
        _manager = LiveManager()
        _manager.start()
