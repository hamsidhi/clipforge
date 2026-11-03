"""`clipforge` command line. The web app calls exactly the same pipeline functions."""

from __future__ import annotations

import sys
import webbrowser
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from clipforge.config import ConfigError, load_config
from clipforge.utils.log import console

app = typer.Typer(add_completion=False, no_args_is_help=True, help="ClipForge — podcast → vertical shorts, 100% free & local.")

if sys.platform == "win32":  # make rich/emoji output safe on old Windows consoles
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


def _cfg(overrides: dict | None = None):
    try:
        cfg = load_config()
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    return cfg.with_overrides(overrides or {})


def _parse_section(section: str | None) -> tuple[float, float] | None:
    """'10:00-18:30' or '600-1110' -> (600.0, 1110.0)."""
    if not section:
        return None

    def secs(s: str) -> float:
        parts = [float(p) for p in s.strip().split(":")]
        total = 0.0
        for p in parts:
            total = total * 60 + p
        return total

    try:
        a, b = section.split("-")
        return secs(a), secs(b)
    except ValueError:
        raise typer.BadParameter("Use START-END, e.g. 10:00-18:00")


def _fail(exc: Exception) -> None:
    console.print(f"\n[bold red]Error:[/bold red] {exc}")
    raise typer.Exit(1)


@app.command()
def run(
    source: str = typer.Argument(..., help="YouTube URL or local video file"),
    clips: Optional[int] = typer.Option(None, "--clips", help="Number of clips (15-25)"),
    min_sec: Optional[float] = typer.Option(None, "--min", help="Min clip length (s)"),
    max_sec: Optional[float] = typer.Option(None, "--max", help="Max clip length (s)"),
    provider: Optional[str] = typer.Option(None, "--provider", help="ollama | gemini | groq | mock"),
    model: Optional[str] = typer.Option(None, "--model", help="LLM model name (default: auto)"),
    caption_script: Optional[str] = typer.Option(None, "--caption-script", help="roman | devanagari"),
    layout: Optional[str] = typer.Option(None, "--layout", help="auto | track | split | blur"),
    whisper_model: Optional[str] = typer.Option(None, "--whisper-model", help="tiny|base|small|medium|large-v3"),
    language: Optional[str] = typer.Option(None, "--language", help="hi | en | auto"),
    section: Optional[str] = typer.Option(None, "--section", help="Only process part of the video, e.g. 10:00-25:00"),
    force: bool = typer.Option(False, "--force", help="Ignore caches for selection + render"),
) -> None:
    """Full pipeline: download → transcribe → analyze → select → reframe → captions → render → metadata."""
    from clipforge import pipeline

    cfg = _cfg({
        "clips.count": clips, "clips.min_sec": min_sec, "clips.max_sec": max_sec, "llm.provider": provider,
        "llm.model": model, "captions.script": caption_script, "reframe.layout": layout,
        "whisper.model": whisper_model, "whisper.language": language,
    })
    if provider and not model:
        cfg = cfg.with_overrides({"llm.model": "auto"} if provider != load_config().llm.provider else {})
    try:
        ws = pipeline.run_all(source, cfg, section=_parse_section(section), force=force)
    except Exception as exc:
        _fail(exc)
        return
    _summary(ws.video_id)


@app.command()
def transcribe(
    source: str = typer.Argument(..., help="YouTube URL or local video file"),
    whisper_model: Optional[str] = typer.Option(None, "--whisper-model"),
    language: Optional[str] = typer.Option(None, "--language"),
    provider: Optional[str] = typer.Option(None, "--provider", help="LLM used for Roman Hinglish"),
    section: Optional[str] = typer.Option(None, "--section"),
) -> None:
    """Download (if needed) + transcribe + analyze only."""
    from clipforge import pipeline

    cfg = _cfg({"whisper.model": whisper_model, "whisper.language": language, "llm.provider": provider})
    try:
        ws = pipeline.prepare(source, cfg, section=_parse_section(section))
    except Exception as exc:
        _fail(exc)
        return
    console.print(f"[green]Done.[/green] video_id = [bold]{ws.video_id}[/bold]  →  {ws.transcript}")


@app.command()
def select(
    video_id: str,
    provider: Optional[str] = typer.Option(None, "--provider"),
    clips: Optional[int] = typer.Option(None, "--clips"),
    force: bool = typer.Option(False, "--force", help="Re-ask the LLM even if cached"),
) -> None:
    """(Re)select clip candidates for an already transcribed video → candidates.json."""
    from clipforge import pipeline

    cfg = _cfg({"llm.provider": provider, "clips.count": clips})
    try:
        pipeline.select(pipeline.open_workspace(video_id, cfg), cfg, force=force)
    except Exception as exc:
        _fail(exc)
        return
    _summary(video_id, rendered=False)


@app.command()
def render(
    video_id: str,
    only: Optional[str] = typer.Option(None, "--only", help="Comma-separated clip ranks, e.g. 3,7,12"),
    force: bool = typer.Option(False, "--force", help="Re-render even if unchanged"),
) -> None:
    """Render clips from candidates.json (edit it first to change start/end/title)."""
    from clipforge import pipeline

    cfg = _cfg()
    ranks = [int(x) for x in only.split(",") if x.strip()] if only else None
    try:
        ws = pipeline.open_workspace(video_id, cfg)
        pipeline.render(ws, cfg, only=ranks, force=force)
        pipeline.metadata(ws, cfg)
    except Exception as exc:
        _fail(exc)
        return
    _summary(video_id)


@app.command()
def preview(video_id: str, no_open: bool = typer.Option(False, "--no-open")) -> None:
    """Build gallery.html with every clip playable side by side, and open it."""
    from clipforge import pipeline

    cfg = _cfg()
    ws = pipeline.open_workspace(video_id, cfg)
    path = pipeline.build_gallery(ws)
    console.print(f"Gallery: {path}")
    if not no_open:
        webbrowser.open(path.as_uri())


@app.command()
def doctor(fix: bool = typer.Option(False, "--fix", help="Download missing fonts / face models")) -> None:
    """Check ffmpeg, yt-dlp, Ollama, GPU, fonts, Node — and print the fix for anything missing."""
    from clipforge.doctor import run_checks

    checks = run_checks(fix_assets=fix)
    t = Table(title="ClipForge doctor", show_lines=False)
    t.add_column("")
    t.add_column("Check")
    t.add_column("Status")
    t.add_column("Fix")
    bad = 0
    for c in checks:
        icon = "[green]✔[/green]" if c.ok else ("[red]✖[/red]" if c.required else "[yellow]–[/yellow]")
        if not c.ok and c.required:
            bad += 1
        t.add_row(icon, c.name, c.detail, "" if c.ok else c.fix)
    console.print(t)
    if bad:
        console.print(f"[red]{bad} required check(s) failed.[/red]" + ("" if fix else " Try: clipforge doctor --fix"))
        raise typer.Exit(1)
    console.print("[green]All required checks passed.[/green]")


@app.command()
def ui(
    dev: bool = typer.Option(False, "--dev", help="Also run the Vite dev server with hot reload"),
    host: str = typer.Option("127.0.0.1", "--host", help="Use 0.0.0.0 to open from your phone on the same Wi-Fi"),
    port: int = typer.Option(8000, "--port"),
    no_browser: bool = typer.Option(False, "--no-browser"),
) -> None:
    """Launch the web app (FastAPI + React) and open the browser."""
    from clipforge.api.server import serve

    serve(host=host, port=port, dev=dev, open_browser=not no_browser)


@app.command()
def upload(
    video_id: str,
    only: Optional[str] = typer.Option(None, "--only", help="Clip ranks to upload, e.g. 1,2,3"),
    privacy: Optional[str] = typer.Option(None, "--privacy", help="private | unlisted | public"),
) -> None:
    """Upload rendered clips to YouTube as private/scheduled Shorts (needs setup — see README)."""
    from clipforge import pipeline
    from clipforge.stages import upload as up

    cfg = _cfg({"upload.privacy": privacy})
    ranks = [int(x) for x in only.split(",") if x.strip()] if only else None
    try:
        up.upload_clips(pipeline.open_workspace(video_id, cfg), cfg, ranks)
    except Exception as exc:
        _fail(exc)


@app.command(name="list")
def list_videos() -> None:
    """List processed videos in the workspace."""
    from clipforge import pipeline

    cfg = _cfg()
    t = Table("video_id", "title", "clips")
    for vid, title, n in pipeline.list_workspaces(cfg):
        t.add_row(vid, title, str(n))
    console.print(t)


def _summary(video_id: str, rendered: bool = True) -> None:
    from clipforge import pipeline
    from clipforge.utils.timecode import fmt_ts

    cfg = _cfg()
    ws = pipeline.open_workspace(video_id, cfg)
    cands = pipeline.load_candidates(ws)
    t = Table(title=f"Clips for {video_id}")
    for col in ("#", "score", "start", "dur", "title", "file"):
        t.add_column(col)
    for c in cands:
        d = ws.find_clip_dir(c.rank)
        f = next(d.glob("*.mp4"), None) if d else None
        t.add_row(str(c.rank), f"{c.score:.1f}", fmt_ts(c.start), f"{c.duration:.0f}s", c.title,
                  f.name if f else ("—" if rendered else ""))
    console.print(t)
    if rendered:
        console.print(f"Output folder: [bold]{ws.clips_dir}[/bold]   metadata: {ws.metadata_csv}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
