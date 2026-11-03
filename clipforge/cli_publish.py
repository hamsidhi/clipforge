"""Publishing CLI (Phase 12): channels, schedule, queue, batch, sync, scheduler, schedule-task."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from clipforge.utils.log import console

channels_app = typer.Typer(help="Connected YouTube channels.", no_args_is_help=True)
queue_app = typer.Typer(help="The publish queue.", no_args_is_help=True)
batch_app = typer.Typer(help="Assisted mode: upload batches for YouTube Studio.", no_args_is_help=True)
task_app = typer.Typer(help="Windows Task Scheduler entry that runs `clipforge scheduler --once`.", no_args_is_help=True)


def _fail(exc: Exception) -> None:
    console.print(f"[bold red]Error:[/bold red] {exc}")
    raise typer.Exit(1)


def _db():
    from clipforge.api import db

    db.engine()
    return db


@channels_app.command("list")
def channels_list() -> None:
    _db()
    from clipforge.publish import channels as chs, queue as q

    t = Table("id", "title", "auth", "paused", "confirmed", "queued", "scheduled")
    for ch in chs.all_channels():
        its = q.items(ch.id)
        t.add_row(ch.id, ch.title, ch.auth_status, str(ch.paused), str(ch.confirmed),
                  str(sum(i.status == "queued" for i in its)), str(sum(i.status == "scheduled" for i in its)))
    console.print(t)


@channels_app.command("add")
def channels_add() -> None:
    """Opens Google sign-in in the browser; pick the channel (Brand Accounts are listed by Google)."""
    _db()
    from clipforge.publish import channels as chs

    try:
        res = chs.add_via_local_server()
    except Exception as exc:
        _fail(exc)
        return
    if "pick" in res:
        for i, c in enumerate(res["channels"], 1):
            console.print(f"{i}. {c['title']} ({c['id']})")
        n = typer.prompt("Which channel", type=int)
        ch = chs.pick_pending(res["pick"], res["channels"][n - 1]["id"])
        console.print(f"[green]Connected[/green] {ch.title}")
    else:
        console.print(f"[green]Connected[/green] {res['connected'][0]['title']}")
    console.print("Before the first upload, confirm the channel in the web app (Channels page) or: "
                  "clipforge channels confirm <id>")


@channels_app.command("confirm")
def channels_confirm(channel_id: str) -> None:
    _db()
    from clipforge.publish import channels as chs

    ch = chs.get(channel_id)
    if not ch:
        _fail(ValueError("Unknown channel"))
    if typer.confirm(f"Upload Shorts to '{ch.title}' ({ch.handle or ch.id})?"):  # type: ignore[union-attr]
        chs.update(channel_id, confirmed=True)


@channels_app.command("remove")
def channels_remove(channel_id: str) -> None:
    _db()
    from clipforge.publish import channels as chs

    chs.remove(channel_id)
    console.print("Removed (its login token was deleted).")


def schedule(
    video_id: str = typer.Argument(..., help="Workspace video id (see `clipforge list`)"),
    channel: str = typer.Option(..., "--channel", help="Channel id (see `clipforge channels list`)"),
    only: Optional[str] = typer.Option(None, "--only", help="Clip ranks, e.g. 1,2 (default: approved clips)"),
) -> None:
    """Add clips to a channel's queue, write YouTube metadata and put them in the next free slots."""
    _db()
    from clipforge.publish import queue as q
    from clipforge.publish.timez import fmt_local

    ranks = [int(x) for x in only.split(",") if x.strip()] if only else None
    try:
        added = q.add_clips(channel, video_id, ranks)
        for it in added:
            console.print(f"metadata for #{it.rank} …")
            q.generate_metadata(it.id)
        done = q.autoschedule(channel, [it.id for it in added])
    except Exception as exc:
        _fail(exc)
        return
    for it in done:
        console.print(f"  {fmt_local(it.slot_utc)}  {it.title}")
    if len(done) < len(added):
        console.print(f"[yellow]{len(added) - len(done)} clip(s) stay draft (fix title problems in the Schedule page).[/yellow]")


@queue_app.command("list")
def queue_list(channel: Optional[str] = typer.Option(None, "--channel")) -> None:
    _db()
    from clipforge.publish import queue as q
    from clipforge.publish.timez import fmt_local

    t = Table("id", "channel", "status", "slot (IST)", "title", "error")
    for it in q.items(channel):
        t.add_row(str(it.id), it.channel_id[:10], it.status, fmt_local(it.slot_utc), it.title[:50], it.error[:40])
    console.print(t)


@batch_app.command("prepare")
def batch_prepare(channel: str = typer.Option(..., "--channel"), n: Optional[int] = typer.Option(None, "--n")) -> None:
    _db()
    from clipforge.publish import batch

    try:
        res = batch.prepare(channel, n)
    except Exception as exc:
        _fail(exc)
        return
    console.print(f"[green]{res['count']} clips[/green] → {res['folder']}\nOpen batch.html there and follow the steps.")


def sync(channel: str = typer.Option(..., "--channel")) -> None:
    """Link your Studio uploads to the queue and refresh stats (read-only API calls)."""
    _db()
    from clipforge.config import load_config
    from clipforge.publish import analytics, reconcile, youtube as yt
    from clipforge.publish.timez import now_utc

    try:
        res = reconcile.sync(channel)
        cfg = load_config()
        analytics.sync_channel(channel, yt.client_for(channel, cfg), cfg, now_utc())
    except Exception as exc:
        _fail(exc)
        return
    console.print(f"matched {len(res['matched'])}, unmatched queue items {len(res['unmatched_items'])}, "
                  f"unmatched YouTube Shorts {len(res['unmatched_videos'])}")


def scheduler(once: bool = typer.Option(True, "--once/--loop", help="Process due work and exit (default)")) -> None:
    """Upload due clips (API mode), refresh statuses and sync stats. Task Scheduler runs this."""
    _db()
    import time

    from clipforge.publish import runner

    while True:
        rep = runner.run_once()
        console.print(f"uploaded {len(rep.uploaded)}, published {len(rep.published)}, failed {len(rep.failed)}, "
                      f"locked {len(rep.locked)}, missed {len(rep.missed)}, synced {len(rep.synced)}"
                      + (" — upload quota used up for today" if rep.quota_exhausted else ""))
        for ch, why in rep.skipped_channels.items():
            console.print(f"  [yellow]skipped {ch}: {why}[/yellow]")
        if once:
            return
        time.sleep(600)


TASK_NAMES = ("ClipForge Scheduler (logon)", "ClipForge Scheduler (every 2h)")


def _exe() -> str:
    exe = shutil.which("clipforge") or str(Path(sys.executable).parent / "clipforge.exe")
    return exe


def task_commands(exe: str) -> list[list[str]]:
    action = f'"{exe}" scheduler --once'
    return [
        ["schtasks", "/Create", "/F", "/TN", TASK_NAMES[0], "/TR", action, "/SC", "ONLOGON", "/RL", "LIMITED"],
        ["schtasks", "/Create", "/F", "/TN", TASK_NAMES[1], "/TR", action, "/SC", "HOURLY", "/MO", "2", "/RL", "LIMITED"],
    ]


@task_app.command("install")
def task_install(dry_run: bool = typer.Option(False, "--dry-run", help="Only print the commands")) -> None:
    """Register Windows Task Scheduler jobs: at login and every 2 hours (macOS/Linux: see README for cron)."""
    if sys.platform != "win32":
        console.print("Not Windows — add this to `crontab -e` instead:\n"
                      f"  @reboot {_exe()} scheduler --once\n  0 */2 * * * {_exe()} scheduler --once")
        return
    for cmd in task_commands(_exe()):
        console.print(" ".join(f'"{c}"' if " " in c else c for c in cmd))
        if not dry_run:
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                msg = (res.stderr or res.stdout).strip()
                hint = " (the 'at logon' task may need an elevated terminal)" if "denied" in msg.lower() else ""
                console.print(f"[yellow]{msg}{hint}[/yellow]")
            else:
                console.print("[green]ok[/green]")


@task_app.command("remove")
def task_remove(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    for name in TASK_NAMES:
        cmd = ["schtasks", "/Delete", "/F", "/TN", name]
        console.print(" ".join(cmd))
        if not dry_run and sys.platform == "win32":
            subprocess.run(cmd, capture_output=True, text=True)


def register(app: typer.Typer) -> None:
    app.add_typer(channels_app, name="channels")
    app.add_typer(queue_app, name="queue")
    app.add_typer(batch_app, name="batch")
    app.add_typer(task_app, name="schedule-task")
    app.command(name="schedule")(schedule)
    app.command(name="sync")(sync)
    app.command(name="scheduler")(scheduler)
