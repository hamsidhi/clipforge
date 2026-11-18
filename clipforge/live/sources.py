"""Footage + cheap signals from a live stream link (YouTube / Twitch / Kick / anything yt-dlp reads).

HlsSource     ffmpeg pulls the stream at ≤ `stream_height` with `-c copy` (no re-encode) into 10-s .ts segments named by
              wall time (the last `hls_buffer_min` minutes are kept). A moment = the segments around it joined by stream
              copy (waits until the segment holding the moment's end is closed).
SegmentReader each CLOSED segment is read once: audio loudness 5×/s (mono 8 kHz decode — cheap) and keyframes only
              (`-skip_frame nokey`, 640 px) for the game HUD + facecam detection → callbacks with session times.
FileSource    a recorded file played as if live (`clipforge live simulate`).
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from clipforge.live.governor import make_low_priority, popen_kwargs
from clipforge.live.scorer import Moment
from clipforge.utils.ffmpeg import ffmpeg_bin, probe_video
from clipforge.utils.log import log

SEG_SEC = 10


class SourceError(RuntimeError):
    pass


def _duration(path: Path) -> float:
    try:
        return float(probe_video(path).get("duration") or 0)
    except Exception:
        return 0.0


def _ytdlp(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "yt_dlp", "--no-warnings", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, **popen_kwargs())


def platform_of(url: str) -> str:
    u = url.lower()
    if "twitch.tv" in u:
        return "twitch"
    if "kick.com" in u:
        return "kick"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    return "other"


def channel_of(url: str) -> str:
    """twitch.tv/name, kick.com/name → name (chat channel)."""
    path = url.split("://", 1)[-1].split("?", 1)[0].rstrip("/").split("/")
    return path[1].lower() if len(path) > 1 else ""


def live_url(url: str) -> str:
    """A YouTube channel link → its /live page (the current broadcast)."""
    u = url.strip().rstrip("/")
    if platform_of(u) == "youtube" and ("/@" in u or "/channel/" in u or "/c/" in u) and not u.endswith("/live"):
        u += "/live"
    return u


@dataclass
class StreamInfo:
    url: str
    is_live: bool
    title: str = ""
    streamer: str = ""
    platform: str = "other"
    video_id: str = ""
    categories: str = ""


def stream_info(url: str) -> StreamInfo:
    """yt-dlp metadata without downloading (no API quota)."""
    u = live_url(url)
    try:
        r = _ytdlp(["--skip-download", "-J", u], timeout=90)
        d = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
    except Exception as exc:
        log.warning(f"[live] couldn't read {url}: {exc}")
        d = {}
    if d.get("_type") == "playlist" and d.get("entries"):
        d = next((e for e in d["entries"] if e and e.get("is_live")), d["entries"][0] or {})
    cats = d.get("categories") or []
    if d.get("genre"):
        cats.append(d["genre"])
    return StreamInfo(url=u, is_live=bool(d.get("is_live")), title=str(d.get("title") or "")[:200],
                      streamer=str(d.get("uploader") or d.get("channel") or channel_of(u) or ""),
                      platform=platform_of(u), video_id=str(d.get("id") or ""),
                      categories=", ".join(str(c) for c in cats))


def is_live(url: str) -> bool:
    try:
        r = _ytdlp(["--skip-download", "--print", "is_live", live_url(url)])
        return r.returncode == 0 and r.stdout.strip().splitlines()[-1:] == ["True"]
    except Exception as exc:
        log.warning(f"[live] live check failed for {url}: {exc}")
        return False


def seg_time(f: Path) -> float | None:
    try:
        return dt.datetime.strptime(f.stem, "%Y%m%d_%H%M%S").timestamp()
    except ValueError:
        return None


class FileSource:
    kind = "file"

    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, m: Moment, now: float) -> tuple[Path, float, float]:
        return self.path, m.start, m.duration


class HlsSource:
    kind = "hls"

    def __init__(self, url: str, buf_dir: Path, keep_min: int = 5, max_cores: int = 2, height: int = 720) -> None:
        self.url, self.dir, self.keep_sec = live_url(url), buf_dir, keep_min * 60
        self.max_cores, self.height = max_cores, height
        self.proc: subprocess.Popen | None = None
        self.t0_wall = 0.0  # wall time at session clock 0
        self.stop_ev = threading.Event()
        self.restarts = 0

    def media_urls(self) -> list[str]:
        """1 URL (video+audio) or 2 (YouTube sometimes only offers separate video and audio streams)."""
        h = self.height
        r = _ytdlp(["-g", "-f", f"best[height<={h}]/bv*[height<={h}]+ba/best/bv*+ba", self.url], timeout=90)
        if r.returncode != 0 or not r.stdout.strip():
            raise SourceError(f"Couldn't open the live stream: {(r.stderr or 'not live?').strip()[-300:]}")
        return r.stdout.strip().splitlines()[:2]

    def media_url(self) -> str:
        return self.media_urls()[0]

    def start(self, t0_wall: float) -> None:
        self.t0_wall = t0_wall
        self.dir.mkdir(parents=True, exist_ok=True)
        self._spawn()
        threading.Thread(target=self._keep_loop, daemon=True, name="live-hls-keep").start()

    def _spawn(self) -> None:
        urls = self.media_urls()
        inputs: list[str] = []
        for u in urls:
            inputs += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10", "-i", u]
        audio = f"{len(urls) - 1}:a:0?"
        cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error", *inputs, "-map", "0:v:0?", "-map", audio,
               "-c", "copy", "-f", "segment", "-segment_time", str(SEG_SEC), "-reset_timestamps", "1", "-strftime", "1",
               str(self.dir / "%Y%m%d_%H%M%S.ts")]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **popen_kwargs())
        make_low_priority(self.proc.pid, self.max_cores)

    def _keep_loop(self) -> None:
        """Prune old segments; restart the puller when the media URL expires (YouTube/Twitch URLs do)."""
        while not self.stop_ev.wait(15):
            cutoff = time.time() - self.keep_sec
            for f in self.dir.glob("*.ts"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:
                    pass
            if self.proc is not None and self.proc.poll() is not None and not self.stop_ev.is_set():
                if self.restarts >= 5:
                    continue
                self.restarts += 1
                log.info(f"[live] stream puller stopped — reconnecting ({self.restarts}/5)")
                try:
                    self._spawn()
                except Exception as exc:
                    log.warning(f"[live] reconnect failed: {exc}")

    @property
    def dead(self) -> bool:
        return self.proc is not None and self.proc.poll() is not None and self.restarts >= 5

    def stop(self) -> None:
        self.stop_ev.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def closed_segments(self) -> list[tuple[float, Path]]:
        segs = sorted((t, f) for f in self.dir.glob("*.ts") if (t := seg_time(f)) is not None)
        return segs[:-1]  # the newest one is still being written

    def save(self, m: Moment, now: float, wait: float = 40) -> tuple[Path, float, float]:
        a_wall, b_wall = self.t0_wall + m.start, self.t0_wall + m.end
        deadline = time.time() + wait
        while True:  # wait until the segment holding the moment's end is closed
            segs = self.closed_segments()
            if segs and segs[-1][0] + SEG_SEC >= b_wall:
                break
            if time.time() > deadline or self.stop_ev.is_set():
                break
            time.sleep(1)
        use = [(t, f) for t, f in segs if t + SEG_SEC >= a_wall and t <= b_wall]
        if not use:
            raise SourceError("No stream footage for this moment (the stream dropped?)")
        lst = self.dir / f"moment_{m.id}.txt"
        lst.write_text("".join(f"file '{f.name}'\n" for _, f in use), encoding="utf-8")
        out = self.dir.parent / f"moment_{m.id}.ts"
        r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-f", "concat",
                            "-safe", "0", "-i", lst.name, "-c", "copy", str(out)], cwd=self.dir, capture_output=True,
                           text=True, **popen_kwargs())
        lst.unlink(missing_ok=True)
        if r.returncode != 0:
            raise SourceError(f"Couldn't join the stream segments: {r.stderr.strip()[-300:]}")
        start = max(0.0, a_wall - use[0][0])
        return out, start, min(m.duration, b_wall - a_wall)


# ----------------------------------------------------------------------------- signals from closed segments
def segment_levels(f: Path, rate: int = 5) -> np.ndarray:
    r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(f), "-vn", "-ac", "1",
                        "-ar", "8000", "-f", "s16le", "-"], capture_output=True, **popen_kwargs())
    a = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768
    n = 8000 // rate
    if a.size < n:
        return np.array([], dtype=np.float32)
    a = a[: a.size // n * n].reshape(-1, n)
    return 20 * np.log10(np.maximum(np.sqrt((a ** 2).mean(axis=1)), 1e-6))


def segment_keyframes(f: Path, width: int = 640) -> list[tuple[float, np.ndarray]]:
    """Keyframes only (no full decode) as small BGR frames, with their offset in the segment."""
    import cv2

    out_dir = f.parent / "_kf"
    out_dir.mkdir(exist_ok=True)
    for old in out_dir.glob(f"{f.stem}_*.jpg"):
        old.unlink(missing_ok=True)
    r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error", "-skip_frame", "nokey", "-i",
                        str(f), "-vf", f"scale={width}:-2", "-fps_mode", "passthrough", "-frame_pts", "1", "-q:v", "5",
                        str(out_dir / f"{f.stem}_%d.jpg")], capture_output=True, **popen_kwargs())
    if r.returncode != 0:
        return []
    frames = []
    files = sorted(out_dir.glob(f"{f.stem}_*.jpg"), key=lambda p: int(p.stem.rsplit("_", 1)[1]))
    for i, p in enumerate(files):
        img = cv2.imread(str(p))
        p.unlink(missing_ok=True)
        if img is not None:
            frames.append((min(SEG_SEC - 0.1, i * SEG_SEC / max(1, len(files))), img))
    return frames


class SegmentReader(threading.Thread):
    """Reads each closed segment once → on_audio(t, db) and on_frame(t, frame) with session times."""

    def __init__(self, src: HlsSource, on_audio: Callable[[float, float], None],
                 on_frame: Callable[[float, np.ndarray], None], allow: Callable[[], bool]) -> None:
        super().__init__(daemon=True, name="live-segments")
        self.src, self.on_audio, self.on_frame, self.allow = src, on_audio, on_frame, allow
        self.done: set[str] = set()
        self.stop_ev = threading.Event()
        self.last_seg_at = time.time()

    def run(self) -> None:
        while not self.stop_ev.wait(1):
            for t_wall, f in self.src.closed_segments():
                if f.name in self.done:
                    continue
                self.done.add(f.name)
                self.last_seg_at = time.time()
                base = t_wall - self.src.t0_wall
                try:
                    for i, db in enumerate(segment_levels(f)):
                        self.on_audio(base + i / 5, float(db))
                    if self.allow():
                        for off, frame in segment_keyframes(f):
                            self.on_frame(base + off, frame)
                except Exception as exc:
                    log.warning(f"[live] segment {f.name}: {exc}")

    def stop(self) -> None:
        self.stop_ev.set()
