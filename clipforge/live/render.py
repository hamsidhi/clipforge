"""Fast live render: one ffmpeg run per clip, AMD AMF on the iGPU (CPU stays free for the game/OBS), x264 ultrafast at
idle priority as the fallback. Layout 1080×1920: facecam on top (35 %), gameplay below (65 %) — or gameplay only.
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from clipforge.live.governor import make_low_priority, popen_kwargs
from clipforge.utils.ffmpeg import FFmpegError, ffmpeg_bin
from clipforge.utils.log import log

W, H = 1080, 1920
TOP_H = 672            # 35 % facecam
BOT_H = H - TOP_H      # 65 % gameplay


def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


@dataclass
class Layout:
    src_w: int
    src_h: int
    facecam: tuple[int, int, int, int] | None = None   # x, y, w, h in source pixels
    gameplay_only: str = "fit_blur"                     # crop | fit_blur (used when there's no facecam)
    focus_x: float = 0.5                                # gameplay centre (crosshair = 0.5)

    @staticmethod
    def scale_rect(rect: tuple[int, int, int, int] | None, canvas: tuple[int, int], src: tuple[int, int]):
        """OBS canvas coordinates → coordinates in the saved file (the output may be scaled)."""
        if not rect:
            return None
        sx, sy = src[0] / max(1, canvas[0]), src[1] / max(1, canvas[1])
        x, y, w, h = rect
        return int(x * sx), int(y * sy), int(w * sx), int(h * sy)


def _crop_to(aspect: float, x: int, y: int, w: int, h: int, focus_x: float = 0.5) -> tuple[int, int, int, int]:
    """Largest crop of the rect with the given aspect (w/h), centred (horizontally on focus_x)."""
    if w / h > aspect:
        cw, ch = _even(h * aspect), _even(h)
        cx = x + int((w - cw) * focus_x)
        return cw, ch, cx, y
    cw, ch = _even(w), _even(w / aspect)
    return cw, ch, x, y + (h - ch) // 2


def video_graph(lay: Layout, ass_name: str | None = None, fonts_dir: str | None = None, src: str = "0:v") -> str:
    """filter_complex producing [vout] (1080×1920)."""
    sw, sh = lay.src_w, lay.src_h
    parts: list[str] = []
    if lay.facecam:
        fx, fy, fw, fh = lay.facecam
        cw, ch, cx, cy = _crop_to(W / TOP_H, fx, fy, fw, fh)
        gw, gh, gx, gy = _crop_to(W / BOT_H, 0, 0, sw, sh, lay.focus_x)
        parts.append(f"[{src}]split=2[fc][gp]")
        parts.append(f"[fc]crop={cw}:{ch}:{cx}:{cy},scale={W}:{TOP_H},setsar=1[top]")
        parts.append(f"[gp]crop={gw}:{gh}:{gx}:{gy},scale={W}:{BOT_H},setsar=1[bot]")
        parts.append("[top][bot]vstack=inputs=2[v0]")
    elif lay.gameplay_only == "crop":
        gw, gh, gx, gy = _crop_to(W / H, 0, 0, sw, sh, lay.focus_x)
        parts.append(f"[{src}]crop={gw}:{gh}:{gx}:{gy},scale={W}:{H},setsar=1[v0]")
    else:
        parts.append(f"[{src}]split=2[bg][fg]")
        parts.append(f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:2,eq=brightness=-0.08[bgb]")
        parts.append(f"[fg]scale={W}:-2,setsar=1[fgs]")
        parts.append("[bgb][fgs]overlay=(W-w)/2:(H-h)/2[v0]")
    if ass_name:
        fd = f":fontsdir='{fonts_dir}'" if fonts_dir else ""
        parts.append(f"[v0]ass=filename='{ass_name}'{fd}[vout]")
    else:
        parts.append("[v0]null[vout]")
    return ";".join(parts)


# ----------------------------------------------------------------------------- encoders
_amf_ok: bool | None = None
_amf_lock = threading.Lock()


def amf_available() -> bool:
    """Does h264_amf actually encode on this machine (driver present)? Probed once with a 0.2 s test."""
    global _amf_ok
    with _amf_lock:
        if _amf_ok is None:
            try:
                r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                    "color=c=black:s=256x256:d=0.2", "-c:v", "h264_amf", "-f", "null", "-"],
                                   capture_output=True, text=True, timeout=30, **popen_kwargs())
                _amf_ok = r.returncode == 0
                if not _amf_ok:
                    log.info(f"[live] AMD AMF not usable: {r.stderr.strip()[-200:]}")
            except Exception as exc:
                log.info(f"[live] AMD AMF probe failed: {exc}")
                _amf_ok = False
        return _amf_ok


def encoder_args(choice: str = "auto", threads: int = 2) -> tuple[str, list[str]]:
    if choice in ("auto", "amf") and amf_available():
        return "amf", ["-c:v", "h264_amf", "-usage", "transcoding", "-quality", "speed", "-rc", "cqp",
                       "-qp_i", "21", "-qp_p", "23", "-qp_b", "25", "-pix_fmt", "yuv420p"]
    return "x264", ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", str(threads),
                    "-pix_fmt", "yuv420p"]


@dataclass
class RenderStats:
    seconds: float
    encoder: str
    cpu_avg: float          # total system CPU % while rendering
    proc_cpu: float         # ffmpeg's own CPU (% of one core, averaged)


def render(src: Path, start: float, duration: float, lay: Layout, out: Path, *, ass: Path | None = None,
           fonts_dir: Path | None = None, encoder: str = "auto", max_cores: int = 2,
           stop: threading.Event | None = None, hw_decode: bool | None = None) -> RenderStats:
    """Cut [start, start+duration] of `src` and render the vertical clip. Runs at idle priority on our cores.
    With AMF, decoding runs on the iGPU too (d3d11va): ~2.4× less CPU than software decoding (measured)."""
    import sys
    import psutil

    src, out = Path(src).resolve(), Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    cwd = out.parent
    ass_name = None
    fonts_rel = None
    if ass is not None:
        import os

        ass_name = os.path.relpath(ass, cwd).replace("\\", "/")
        if fonts_dir is not None:
            try:
                fonts_rel = os.path.relpath(fonts_dir, cwd).replace("\\", "/")
            except ValueError:
                fonts_rel = None
    graph = video_graph(lay, ass_name, fonts_rel)
    enc, vargs = encoder_args(encoder, threads=max_cores)
    tmp = out.with_suffix(".part.mp4")
    if hw_decode is None:
        hw_decode = enc == "amf" and sys.platform == "win32"
    hw = ["-hwaccel", "d3d11va"] if hw_decode else []
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-filter_complex_threads", "1", *hw,
           "-ss", f"{max(0.0, start):.3f}", "-t", f"{duration:.3f}", "-i", str(src),
           "-filter_complex", graph, "-map", "[vout]", "-map", "0:a:0?",
           "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-ar", "48000",
           *vargs, "-r", "30", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(tmp)]
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", **popen_kwargs())
    make_low_priority(proc.pid, max_cores, idle=True)
    sys_cpu: list[float] = []
    proc_cpu: list[float] = []
    try:
        pp = psutil.Process(proc.pid)
        pp.cpu_percent(None)
    except Exception:
        pp = None
    psutil.cpu_percent(None)
    while proc.poll() is None:
        if stop is not None and stop.is_set():
            proc.kill()
            proc.wait()
            raise FFmpegError("render stopped (the governor or you paused live work)")
        time.sleep(0.5)
        sys_cpu.append(psutil.cpu_percent(None))
        if pp is not None:
            try:
                proc_cpu.append(pp.cpu_percent(None))
            except Exception:
                pass
    err = proc.stderr.read() if proc.stderr else ""
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        if hw_decode:  # no GPU decoder for this file: decode on the CPU
            log.warning(f"[live] GPU decode failed, decoding on the CPU: {err.strip()[-200:]}")
            return render(src, start, duration, lay, out, ass=ass, fonts_dir=fonts_dir, encoder=encoder,
                          max_cores=max_cores, stop=stop, hw_decode=False)
        if enc == "amf" and encoder == "auto":  # driver hiccup mid-stream: fall back once
            log.warning(f"[live] AMF render failed, retrying with x264: {err.strip()[-300:]}")
            return render(src, start, duration, lay, out, ass=ass, fonts_dir=fonts_dir, encoder="x264",
                          max_cores=max_cores, stop=stop)
        raise FFmpegError(f"live render failed: {err.strip()[-600:]}")
    tmp.replace(out)
    return RenderStats(round(time.monotonic() - t0, 2), enc, round(sum(sys_cpu) / max(1, len(sys_cpu)), 1),
                       round(sum(proc_cpu) / max(1, len(proc_cpu)), 1))


def cut_copy(src: Path, start: float, duration: float, out: Path) -> Path:
    """Stream-copy a section (keyframe-aligned, no re-encode) — for the best-of and HLS segments."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-ss", f"{max(0.0, start):.3f}",
           "-t", f"{duration:.3f}", "-i", str(src), "-c", "copy", "-avoid_negative_ts", "make_zero", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, **popen_kwargs())
    if r.returncode != 0:
        raise FFmpegError(f"cut failed: {r.stderr.strip()[-400:]}")
    return out


def thumb(clip: Path, out: Path, t: float = 1.0) -> Path | None:
    r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i",
                        str(clip), "-frames:v", "1", "-vf", "scale=360:-2", str(out)], capture_output=True,
                       **popen_kwargs())
    return out if r.returncode == 0 and out.exists() else None
