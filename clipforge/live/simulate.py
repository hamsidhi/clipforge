"""`clipforge live simulate <file>`: replay a recorded stream as if it were live — same engine, same detectors, real
renders — and report moments, render times and CPU. `make_sample()` builds a synthetic BGMI-like test stream
(facecam box, kill-feed lines, a "WINNER WINNER" banner and loud reactions) when you have no gameplay file yet.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import numpy as np

from clipforge.config import FONTS_DIR, Config
from clipforge.live.games import detect_pack
from clipforge.live.governor import Governor, popen_kwargs
from clipforge.live.render import Layout
from clipforge.live.scorer import Signal
from clipforge.live.session import LiveEngine
from clipforge.live.sources import FileSource
from clipforge.utils.ffmpeg import ffmpeg_bin, probe_video
from clipforge.utils.log import log

SAMPLE_FACECAM = (40, 770, 480, 270)


def make_sample(out: Path, seconds: int = 150) -> tuple[Path, tuple[int, int, int, int]]:
    """Synthetic 1080p30 'BGMI' stream: dark noisy game, facecam bottom-left, 3 kills at ~39-42 s with a loud
    reaction, the Chicken Dinner banner at 95.5 s with a reaction, and a lone loud moment at 130 s."""
    out.parent.mkdir(parents=True, exist_ok=True)
    font = "Montserrat-ExtraBold.ttf"

    def dt(text: str, a: float, b: float, y: int, size: int, color: str) -> str:
        return (f"drawtext=fontfile={font}:text='{text}':fontsize={size}:fontcolor={color}:x=(w-text_w)/2:y={y}:"
                f"enable='between(t,{a},{b})'")
    vf = ",".join([
        dt("You knocked out Enemy1", 38.5, 39.8, 680, 44, "white"),
        dt("You killed Enemy2 with M416", 40.0, 41.2, 680, 44, "white"),
        dt("Enemy3 finally eliminated", 41.5, 42.8, 680, 44, "white"),
        dt("WINNER WINNER CHICKEN DINNER", 95.5, 100, 200, 92, "0xFFC800"),
    ])
    graph = (f"[0:v]noise=alls=18:allf=t[g];[g][1:v]overlay={SAMPLE_FACECAM[0]}:{SAMPLE_FACECAM[1]}[o];[o]{vf}[v];"
             f"[2:a]volume='if(between(t,39,42.5)+between(t,95.5,98)+between(t,130,131.5),16,1)':eval=frame[a]")
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", f"color=c=0x1c2a22:s=1920x1080:r=30:d={seconds}",
           "-f", "lavfi", "-i", f"smptehdbars=s={SAMPLE_FACECAM[2]}x{SAMPLE_FACECAM[3]}:r=30:d={seconds}",
           "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=pink:a=0.02:r=48000",
           "-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
           "-g", "60", "-c:a", "aac", "-b:a", "128k", str(out.resolve())]
    r = subprocess.run(cmd, cwd=FONTS_DIR, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"sample generation failed: {r.stderr[-600:]}")
    return out, SAMPLE_FACECAM


def audio_levels(path: Path, rate: int = 5) -> np.ndarray:
    """dBFS per 1/rate s (mono), from the file's first audio track."""
    r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1",
                        "-ar", "8000", "-f", "s16le", "-"], capture_output=True, **popen_kwargs())
    a = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768
    n = 8000 // rate
    if a.size < n:
        return np.array([-120.0])
    a = a[: a.size // n * n].reshape(-1, n)
    rms = np.sqrt((a ** 2).mean(axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-6))


def hud_frames(path: Path, every: float, width: int = 640):
    """(t, BGR frame) every `every` seconds, decoded by ffmpeg at low resolution (like OBS screenshots)."""
    info = probe_video(path)
    w0, h0 = int(info.get("width") or 1920), int(info.get("height") or 1080)
    h = int(round(h0 * width / w0 / 2) * 2)
    proc = subprocess.Popen([ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(path),
                             "-vf", f"fps=1/{every},scale={width}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                            stdout=subprocess.PIPE, **popen_kwargs())
    size = width * h * 3
    i = 0
    assert proc.stdout is not None
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        yield (i + 0.5) * every, np.frombuffer(buf, np.uint8).reshape(h, width, 3)
        i += 1
    proc.wait()


def simulate(cfg: Config, path: Path, *, speed: float = 0, game: str = "auto", facecam: tuple | None = None,
             marks: list[float] | None = None, notify: bool = False) -> dict:
    """speed 0 = as fast as possible, 1 = real time, 4 = 4×. Renders really happen (that's what we measure)."""
    import psutil

    info = probe_video(path)
    dur = float(info.get("duration") or 0)
    w, h = int(info.get("width") or 1920), int(info.get("height") or 1080)
    pack = detect_pack([path.stem], game if game != "auto" else cfg.live.game_pack)
    sim_t = [0.0]
    lay = Layout(w, h, facecam, pack.gameplay_only)
    # as-fast-as-possible replays burn CPU themselves (decoding): the governor would pause on its own simulation
    gov = Governor(cpu_ceiling=cfg.live.cpu_ceiling if speed > 0 else 100)
    eng = LiveEngine(cfg, source=FileSource(path), pack=pack, clock=lambda: sim_t[0], layout=lay, notify=notify,
                     governor=gov, title=f"Simulation · {path.name}", simulate=True,
                     sid=f"live_sim_{time.strftime('%Y%m%d_%H%M%S')}")
    levels = audio_levels(path)
    frames = hud_frames(path, cfg.live.hud_every_sec)
    next_frame = next(frames, None)
    marks = sorted(marks or [])
    step = 0.2
    t0 = time.monotonic()
    cpu: list[float] = []
    psutil.cpu_percent(None)
    last_gov = 0.0
    while sim_t[0] < dur:
        t = sim_t[0]
        i = int(t / step)
        if i < len(levels):
            eng.audio_levels(float(levels[i]), None)
        while next_frame is not None and next_frame[0] <= t:
            eng.hud_frame(next_frame[1])
            next_frame = next(frames, None)
        while marks and marks[0] <= t:
            eng.signal(Signal(t, "manual", 1.0, "marked (simulation)"))
            marks.pop(0)
        eng.tick()
        if time.monotonic() - last_gov >= 1:
            gov.sample()
            cpu.append(gov.cpu)
            last_gov = time.monotonic()
        sim_t[0] = round(t + step, 3)
        if speed > 0:
            time.sleep(step / speed)
    recap = eng.finish(wait=1800)
    from clipforge.live import store

    sess = store.load_session(cfg, eng.sid)
    report = {
        "file": str(path), "duration": round(dur, 1), "game": pack.name, "facecam": bool(facecam),
        "session": eng.sid, "wall_seconds": round(time.monotonic() - t0, 1),
        "cpu_avg": round(sum(cpu) / max(1, len(cpu)), 1), "cpu_max": round(max(cpu or [0]), 1),
        "moments": eng.moments,
        "clips": [{k: c[k] for k in ("rank", "trigger", "score", "reason", "title", "duration", "captions", "render")}
                  for c in sess.get("clips", [])],
        "recap": {k: v for k, v in recap.items() if k != "top"},
    }
    out = cfg.workspace / eng.sid / "simulation.json"
    out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    log.info(f"[live] simulation report → {out}")
    return report
