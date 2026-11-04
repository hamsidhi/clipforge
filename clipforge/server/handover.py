"""`clipforge server handover --to <tailscale host> --dry-run | --run | --rollback` — move the database, workspace and
logins from this laptop to the always-on server, then make the laptop a worker. YOU run it; ClipForge never moves your
data by itself.

Over SSH only (Tailscale): `tar` is streamed into `ssh host tar -x` (Windows 10+ ships ssh.exe + tar.exe), every file's
sha256 is checked on the server (`sha256sum -c`), and nothing goes through git or Telegram. Secrets never appear on a
command line or in logs.

Order (each step is safe to re-run):
  1. preflight: SSH works, the server has ClipForge installed and enough disk; nothing is uploading here right now
  2. back up this laptop's config.yaml / .env / tokens to workspace/handover_backup/<time>/ (for --rollback)
  3. switch this laptop to `worker` mode FIRST → it stops publishing and stops the Telegram bot immediately
  4. consistent DB snapshot (sqlite backup API) + stream workspace/.env/config/client_secret to the server's staging dir
  5. verify checksums on the server, then `clipforge server finish-import` there (mode=server, restart the service)
  6. `clipforge server add-worker <this laptop>` there → the key is saved in this laptop's .env (never printed)
  7. move the YouTube/Instagram token files off this laptop into the handover backup (a worker holds no tokens)
`--rollback`: stop the server's service (so two machines never publish), restore this laptop's config + tokens
(mode standalone) and re-enable the scheduled task.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import socket
import sqlite3
from contextlib import closing
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from clipforge import config as _config
from clipforge.config import ROOT_DIR, config_path, load_config, save_config, save_secret
from clipforge.utils.log import log

REMOTE_DIR = "/opt/clipforge/app"      # deploy/install.sh layout
STAGING = "/opt/clipforge/handover"
SKIP_DIRS = {"_incoming", "logs", "backups", "handover_backup", "_frames"}
SOURCES = {"source.mp4", "audio.wav"}


class HandoverError(RuntimeError):
    pass


class Transport(Protocol):
    def describe(self) -> str: ...
    def check(self) -> dict: ...
    def send(self, items: list[tuple[Path, str]], manifest: dict[str, str]) -> None: ...
    def verify(self) -> list[str]: ...
    def finish_import(self) -> None: ...
    def add_worker(self, name: str) -> str: ...
    def stop_server(self) -> bool: ...


@dataclass
class Plan:
    files: list[tuple[Path, str]] = field(default_factory=list)  # (local path, name on the server)
    bytes: int = 0
    tokens: list[Path] = field(default_factory=list)
    skipped_sources: int = 0


# ============================================================================ transports
class SshTransport:
    def __init__(self, host: str, remote_dir: str = REMOTE_DIR, staging: str = STAGING) -> None:
        if not host or any(c in host for c in " ;&|`$'\"\\"):
            raise HandoverError("Give the server's Tailscale name, e.g. clipforge-server or ubuntu@clipforge-server")
        self.host, self.remote_dir, self.staging = host, remote_dir, staging

    def describe(self) -> str:
        return f"ssh {self.host}:{self.remote_dir}"

    def _ssh(self, cmd: str, input_: bytes | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
        return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", self.host, cmd], input=input_,
                              capture_output=True, timeout=timeout)

    def check(self) -> dict:
        r = self._ssh(f"test -x {self.remote_dir}/.venv/bin/clipforge && df -Pk {self.remote_dir} | tail -1")
        if r.returncode != 0:
            raise HandoverError(f"Can't reach ClipForge on {self.host} over SSH ({r.stderr.decode(errors='replace')[:200]}). "
                                "Is Tailscale up on both, and did deploy/install.sh finish?")
        free_kb = int(r.stdout.decode().split()[3])
        return {"free": free_kb * 1024}

    def send(self, items: list[tuple[Path, str]], manifest: dict[str, str]) -> None:
        proc = subprocess.Popen(["ssh", "-o", "BatchMode=yes", self.host,
                                 f"rm -rf {self.staging} && mkdir -p {self.staging} && tar -xf - -C {self.staging}"],
                                stdin=subprocess.PIPE)
        assert proc.stdin
        with tarfile.open(fileobj=proc.stdin, mode="w|") as tar:
            for path, name in items:
                tar.add(path, name)
            data = ("\n".join(f"{h}  {n}" for n, h in manifest.items()) + "\n").encode()
            info = tarfile.TarInfo("SHA256SUMS")
            info.size = len(data)
            import io

            tar.addfile(info, io.BytesIO(data))
        proc.stdin.close()
        if proc.wait() != 0:
            raise HandoverError("Copy to the server failed (ssh/tar). Nothing on the server was changed — run it again.")

    def verify(self) -> list[str]:
        r = self._ssh(f"cd {self.staging} && sha256sum --quiet -c SHA256SUMS", timeout=3600)
        return [ln for ln in r.stdout.decode(errors="replace").splitlines() if "FAILED" in ln] if r.returncode else []

    def finish_import(self) -> None:
        r = self._ssh(f"sudo -u clipforge {self.remote_dir}/.venv/bin/clipforge server finish-import --from {self.staging} "
                      "&& sudo systemctl restart clipforge", timeout=600)
        if r.returncode != 0:
            raise HandoverError(f"finish-import failed on the server: {r.stderr.decode(errors='replace')[:300]}")

    def add_worker(self, name: str) -> str:
        r = self._ssh(f"sudo -u clipforge {self.remote_dir}/.venv/bin/clipforge server add-worker {json.dumps(name)} --key-only")
        key = r.stdout.decode().strip().splitlines()[-1] if r.returncode == 0 and r.stdout else ""
        if not key.startswith("cfw_"):
            raise HandoverError("Couldn't create the worker key on the server — run `clipforge server add-worker` there.")
        return key

    def stop_server(self) -> bool:
        return self._ssh("sudo systemctl stop clipforge").returncode == 0


class DirTransport:
    """A local folder that plays the server (tests / a dry rehearsal on one machine)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.staging = self.root / "handover"
        self.log: list[str] = []
        self.stopped = False

    def describe(self) -> str:
        return f"dir {self.root}"

    def check(self) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        return {"free": shutil.disk_usage(self.root).free}

    def send(self, items: list[tuple[Path, str]], manifest: dict[str, str]) -> None:
        shutil.rmtree(self.staging, ignore_errors=True)
        for path, name in items:
            dst = self.staging / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dst)
        (self.staging / "SHA256SUMS").write_text("\n".join(f"{h}  {n}" for n, h in manifest.items()) + "\n")
        self.log.append("send")

    def verify(self) -> list[str]:
        bad = []
        for line in (self.staging / "SHA256SUMS").read_text().splitlines():
            if line.strip():
                h, name = line.split("  ", 1)
                if _sha(self.staging / name) != h:
                    bad.append(f"{name}: FAILED")
        return bad

    def finish_import(self) -> None:
        finish_import(self.staging, self.root / "app", workspace=self.root / "app" / "workspace")
        self.log.append("finish_import")

    def add_worker(self, name: str) -> str:
        self.log.append(f"add_worker {name}")
        return "cfw_" + hashlib.sha256(name.encode()).hexdigest()[:32]

    def stop_server(self) -> bool:
        self.stopped = True
        self.log.append("stop_server")
        return True


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def transport_for(to: str) -> Transport:
    if to.startswith("dir:"):
        return DirTransport(Path(to[4:]))
    return SshTransport(to)


# ============================================================================ plan / run / rollback
def plan(skip_sources: bool = False) -> Plan:
    cfg = load_config()
    out = Plan()
    root = cfg.workspace
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(root)
        if rel.parts[0] in SKIP_DIRS or f.name in ("clipforge.db", "clipforge.db-wal", "clipforge.db-shm", ".hashes.json"):
            continue
        if f.suffix in (".part", ".tmp") or f.name.endswith(".tmp.mp4"):
            continue
        if skip_sources and f.name in SOURCES:
            out.skipped_sources += 1
            continue
        out.files.append((f, "workspace/" + rel.as_posix()))
        if rel.parts[0] in ("channels", "accounts") and f.name == "token.json":
            out.tokens.append(f)
    for p, name in ((_config.ENV_PATH, ".env"), (config_path(), "config.yaml"), (ROOT_DIR / "client_secret.json", "client_secret.json")):
        if p.exists():
            out.files.append((p, name))
    out.bytes = sum(p.stat().st_size for p, _ in out.files)
    return out


def _backup_dir() -> Path:
    d = load_config().workspace / "handover_backup" / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    return d


def dry_run(to: str, skip_sources: bool = False, transport: Transport | None = None) -> list[str]:
    t = transport or transport_for(to)
    p = plan(skip_sources)
    lines = [f"Target: {t.describe()}",
             f"Will copy {len(p.files)} files, {p.bytes / 1024 ** 3:.2f} GB (+ a consistent snapshot of clipforge.db)"
             + (f"; skipping {p.skipped_sources} source videos (the server re-downloads them if you edit those jobs)"
                if skip_sources else ""),
             f"Logins moved off this laptop afterwards: {len(p.tokens)} token file(s)",
             "This laptop then becomes a worker: it stops publishing and stops the Telegram bot."]
    info = t.check()
    if info["free"] < p.bytes * 1.2:
        lines.append(f"⚠ The server has only {info['free'] / 1024 ** 3:.1f} GB free — not enough. Use --skip-sources or free space.")
    else:
        lines.append(f"Server reachable, {info['free'] / 1024 ** 3:.1f} GB free — OK.")
    if load_config().deployment.mode != "standalone":
        lines.append(f"⚠ This computer is in '{load_config().deployment.mode}' mode — handover expects 'standalone'.")
    return lines


def run(to: str, skip_sources: bool = False, transport: Transport | None = None, worker_name: str | None = None,
        server_url: str | None = None) -> dict:
    cfg = load_config()
    if cfg.deployment.mode != "standalone":
        raise HandoverError(f"This computer is in '{cfg.deployment.mode}' mode. Run --rollback first if you want to redo it.")
    t = transport or transport_for(to)
    p = plan(skip_sources)
    info = t.check()
    if info["free"] < p.bytes * 1.2:
        raise HandoverError("Not enough free disk on the server (see --dry-run).")
    from clipforge.publish import runner

    if getattr(runner, "_current_lock", None) is not None:
        raise HandoverError("An upload is running right now — wait a minute and run it again.")
    bk = _backup_dir()
    for path, name in ((config_path(), "config.yaml"), (_config.ENV_PATH, ".env")):
        if path.exists():
            shutil.copy2(path, bk / name)
    # 3. stop publishing + the bot here before copying the DB (nothing can change on the laptop after the snapshot)
    host = to.split("@")[-1] if not to.startswith("dir:") else "localhost"
    url = server_url or f"https://{host}"  # tailscale serve on the server (install.sh); MagicDNS full name
    save_config(load_config().with_overrides({"deployment.mode": "worker", "deployment.server_url": url}))
    (bk / "MODE_SWITCHED").write_text(url)
    try:
        from clipforge.notify import telegram

        if telegram._bot is not None:
            telegram._bot.shutdown(wait=2)
    except Exception:
        pass
    # 4. snapshot + send
    snap = bk / "clipforge.db"
    db_path = cfg.workspace / "clipforge.db"
    if db_path.exists():
        with closing(sqlite3.connect(db_path)) as a, closing(sqlite3.connect(snap)) as b:
            a.backup(b)
    items = [*p.files, *([(snap, "workspace/clipforge.db")] if snap.exists() else [])]
    manifest = {name: _sha(path) for path, name in items}
    t.send(items, manifest)
    bad = t.verify()
    if bad:
        raise HandoverError("Checksum mismatch on the server: " + "; ".join(bad[:5]) + " — run --run again (it resends).")
    t.finish_import()
    key = t.add_worker(worker_name or socket.gethostname())
    save_secret("CLIPFORGE_WORKER_KEY", key)
    # 7. tokens leave the laptop (kept in the backup folder for --rollback)
    moved = 0
    for f in p.tokens:
        dst = bk / "tokens" / f.relative_to(cfg.workspace)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), dst)
        moved += 1
    _set_scheduler_task(enabled=False)
    (bk / "DONE").write_text(json.dumps({"to": t.describe(), "files": len(items), "tokens_moved": moved}))
    log.info(f"[handover] done → {t.describe()}; rollback: clipforge server handover --rollback")
    return {"backup": str(bk), "files": len(items), "bytes": p.bytes, "tokens_moved": moved, "server_url": url}


def latest_backup() -> Path | None:
    d = load_config().workspace / "handover_backup"
    runs = sorted([x for x in d.iterdir() if (x / "MODE_SWITCHED").exists()]) if d.exists() else []
    return runs[-1] if runs else None


def rollback(to: str | None = None, transport: Transport | None = None) -> list[str]:
    """Make this laptop standalone again. Stops the server's service first so two machines never publish."""
    out = []
    bk = latest_backup()
    if not bk:
        raise HandoverError("No handover to roll back (workspace/handover_backup is empty).")
    t = transport or (transport_for(to) if to else None)
    if t is not None:
        try:
            stopped = t.stop_server()
        except Exception:
            stopped = False
        out.append("Server stopped." if stopped else
                   "⚠ Couldn't stop the server — stop it yourself NOW (sudo systemctl stop clipforge) or both machines may publish.")
    else:
        out.append("⚠ No --to given: stop the server yourself (sudo systemctl stop clipforge) before using this laptop.")
    cfg = load_config()
    tokens = bk / "tokens"
    if tokens.exists():
        for f in tokens.rglob("token.json"):
            dst = cfg.workspace / f.relative_to(tokens)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), dst)
        out.append("Logins restored on this laptop.")
    if (bk / "config.yaml").exists():
        shutil.copy2(bk / "config.yaml", config_path())
    save_config(load_config().with_overrides({"deployment.mode": "standalone"}))
    save_secret("CLIPFORGE_WORKER_KEY", "")
    _set_scheduler_task(enabled=True)
    (bk / "MODE_SWITCHED").rename(bk / "ROLLED_BACK")
    out.append("This laptop is standalone again (publishing + Telegram run here). Restart `clipforge ui`.")
    return out


def finish_import(staging: Path, app_dir: Path, workspace: Path | None = None) -> list[str]:
    """Runs ON THE SERVER: move the verified staging copy into place and switch to server mode. Idempotent."""
    staging, app_dir = Path(staging), Path(app_dir)
    ws = workspace or (app_dir / "workspace")
    moved = []
    for f in sorted(staging.rglob("*")):
        if not f.is_file() or f.name == "SHA256SUMS":
            continue
        rel = f.relative_to(staging).as_posix()
        if rel.startswith("workspace/"):
            dst = ws / rel[len("workspace/"):]
        elif rel in (".env", "config.yaml", "client_secret.json"):
            dst = app_dir / rel
        else:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and rel in (".env", "config.yaml"):
            shutil.copy2(dst, dst.with_name(dst.name + ".before-handover"))
        os.replace(f, dst)
        moved.append(rel)
    for wal in ("clipforge.db-wal", "clipforge.db-shm"):
        (ws / wal).unlink(missing_ok=True)
    cfgp = app_dir / "config.yaml"
    if cfgp.exists():
        from clipforge.config import Config, update_yaml_text

        text = cfgp.read_text(encoding="utf-8")
        # the laptop's config points output.dir at its own disk; on the server the workspace is app_dir/workspace
        new, complete = update_yaml_text(text, {"deployment": {"mode": "server", "server_url": ""},
                                                "output": {"dir": ws.as_posix()}})
        if not complete:
            import yaml

            data = yaml.safe_load(text) or {}
            data.setdefault("deployment", {})["mode"] = "server"
            data.setdefault("output", {})["dir"] = ws.as_posix()
            new = yaml.safe_dump(Config.model_validate(data).model_dump(mode="json"), sort_keys=False, allow_unicode=True)
        cfgp.write_text(new, encoding="utf-8")
    shutil.rmtree(staging, ignore_errors=True)
    return moved


def _set_scheduler_task(enabled: bool) -> None:
    """Windows: disable / enable the laptop's publishing task (no-op elsewhere or when it isn't installed)."""
    if os.name != "nt":
        return
    try:
        from clipforge.cli_publish import DAEMON_TASK, TASK_NAME

        for name in (TASK_NAME, DAEMON_TASK):
            subprocess.run(["schtasks", "/Change", "/TN", name, "/ENABLE" if enabled else "/DISABLE"],
                           capture_output=True, timeout=30)
    except Exception as exc:
        log.debug(f"[handover] scheduler task: {exc}")


# ============================================================================ tokens for a headless server
def export_tokens(channel: str | None = None) -> tuple[list[tuple[Path, str]], Path]:
    """Token files + their account rows (JSON) — what the server needs to publish to accounts connected here."""
    from clipforge.api import db
    from clipforge.publish import channels as chs

    cfg = load_config()
    rows = [c for c in chs.all_channels() if channel in (None, c.id)]
    if not rows:
        raise HandoverError("No such account here — connect it on this laptop first (Accounts page).")
    items: list[tuple[Path, str]] = []
    for c in rows:
        files = list((cfg.workspace / "channels" / c.id).glob("*.json"))
        files += list((cfg.workspace / "accounts" / "instagram" / c.id.split(":")[-1]).glob("*.json"))
        for f in files:
            items.append((f, "workspace/" + f.relative_to(cfg.workspace).as_posix()))
    rows_p = cfg.workspace / "handover_backup" / "accounts.json"
    rows_p.parent.mkdir(parents=True, exist_ok=True)
    rows_p.write_text(json.dumps([c.model_dump(mode="json") for c in rows], default=str), encoding="utf-8")
    items.append((rows_p, "accounts.json"))
    _ = db  # (rows come from the DB through channels.all_channels)
    return items, rows_p


def import_token(to: str, channel: str | None = None, keep: bool = False, transport: Transport | None = None) -> int:
    """Laptop → server over SSH: copy the logins of one (or every) account, verify, register them there."""
    t = transport or transport_for(to)
    items, rows_p = export_tokens(channel)
    manifest = {name: _sha(path) for path, name in items}
    t.send(items, manifest)
    bad = t.verify()
    if bad:
        raise HandoverError("Checksum mismatch on the server — run it again.")
    if isinstance(t, DirTransport):
        receive_tokens(t.staging, t.root / "app" / "workspace")
    else:
        r = t._ssh(f"sudo -u clipforge {t.remote_dir}/.venv/bin/clipforge server receive-tokens --from {t.staging}")  # type: ignore[attr-defined]
        if r.returncode != 0:
            raise HandoverError("The server couldn't register the accounts: " + r.stderr.decode(errors="replace")[:300])
    rows_p.unlink(missing_ok=True)
    tokens = [p for p, n in items if p.name == "token.json"]
    if not keep:
        for p in tokens:
            p.unlink(missing_ok=True)  # a worker holds no logins; the server refreshes them from now on
    return len(tokens)


def receive_tokens(staging: Path, workspace: Path | None = None) -> int:
    """Runs ON THE SERVER: register the accounts + move their token files into place."""
    from clipforge.api import db

    ws = workspace or load_config().workspace
    staging = Path(staging)
    rows = json.loads((staging / "accounts.json").read_text(encoding="utf-8"))
    n = 0
    for f in sorted(staging.rglob("*.json")):
        rel = f.relative_to(staging).as_posix()
        if rel.startswith("workspace/") and (rel.startswith("workspace/channels/") or rel.startswith("workspace/accounts/")):
            dst = ws / rel[len("workspace/"):]
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(f, dst)
            n += 1
    with db.session() as s:
        for r in rows:
            r = {k: v for k, v in r.items() if k in db.Channel.model_fields}
            for k in ("created_at", "last_sync_at"):
                if r.get(k):
                    r[k] = dt.datetime.fromisoformat(str(r[k]))
            row = s.get(db.Channel, r["id"]) or db.Channel(id=r["id"])
            for k, v in r.items():
                setattr(row, k, v)
            row.auth_status = "ok"
            s.add(row)
        s.commit()
    shutil.rmtree(staging, ignore_errors=True)
    return n
