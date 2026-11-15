"""`clipforge live bench`: render the same clip with AMD AMF and with x264 ultrafast while N "game" processes burn CPU
at normal priority. Reports render time, total CPU and how much the game processes slowed down (their loop rate
vs a baseline) — the number that matters for FPS.
"""

from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

from clipforge.config import load_config
from clipforge.live.render import Layout, render


def _burn(counter, stop) -> None:
    n = 0
    while not stop.is_set():
        for _ in range(20000):
            n += 1
        with counter.get_lock():
            counter.value += 1


def _rate(counter, secs: float) -> float:
    a = counter.value
    time.sleep(secs)
    return (counter.value - a) / secs


def run_bench(file: Path | None, seconds: float = 40, load: int = 6) -> dict:
    cfg = load_config()
    if file is None:
        from clipforge.live.simulate import make_sample

        file, _ = make_sample(cfg.workspace / "samples" / "bgmi_sample.mp4")
    from clipforge.utils.ffmpeg import probe_video

    info = probe_video(file)
    lay = Layout(int(info.get("width") or 1920), int(info.get("height") or 1080), (40, 770, 480, 270), "crop")
    out_dir = cfg.workspace / "samples" / "bench"
    counter, stop = mp.Value("l", 0), mp.Event()
    procs = [mp.Process(target=_burn, args=(counter, stop), daemon=True) for _ in range(load)]
    for p in procs:
        p.start()
    try:
        time.sleep(2)
        base = _rate(counter, 5)
        res: dict = {"file": str(file), "clip_seconds": seconds, "game_processes": load, "baseline_game_rate": round(base, 1)}
        import threading

        for enc in ("amf", "x264"):
            box: dict = {}

            def go(e=enc):
                box["stats"] = render(file, 30, seconds, lay, out_dir / f"bench_{e}.mp4", encoder=e,
                                      max_cores=cfg.live.max_cores)
            th = threading.Thread(target=go)
            a = counter.value
            t0 = time.monotonic()
            th.start()
            th.join()
            el = time.monotonic() - t0
            rate = (counter.value - a) / el
            st = box["stats"]
            res[enc] = {"encoder_used": st.encoder, "render_seconds": st.seconds, "x_realtime": round(seconds / st.seconds, 2),
                        "system_cpu_avg": st.cpu_avg, "ffmpeg_cpu_pct_of_one_core": st.proc_cpu,
                        "game_slowdown_pct": round(max(0.0, (base - rate) / base * 100), 1)}
            time.sleep(3)
        return res
    finally:
        stop.set()
        for p in procs:
            p.join(5)
