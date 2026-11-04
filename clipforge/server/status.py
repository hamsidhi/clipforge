"""Machine status for Telegram /server and `clipforge server status`: uptime, disk, RAM, CPU, load, queue, workers,
current jobs, last backup. No psutil: /proc on Linux, ctypes on Windows, best effort elsewhere."""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import socket
import sys
import time
from pathlib import Path

from clipforge.config import load_config

BOOT = time.time()


def node_name() -> str:
    return load_config().deployment.node_name or socket.gethostname()


def memory() -> dict:
    """{'total', 'available'} in bytes (0 when unknown)."""
    try:
        if sys.platform.startswith("linux"):
            info = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
            return {"total": info.get("MemTotal", 0), "available": info.get("MemAvailable", 0)}
        if sys.platform == "win32":
            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = MS()
            ms.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))  # type: ignore[attr-defined]
            return {"total": ms.ullTotalPhys, "available": ms.ullAvailPhys}
    except Exception:
        pass
    return {"total": 0, "available": 0}


def uptime() -> float:
    try:
        if sys.platform.startswith("linux"):
            return float(Path("/proc/uptime").read_text().split()[0])
        if sys.platform == "win32":
            return ctypes.windll.kernel32.GetTickCount64() / 1000  # type: ignore[attr-defined]
    except Exception:
        pass
    return time.time() - BOOT


def load() -> tuple[float, float, float] | None:
    try:
        return os.getloadavg()  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None


def snapshot() -> dict:
    cfg = load_config()
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    du = shutil.disk_usage(cfg.workspace)
    out = {
        "node": node_name(), "mode": cfg.deployment.mode, "platform": f"{platform.system()} {platform.machine()}",
        "cpus": os.cpu_count() or 0, "load": load(), "uptime_sec": uptime(), "app_uptime_sec": time.time() - BOOT,
        "memory": memory(), "disk": {"total": du.total, "used": du.used, "free": du.free,
                                     "pct": round(100 * du.used / du.total, 1) if du.total else 0},
        "jobs": [], "workers": [], "tasks": {}, "last_backup": None,
    }
    try:
        from clipforge.api import db

        out["jobs"] = [{"id": j.id, "title": j.title, "stage": j.stage, "progress": j.progress}
                       for j in db.list_jobs() if j.status in ("running", "queued")]
    except Exception:
        pass
    try:
        from clipforge.server import leases

        out["workers"] = leases.workers()
        out["tasks"] = leases.counts()
    except Exception:
        pass
    try:
        from clipforge.server import backup

        out["last_backup"] = backup.last_backup(cfg)
    except Exception:
        pass
    return out


def _dur(sec: float) -> str:
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    return f"{d}d {h}h" if d else f"{h}h {rem // 60}m"


def _gb(n: float) -> str:
    return f"{n / 1024 ** 3:.1f} GB"


def telegram_text() -> str:
    from clipforge.notify.tg_api import esc

    s = snapshot()
    mem = s["memory"]
    lines = [f"🖥 <b>{esc(s['node'])}</b> · {s['mode']} · {esc(s['platform'])} · {s['cpus']} CPU",
             f"Up {_dur(s['uptime_sec'])} (ClipForge {_dur(s['app_uptime_sec'])})"]
    if s["load"]:
        lines.append("Load: " + " / ".join(f"{x:.2f}" for x in s["load"]))
    if mem["total"]:
        lines.append(f"RAM: {_gb(mem['total'] - mem['available'])} used of {_gb(mem['total'])}")
    d = s["disk"]
    lines.append(f"Disk: {d['pct']}% used · {_gb(d['free'])} free")
    if s["jobs"]:
        lines.append("Jobs: " + "; ".join(f"#{j['id']} {j['stage']} {round((j['progress'] or 0) * 100)}%" for j in s["jobs"]))
    else:
        lines.append("Jobs: none running")
    if s["tasks"]:
        lines.append("Tasks: " + ", ".join(f"{k} {v}" for k, v in s["tasks"].items()))
    if s["mode"] == "server":
        w = s["workers"]
        lines.append("Workers: " + (", ".join(f"{'🟢' if x['online'] else '⚪'} {esc(x['name'])}" for x in w) if w else "none added"))
    b = s["last_backup"]
    lines.append(f"Last backup: {esc(b['name'])} ({b['age']})" if b else "Last backup: none yet")
    return "\n".join(lines)
