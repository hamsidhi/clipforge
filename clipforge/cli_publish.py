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


def scheduler(once: bool = typer.Option(True, "--once/--loop", help="Process due work and exit (default)"),
              daemon: bool = typer.Option(False, "--daemon", help="Always-on: check every minute (Instagram needs it)")) -> None:
    """Upload due clips (API mode), publish Instagram posts at their slot, refresh statuses, sync stats.
    Task Scheduler runs `--once`; `--daemon` is the always-on mode (the web app runs the same loop itself)."""
    _db()
    import time

    from clipforge.publish import runner

    loop = daemon or not once
    while True:
        rep = runner.run_once(source="daemon" if loop else "task")
        if not loop or rep.uploaded or rep.published or rep.failed or rep.missed or rep.rescheduled:
            console.print(f"uploaded {len(rep.uploaded)}, published {len(rep.published)}, failed {len(rep.failed)}, "
                          f"locked {len(rep.locked)}, missed {len(rep.missed)}, moved {len(rep.rescheduled)}, "
                          f"synced {len(rep.synced)}" + (" — upload quota used up for today" if rep.quota_exhausted else ""))
        for msg in rep.messages:
            console.print(f"  {msg}")
        for ch, why in rep.skipped_channels.items():
            console.print(f"  [yellow]skipped {ch}: {why}[/yellow]")
        if not loop:
            return
        try:
            from clipforge.utils import power

            power.update()
        except Exception:
            pass
        time.sleep(60)


def publish_test(channel: str = typer.Argument(..., help="Channel id (UC…) — see `clipforge channels list`"),
                 days: int = typer.Option(30, help="publishAt = now + this many days (you delete the video before that)")) -> None:
    """Upload ONE tiny test video as private + publishAt and report what YouTube did with it (locked or scheduled).
    Delete the test video in YouTube Studio afterwards — ClipForge's login can't delete or unschedule videos."""
    _db()
    import datetime as dt
    import tempfile

    from clipforge.config import load_config
    from clipforge.publish import channels as chs, runner
    from clipforge.publish import youtube as yt
    from clipforge.publish.timez import iso_utc, now_utc
    from clipforge.utils.ffmpeg import run_ffmpeg

    ch = chs.get(channel)
    if not ch:
        raise typer.BadParameter("Unknown channel id")
    cfg = load_config()
    when = now_utc() + dt.timedelta(days=days)
    tmp = Path(tempfile.mkdtemp()) / "clipforge-api-test.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "color=black:s=1080x1920:d=3", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(tmp)])
    client = yt.client_for(channel, cfg)
    body = {"snippet": {"title": "ClipForge API test — delete me", "description": "Private test upload by ClipForge.",
                        "categoryId": "22"},
            "status": {"privacyStatus": "private", "publishAt": iso_utc(when), "selfDeclaredMadeForKids": False}}
    console.print(f"Uploading a 3-second black test video to [bold]{ch.title}[/bold] as private, publishAt {iso_utc(when)} …")
    vid = client.upload(tmp, body)
    v = (client.videos([vid]) or [{}])[0]
    status, _reason, msg = runner.classify_ex(v, when, "scheduled")
    console.print({"video_id": vid, "status": v.get("status"), "result": status})
    console.print("[green]Not locked: API mode can publish (YouTube keeps the schedule).[/green]" if status == "scheduled"
                  else f"[yellow]{msg}[/yellow]")
    console.print(f"Now delete it: https://studio.youtube.com/video/{vid}/edit → ⋮ → Delete forever "
                  f"(otherwise it goes public on {when:%d %b %Y}).")


TASK_NAME = "ClipForge Scheduler"
DAEMON_TASK = "ClipForge Scheduler Daemon"
OLD_TASK_NAMES = ("ClipForge Scheduler (logon)", "ClipForge Scheduler (every 2h)")


def _exe() -> str:
    exe = shutil.which("clipforge") or str(Path(sys.executable).parent / "clipforge.exe")
    return exe


def scheduler_task_xml(daemon: bool = False) -> str:
    from clipforge.config import ROOT_DIR
    from clipforge.utils import winsched

    if daemon:
        return winsched.task_xml("ClipForge: always-on publishing scheduler (checks every minute).", winsched.pythonw(),
                                 "-m clipforge scheduler --daemon", str(ROOT_DIR), logon=True, restart_on_failure=True,
                                 time_limit="PT0S")
    return winsched.task_xml("ClipForge: upload due clips, publish/refresh statuses (every 30 min, at logon, on wake).",
                             winsched.pythonw(), "-m clipforge scheduler --once", str(ROOT_DIR), logon=True,
                             every_minutes=30, on_wake=True)


def task_status() -> list[dict]:
    from clipforge.utils import winsched

    return [t for t in (winsched.query(TASK_NAME), winsched.query(DAEMON_TASK)) if t]


@task_app.command("install")
def task_install(dry_run: bool = typer.Option(False, "--dry-run", help="Only print the task definition"),
                 daemon: bool = typer.Option(False, "--daemon", help="Also start the always-on scheduler at login "
                                             "(hidden). Needed for Instagram if the web app isn't running.")) -> None:
    """Register Windows Task Scheduler jobs: every 30 min + at login + when the laptop wakes (macOS/Linux: cron)."""
    from clipforge.utils import winsched

    if sys.platform != "win32" and not dry_run:
        lines = ["Not Windows — add this to `crontab -e` instead:", f"  @reboot {_exe()} scheduler --once",
                 f"  */30 * * * * {_exe()} scheduler --once"] + ([f"  @reboot {_exe()} scheduler --daemon"] if daemon else [])
        console.print("\n".join(lines))
        return
    todo = [(TASK_NAME, scheduler_task_xml())] + ([(DAEMON_TASK, scheduler_task_xml(True))] if daemon else [])
    for name, xml in todo:
        console.print(f"[bold]{name}[/bold]")
        if dry_run:
            console.print(xml)
            continue
        ok, msg = winsched.install(name, xml)
        console.print(f"[green]{msg}[/green]" if ok else f"[yellow]{msg}[/yellow]")
    if not dry_run:
        for old in OLD_TASK_NAMES:  # Phase 12 tasks (every 2 h) are replaced
            winsched.remove(old)


@task_app.command("remove")
def task_remove(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    from clipforge.utils import winsched

    for name in (TASK_NAME, DAEMON_TASK, *OLD_TASK_NAMES):
        console.print(f"schtasks /Delete /F /TN \"{name}\"")
        if not dry_run:
            winsched.remove(name)


@task_app.command("status")
def task_show() -> None:
    from clipforge.publish import runner

    for t in task_status() or [{"name": "(no ClipForge tasks installed — run `clipforge schedule-task install`)"}]:
        console.print(t)
    console.print({"last scheduler run": runner.read_state()})


def register(app: typer.Typer) -> None:
    app.add_typer(channels_app, name="channels")
    app.add_typer(queue_app, name="queue")
    app.add_typer(batch_app, name="batch")
    app.add_typer(task_app, name="schedule-task")
    app.command(name="schedule")(schedule)
    app.command(name="sync")(sync)
    app.command(name="scheduler")(scheduler)
    app.command(name="publish-test")(publish_test)
