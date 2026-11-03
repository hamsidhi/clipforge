"""Thin, safe wrappers around the ffmpeg / ffprobe executables.

We call ffmpeg through `subprocess` (never moviepy) for speed and full control over filtergraphs.
"""

from __future__ import annotations

import functools
import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Sequence

from clipforge.utils.log import log


class FFmpegError(RuntimeError):
    """ffmpeg/ffprobe failed. The message contains the tail of stderr."""


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    if platform.system() == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        dirs.append(local / "Microsoft" / "WinGet" / "Links")
        pkg_root = local / "Microsoft" / "WinGet" / "Packages"
        if pkg_root.exists():
            dirs.extend(sorted(pkg_root.glob("*FFmpeg*/*/bin"), reverse=True))
        dirs += [Path("C:/ffmpeg/bin"), Path("C:/Program Files/ffmpeg/bin"), Path(os.environ.get("ProgramData", "C:/ProgramData")) / "chocolatey" / "bin"]
        dirs.append(Path.home() / "scoop" / "shims")
    else:
        dirs += [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/usr/bin")]
    return dirs


@functools.lru_cache(maxsize=4)
def _find(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    exe = f"{name}.exe" if platform.system() == "Windows" else name
    for d in _candidate_dirs():
        p = d / exe
        if p.exists():
            # also expose it on PATH: yt-dlp / PySceneDetect look ffmpeg up there themselves
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
            return str(p)
    return None


def ffmpeg_bin() -> str:
    exe = _find("ffmpeg")
    if not exe:
        raise FFmpegError("ffmpeg not found. Run `clipforge doctor` for the install command for your OS.")
    return exe


def ffprobe_bin() -> str:
    exe = _find("ffprobe")
    if not exe:
        raise FFmpegError("ffprobe not found. It ships with ffmpeg — run `clipforge doctor`.")
    return exe


def run_ffmpeg(
    args: Sequence[str],
    *,
    duration: float | None = None,
    on_progress: Callable[[float], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    cwd: Path | None = None,
) -> None:
    """Run ffmpeg with `args` (without the executable). Raises FFmpegError with stderr on failure.

    If `duration` and `on_progress` are given, progress fractions (0..1) are reported live.
    """
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    if on_progress and duration:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += list(args)
    log.debug("ffmpeg " + " ".join(cmd[1:]))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", cwd=cwd,
    )
    stderr_lines: list[str] = []
    import threading

    def _drain() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)

    t = threading.Thread(target=_drain, daemon=True)
    t.start()
    assert proc.stdout is not None
    for line in proc.stdout:
        if cancel and cancel():
            proc.kill()
            proc.wait()
            from clipforge.utils.log import CancelledError

            raise CancelledError("Job cancelled by user")
        if on_progress and duration and line.startswith("out_time_us="):
            try:
                us = int(line.split("=", 1)[1])
                on_progress(min(1.0, us / 1e6 / duration))
            except ValueError:
                pass
    proc.wait()
    t.join(timeout=5)
    if proc.returncode != 0:
        tail = "".join(stderr_lines[-25:]).strip()
        raise FFmpegError(f"ffmpeg failed (exit {proc.returncode}):\n{tail}\n\nCommand: {' '.join(cmd)}")


def ffprobe_json(path: Path | str) -> dict:
    cmd = [ffprobe_bin(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise FFmpegError(f"ffprobe could not read {path}: {res.stderr.strip()}")
    return json.loads(res.stdout)


def probe_video(path: Path | str) -> dict:
    """Return {duration, width, height, fps, has_audio, vcodec, acodec}."""
    data = ffprobe_json(path)
    v = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in data["streams"] if s.get("codec_type") == "audio"), None)
    if v is None:
        raise FFmpegError(f"{path} has no video stream")
    fps = 0.0
    rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1"
    try:
        num, den = rate.split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        pass
    duration = float(data.get("format", {}).get("duration") or v.get("duration") or 0)
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = abs(int(sd["rotation"]))
    if rot in (90, 270):
        w, h = h, w
    return {
        "duration": duration, "width": w, "height": h, "fps": fps,
        "has_audio": a is not None, "vcodec": v.get("codec_name"), "acodec": a.get("codec_name") if a else None,
    }


def escape_filter_path(path: Path | str) -> str:
    """Escape a file path for use INSIDE an ffmpeg filtergraph option value.

    Windows `C:\\x\\y.ass` -> `C\\:/x/y.ass`. Quotes/commas/brackets are escaped too so the value can be
    wrapped in single quotes in the filter string.
    """
    p = str(Path(path).resolve()).replace("\\", "/")
    p = p.replace(":", "\\:").replace("'", "\\'")
    return p


def measure_loudness(src: Path | str, start: float, duration: float, target: float = -14.0) -> dict | None:
    """First loudnorm pass on [start, start+duration]. Returns the measured_* values or None."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src),
           "-vn", "-af", f"loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", res.stderr, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        if data.get("input_i") in ("-inf", "inf") or float(data["input_i"]) < -70:
            return None  # silence: nothing to normalise
        return data
    except (ValueError, KeyError):
        return None


def extract_frame(src: Path, t: float, out: Path, width: int | None = None) -> Path:
    vf = ["-vf", f"scale={width}:-2"] if width else []
    run_ffmpeg(["-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1", *vf, "-q:v", "3", str(out)])
    return out


def has_encoder(name: str) -> bool:
    try:
        out = subprocess.run([ffmpeg_bin(), "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20).stdout
        return re.search(rf"\b{re.escape(name)}\b", out) is not None
    except Exception:
        return False


def has_filter(name: str) -> bool:
    try:
        out = subprocess.run([ffmpeg_bin(), "-hide_banner", "-filters"], capture_output=True, text=True, timeout=20).stdout
        return re.search(rf"\s{re.escape(name)}\s", out) is not None
    except Exception:
        return False


def version() -> str | None:
    try:
        out = subprocess.run([ffmpeg_bin(), "-version"], capture_output=True, text=True, timeout=20).stdout
        return out.splitlines()[0] if out else None
    except Exception:
        return None


class Timer:
    """Tiny ETA helper: feed it fractions, read `eta`."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()

    def eta(self, fraction: float) -> float | None:
        if fraction <= 0.01:
            return None
        elapsed = time.monotonic() - self.t0
        return max(0.0, elapsed / fraction - elapsed)
