"""FastAPI app factory + `clipforge ui` launcher. In production FastAPI also serves frontend/dist."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
import webbrowser
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from clipforge import __version__
from clipforge.config import FRONTEND_DIST, ROOT_DIR
from clipforge.utils.log import log

NOT_BUILT = """<!doctype html><html><body style="font-family:system-ui;background:#0b0b0f;color:#eee;padding:40px">
<h2>ClipForge API is running ✔</h2>
<p>The web UI is not built yet. Run once:</p>
<pre style="background:#16161d;padding:16px;border-radius:8px">cd frontend
npm install
npm run build</pre>
<p>…then restart <code>clipforge ui</code>. API docs: <a style="color:#ffe600" href="/docs">/docs</a></p></body></html>"""


def _quiet_windows_disconnects() -> None:
    """Windows' Proactor event loop logs a full traceback (ConnectionResetError / WinError 10054) every time a
    browser drops a connection — closing a tab, seeking a video, an SSE reconnect. It's harmless; hide just that."""
    import asyncio
    import sys

    if sys.platform != "win32":
        return
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def handler(lp, context):
        exc = context.get("exception")
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
            return
        if previous:
            previous(lp, context)
        else:
            lp.default_exception_handler(context)

    loop.set_exception_handler(handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from clipforge.api import db
    from clipforge.api.jobs import manager
    from clipforge.api.routes import load_custom_presets

    _quiet_windows_disconnects()
    db.engine()
    load_custom_presets()
    if not manager.thread.is_alive():
        manager.start()
    from clipforge.publish.worker import worker

    # CLIPFORGE_NO_TICKER=1: never contact YouTube in the background (demo / screenshots)
    worker.start(ticker=getattr(app.state, "publish_ticker", True) and os.environ.get("CLIPFORGE_NO_TICKER") != "1")
    if getattr(app.state, "publish_ticker", True) and os.environ.get("CLIPFORGE_NO_TICKER") != "1":
        from clipforge.notify import telegram

        telegram.bot().start()  # idles until a token is set and telegram.enabled is true
    yield


def create_app() -> FastAPI:
    from clipforge.api.routes import router

    app = FastAPI(title="ClipForge API", version=__version__, lifespan=lifespan,
                  description="Local API behind the ClipForge web app. Everything runs on your machine.")
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                       allow_methods=["*"], allow_headers=["*"])
    from clipforge.api.auth import AuthMiddleware, router as auth_router

    app.add_middleware(AuthMiddleware)  # login for anything not coming straight from this laptop (Phase 14)
    app.include_router(auth_router)
    app.include_router(router)
    from clipforge.api.publish_routes import router as publish_router

    app.include_router(publish_router)
    from clipforge.api.ig_routes import router as ig_router
    from clipforge.api.remote_routes import router as remote_router

    app.include_router(ig_router)
    app.include_router(remote_router)

    if (FRONTEND_DIST / "assets").exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api/"):
            return HTMLResponse("Not found", status_code=404)
        index = FRONTEND_DIST / "index.html"
        candidate = (FRONTEND_DIST / full_path).resolve()
        if full_path and candidate.is_file() and FRONTEND_DIST.resolve() in candidate.parents:
            if full_path in ("sw.js", "manifest.webmanifest"):  # PWA files must never be cached stale
                return FileResponse(candidate, headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})
            return FileResponse(candidate)
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return HTMLResponse(NOT_BUILT)

    return app


def _file_logging() -> None:
    """Also log to workspace/logs/clipforge.log (rotated: 5 × 2 MB) — the service runs without a console."""
    import logging
    from logging.handlers import RotatingFileHandler

    from clipforge.config import load_config

    d = load_config().workspace / "logs"
    d.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("clipforge")
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    h = RotatingFileHandler(d / "clipforge.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    h.setLevel(logging.INFO)
    root.addHandler(h)


def _lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def serve(host: str = "127.0.0.1", port: int = 8000, dev: bool = False, open_browser: bool = True) -> None:
    import uvicorn

    from clipforge.config import ConfigError, load_config

    try:  # fail once, clearly, instead of answering every request with a 500
        load_config()
    except ConfigError as exc:
        raise SystemExit(f"\n{exc}\n\nFix config.yaml (or delete the offending lines — defaults are used) and start again.")
    from clipforge.api.auth import password_set

    if host not in ("127.0.0.1", "localhost", "::1") and not password_set():
        raise SystemExit("\nRefusing to listen on {host}: anyone on that network could use ClipForge without a password.\n"
                         "Set one first:  clipforge auth set-password\n"
                         "(For your phone, prefer `clipforge remote setup` — Tailscale keeps it private.)".format(host=host))
    _file_logging()

    url = f"http://localhost:{port}"
    vite = None
    if dev:
        npm = shutil.which("npm")
        if not npm:
            raise SystemExit("npm not found — install Node.js to use --dev (or run without --dev).")
        vite = subprocess.Popen([npm, "run", "dev", "--", "--host", host], cwd=ROOT_DIR / "frontend")
        url = "http://localhost:5173"
    elif not (FRONTEND_DIST / "index.html").exists():
        log.warning("Web UI not built yet: cd frontend && npm install && npm run build")

    if host == "0.0.0.0":
        ip = _lan_ip()
        if ip:
            log.info(f"On your phone (same Wi-Fi) open: http://{ip}:{5173 if dev else port}")
    log.info(f"ClipForge UI → {url}   (API docs: http://localhost:{port}/docs)  —  Ctrl+C to stop")
    if open_browser:
        threading.Thread(target=lambda: (time.sleep(1.5), webbrowser.open(url)), daemon=True).start()
    try:
        uvicorn.run(create_app(), host=host, port=port, log_level="warning")
    finally:
        if vite:
            vite.terminate()
