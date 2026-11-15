"""`clipforge live …` — live gaming mode (see HANDOFF_LIVE.md)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

console = Console()
live_app = typer.Typer(help="Live clipping: paste a live stream link (YouTube / Twitch / Kick) → highlight clips while it's live.",
                       no_args_is_help=True)


@live_app.command("run")
def run() -> None:
    """Run the live agent in this terminal (the app starts it by itself while live.enabled is true)."""
    from clipforge.live.agent import run_agent

    run_agent()


@live_app.command("status")
def status() -> None:
    """What the live agent is doing right now."""
    from clipforge.notify.tg_live import status_text
    import re

    console.print(re.sub(r"</?[a-z]+>", "", status_text()))


@live_app.command("watch")
def watch(url: str = typer.Argument(..., help="A live stream link, e.g. https://www.twitch.tv/name")) -> None:
    """Start watching a live stream (the live agent must be running: it runs with the app)."""
    from clipforge.config import load_config
    from clipforge.live import store

    store.send_command(load_config(), "start", url=url)
    console.print("Sent — `clipforge live status` shows it in ~20 s.")


@live_app.command("stop")
def stop(session: str = typer.Argument("", help="Session id from `live status` ('' = all)")) -> None:
    """Stop watching a stream (clips in progress finish)."""
    from clipforge.config import load_config
    from clipforge.live import store

    store.send_command(load_config(), "stop", sid=session)
    console.print("Stopping.")


@live_app.command("clip")
def clip(note: str = typer.Argument("", help="Optional note")) -> None:
    """Clip the last moment of the (first) watched stream (same as /clip on Telegram)."""
    from clipforge.config import load_config
    from clipforge.live import store

    store.send_command(load_config(), "clip", note=note, source="CLI")
    console.print("Marked — the clip renders in 1–3 minutes.")


@live_app.command("sample")
def sample(out: Path = typer.Option(Path("workspace/samples/bgmi_sample.mp4")), seconds: int = 150) -> None:
    """Make a synthetic gameplay test stream (facecam, kill feed, Chicken Dinner banner, loud reactions)."""
    from clipforge.live.simulate import make_sample

    p, fc = make_sample(out, seconds)
    console.print(f"Sample: {p}  (facecam {fc[0]},{fc[1]},{fc[2]},{fc[3]})")


@live_app.command("simulate")
def simulate(file: Path = typer.Argument(..., exists=True, help="A recorded stream / gameplay video"),
             speed: float = typer.Option(0, help="0 = as fast as possible, 1 = real time, 4 = 4×"),
             game: str = typer.Option("auto", help="bgmi_pubg | valorant | cs2 | free_fire | gta5 | gta6 | minecraft | generic"),
             facecam: Optional[str] = typer.Option(None, help="x,y,w,h of the facecam ('sample' = the test sample; omit = find it)"),
             mark: list[float] = typer.Option([], help="Seconds to add a manual /clip mark at (repeatable)"),
             captions: bool = typer.Option(True, help="Transcribe + burn captions"),
             notify: bool = typer.Option(False, help="Send the clips to Telegram too"),
             streamer: str = typer.Option("", help="Streamer name for titles/credits")) -> None:
    """Replay a file as if it were live: same detectors + renders, then a report (moments, render time, CPU)."""
    from clipforge.config import load_config
    from clipforge.live.simulate import SAMPLE_FACECAM
    from clipforge.live.simulate import simulate as sim

    cfg = load_config()
    cfg.live.captions = captions
    fc = SAMPLE_FACECAM if facecam == "sample" else (tuple(int(x) for x in facecam.split(",")) if facecam else None)
    r = sim(cfg, file, speed=speed, game=game, facecam=fc, marks=list(mark), notify=notify, streamer=streamer)
    t = Table(title=f"{r['game']} · {r['duration']} s replayed in {r['wall_seconds']} s · CPU avg {r['cpu_avg']}% max {r['cpu_max']}%")
    for col in ("#", "at", "score", "why", "title", "ready in", "encoder", "CPU"):
        t.add_column(col)
    for c in r["clips"]:
        rd = c["render"]
        t.add_row(str(c["rank"]), f"{c['trigger']:.0f}s", f"{c['score']:.1f}", c["reason"][:50], c["title"][:40],
                  f"{rd['total_seconds']}s", rd["encoder"], f"{rd['cpu_avg']}%")
    console.print(t)
    skipped = [m for m in r["moments"] if m.get("state") != "ready"]
    for m in skipped:
        console.print(f"[yellow]moment at {m['t']}s: {m['state']}[/yellow]")
    console.print(f"Clips: {Path(load_config().workspace) / r['session'] / 'clips'}")


@live_app.command("bench")
def bench(file: Optional[Path] = typer.Argument(None), seconds: float = 40, load: int = typer.Option(6, help="CPU burner processes")) -> None:
    """AMD AMF vs x264 ultrafast for one clip, while a CPU load generator runs."""
    from clipforge.live.bench import run_bench

    r = run_bench(file, seconds, load)
    console.print_json(json.dumps(r))


def register(app: typer.Typer) -> None:
    app.add_typer(live_app, name="live")
