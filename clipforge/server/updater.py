"""`clipforge server update`: pull, install, back up + migrate the DB, restart, health check — rollback if unhealthy."""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
import subprocess
import sys
import time

import httpx

from clipforge.config import ROOT_DIR, load_config


def _run(*args: str, timeout: float = 900) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), cwd=ROOT_DIR, capture_output=True, text=True, timeout=timeout)


def healthy(url: str, wait: float = 90) -> bool:
    end = time.time() + wait
    while time.time() < end:
        try:
            if httpx.get(url, timeout=3).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(3)
    return False


def update(health_url: str = "http://127.0.0.1:8000/api/health", restart: tuple[str, ...] = ("sudo", "systemctl", "restart", "clipforge")) -> tuple[bool, str]:
    old = _run("git", "rev-parse", "HEAD").stdout.strip()
    db = load_config().workspace / "clipforge.db"
    bak = db.with_name(f"clipforge.db.pre-update-{old[:8]}")
    if db.exists():
        with closing(sqlite3.connect(db)) as a, closing(sqlite3.connect(bak)) as b:
            a.backup(b)
    r = _run("git", "pull", "--ff-only")
    if r.returncode != 0:
        return False, f"git pull failed (local changes?): {r.stderr[:300]}"
    new = _run("git", "rev-parse", "HEAD").stdout.strip()
    if new == old:
        return True, "Already up to date."
    pip = _run(sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt")
    if pip.returncode == 0:
        _run(sys.executable, "-m", "pip", "install", "-q", "-e", ".")
    _run(*restart)  # the new code migrates the DB on start (an older schema is copied to clipforge.db.bak-v<n> first)
    if pip.returncode == 0 and healthy(health_url):
        return True, f"Updated {old[:8]} → {new[:8]} and healthy. DB copy kept: {bak.name}"
    # rollback: code, packages, database
    _run("git", "reset", "--hard", old)
    _run(sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt")
    _run("sudo", "systemctl", "stop", "clipforge")
    if bak.exists():
        shutil.copy2(bak, db)
        for wal in ("clipforge.db-wal", "clipforge.db-shm"):
            (db.parent / wal).unlink(missing_ok=True)
    _run(*restart)
    ok = healthy(health_url)
    return False, (f"Update to {new[:8]} wasn't healthy — rolled back to {old[:8]}"
                   + (" (healthy again)." if ok else " but the old version isn't healthy either: `clipforge server logs`."))
