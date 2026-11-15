"""The live agent: `clipforge live run` — its own process at BELOW_NORMAL priority pinned to `live.max_cores` cores
(the app's `LiveManager` keeps it running while `live.enabled`). It waits for OBS to go live (or, without OBS, polls
`live.stream_urls`), runs one LiveEngine per stream, writes workspace/live/status.json and obeys commands.jsonl.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from clipforge.config import Config, load_config, load_secrets
from clipforge.live import store
from clipforge.live.games import detect_pack
from clipforge.live.governor import Governor, make_low_priority
from clipforge.live.audio import obs_level_db
from clipforge.live.obs import ObsClient, ObsError, facecam_rect
from clipforge.live.render import Layout
from clipforge.utils.log import log


def _match(name: str, names: list[str]) -> bool:
    low = name.lower()
    return any(n.lower() in low for n in names)


# ----------------------------------------------------------------------------- hotkey (Windows)
class Hotkey(threading.Thread):
    """Global hotkey via RegisterHotKey (no keyboard hook, no admin). `ctrl+alt+c` → callback."""

    MODS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}

    def __init__(self, spec: str, fn) -> None:
        super().__init__(daemon=True, name="live-hotkey")
        self.spec, self.fn = spec, fn
        self.ok = False

    def run(self) -> None:
        if sys.platform != "win32" or not self.spec:
            return
        import ctypes
        from ctypes import wintypes

        parts = [p.strip().lower() for p in self.spec.split("+")]
        mods = sum(self.MODS.get(p, 0) for p in parts[:-1]) | 0x4000  # MOD_NOREPEAT
        key = parts[-1]
        vk = ord(key.upper()) if len(key) == 1 else {"f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B}.get(key, 0)
        user32 = ctypes.windll.user32
        if not vk or not user32.RegisterHotKey(None, 1, mods, vk):
            log.warning(f"[live] hotkey {self.spec} couldn't be registered (another app uses it?)")
            return
        self.ok = True
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                try:
                    self.fn()
                except Exception as exc:
                    log.warning(f"[live] hotkey: {exc}")


# ----------------------------------------------------------------------------- the agent
class Agent:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.gov = Governor(cpu_ceiling=cfg.live.cpu_ceiling)
        self.obs: ObsClient | None = None
        self.engine = None
        self.tail = store.CommandTail(cfg)
        self.note = "Starting…"
        self.obs_state = {"connected": False, "streaming": False, "replay": False}
        self._stream_flag = threading.Event()
        self._stopped_flag = threading.Event()
        self.last_poll = 0.0
        self.quit = threading.Event()

    # ------------------------------------------------------------------ status
    def write_status(self) -> None:
        st = {"pid": os.getpid(), "note": self.note, "obs": self.obs_state, "source": self.cfg.live.source,
              "governor": {"state": self.gov.state, "cpu": round(self.gov.cpu, 1), "reason": self.gov.reason},
              "live": self.engine.status() if self.engine else None}
        st["live_session"] = self.engine.sid if self.engine else ""
        if self.engine:
            st["live"]["obs_replay"] = self.obs_state.get("replay")
        try:
            store.write_status(self.cfg, st)
        except Exception as exc:
            log.warning(f"[live] status write failed: {exc}")

    def handle_commands(self) -> None:
        for c in self.tail.read():
            if c.get("cmd") == "quit":
                self.quit.set()
            elif self.engine:
                self.engine.command(c)
            elif c.get("cmd") == "clip":
                self.note = "Not live right now — /clip works while you're streaming."

    def hotkey_mark(self) -> None:
        if self.engine:
            self.engine.mark("", "hotkey")

    # ------------------------------------------------------------------ OBS
    def connect_obs(self) -> bool:
        lc = self.cfg.live.obs
        pw = os.environ.get(lc.password_env) or getattr(load_secrets(), lc.password_env.lower(), None) or _env_file(lc.password_env)
        try:
            self.obs = ObsClient(lc.host, lc.port, pw or "").open()
        except Exception as exc:
            self.obs = None
            self.obs_state["connected"] = False
            self.note = f"OBS not reachable ({str(exc)[:120]}). Is OBS open with Tools → WebSocket Server enabled?"
            return False
        self.obs_state["connected"] = True
        self.obs.on("StreamStateChanged", self._on_stream)
        self.obs.on("_Disconnected", lambda d: self._stopped_flag.set())
        log.info("[live] connected to OBS")
        return True

    def _on_stream(self, d: dict) -> None:
        if d.get("outputActive"):
            self._stream_flag.set()
        elif d.get("outputState") in ("OBS_WEBSOCKET_OUTPUT_STOPPED", "OBS_WEBSOCKET_OUTPUT_STOPPING"):
            self._stopped_flag.set()

    def obs_session(self) -> None:
        from clipforge.live.session import LiveEngine
        from clipforge.live.sources import ObsReplaySource

        obs, lc = self.obs, self.cfg.live
        assert obs is not None
        try:
            if not obs.replay_active() and lc.obs.start_replay_buffer:
                obs.request("StartReplayBuffer")
                time.sleep(1)
            self.obs_state["replay"] = obs.replay_active()
        except ObsError as exc:
            self.obs_state["replay"] = False
            log.warning(f"[live] Replay Buffer: {exc}")
        if not self.obs_state["replay"]:
            self.note = ("OBS Replay Buffer is OFF — enable it in OBS (Settings → Output → Replay Buffer, 120 s). "
                         "No clips can be cut without it.")
            _tell(self.note)
        names = obs.names_for_game_detection()
        pack = detect_pack(names, lc.game_pack)
        bw, bh = obs.video_size()
        scene = obs.program_scene()
        fc = self._facecam(scene, (bw, bh))
        stream_url = next((u for u in lc.stream_urls), "")
        t0 = time.monotonic()
        lay = Layout(bw, bh, fc, pack.gameplay_only)
        # replay files are at the OUTPUT resolution: rescale the facecam rect when the first replay arrives
        eng = LiveEngine(self.cfg, source=_ScaledObsSource(ObsReplaySource(obs), lay, (bw, bh)), pack=pack,
                         clock=lambda: time.monotonic() - t0, layout=lay, stream_url=stream_url, governor=self.gov)
        self.engine = eng
        self.note = f"Live: {pack.label}"
        _tell(f"🔴 Live clipping started · {pack.label} · facecam {'found' if fc else 'not found (gameplay-only layout)'}"
              f" · replay buffer {'on' if self.obs_state['replay'] else 'OFF'}")
        hud_source = next((n for n in names[1:] if n and _match(n, ["game", "capture", "window"])), scene) or scene

        def on_meters(d: dict) -> None:
            mic = desk = None
            for inp in d.get("inputs", []):
                name = str(inp.get("inputName", ""))
                if _match(name, lc.obs.mic_input_names):
                    mic = obs_level_db(inp.get("inputLevelsMul", []))
                elif _match(name, lc.obs.desktop_input_names):
                    desk = obs_level_db(inp.get("inputLevelsMul", []))
            eng.audio_levels(mic, desk)

        def on_scene(d: dict) -> None:
            nonlocal scene
            scene = d.get("sceneName") or scene
            lay.facecam = Layout.scale_rect(self._facecam(scene, (bw, bh)), (bw, bh), (lay.src_w, lay.src_h))

        obs.on("InputVolumeMeters", on_meters)
        obs.on("CurrentProgramSceneChanged", on_scene)
        chats = self._start_chat(eng)
        last_hud = last_stats = last_status = 0.0
        self._stopped_flag.clear()
        while not self._stopped_flag.is_set() and not eng.stop_ev.is_set() and not self.quit.is_set():
            now = time.monotonic()
            eng.tick()
            self.handle_commands()
            if now - last_status >= 1:
                self.gov.sample()
                self.write_status()
                last_status = now
            if now - last_stats >= 5:
                try:
                    self.gov.obs_stats(obs.request("GetStats"))
                except ObsError:
                    pass
                last_stats = now
            if now - last_hud >= lc.hud_every_sec and self.gov.allow_detectors() and not eng.paused and pack.rules:
                last_hud = now
                try:
                    import cv2

                    jpg = obs.screenshot(hud_source, 640)
                    frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                    eng.hud_frame(frame)
                except Exception as exc:
                    log.debug(f"[live] HUD screenshot: {exc}")
            if not obs.connected:
                break
            time.sleep(0.25)
        for c in chats:
            c.stop()
        self.note = "Stream ended — finishing clips…"
        self.write_status()
        eng.finish()
        self.engine = None
        self.note = "Waiting for the stream to start."

    def _facecam(self, scene: str, base: tuple[int, int]):
        try:
            hit = self.obs.find_item(scene, self.cfg.live.obs.facecam_source_names) if self.obs else None
        except ObsError:
            hit = None
        return facecam_rect(hit[1], base) if hit else None

    def _start_chat(self, eng) -> list:
        from clipforge.live.chat import TwitchChat, YouTubeChat

        out = []
        cc = self.cfg.live.chat
        if cc.twitch_channel:
            t = TwitchChat(cc.twitch_channel, eng.chat_msg, eng.clock)
            t.start()
            out.append(t)
        if cc.youtube_live:
            try:
                from googleapiclient.discovery import build

                from clipforge.live.publish import gaming_accounts
                from clipforge.publish import quota
                from clipforge.publish.youtube import load_credentials

                ch = next((c for c in gaming_accounts("youtube")), None)
                if ch is None:
                    log.warning("[live] YouTube chat: no gaming YouTube account connected")
                else:
                    svc = build("youtube", "v3", credentials=load_credentials(ch.id), cache_discovery=False)
                    y = YouTubeChat(svc, eng.chat_msg, eng.clock, min_sec=cc.youtube_poll_min_sec,
                                    units_per_poll=cc.youtube_units_per_poll, max_units=cc.youtube_max_units,
                                    charge=lambda n: quota.charge(self.cfg, units=n))
                    y.start()
                    out.append(y)
            except Exception as exc:
                log.warning(f"[live] YouTube chat not started: {exc}")
        return out

    # ------------------------------------------------------------------ HLS (no OBS)
    def hls_session(self, url: str) -> None:
        from clipforge.live.games import PACKS
        from clipforge.live.session import LiveEngine
        from clipforge.live.sources import HlsSource

        lc = self.cfg.live
        buf = self.cfg.workspace / "live" / "hls_buffer"
        src = HlsSource(url, buf, lc.hls_buffer_min, lc.max_cores)
        t0w, t0 = time.time(), time.monotonic()
        try:
            src.start(t0w)
        except Exception as exc:
            self.note = f"Couldn't read the live stream: {exc}"
            return
        pack = PACKS.get(lc.game_pack) or PACKS["generic"]
        eng = LiveEngine(self.cfg, source=src, pack=pack, clock=lambda: time.monotonic() - t0,
                         layout=Layout(1280, 720, None, pack.gameplay_only), stream_url=url, governor=self.gov)
        self.engine = eng
        self.note = f"Live (stream link, no OBS): {url}"
        _tell(f"🔴 Live clipping started from your stream link (no OBS) · {pack.label}. Signals: chat + /clip + hotkey.")
        chats = self._start_chat(eng)
        last_status, last_check = 0.0, time.monotonic()
        while not eng.stop_ev.is_set() and not self.quit.is_set():
            eng.tick()
            self.handle_commands()
            now = time.monotonic()
            if now - last_status >= 1:
                self.gov.sample()
                self.write_status()
                last_status = now
            if now - last_check >= lc.poll_live_min * 60:
                last_check = now
                from clipforge.live.sources import is_live

                if not is_live(url) or (src.proc and src.proc.poll() is not None):
                    break
            time.sleep(0.25)
        for c in chats:
            c.stop()
        src.stop()
        eng.finish()
        self.engine = None

    # ------------------------------------------------------------------ main loop
    def run(self) -> None:
        make_low_priority(None, self.cfg.live.max_cores, idle=False)
        hk = Hotkey(self.cfg.live.hotkey, self.hotkey_mark)
        hk.start()
        lc = self.cfg.live
        while not self.quit.is_set():
            self.handle_commands()
            self.gov.sample()
            if lc.source == "obs":
                if self.obs is None or not self.obs.connected:
                    self.connect_obs()
                if self.obs is not None and self.obs.connected:
                    try:
                        streaming = self.obs.streaming()
                        self.obs_state["streaming"] = streaming
                        self.obs_state["replay"] = self.obs.replay_active()
                    except ObsError:
                        streaming = False
                    if streaming:
                        self.obs_session()
                        continue
                    self.note = "OBS connected · waiting for you to go live."
            if (self.obs is None or not self.obs.connected or lc.source == "hls") and lc.stream_urls \
                    and time.monotonic() - self.last_poll >= lc.poll_live_min * 60:
                self.last_poll = time.monotonic()
                from clipforge.live.sources import is_live

                url = next((u for u in lc.stream_urls if is_live(u)), "")
                if url:
                    self.hls_session(url)
                    continue
            self.write_status()
            self._stream_flag.wait(5 if self.obs is not None and self.obs.connected else 30)
            self._stream_flag.clear()
        self.write_status()


class _ScaledObsSource:
    """Wraps the replay source: on the first replay, learn the output size and rescale the facecam rect."""

    out_size: tuple[int, int] | None = None

    def __init__(self, inner, lay: Layout, canvas: tuple[int, int]) -> None:
        self.inner, self.lay, self.canvas = inner, lay, canvas
        self.kind = inner.kind

    def save(self, m, now):
        path, start, dur = self.inner.save(m, now)
        from clipforge.utils.ffmpeg import probe_video

        info = probe_video(path)
        size = (int(info.get("width") or self.canvas[0]), int(info.get("height") or self.canvas[1]))
        if size != (self.lay.src_w, self.lay.src_h):
            self.lay.facecam = Layout.scale_rect(self.lay.facecam, (self.lay.src_w, self.lay.src_h), size)
            self.lay.src_w, self.lay.src_h = size
            _ScaledObsSource.out_size = size
        return path, start, dur


def _env_file(name: str) -> str:
    from clipforge.config import ENV_PATH

    try:
        for ln in Path(ENV_PATH).read_text(encoding="utf-8").splitlines():
            if ln.startswith(f"{name}="):
                return ln.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _tell(text: str) -> None:
    try:
        from clipforge.notify.telegram import bot

        b = bot()
        if b.token:
            b.send_text(text)
    except Exception as exc:
        log.warning(f"[live] Telegram: {exc}")


def run_agent() -> None:
    cfg = load_config()
    Agent(cfg).run()


# ----------------------------------------------------------------------------- inside the app: keep the agent alive
class LiveManager(threading.Thread):
    """Runs in `clipforge ui` / the service: starts `clipforge live run` while live.enabled (restarts it if it dies,
    stops it when disabled). The agent is a separate process so the web app never slows down and vice versa."""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="live-manager")
        self.proc: subprocess.Popen | None = None
        self.stop_ev = threading.Event()

    def run(self) -> None:
        while not self.stop_ev.wait(15):
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
        exe = sys.executable
        log.info("[live] starting the live agent")
        self.proc = subprocess.Popen([exe, "-m", "clipforge", "live", "run"], creationflags=flags,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop_agent(self, cfg: Config) -> None:
        store.send_command(cfg, "quit")
        try:
            self.proc.wait(30)  # type: ignore[union-attr]
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
