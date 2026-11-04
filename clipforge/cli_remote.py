"""CLI for Phase 14: `clipforge auth …`, `clipforge remote …`, `clipforge service …`, `clipforge watch`, `clipforge telegram …`."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer

from clipforge.utils.log import console

auth_app = typer.Typer(help="Password + logged-in devices (for using ClipForge from your phone).", no_args_is_help=True)
remote_app = typer.Typer(help="Private remote access from your phone via Tailscale.", no_args_is_help=True)
service_app = typer.Typer(help="Keep ClipForge running at login (Windows Task Scheduler + watchdog).", no_args_is_help=True)
telegram_app = typer.Typer(help="Telegram bot: notifications + quick commands.", no_args_is_help=True)


def _db() -> None:
    from clipforge.api import db

    db.engine()


# ----------------------------------------------------------------------------- auth
@auth_app.command("set-password")
def set_password(password: Optional[str] = typer.Option(None, help="Omit to type it hidden")) -> None:
    """Set (or change) the login password. Every logged-in device has to log in again."""
    _db()
    from clipforge.api import auth

    pw = password or typer.prompt("New password (10+ characters)", hide_input=True, confirmation_prompt=True)
    try:
        auth.set_password(pw)
    except ValueError as exc:
        raise typer.BadParameter(str(exc))
    console.print("[green]Password saved[/green] (only its scrypt hash, in .env). Restart `clipforge ui` if it's running.")


@auth_app.command("sessions")
def sessions() -> None:
    """List logged-in devices."""
    _db()
    from sqlmodel import select

    from clipforge.api import db

    with db.session() as s:
        for r in s.exec(select(db.AuthSession).where(db.AuthSession.revoked == False)).all():  # noqa: E712
            console.print(f"#{r.id} {r.device} · {r.ip} · last seen {r.last_seen:%Y-%m-%d %H:%M} UTC · until {r.expires_at:%Y-%m-%d}")


@auth_app.command("revoke-all")
def revoke_all() -> None:
    """Log every device out."""
    _db()
    from clipforge.api import auth

    console.print(f"{auth.revoke_all('clipforge auth revoke-all')} device(s) logged out.")


# ----------------------------------------------------------------------------- remote (Tailscale)
def _print_status(st) -> None:
    console.print(f"Tailscale installed: {st.installed} · connected: {st.running} · MagicDNS: {st.magic_dns} · "
                  f"HTTPS certificates: {st.https_certs} · serving ClipForge: {st.serving}")
    for p in st.problems:
        console.print(f"[yellow]• {p}[/yellow]")
    for i, s in enumerate(st.steps, 1):
        console.print(f"  {i}. {s}")


@remote_app.command("setup")
def remote_setup(port: int = typer.Option(8000)) -> None:
    """Check Tailscale, then publish ClipForge privately at https://<laptop>.<tailnet>.ts.net (tailnet only)."""
    _db()
    from clipforge import remote
    from clipforge.api import auth

    if not auth.password_set():
        console.print("[yellow]Set a password first — anyone in your tailnet could otherwise use ClipForge:[/yellow]")
        set_password(None)
    st = remote.setup(port)
    _print_status(st)
    if st.serving and st.url:
        console.print(f"\n[bold green]On your phone (Tailscale app on, same account):[/bold green] {st.url}\n")
        console.print(remote.qr_terminal(st.url))
        console.print("\nKeep ClipForge running: `clipforge service install` (starts at login, restarts on crash).")


@remote_app.command("status")
def remote_status(port: int = typer.Option(8000)) -> None:
    from clipforge import remote

    st = remote.status(port)
    _print_status(st)
    if st.url:
        console.print(f"Phone URL: {st.url}")


@remote_app.command("stop")
def remote_stop(port: int = typer.Option(8000)) -> None:
    """Stop serving ClipForge on the tailnet."""
    from clipforge import remote

    console.print(remote.stop(port))


# ----------------------------------------------------------------------------- service
@service_app.command("install")
def service_install(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Start ClipForge (web app + scheduler) hidden at every login, restarted automatically if it crashes."""
    from clipforge import service

    if dry_run or sys.platform != "win32":
        console.print(service.task_xml())
        if sys.platform != "win32":
            console.print("Not Windows: add `@reboot <venv>/bin/python -m clipforge service run` to `crontab -e` "
                          "(or a systemd user service / launchd agent).")
        return
    ok, msg = service.install()
    console.print(f"[green]{msg}[/green]" if ok else f"[yellow]{msg}[/yellow]")
    if ok:
        console.print("It starts at your next login. To start it now: `schtasks /Run /TN \"ClipForge Service\"`.")


@service_app.command("remove")
def service_remove() -> None:
    from clipforge import service

    console.print(service.remove()[1])


@service_app.command("status")
def service_status() -> None:
    from clipforge import service

    console.print(service.state() or "Not installed (`clipforge service install`).")


@service_app.command("run")
def service_run(port: int = typer.Option(8000)) -> None:
    """The watchdog the task runs (keeps `clipforge ui` alive). Usually not run by hand."""
    from clipforge import service

    service.run(port)


# ----------------------------------------------------------------------------- watch folder
def watch(folder: Optional[Path] = typer.Argument(None, help="Folder to watch (default: remote.watch_folder)"),
          interval: float = typer.Option(10, help="Seconds between checks")) -> None:
    """Start a ClipForge job for every video dropped into a folder (e.g. a Google Drive folder synced to this laptop)."""
    _db()
    from clipforge import watch as w
    from clipforge.config import load_config

    target = folder or (Path(load_config().remote.watch_folder) if load_config().remote.watch_folder else None)
    if not target:
        raise typer.BadParameter("Give a folder, or set remote.watch_folder in config.yaml.")
    console.print(f"Watching {target} — drop video files there. Ctrl+C to stop.")
    w.watch(target, interval, on_job=lambda f, j: console.print(f"Job {j} started for {f.name}"))


# ----------------------------------------------------------------------------- telegram
@telegram_app.command("test")
def telegram_test() -> None:
    """Send a test message to the paired Telegram chat."""
    _db()
    from clipforge.notify import telegram as tg

    console.print(tg.bot().send_text("✅ ClipForge test message") and "sent" or "not paired / no token")


def register(app: typer.Typer) -> None:
    app.add_typer(auth_app, name="auth")
    app.add_typer(remote_app, name="remote")
    app.add_typer(service_app, name="service")
    app.add_typer(telegram_app, name="telegram")
    app.command(name="watch")(watch)
