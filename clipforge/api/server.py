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


@asynccontextmanager
async def lifespan(app: FastAPI):
    from clipforge.api import db
    from clipforge.api.jobs import manager
    from clipforge.api.routes import load_custom_presets

    db.engine()
    load_custom_presets()
    if not manager.thread.is_alive():
        manager.start()
    from clipforge.publish.worker import worker

    # CLIPFORGE_NO_TICKER=1: never contact YouTube in the background (demo / screenshots)
    worker.start(ticker=getattr(app.state, "publish_ticker", True) and os.environ.get("CLIPFORGE_NO_TICKER") != "1")
    yield


def create_app() -> FastAPI:
    from clipforge.api.routes import router

    app = FastAPI(title="ClipForge API", version=__version__, lifespan=lifespan,
                  description="Local API behind the ClipForge web app. Everything runs on your machine.")
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                       allow_methods=["*"], allow_headers=["*"])
    app.include_router(router)
    from clipforge.api.publish_routes import router as publish_router

    app.include_router(publish_router)

    if (FRONTEND_DIST / "assets").exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api/"):
            return HTMLResponse("Not found", status_code=404)
        index = FRONTEND_DIST / "index.html"
        candidate = (FRONTEND_DIST / full_path).resolve()
        if full_path and candidate.is_file() and FRONTEND_DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return HTMLResponse(NOT_BUILT)

    return app


def _lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def serve(host: str = "127.0.0.1", port: int = 8000, dev: bool = False, open_browser: bool = True) -> None:
    import uvicorn

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
