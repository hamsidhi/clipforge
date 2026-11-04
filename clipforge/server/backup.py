"""Encrypted backups of what can't be rebuilt: the database, candidates.json files (your edits), logins/tokens,
config.yaml and .env. Videos and clips are NOT backed up (they can be rebuilt / re-downloaded).

Encryption (the key stays with you):
  * `server.backup_recipient: age1…` and the `age` tool installed → standard age encryption (`age -d -i key.txt`).
  * otherwise ClipForge's built-in format "CFBK1": X25519 (ephemeral) + HKDF-SHA256 + ChaCha20-Poly1305 in 1 MiB
    chunks with a last-chunk flag (truncation is detected). `clipforge server backup-keygen` makes the key pair: the
    public key (cfpk1:…) goes in config, the secret key file (cfsk1:…) stays on your laptop / password manager.
  * no recipient configured → the backup is refused (it contains tokens).
Retention: the newest backup of each of the last `backup_keep_daily` days + of each of the last `backup_keep_weekly`
older weeks. Copies: the laptop pulls the newest one over Tailscale (worker), and `rclone copy` to an rclone crypt
remote (e.g. your own Google Drive) if `server.rclone_remote` is set. Never a public location.
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import os
import shutil
import sqlite3
from contextlib import closing
import subprocess
import tarfile
import tempfile
from pathlib import Path

from clipforge import config as _config
from clipforge.config import Config, config_path, load_config
from clipforge.utils.log import log

MAGIC = b"CFBK1\n"
CHUNK = 1 << 20


class BackupError(RuntimeError):
    pass


# ============================================================================ keys + built-in encryption
def keygen() -> tuple[str, str]:
    """(public 'cfpk1:…', secret 'cfsk1:…')."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    sk = X25519PrivateKey.generate()
    raw_sk = sk.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    raw_pk = sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "cfpk1:" + base64.urlsafe_b64encode(raw_pk).decode(), "cfsk1:" + base64.urlsafe_b64encode(raw_sk).decode()


def _derive(shared: bytes, salt: bytes, eph: bytes, recip: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(hashes.SHA256(), 32, salt, b"clipforge-backup-v1" + eph + recip).derive(shared)


def _nonce(i: int, last: bool) -> bytes:
    return i.to_bytes(11, "big") + (b"\x01" if last else b"\x00")


def encrypt(src: Path, dst: Path, recipient: str) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    if not recipient.startswith("cfpk1:"):
        raise BackupError("backup_recipient must be cfpk1:… (clipforge server backup-keygen) or age1… with age installed")
    rpk_raw = base64.urlsafe_b64decode(recipient[6:])
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    salt = os.urandom(16)
    key = _derive(eph.exchange(X25519PublicKey.from_public_bytes(rpk_raw)), salt, eph_pub, rpk_raw)
    aead = ChaCha20Poly1305(key)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(MAGIC + eph_pub + salt)
        i = 0
        chunk = fin.read(CHUNK)
        while True:
            nxt = fin.read(CHUNK)
            last = not nxt
            ct = aead.encrypt(_nonce(i, last), chunk, None)
            fout.write(len(ct).to_bytes(4, "big") + ct)
            if last:
                break
            chunk, i = nxt, i + 1


def decrypt(src: Path, dst: Path, secret: str) -> None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    secret = secret.strip()
    if not secret.startswith("cfsk1:"):
        raise BackupError("That isn't a ClipForge backup secret key (cfsk1:…)")
    sk = X25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(secret[6:]))
    my_pub = sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        if fin.read(len(MAGIC)) != MAGIC:
            raise BackupError("Not a ClipForge backup (CFBK1)")
        eph_pub, salt = fin.read(32), fin.read(16)
        aead = ChaCha20Poly1305(_derive(sk.exchange(X25519PublicKey.from_public_bytes(eph_pub)), salt, eph_pub, my_pub))
        i = 0
        while True:
            n = fin.read(4)
            if len(n) < 4:
                raise BackupError("Backup is truncated (the last part is missing)")
            ct = fin.read(int.from_bytes(n, "big"))
            peek = fin.read(1)
            last = not peek
            if peek:
                fin.seek(-1, io.SEEK_CUR)
            try:
                fout.write(aead.decrypt(_nonce(i, last), ct, None))
            except InvalidTag:
                raise BackupError("Wrong key, or the backup was damaged/tampered with")
            if last:
                return
            i += 1


# ============================================================================ make / list / prune
def backup_dir(cfg: Config | None = None) -> Path:
    cfg = cfg or load_config()
    d = Path(cfg.server.backup_dir).expanduser() if cfg.server.backup_dir else cfg.workspace / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _files_to_back_up(cfg: Config) -> list[tuple[Path, str]]:
    """(path on disk, name inside the archive). The DB is added separately (consistent copy)."""
    from clipforge.publish import youtube as yt

    out: list[tuple[Path, str]] = []
    cp = config_path()
    if cp.exists():
        out.append((cp, "config.yaml"))
    if _config.ENV_PATH.exists():
        out.append((_config.ENV_PATH, ".env"))
    cs = yt.client_secret_path()
    if cs.exists():
        out.append((cs, "client_secret.json"))
    root = cfg.workspace
    for pattern in ("*/candidates.json", "channels/**/*.json", "accounts/**/*.json", "scheduler_state.json"):
        for f in root.glob(pattern):
            if f.is_file():
                out.append((f, "workspace/" + f.relative_to(root).as_posix()))
    return out


def make(cfg: Config | None = None, now: dt.datetime | None = None) -> Path:
    cfg = cfg or load_config()
    recipient = cfg.server.backup_recipient.strip()
    if not recipient:
        raise BackupError("No backup key: run `clipforge server backup-keygen`, put the public key in "
                          "server.backup_recipient and keep the secret key safe (backups contain your logins).")
    now = now or dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    out_dir = backup_dir(cfg)
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        dbcopy = tmpd / "clipforge.db"
        src_db = cfg.workspace / "clipforge.db"
        if src_db.exists():
            with closing(sqlite3.connect(src_db)) as a, closing(sqlite3.connect(dbcopy)) as b:
                a.backup(b)  # consistent even while the app writes (WAL)
        tar_p = tmpd / "backup.tar.gz"
        with tarfile.open(tar_p, "w:gz") as tar:
            if dbcopy.exists():
                tar.add(dbcopy, "workspace/clipforge.db")
            for path, name in _files_to_back_up(cfg):
                tar.add(path, name)
        if recipient.startswith("age1"):
            if not shutil.which("age"):
                raise BackupError("backup_recipient is an age key but the `age` tool isn't installed (apt install age)")
            dst = out_dir / f"clipforge-{stamp}.tar.gz.age"
            subprocess.run(["age", "-r", recipient, "-o", str(dst), str(tar_p)], check=True)
        else:
            dst = out_dir / f"clipforge-{stamp}.tar.gz.cfbk"
            encrypt(tar_p, dst, recipient)
    prune(cfg, now)
    if cfg.server.rclone_remote:
        _rclone(dst, cfg.server.rclone_remote)
    log.info(f"[backup] {dst.name} ({dst.stat().st_size / 1024:.0f} KB)")
    return dst


def _rclone(f: Path, remote: str) -> None:
    if not shutil.which("rclone"):
        log.warning("[backup] server.rclone_remote is set but rclone isn't installed — skipped the off-site copy")
        return
    try:
        subprocess.run(["rclone", "copy", str(f), remote], check=True, capture_output=True, timeout=1800)
    except Exception as exc:
        log.warning(f"[backup] rclone copy failed: {exc}")


def _stamp(p: Path) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(p.name.split("-", 1)[1][:15], "%Y%m%d-%H%M%S").replace(tzinfo=dt.timezone.utc)
    except (IndexError, ValueError):
        return None


def list_backups(cfg: Config | None = None) -> list[Path]:
    d = backup_dir(cfg)
    files = [p for p in d.glob("clipforge-*.tar.gz.*") if _stamp(p)]
    return sorted(files, key=lambda p: _stamp(p), reverse=True)  # type: ignore[arg-type,return-value]


def prune(cfg: Config | None = None, now: dt.datetime | None = None) -> list[Path]:
    """Keep the newest backup of each of the last N days, then the newest of each of the M weeks before that."""
    cfg = cfg or load_config()
    keep: set[Path] = set()
    days: set = set()
    weeks: set = set()
    weekly = 0
    for p in list_backups(cfg):
        t = _stamp(p)
        assert t
        day, week = t.date(), t.isocalendar()[:2]
        if day not in days and len(days) < cfg.server.backup_keep_daily:
            days.add(day)
            weeks.add(week)
            keep.add(p)
        elif week not in weeks and weekly < cfg.server.backup_keep_weekly:
            weeks.add(week)
            weekly += 1
            keep.add(p)
    removed = [p for p in list_backups(cfg) if p not in keep]
    for p in removed:
        p.unlink(missing_ok=True)
    return removed


def last_backup(cfg: Config | None = None) -> dict | None:
    b = list_backups(cfg)
    if not b:
        return None
    t = _stamp(b[0])
    age = dt.datetime.now(dt.timezone.utc) - t  # type: ignore[operator]
    hours = int(age.total_seconds() // 3600)
    return {"name": b[0].name, "path": str(b[0]), "at": t, "age": f"{hours // 24}d {hours % 24}h ago" if hours >= 24 else f"{hours}h ago"}


# ============================================================================ restore
def restore(backup: Path, secret: str, target: Path, apply: bool = False, cfg: Config | None = None) -> list[str]:
    """Decrypt + unpack into `target` (always), and with apply=True copy everything to its real place, after saving the
    current files to `<target>/before-restore/`. Stop ClipForge first (the CLI checks)."""
    target.mkdir(parents=True, exist_ok=True)
    tar_p = target / "backup.tar.gz"
    if backup.name.endswith(".age"):
        if not shutil.which("age"):
            raise BackupError("This backup was made with age — install age to restore it.")
        key_file = target / "age.key"
        key_file.write_text(secret, encoding="utf-8")
        try:
            subprocess.run(["age", "-d", "-i", str(key_file), "-o", str(tar_p), str(backup)], check=True)
        finally:
            key_file.unlink(missing_ok=True)
    else:
        decrypt(backup, tar_p, secret)
    unpacked = target / "unpacked"
    with tarfile.open(tar_p, "r:gz") as tar:
        tar.extractall(unpacked, filter="data")  # refuses absolute paths / .. / links outside
    tar_p.unlink(missing_ok=True)
    names = sorted(p.relative_to(unpacked).as_posix() for p in unpacked.rglob("*") if p.is_file())
    if apply:
        cfg = cfg or load_config()
        before = target / "before-restore"
        for rel in names:
            dest = _real_place(rel, cfg)
            if dest.exists():
                keep = before / rel
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, keep)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(unpacked / rel, dest)
        for wal in ("clipforge.db-wal", "clipforge.db-shm"):
            try:
                (cfg.workspace / wal).unlink(missing_ok=True)
            except OSError:
                log.warning(f"[restore] {wal} is in use — stop ClipForge before restoring")
    return names


def _real_place(rel: str, cfg: Config) -> Path:
    from clipforge.config import ROOT_DIR

    if rel == "config.yaml":
        return config_path()
    if rel == ".env":
        return _config.ENV_PATH
    if rel == "client_secret.json":
        return ROOT_DIR / "client_secret.json"
    if rel.startswith("workspace/"):
        return cfg.workspace / rel[len("workspace/"):]
    raise BackupError(f"unexpected file in the backup: {rel}")


def nightly_if_due(now: dt.datetime | None = None) -> Path | None:
    """Called by the server's ticker: one backup a day at server.backup_time (IST) + housekeeping."""
    from clipforge.notify import tg_store as st
    from clipforge.publish.timez import utc_to_local

    cfg = load_config()
    now = now or dt.datetime.now(dt.timezone.utc)
    local = utc_to_local(now, cfg.publishing.timezone)
    hh, mm = (int(x) for x in cfg.server.backup_time.split(":"))
    day = local.date().isoformat()
    if st.kv_get("backup_day") == day or (local.hour, local.minute) < (hh, mm):
        return None
    st.kv_set("backup_day", day)
    from clipforge.server import housekeeping

    try:
        p = housekeeping.run(cfg)
        if p.delete:
            log.info(f"[cleanup] freed {p.bytes / 1024 ** 3:.1f} GB ({len(p.delete)} files)")
    except Exception as exc:
        log.warning(f"[cleanup] failed: {exc}")
    if not cfg.server.backup_recipient:
        return None
    try:
        return make(cfg, now)
    except Exception as exc:
        log.error(f"[backup] nightly backup failed: {exc}")
        try:
            from clipforge.notify import telegram

            from clipforge.notify.tg_core import scrub

            telegram.bot().notify("job_failed", f"💾 Nightly backup failed: {scrub(str(exc))[:300]} — details in the server log.",
                                  key=f"backup_fail:{day}")
        except Exception:
            pass
        return None
