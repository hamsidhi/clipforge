"""Settings → Phone & Telegram (Phase 14): Tailscale status/setup, QR code, Telegram bot token + pairing."""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal, Optional

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


def _laptop_only(request: Request) -> None:
    """Bot token, pairing and the PIN are security settings: only on the computer itself (or its CLI over SSH)."""
    if not _local(request):
        raise HTTPException(403, "Do this on the computer itself (or `clipforge telegram …` over SSH) — not remotely.")


@router.get("/telegram", summary="Telegram bot state (token masked)")
def telegram_state() -> dict:
    from clipforge.notify import tg_core
    from clipforge.notify import tg_store as st

    sec = Secrets()
    cfg = load_config().telegram
    b = _bot()
    return {"token": mask_secret(sec.telegram_bot_token), "paired": bool(st.chats()), "enabled": cfg.enabled,
            "running": bool(b.thread and b.thread.is_alive()), "polling": b.polling, "settings": cfg.model_dump(),
            "pin_set": bool(sec.telegram_pin_hash), "pin_required": tg_core.pin_enabled(),
            "devices": [{"chat_id": c.chat_id, "name": c.name, "role": c.role, "kind": c.kind,
                         "paired_at": c.paired_at, "last_seen": c.last_seen} for c in st.chats()],
            "poller": st.poller_owner(), "mode": load_config().deployment.mode}


class TokenIn(BaseModel):
    token: str


@router.post("/telegram/token", summary="Save the bot token from @BotFather (checked with getMe)")
def telegram_token(body: TokenIn, request: Request) -> dict:
    from clipforge.notify import telegram as tg
    from clipforge.notify import tg_store as st

    _laptop_only(request)
    token = body.token.strip()
    try:
        me = tg.BotAPI(token, _bot().transport, load_config().telegram.api_base_url).call("getMe")
    except tg.TgError as exc:
        raise HTTPException(400, f"Telegram didn't accept that token: {exc}")
    save_secret("TELEGRAM_BOT_TOKEN", token)
    st.remove_all_chats()  # a new bot needs a new pairing
    st.kv_del("offset")
    cfg = load_config()
    save_config(cfg.with_overrides({"telegram.enabled": True}))
    _bot().start()
    return {**telegram_state(), "bot_username": me.get("username")}


class PairIn(BaseModel):
    role: Literal["owner", "viewer"] = "owner"
    allow_group: bool = False


@router.post("/telegram/pair", summary="One-time code: send `/pair <code>` to your bot within 10 minutes")
def telegram_pair(request: Request, body: PairIn = PairIn()) -> dict:
    _laptop_only(request)
    if not Secrets().telegram_bot_token:
        raise HTTPException(400, "Save the bot token first.")
    b = _bot()
    b.start()
    return {"code": b.new_pair_code(body.role, body.allow_group), "expires_in": 600, "role": body.role}


class UnpairIn(BaseModel):
    chat_id: Optional[int] = None  # None = every device


@router.post("/telegram/unpair")
def telegram_unpair(request: Request, body: UnpairIn = UnpairIn()) -> dict:
    from clipforge.api.auth import audit
    from clipforge.notify import tg_store as st

    _laptop_only(request)
    if body.chat_id is None:
        st.remove_all_chats()
        audit("telegram", "Unpaired every Telegram chat (Settings)")
    elif not st.remove_chat(body.chat_id):
        raise HTTPException(404, "Not a paired chat")
    else:
        audit("telegram", f"Unpaired Telegram chat {body.chat_id} (Settings)")
    return telegram_state()


class PinIn(BaseModel):
    pin: str = ""                 # 4–8 digits; empty = remove the PIN
    required: Optional[bool] = None


@router.post("/telegram/pin", summary="Set / remove the PIN asked before dangerous Telegram actions (stored hashed)")
def telegram_pin(body: PinIn, request: Request) -> dict:
    from clipforge.api.auth import audit
    from clipforge.notify import tg_core

    _laptop_only(request)
    if body.pin:
        try:
            tg_core.set_pin(body.pin)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        audit("telegram", "Telegram PIN set")
    elif body.required is None:
        tg_core.clear_pin()
        audit("telegram", "Telegram PIN removed")
    req = body.required if body.required is not None else bool(body.pin)
    cfg = load_config()
    save_config(cfg.with_overrides({"telegram.pin_required": req}))
    return telegram_state()


@router.post("/telegram/test")
def telegram_test() -> dict:
    ok = _bot().send_text("✅ ClipForge test message — notifications work.")
    if not ok:
        raise HTTPException(400, "Not paired yet (or no token).")
    return {"ok": True}


class WebAppIn(BaseModel):
    init_data: str


@router.post("/telegram/webapp", summary="Mini App: check Telegram initData (HMAC) and that the user is paired")
def telegram_webapp(body: WebAppIn) -> dict:
    """Extra check when the app is opened inside Telegram. It never logs you in by itself: the ClipForge session
    (password) is still required for every /api call (the AuthMiddleware runs before this route)."""
    from clipforge.notify import tg_store as st
    from clipforge.notify.webapp import InitDataError, validate_init_data

    if not load_config().telegram.mini_app:
        raise HTTPException(404, "Mini App is off (telegram.mini_app)")
    try:
        data = validate_init_data(body.init_data, Secrets().telegram_bot_token or "")
    except InitDataError as exc:
        raise HTTPException(403, str(exc))
    uid = int(data["user"]["id"])
    paired = [c for c in st.chats() if c.user_id == uid]
    if not paired:
        raise HTTPException(403, "This Telegram account isn't paired with ClipForge.")
    return {"ok": True, "user_id": uid, "role": paired[0].role}
