"""Settings → Phone & Telegram (Phase 14): Tailscale status/setup, QR code, Telegram bot token + pairing."""

from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from clipforge.config import Secrets, load_config, mask_secret, save_config, save_secret

router = APIRouter(prefix="/api", tags=["remote"])


def _local(request: Request) -> bool:
    from clipforge.api.auth import is_local

    return is_local(request)


@router.get("/remote/status", summary="Tailscale + remote access state")
def remote_status(request: Request) -> dict:
    from clipforge import remote
    from clipforge.api.auth import password_set

    st = remote.status(request.url.port or 8000) if _local(request) else remote.RemoteStatus(installed=True, serving=True)
    d = asdict(st)
    d.update({"local": _local(request), "password_set": password_set(), "here": str(request.base_url).rstrip("/")})
    return d


@router.post("/remote/setup", summary="Run `tailscale serve` for ClipForge (laptop only)")
def remote_setup(request: Request) -> dict:
    from clipforge import remote
    from clipforge.api.auth import password_set

    if not _local(request):
        raise HTTPException(400, "Run this on the laptop.")
    if not password_set():
        raise HTTPException(400, "Set a password first (below) — anyone in your tailnet could otherwise use ClipForge.")
    return asdict(remote.setup(request.url.port or 8000))


@router.get("/remote/qr.svg", include_in_schema=False)
def remote_qr(url: str) -> Response:
    from clipforge import remote

    if not url.startswith("https://") or len(url) > 300:
        raise HTTPException(400, "Only https links")
    return Response(remote.qr_svg(url), media_type="image/svg+xml")


# ----------------------------------------------------------------------------- telegram
def _bot():
    from clipforge.notify import telegram

    return telegram.bot()


@router.get("/telegram", summary="Telegram bot state (token masked)")
def telegram_state() -> dict:
    sec = Secrets()
    cfg = load_config().telegram
    return {"token": mask_secret(sec.telegram_bot_token), "paired": bool(sec.telegram_chat_id), "enabled": cfg.enabled,
            "running": bool(_bot().thread and _bot().thread.is_alive()), "settings": cfg.model_dump()}


class TokenIn(BaseModel):
    token: str


@router.post("/telegram/token", summary="Save the bot token from @BotFather (checked with getMe)")
def telegram_token(body: TokenIn) -> dict:
    from clipforge.notify import telegram as tg

    token = body.token.strip()
    try:
        me = tg.BotAPI(token, _bot().transport).call("getMe")
    except tg.TgError as exc:
        raise HTTPException(400, f"Telegram didn't accept that token: {exc}")
    save_secret("TELEGRAM_BOT_TOKEN", token)
    save_secret("TELEGRAM_CHAT_ID", "")  # a new bot needs a new pairing
    cfg = load_config()
    save_config(cfg.with_overrides({"telegram.enabled": True}))
    _bot().start()
    return {**telegram_state(), "bot_username": me.get("username")}


@router.post("/telegram/pair", summary="One-time code: send `/pair <code>` to your bot within 10 minutes")
def telegram_pair() -> dict:
    if not Secrets().telegram_bot_token:
        raise HTTPException(400, "Save the bot token first.")
    b = _bot()
    b.start()
    return {"code": b.new_pair_code(), "expires_in": 600}


@router.post("/telegram/unpair")
def telegram_unpair() -> dict:
    save_secret("TELEGRAM_CHAT_ID", "")
    return telegram_state()


class TgSettings(BaseModel):
    enabled: Optional[bool] = None


@router.post("/telegram/test")
def telegram_test() -> dict:
    ok = _bot().send_text("✅ ClipForge test message — notifications work.")
    if not ok:
        raise HTTPException(400, "Not paired yet (or no token).")
    return {"ok": True}
