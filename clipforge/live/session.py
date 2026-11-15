"""One watched live stream = one LiveEngine: signals → (re-ordered by time) → scorer → save footage → render queue
(when the governor allows) → clip in workspace/live_<…>/ → Telegram draft with buttons → optional auto-post.

The engine is clock-driven (`clock()` = seconds since we started watching) so `simulate.py` can replay a file faster
than real time with the same code. Signals arrive late and out of order (chat is instant, audio/video come from closed
10-s segments): they wait `lag` seconds in a buffer and reach the scorer in time order.
"""

from __future__ import annotations

import datetime as dt
import queue
import threading
import time
from pathlib import Path
from typing import Callable

from clipforge import pipeline
from clipforge.config import FONTS_DIR, Config
from clipforge.live import store
from clipforge.live.audio import HypeDetector
from clipforge.live.chat import ChatMsg, ChatSpike
from clipforge.live.facecam import FaceCam
from clipforge.live.games import GamePack, hashtags_for, title_for
from clipforge.live.governor import Governor
from clipforge.live.hud import HudWatcher, Ocr
from clipforge.live.render import Layout, render, thumb
from clipforge.live.scorer import Moment, MomentScorer, ScorerConfig, Signal
from clipforge.models import ClipCandidate, VideoInfo
from clipforge.utils.log import log
from clipforge.workspace import Workspace


class LiveEngine:
    def __init__(self, cfg: Config, *, source, pack: GamePack, clock: Callable[[], float], layout: Layout,
                 stream_url: str = "", sid: str | None = None, governor: Governor | None = None,
                 notify: bool = True, title: str = "", simulate: bool = False, streamer: str = "",
                 platform: str = "", lag: float = 0.0, detect_cam: bool = False) -> None:
        self.cfg, self.source, self.pack, self.clock, self.layout = cfg, source, pack, clock, layout
        self.sid = sid or store.session_id()
        self.notify = notify
        self.simulate = simulate
        self.ws = Workspace(cfg.workspace, self.sid)
        self.gov = governor or Governor(cpu_ceiling=cfg.live.cpu_ceiling)
        self.scorer = MomentScorer(ScorerConfig.from_live(cfg.live, pack))
        self.audio = HypeDetector("audio", note="loud moment")
        self.chat = ChatSpike(cfg.live.chat.hype_words, [])
        self.hud = HudWatcher(pack, Ocr())
        self.cam = FaceCam() if detect_cam else None
        self.streamer, self.platform, self.stream_url, self.title = streamer, platform, stream_url, title
        self.lag = lag
        self.pending: list[Signal] = []
        self._plock = threading.Lock()
        self.paused = False
        self.auto_post = cfg.live.auto_post
        self.stop_ev = threading.Event()
        self.render_q: queue.Queue = queue.Queue()
        self.moments: list[dict] = []
        self.rank = 0
        self.busy = 0
        self.started_wall = time.time()
        self.stats: list[dict] = []
        info = VideoInfo(id=self.sid, title=title or f"Live {dt.datetime.now():%d %b %H:%M} · {pack.label}",
                         channel=streamer or "live", url=stream_url, source_path="")
        self.ws.write_model(self.ws.info, info)
        if not self.ws.candidates.exists():
            pipeline.save_candidates(self.ws, [])
        sess = store.load_session(cfg, self.sid) or {
            "id": self.sid, "started_at": dt.datetime.now().isoformat(timespec="seconds"), "stream_url": stream_url,
            "vod_url": "", "live": not simulate, "simulated": simulate, "game": pack.name, "game_label": pack.label,
            "source": getattr(source, "kind", "?"), "clips": [], "last_post": {}, "streamer": streamer,
            "platform": platform, "title": title}
        store.save_session(cfg, self.sid, sess)
        self.worker = threading.Thread(target=self._worker, daemon=True, name="live-render")
        self.worker.start()

    # ------------------------------------------------------------------ input
    def signal(self, sig: Signal) -> Moment | None:
        if sig.kind != "manual" and (self.paused or not self.gov.allow_detectors()):
            return None
        if self.lag > 0:
            with self._plock:
                self.pending.append(sig)
            return None
        return self._score(sig)

    def _score(self, sig: Signal) -> Moment | None:
        m = self.scorer.add(sig)
        if m:
            log.info(f"[live] moment #{m.id} at {m.trigger:.0f}s score {m.score} — {m.reason_text()}")
        return m

    def mark(self, note: str = "", source: str = "manual") -> None:
        self.signal(Signal(self.clock(), "manual", 1.0, f"marked ({source})" + (f": {note[:60]}" if note else ""),
                           {"note": note}))

    def audio_level(self, t: float, db: float) -> None:
        s = self.audio.feed(t, db)
        if s:
            self.signal(s)

    def chat_msg(self, msg: ChatMsg) -> None:
        for s in self.chat.feed(msg):
            self.signal(s)

    def frame(self, t: float, frame) -> None:
        if self.cam is not None:
            dec = self.cam.feed(t, frame)
            if dec is not None:
                self.apply_cam(dec)
        if self.paused or not self.gov.allow_detectors():
            return
        for s in self.hud.feed(t, frame):
            self.signal(s)

    def apply_cam(self, dec) -> None:
        lay = self.layout
        if dec.kind == "facecam" and dec.rect:
            x, y, w, h = dec.rect
            lay.facecam = (int(x * lay.src_w), int(y * lay.src_h), int(w * lay.src_w), int(h * lay.src_h))
        else:
            lay.facecam = None
            lay.gameplay_only = "crop" if dec.kind == "irl" else self.pack.gameplay_only
            lay.focus_x = dec.focus_x if dec.kind == "irl" else 0.5
        log.info(f"[live] {self.streamer or self.sid}: camera → {dec.kind} {lay.facecam or ''}")

    def command(self, c: dict) -> None:
        cmd = c.get("cmd")
        if cmd == "clip":
            self.mark(c.get("note", ""), c.get("source", "app"))
        elif cmd == "pause":
            self.paused = True
        elif cmd == "resume":
            self.paused = False
        elif cmd == "auto":
            self.auto_post = bool(c.get("on"))
        elif cmd == "stop":
            self.stop_ev.set()

    # ------------------------------------------------------------------ time
    def tick(self) -> None:
        """Call often (≥ 2×/s live). Feeds signals older than `lag` in time order, then saves the footage of moments
        whose post-roll has passed (on the lagged clock)."""
        now = self.clock() - self.lag
        if self.lag > 0:
            with self._plock:
                ready = sorted((s for s in self.pending if s.t <= now), key=lambda s: s.t)
                self.pending = [s for s in self.pending if s.t > now]
            for s in ready:
                self._score(s)
        for m in self.scorer.due(now):
            self._save(m)

    def _save(self, m: Moment) -> None:
        now = self.clock()
        rec = {"id": m.id, "t": round(m.trigger, 1), "score": m.score, "reason": m.reason_text(), "manual": m.manual,
               "event": m.event, "state": "saving"}
        self.moments.append(rec)
        try:
            path, start, dur = self.source.save(m, now)
        except Exception as exc:
            rec["state"] = f"footage failed: {str(exc)[:120]}"
            log.warning(f"[live] moment #{m.id}: {exc}")
            return
        rec["state"] = "queued"
        self.render_q.put((m, Path(path), start, dur, rec))

    # ------------------------------------------------------------------ render worker
    def _worker(self) -> None:
        while True:
            item = self.render_q.get()
            if item is None:
                break
            m, path, start, dur, rec = item
            while not self.gov.allow_render() and not self.stop_ev.is_set():
                rec["state"] = "waiting (CPU busy)"
                time.sleep(1)
                if self.gov_owner_idle():
                    self.gov.sample()  # nobody else samples (stream over / simulation done)
            self.busy += 1
            try:
                self._make_clip(m, path, start, dur, rec)
            except Exception as exc:
                rec["state"] = f"render failed: {str(exc)[:160]}"
                log.error(f"[live] clip for moment #{m.id} failed: {exc}")
            finally:
                self.busy -= 1
                self.render_q.task_done()

    def _make_clip(self, m: Moment, src: Path, start: float, dur: float, rec: dict) -> None:
        from clipforge.live import captions as cap

        t0 = time.monotonic()
        self.rank += 1
        rank = self.rank
        n_kills = max((s for s in [0] + [int(r.split(" ")[2]) for r in m.reasons if r.startswith("kill feed:")]), default=0)
        title = title_for(self.pack, m.event or ("manual" if m.manual else ""), m.kinds, f"{self.sid}:{rank}", n_kills,
                          streamer=self.streamer)
        tags = hashtags_for(self.pack, [self.streamer] if self.streamer else None)
        work = self.ws.work_dir(rank)
        out = self.ws.clip_path(rank, title)
        ass = None
        cap_note = "no captions"
        rec["state"] = "rendering"
        if self.cfg.live.captions and self.gov.allow_render():
            try:
                words = cap.words(src, start, dur, self.cfg)
                ass = cap.write_ass(words, dur, title, self.cfg, work / "captions.ass")
                cap_note = f"{len(words)} words captioned"
            except Exception as exc:
                log.warning(f"[live] captions skipped for #{rank}: {exc}")
                cap_note = "captions skipped (add later)"
        rs = render(src, start, dur, self.layout, out, ass=ass, fonts_dir=FONTS_DIR, encoder=self.cfg.live.encoder,
                    max_cores=self.cfg.live.max_cores, stop=self.stop_ev if not self.simulate else None)
        thumb(out, work / "thumb.jpg")
        cand = ClipCandidate(rank=rank, start=round(m.start, 2), end=round(m.start + dur, 2), title=title, hook=title,
                             topic=m.event or "live", score=m.score, why=m.reason_text())
        with pipeline.candidates_lock(self.sid):
            cands = [c for c in pipeline.load_candidates(self.ws) if c.rank != rank] + [cand]
            pipeline.save_candidates(self.ws, sorted(cands, key=lambda c: c.rank))
        total = round(time.monotonic() - t0, 1)
        clip = {"rank": rank, "moment": m.id, "trigger": round(m.trigger, 1), "score": m.score, "reason": m.reason_text(),
                "manual": m.manual, "event": m.event, "title": title, "hashtags": tags, "file": out.name,
                "duration": round(dur, 1), "status": "ready", "captions": cap_note, "posted": {},
                "source": str(src), "source_start": round(start, 2),
                "render": {"seconds": rs.seconds, "encoder": rs.encoder, "cpu_avg": rs.cpu_avg, "proc_cpu": rs.proc_cpu,
                           "total_seconds": total},
                "created_at": dt.datetime.now().isoformat(timespec="seconds")}
        sess = store.load_session(self.cfg, self.sid)
        sess.setdefault("clips", []).append(clip)
        store.save_session(self.cfg, self.sid, sess)
        self.stats.append(clip)
        rec["state"], rec["clip"] = "ready", rank
        log.info(f"[live] clip #{rank} ready in {total}s ({rs.encoder}, CPU {rs.cpu_avg}%): {title}")
        if self.notify:
            try:
                from clipforge.notify import tg_live

                tg_live.notify_clip(self.cfg, self.sid, clip)
            except Exception as exc:
                log.warning(f"[live] Telegram notify failed: {exc}")
        if self.auto_post and (m.manual or "game_event" in m.kinds) and m.score >= self.cfg.live.auto_post_min_score:
            try:
                from clipforge.live.publish import post_clip

                lines = post_clip(self.cfg, self.sid, rank, "now")
                log.info(f"[live] auto-posted #{rank}: {'; '.join(lines)}")
            except Exception as exc:
                log.warning(f"[live] auto-post of #{rank} failed: {exc}")

    def gov_owner_idle(self) -> bool:
        return self._finishing

    _finishing = False

    # ------------------------------------------------------------------ status / end
    def status(self) -> dict:
        cam = self.cam.decision.kind if self.cam and self.cam.decision else ("facecam" if self.layout.facecam else "checking")
        return {"session": self.sid, "game": self.pack.name, "game_label": self.pack.label, "streamer": self.streamer,
                "platform": self.platform, "url": self.stream_url, "title": self.title, "camera": cam,
                "facecam": bool(self.layout.facecam), "paused": self.paused, "auto_post": self.auto_post,
                "clock": round(self.clock(), 1),
                "meters": {"audio": round(self.audio.level, 1),
                           "chat_rate": self.chat.rate, "score": round(self.scorer.last_score, 2),
                           "threshold": round(self.scorer.threshold(), 2)},
                "governor": {"state": self.gov.state, "cpu": round(self.gov.cpu, 1), "reason": self.gov.reason},
                "hud_events": self.hud.last_events, "moments": self.moments[-30:], "rendering": self.busy,
                "queued": self.render_q.qsize(), "clips_made": len(self.stats)}

    def finish(self, wait: float = 900) -> dict:
        """Stream over: save the open moment, finish rendering, swap LIVE → VOD lines, return the recap."""
        self._finishing = True
        if self.lag > 0:
            with self._plock:
                ready, self.pending = sorted(self.pending, key=lambda s: s.t), []
            for s in ready:
                self._score(s)
        for m in self.scorer.flush():
            self._save(m)
        deadline = time.monotonic() + wait
        while (self.render_q.unfinished_tasks or self.busy) and time.monotonic() < deadline:
            time.sleep(0.5)
        self.render_q.put(None)
        from clipforge.live import publish as lp

        sess = store.load_session(self.cfg, self.sid)
        changed = 0
        if not self.simulate:
            try:
                changed = lp.stream_ended(self.cfg, self.sid, lp.vod_url_for(sess.get("stream_url", ""), sess.get("video_id", "")))
            except Exception as exc:
                log.warning(f"[live] VOD caption swap failed: {exc}")
        sess = store.load_session(self.cfg, self.sid)
        sess["live"] = False
        sess["ended_at"] = dt.datetime.now().isoformat(timespec="seconds")
        sess["moments"] = self.moments
        store.save_session(self.cfg, self.sid, sess)
        clips = sess.get("clips", [])
        recap = {"session": self.sid, "moments": len(self.moments), "clips": len(clips),
                 "posted": sum(1 for c in clips if c.get("posted")), "captions_swapped": changed,
                 "top": sorted(clips, key=lambda c: -c.get("score", 0))[:5]}
        if self.notify:
            try:
                from clipforge.notify import tg_live

                tg_live.send_recap(self.cfg, self.sid, recap)
            except Exception as exc:
                log.warning(f"[live] recap notify failed: {exc}")
        return recap
