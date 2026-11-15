"""Where the footage of a moment comes from. Every source answers `save(moment, clock_now)` with
(file, start offset in the file, duration) — the clip is then rendered from that file.

ObsReplaySource  SaveReplayBuffer → the file OBS just wrote (its end = now). No extra encode at all.
HlsSource        your live stream (YouTube/Twitch) pulled at ≤720p with `-c copy` into 10-s segments (last N minutes).
FileSource       a recorded file played as if live (`clipforge live simulate`).
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

from clipforge.live.governor import make_low_priority, popen_kwargs
from clipforge.live.scorer import Moment
from clipforge.utils.ffmpeg import ffmpeg_bin, probe_video
from clipforge.utils.log import log


class SourceError(RuntimeError):
    pass


def _duration(path: Path) -> float:
    try:
        return float(probe_video(path).get("duration") or 0)
    except Exception:
        return 0.0


class FileSource:
    kind = "file"

    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, m: Moment, now: float) -> tuple[Path, float, float]:
        return self.path, m.start, m.duration


class ObsReplaySource:
    kind = "obs"

    def __init__(self, obs) -> None:
        self.obs = obs
        self._saved = threading.Event()
        self._path = ""
        obs.on("ReplayBufferSaved", self._on_saved)

    def _on_saved(self, data: dict) -> None:
        self._path = data.get("savedReplayPath", "")
        self._saved.set()

    def save(self, m: Moment, now: float) -> tuple[Path, float, float]:
        self._saved.clear()
        self.obs.request("SaveReplayBuffer")
        if not self._saved.wait(30):
            try:
                self._path = self.obs.request("GetLastReplayBufferReplay").get("savedReplayPath", "")
            except Exception as exc:
                raise SourceError(f"OBS didn't save the replay: {exc}")
        p = Path(self._path)
        for _ in range(20):  # OBS may still be closing the file
            if p.exists() and p.stat().st_size > 0:
                break
            time.sleep(0.25)
        if not p.exists():
            raise SourceError(f"The replay file is missing: {p}")
        dur = _duration(p)
        if dur <= 0:
            raise SourceError("Couldn't read the replay file's length")
        # the file ends at the save request (`now`): map session time → file time
        start = max(0.0, dur - (now - m.start))
        return p, start, min(m.duration, dur - start)


class HlsSource:
    """Rolling buffer of the live stream in 10-s .ts segments named by wall time; cut by stream copy."""

    kind = "hls"

    def __init__(self, url: str, buf_dir: Path, keep_min: int = 5, max_cores: int = 2) -> None:
        self.url, self.dir, self.keep_sec = url, buf_dir, keep_min * 60
        self.max_cores = max_cores
        self.proc: subprocess.Popen | None = None
        self.t0_wall = 0.0  # wall time at session clock 0
        self.stop_ev = threading.Event()

    def hls_url(self) -> str:
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "-g", "-f", "best[height<=720]/best", "--no-warnings", self.url],
                           capture_output=True, text=True, timeout=60, **popen_kwargs())
        if r.returncode != 0 or not r.stdout.strip():
            raise SourceError(f"Couldn't get the live stream URL: {r.stderr.strip()[-300:]}")
        return r.stdout.strip().splitlines()[0]

    def start(self, t0_wall: float) -> None:
        self.t0_wall = t0_wall
        self.dir.mkdir(parents=True, exist_ok=True)
        url = self.hls_url()
        cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error", "-i", url, "-map", "0:v:0", "-map", "0:a:0?",
               "-c", "copy", "-f", "segment", "-segment_time", "10", "-reset_timestamps", "1", "-strftime", "1",
               str(self.dir / "%Y%m%d_%H%M%S.ts")]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **popen_kwargs())
        make_low_priority(self.proc.pid, self.max_cores)
        threading.Thread(target=self._prune_loop, daemon=True, name="live-hls-prune").start()

    def _prune_loop(self) -> None:
        while not self.stop_ev.wait(20):
            cutoff = time.time() - self.keep_sec
            for f in self.dir.glob("*.ts"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:
                    pass

    def stop(self) -> None:
        self.stop_ev.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def save(self, m: Moment, now: float) -> tuple[Path, float, float]:
        import datetime as dt

        a_wall, b_wall = self.t0_wall + m.start, self.t0_wall + m.end
        segs = []
        for f in sorted(self.dir.glob("*.ts")):
            try:
                t = dt.datetime.strptime(f.stem, "%Y%m%d_%H%M%S").timestamp()
            except ValueError:
                continue
            if t + 10 >= a_wall and t <= b_wall:
                segs.append((t, f))
        if not segs:
            raise SourceError("No stream segments for this moment (the HLS buffer isn't running?)")
        lst = self.dir / f"moment_{m.id}.txt"
        lst.write_text("".join(f"file '{f.name}'\n" for _, f in segs), encoding="utf-8")
        out = self.dir / f"moment_{m.id}.ts"
        r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-f", "concat",
                            "-safe", "0", "-i", lst.name, "-c", "copy", out.name], cwd=self.dir, capture_output=True,
                           text=True, **popen_kwargs())
        if r.returncode != 0:
            raise SourceError(f"Couldn't join the stream segments: {r.stderr.strip()[-300:]}")
        start = max(0.0, a_wall - segs[0][0])
        return out, start, m.duration


def is_live(url: str) -> bool:
    """Is this channel live right now? (yt-dlp, no API quota). YouTube: <channel>/live; Twitch: the channel URL."""
    u = url.rstrip("/")
    if "youtube.com" in u and not u.endswith("/live") and "watch?" not in u:
        u += "/live"
    try:
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "--no-warnings", "--skip-download", "--print", "is_live", u],
                           capture_output=True, text=True, timeout=60, **popen_kwargs())
        return r.returncode == 0 and r.stdout.strip().splitlines()[-1:] == ["True"]
    except Exception as exc:
        log.warning(f"[live] live check failed for {url}: {exc}")
        return False
