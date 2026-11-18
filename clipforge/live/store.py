"""Live state on disk — shared by the live engine (its own low-priority process), the web app and Telegram.

workspace/live/status.json    engine → everyone, about once a second (meters, governor, moments, clips)
workspace/live/commands.jsonl everyone → engine (clip / pause / resume / auto / stop), tailed by the engine
workspace/live_<YYYYmmdd_HHMM>/ one pipeline workspace per stream (info.json, candidates.json, clips/) + live.json
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from clipforge.config import Config

_lock = threading.Lock()
STALE_SEC = 10  # a status older than this = the engine isn't running


def live_dir(cfg: Config) -> Path:
    d = cfg.workspace / "live"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    for i in range(8):  # Windows: a reader may hold the file for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (i + 1))
    tmp.unlink(missing_ok=True)


def _read(path: Path, default: Any) -> Any:
    for _ in range(3):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (ValueError, PermissionError):
            time.sleep(0.05)
    return default


# ----------------------------------------------------------------------------- status
def write_status(cfg: Config, status: dict) -> None:
    status["heartbeat"] = time.time()
    _atomic_write(live_dir(cfg) / "status.json", status)


def read_status(cfg: Config) -> dict:
    st = _read(live_dir(cfg) / "status.json", {})
    st["engine_running"] = bool(st) and time.time() - float(st.get("heartbeat", 0)) < STALE_SEC
    return st


# ----------------------------------------------------------------------------- commands
def send_command(cfg: Config, cmd: str, **args: Any) -> None:
    line = json.dumps({"cmd": cmd, "at": time.time(), **args}, ensure_ascii=False)
    with _lock, open(live_dir(cfg) / "commands.jsonl", "a", encoding="utf-8") as f:
        f.write(line + "\n")


class CommandTail:
    """The engine reads new commands only (starts at the end of the file: old commands never replay)."""

    def __init__(self, cfg: Config) -> None:
        self.path = live_dir(cfg) / "commands.jsonl"
        self.pos = self.path.stat().st_size if self.path.exists() else 0

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.pos:  # truncated
            self.pos = 0
        if size == self.pos:
            return []
        with open(self.path, encoding="utf-8") as f:
            f.seek(self.pos)
            data = f.read()
            self.pos = f.tell()
        out = []
        for line in data.splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


# ----------------------------------------------------------------------------- sessions
def session_id(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now()
    return "live_" + now.strftime("%Y%m%d_%H%M")


def slug(text: str, n: int = 20) -> str:
    import re

    return (re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "stream")[:n]


def session_file(cfg: Config, sid: str) -> Path:
    return cfg.workspace / sid / "live.json"


def load_session(cfg: Config, sid: str) -> dict:
    return _read(session_file(cfg, sid), {})


def save_session(cfg: Config, sid: str, data: dict) -> None:
    with _lock:
        _atomic_write(session_file(cfg, sid), data)


def update_clip(cfg: Config, sid: str, rank: int, **fields: Any) -> dict:
    with _lock:
        data = _read(session_file(cfg, sid), {})
        for c in data.get("clips", []):
            if c.get("rank") == rank:
                c.update(fields)
                _atomic_write(session_file(cfg, sid), data)
                return c
    raise KeyError(f"live clip {sid}#{rank} not found")


def get_clip(cfg: Config, sid: str, rank: int) -> dict:
    for c in load_session(cfg, sid).get("clips", []):
        if c.get("rank") == rank:
            return c
    raise KeyError(f"live clip {sid}#{rank} not found")


LIVE_FOLDER = "Live streams"  # default folder of a live stream


def folder_of(s: dict) -> str:
    return s.get("folder") or LIVE_FOLDER


def set_folder(cfg: Config, sid: str, folder: str) -> dict:
    with _lock:
        data = _read(session_file(cfg, sid), {})
        if not data:
            raise KeyError(f"live stream {sid} not found")
        data["folder"] = folder or LIVE_FOLDER
        _atomic_write(session_file(cfg, sid), data)
        return data


def sessions(cfg: Config, n: int = 10) -> list[dict]:
    out = []
    if cfg.workspace.exists():
        for d in cfg.workspace.glob("live_*"):
            s = _read(d / "live.json", {})
            if s:
                out.append(s)
    out.sort(key=lambda s: str(s.get("started_at", "")), reverse=True)  # newest first (simulations included)
    return out[:n]
