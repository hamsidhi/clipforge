"""Live gaming page (2026-11-15): status/meters, CLIP NOW, pause/resume/auto, settings, clips of each stream,
post/queue/discard, best-of, and marking accounts as gaming (live clips never go to podcast accounts)."""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from clipforge.config import Config, load_config, save_config
from clipforge.live import store

router = APIRouter(prefix="/api/live", tags=["live"])

SETTINGS_KEYS = {"enabled", "source", "stream_urls", "game_pack", "max_cores", "cpu_ceiling", "cooldown_sec",
                 "max_clips_per_hour", "pre_roll", "post_roll", "min_score", "threshold_pct", "captions", "auto_post",
                 "auto_post_min_score", "min_post_gap_min", "hotkey", "allowed_clip_commanders", "encoder"}


def _session_out(cfg: Config, s: dict) -> dict:
    return {k: s.get(k) for k in ("id", "started_at", "ended_at", "stream_url", "live", "simulated", "game",
                                  "game_label", "source", "best_of")} | {
        "clips": [{k: c.get(k) for k in ("rank", "trigger", "score", "reason", "title", "duration", "status",
                                         "captions", "render", "posted", "hashtags", "created_at", "manual")}
                  for c in s.get("clips", [])]}


@router.get("/status", summary="Live agent status, meters, settings and recent streams")
def live_status() -> dict:
    from clipforge.live.publish import gaming_accounts
    from clipforge.live.render import amf_available

    cfg = load_config()
    lc = cfg.live
    return {"enabled": lc.enabled, "status": store.read_status(cfg),
            "settings": {k: getattr(lc, k) for k in sorted(SETTINGS_KEYS)} | {"twitch_channel": lc.chat.twitch_channel,
                                                                             "youtube_chat": lc.chat.youtube_live},
            "amf": amf_available(),
            "gaming_accounts": [{"id": c.id, "title": c.title, "platform": c.platform or "youtube"} for c in gaming_accounts()],
            "sessions": [_session_out(cfg, s) for s in store.sessions(cfg, 8)]}


class ClipIn(BaseModel):
    note: str = ""


@router.post("/clip", summary="CLIP NOW (same as /clip on Telegram or the hotkey)")
def clip_now(body: ClipIn) -> dict:
    cfg = load_config()
    st = store.read_status(cfg)
    if not st.get("engine_running") or not (st.get("live") or {}).get("session"):
        raise HTTPException(409, "You're not live right now (or the live agent isn't running).")
    store.send_command(cfg, "clip", note=body.note[:80], source="app")
    return {"ok": True}


class ControlIn(BaseModel):
    action: Literal["pause", "resume", "auto_on", "auto_off"]


@router.post("/control", summary="Pause / resume clipping, auto-post on / off (this stream)")
def control(body: ControlIn) -> dict:
    cfg = load_config()
    if body.action.startswith("auto"):
        store.send_command(cfg, "auto", on=body.action == "auto_on")
    else:
        store.send_command(cfg, body.action)
    return {"ok": True}


class SettingsIn(BaseModel):
    values: dict[str, Any]


@router.put("/settings", summary="Save live settings (config.yaml → live)")
def save_settings(body: SettingsIn) -> dict:
    cfg = load_config()
    data = cfg.model_dump()
    for k, v in body.values.items():
        if k == "twitch_channel":
            data["live"]["chat"]["twitch_channel"] = str(v).strip()
        elif k == "youtube_chat":
            data["live"]["chat"]["youtube_live"] = bool(v)
        elif k in SETTINGS_KEYS:
            data["live"][k] = v
        else:
            raise HTTPException(400, f"Unknown live setting: {k}")
    try:
        new = Config.model_validate(data)
    except Exception as exc:
        raise HTTPException(400, f"Invalid live settings: {exc}")
    save_config(new)
    return {"ok": True}


def _clip_path(sid: str, rank: int) -> tuple[Config, dict]:
    cfg = load_config()
    if not sid.startswith("live_") or "/" in sid or "\\" in sid or ".." in sid:
        raise HTTPException(404, "Unknown stream")
    try:
        return cfg, store.get_clip(cfg, sid, rank)
    except KeyError:
        raise HTTPException(404, "Unknown live clip")


@router.get("/sessions/{sid}/clips/{rank}/video", summary="A live clip's video file")
def clip_video(sid: str, rank: int) -> FileResponse:
    cfg, c = _clip_path(sid, rank)
    f = cfg.workspace / sid / "clips" / c["file"]
    if not f.exists():
        raise HTTPException(404, "The clip file is gone")
    return FileResponse(f, media_type="video/mp4")


@router.get("/sessions/{sid}/clips/{rank}/thumb", summary="A live clip's thumbnail")
def clip_thumb(sid: str, rank: int) -> FileResponse:
    cfg, _ = _clip_path(sid, rank)
    f = cfg.workspace / sid / "clips" / "_work" / f"{rank:02d}" / "thumb.jpg"
    if not f.exists():
        raise HTTPException(404, "No thumbnail")
    return FileResponse(f, media_type="image/jpeg")


class PostIn(BaseModel):
    mode: Literal["now", "queue"] = "now"
    title: Optional[str] = None


@router.post("/sessions/{sid}/clips/{rank}/post", summary="Post now / queue a live clip on the gaming accounts")
def post(sid: str, rank: int, body: PostIn) -> dict:
    from clipforge.live.publish import post_clip

    cfg, _ = _clip_path(sid, rank)
    try:
        return {"lines": post_clip(cfg, sid, rank, body.mode, body.title)}
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.post("/sessions/{sid}/clips/{rank}/discard", summary="Discard a live clip")
def discard(sid: str, rank: int) -> dict:
    cfg, _ = _clip_path(sid, rank)
    store.update_clip(cfg, sid, rank, status="discarded")
    return {"ok": True}


@router.post("/sessions/{sid}/bestof", summary="Render the long highlight video of a stream")
def bestof(sid: str) -> dict:
    from clipforge.live.bestof import make_best_of
    from clipforge.publish.worker import worker

    cfg = load_config()
    if not store.load_session(cfg, sid):
        raise HTTPException(404, "Unknown stream")
    worker.submit(f"bestof:{sid}", "Live best-of video", lambda: str(make_best_of(cfg, sid)))
    return {"ok": True, "message": "Rendering the highlight video — it shows up here in a few minutes."}


@router.get("/sessions/{sid}/bestof/video", summary="The stream's long highlight video")
def bestof_video(sid: str) -> FileResponse:
    cfg = load_config()
    s = store.load_session(cfg, sid) if sid.startswith("live_") and ".." not in sid else {}
    if not s.get("best_of"):
        raise HTTPException(404, "No highlight video yet")
    return FileResponse(cfg.workspace / sid / s["best_of"], media_type="video/mp4")


class GamingIn(BaseModel):
    gaming: bool


@router.put("/accounts/{channel_id}", summary="Mark an account as a gaming account (gets live clips)")
def set_gaming(channel_id: str, body: GamingIn) -> dict:
    from clipforge.publish import channels as chs

    ch = chs.get(channel_id)
    if not ch:
        raise HTTPException(404, "Unknown account")
    settings = dict(ch.settings or {})
    settings["content"] = "gaming" if body.gaming else "podcast"
    import json

    chs.update(channel_id, settings_json=json.dumps(settings))
    return {"ok": True, "content": settings["content"]}


@router.get("/accounts", summary="All accounts with their content type (gaming / podcast)")
def accounts() -> list[dict]:
    from clipforge.publish import channels as chs

    return [{"id": c.id, "title": c.title, "platform": c.platform or "youtube",
             "gaming": str((c.settings or {}).get("content", "")).lower() == "gaming",
             "ok": c.auth_status == "ok" and c.confirmed and not c.paused} for c in chs.all_channels()]
