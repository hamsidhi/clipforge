"""CLI for Phase 15b: `clipforge server …` (on the always-on machine) and `clipforge worker` (on the laptop)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from clipforge.utils.log import console

server_app = typer.Typer(help="Always-on server: status, logs, update, backups, workers, handover.", no_args_is_help=True)
worker_app = typer.Typer(help="Laptop worker: take heavy jobs from the server.", invoke_without_command=True)


def _db() -> None:
    from clipforge.api import db

    db.engine()


# ============================================================================ server
@server_app.command("status")
def status() -> None:
    """Uptime, disk, RAM, CPU, workers, jobs, last backup."""
    _db()
    import re

    from clipforge.server import status as st

    console.print(re.sub(r"</?\w+>", "", st.telegram_text()))


@server_app.command("logs")
def logs(follow: bool = typer.Option(False, "--follow", "-f"), lines: int = typer.Option(100, "-n")) -> None:
    """Recent log lines (journalctl on a systemd server, else workspace/logs/clipforge.log)."""
    if sys.platform.startswith("linux") and Path("/run/systemd/system").exists():
        cmd = ["journalctl", "-u", "clipforge", "-n", str(lines)] + (["-f"] if follow else ["--no-pager"])
        raise typer.Exit(subprocess.call(cmd))
    from clipforge.config import load_config

    f = load_config().workspace / "logs" / "clipforge.log"
    if not f.exists():
        console.print("No log yet.")
        return
    with open(f, encoding="utf-8", errors="replace") as fh:
        for line in fh.readlines()[-lines:]:
            console.print(line.rstrip(), markup=False, highlight=False)
        while follow:
            line = fh.readline()
            if line:
                console.print(line.rstrip(), markup=False, highlight=False)
            else:
                time.sleep(1)


@server_app.command("update")
def update(force: bool = typer.Option(False, help="also outside a systemd server"),
           health_url: str = typer.Option("http://127.0.0.1:8000/api/health")) -> None:
    """git pull → pip install → back up + migrate the DB → restart → health check; automatic rollback if unhealthy."""
    from clipforge.server import updater

    if not force and not Path("/run/systemd/system").exists():
        raise typer.BadParameter("This is for the Linux server (systemd). On the laptop: git pull + restart `clipforge ui`.")
    ok, msg = updater.update(health_url)
    console.print(msg)
    raise typer.Exit(0 if ok else 1)


@server_app.command("backup")
def backup_now() -> None:
    """Make an encrypted backup now (DB, candidates.json, logins, config, .env)."""
    _db()
    from clipforge.server import backup

    p = backup.make()
    console.print(f"Backup: {p}")


@server_app.command("backup-keygen")
def backup_keygen(out: Path = typer.Option(Path("clipforge-backup.key"), help="where to write the SECRET key")) -> None:
    """Make the backup key pair. Put the public key in config (server.backup_recipient); keep the secret key off the server."""
    from clipforge.server import backup

    pub, sec = backup.keygen()
    if out.exists():
        raise typer.BadParameter(f"{out} exists — refusing to overwrite a key.")
    out.write_text(sec + "\n", encoding="utf-8")
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    console.print(f"Public key (put it in config.yaml → server.backup_recipient):\n[bold]{pub}[/bold]\n"
                  f"Secret key written to {out} — copy it to your laptop / password manager and DELETE it from the server.")


@server_app.command("restore")
def restore(backup_file: Path, key: Path = typer.Option(..., "--key", help="secret key file (cfsk1:… or an age identity)"),
            target: Path = typer.Option(Path("restore"), help="folder to unpack into"),
            apply: bool = typer.Option(False, "--apply", help="also put the files back in place (stop ClipForge first)")) -> None:
    """Decrypt + unpack a backup (and with --apply, restore it; current files are kept in <target>/before-restore)."""
    from clipforge.server import backup

    if apply and _app_running():
        raise typer.BadParameter("Stop ClipForge first (sudo systemctl stop clipforge / close `clipforge ui`).")
    names = backup.restore(backup_file, key.read_text(encoding="utf-8").strip(), target, apply)
    console.print(f"{len(names)} files {'restored' if apply else 'unpacked to ' + str(target / 'unpacked')}.")


def _app_running() -> bool:
    import httpx

    try:
        return httpx.get("http://127.0.0.1:8000/api/health", timeout=2).status_code == 200
    except Exception:
        return False


@server_app.command("cleanup")
def cleanup(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Delete old sources / published clips by the rules in config (never anything still waiting to be published)."""
    _db()
    from clipforge.server import housekeeping

    p = housekeeping.run(dry_run=dry_run)
    for f in p.delete:
        console.print(("would delete " if dry_run else "deleted ") + str(f))
    for vid, why in p.kept.items():
        console.print(f"kept {vid}: {why}")
    console.print(f"{'Would free' if dry_run else 'Freed'} {p.bytes / 1024 ** 3:.2f} GB.")


@server_app.command("add-worker")
def add_worker(name: str, key_only: bool = typer.Option(False, "--key-only", help="print only the key (scripts)")) -> None:
    """Allow a laptop to take work from this server. The key is shown ONCE — put it in the laptop's .env."""
    _db()
    from clipforge.server import leases

    w, key = leases.add_worker(name)
    if key_only:
        print(key)
        return
    console.print(f"Worker [bold]{w.name}[/bold] ({w.id}) added. On the laptop, in .env:\n  CLIPFORGE_WORKER_KEY={key}\n"
                  "and in config.yaml: deployment.mode: worker, deployment.server_url: http://<this server>:8000")


@server_app.command("remove-worker")
def remove_worker(worker_id: str) -> None:
    """Revoke a worker key (its running task goes back to the queue)."""
    _db()
    from clipforge.server import leases

    console.print("Revoked." if leases.remove_worker(worker_id) else "Unknown worker.")


@server_app.command("workers")
def workers() -> None:
    """List workers (online / offline, current task)."""
    _db()
    from clipforge.server import leases

    for w in leases.workers():
        t = w["task"]
        console.print(f"{'🟢' if w['online'] else '⚪'} {w['id']} {w['name']} caps={','.join(w['capabilities'])}"
                      + (f" — job {t['job_id']} {t['stage']} {round(t['progress'] * 100)}%" if t else ""))


@server_app.command("handover")
def handover(to: Optional[str] = typer.Option(None, "--to", help="the server's Tailscale name, e.g. clipforge-server"),
             dry_run: bool = typer.Option(False, "--dry-run"), run: bool = typer.Option(False, "--run"),
             rollback: bool = typer.Option(False, "--rollback"),
             skip_sources: bool = typer.Option(False, "--skip-sources", help="don't copy source videos (re-downloadable)"),
             worker_name: Optional[str] = typer.Option(None, "--name", help="this laptop's worker name")) -> None:
    """Move DB + workspace + logins to the server over SSH, then make this laptop a worker (prints the rollback)."""
    _db()
    from clipforge.server import handover as ho

    if sum([dry_run, run, rollback]) != 1:
        raise typer.BadParameter("Pick one: --dry-run, --run or --rollback")
    if rollback:
        for line in ho.rollback(to):
            console.print(line)
        return
    if not to:
        raise typer.BadParameter("--to <server> is required")
    if dry_run:
        for line in ho.dry_run(to, skip_sources):
            console.print(line)
        console.print("Nothing was changed. Run again with --run.")
        return
    res = ho.run(to, skip_sources, worker_name=worker_name)
    console.print(f"✅ Handover done: {res['files']} files ({res['bytes'] / 1024 ** 3:.2f} GB), {res['tokens_moved']} logins moved.\n"
                  f"This laptop is now a worker of {res['server_url']}. Restart `clipforge ui` (or `clipforge worker`).\n"
                  f"Undo: clipforge server handover --rollback --to {to}")


@server_app.command("finish-import", hidden=True)
def finish_import(src: Path = typer.Option(..., "--from")) -> None:
    """(runs on the server during a handover) move the verified copy into place, switch to server mode."""
    from clipforge.config import ROOT_DIR
    from clipforge.server import handover as ho

    moved = ho.finish_import(src, ROOT_DIR)
    console.print(f"Imported {len(moved)} files. Restart the service: sudo systemctl restart clipforge")


@server_app.command("import-token")
def import_token(to: str = typer.Option(..., "--to"), channel: Optional[str] = typer.Option(None, "--channel"),
                 keep: bool = typer.Option(False, "--keep", help="keep a copy on this laptop")) -> None:
    """Connect an account on the laptop (OAuth needs a browser), then push its login to the server over SSH."""
    _db()
    from clipforge.server import handover as ho

    n = ho.import_token(to, channel, keep)
    console.print(f"✅ {n} login(s) now on the server{'' if keep else ' (and removed from this laptop)'}.")


@server_app.command("receive-tokens", hidden=True)
def receive_tokens(src: Path = typer.Option(..., "--from")) -> None:
    _db()
    from clipforge.server import handover as ho

    console.print(f"Registered {ho.receive_tokens(src)} login file(s).")


@server_app.command("unfence")
def unfence() -> None:
    """Allow this server to publish again after an emergency standalone on the laptop (make sure the laptop is a worker!)."""
    _db()
    from clipforge.server import dispatch

    dispatch.unfence()
    console.print("Publishing allowed again on this server.")


# ============================================================================ worker
@worker_app.callback()
def worker(ctx: typer.Context,
           standalone_emergency: bool = typer.Option(False, "--standalone-emergency",
                                                     help="server is gone: fence it, then run everything on this laptop"),
           server_is_down: bool = typer.Option(False, "--server-is-down", help="with --standalone-emergency: skip the fence "
                                               "because the server is unreachable (you make sure it's stopped)"),
           once: bool = typer.Option(False, "--once", help="do at most one task, then exit")) -> None:
    """Run the worker loop (connects to deployment.server_url with CLIPFORGE_WORKER_KEY)."""
    if ctx.invoked_subcommand:
        return
    _db()
    from clipforge.config import load_config, save_config
    from clipforge.server import worker as wk

    if standalone_emergency:
        cfg = load_config()
        if cfg.deployment.mode != "worker":
            raise typer.BadParameter("This laptop isn't a worker.")
        try:
            c = wk.client_from_config()
            c.call("POST", "/api/server/fence", json={"reason": "laptop switched to emergency standalone"})
            console.print("Server fenced: it won't publish until `clipforge server unfence`.")
        except Exception as exc:
            if not server_is_down:
                raise typer.BadParameter(f"Couldn't reach the server to fence it ({exc}). If it's really down and you've "
                                         "made sure it can't come back by itself, add --server-is-down.")
            console.print("[yellow]Server unreachable — you confirmed it's down. If it comes back it might publish too: "
                          "stop it (sudo systemctl stop clipforge) before it does.[/yellow]")
        save_config(cfg.with_overrides({"deployment.mode": "standalone"}))
        console.print("This laptop is standalone now (publishing + Telegram run here). Logins: restore them with "
                      "`clipforge server handover --rollback` or reconnect the accounts.")
        return
    c = wk.client_from_config()
    if once:
        c.hello()
        console.print("did one task" if c.run_once() else "nothing to do")
        return
    import signal

    def _stop(*_):
        c.shutdown()

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)
    console.print(f"Worker → {c.server_url} (Ctrl+C to stop; a running task is handed back to the server).")
    c.loop()


@worker_app.command("status")
def worker_status() -> None:
    """Is the server reachable, and is this laptop allowed in?"""
    from clipforge.server import worker as wk

    c = wk.client_from_config()
    info = c.hello()
    console.print(f"Connected to {info['server']} as {info['name']} ({', '.join(info['capabilities'])}).")


@worker_app.command("install-task")
def worker_install_task(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Windows: start the worker at logon and on wake (hidden), restart it if it stops."""
    from clipforge.config import ROOT_DIR
    from clipforge.utils import winsched

    xml = winsched.task_xml("ClipForge worker: take heavy jobs from the always-on server.", winsched.pythonw(),
                            "-m clipforge worker", str(ROOT_DIR), logon=True, on_wake=True, restart_on_failure=True,
                            time_limit="PT0S")
    if dry_run:
        console.print(xml, markup=False)
        return
    ok, msg = winsched.install("ClipForge Worker", xml)
    console.print(msg)


@worker_app.command("remove-task")
def worker_remove_task() -> None:
    from clipforge.utils import winsched

    console.print(winsched.remove("ClipForge Worker")[1])


def register(app: typer.Typer) -> None:
    app.add_typer(server_app, name="server")
    app.add_typer(worker_app, name="worker")
