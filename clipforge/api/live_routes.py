"""Live page (2026-11-15): paste a live stream link (YouTube / Twitch / Kick) → watched streams with meters,
CLIP NOW / pause / auto-post / stop per stream, settings, each stream's clips (post / queue / discard), best-of, and
which accounts get live clips (never the podcast accounts unless switched on)."""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from clipforge.config import Config, load_config, save_config
from clipforge.live import store

router = APIRouter(prefix="/api/live", tags=["live"])

SETTINGS_KEYS = {"enabled", "max_streams", "watch_channels", "game_pack", "max_cores", "cpu_ceiling", "cooldown_sec",
                 "max_clips_per_hour", "pre_roll", "post_roll", "min_score", "threshold_pct", "captions", "auto_post",
                 "auto_post_min_score", "min_post_gap_min", "encoder", "stream_height"}
CHAT_KEYS = {"chat_twitch": "twitch", "chat_kick": "kick", "chat_youtube": "youtube"}


def _session_out(cfg: Config, s: dict) -> dict:
    return {k: s.get(k) for k in ("id", "started_at", "ended_at", "stream_url", "live", "simulated", "game",
                                  "game_label", "source", "best_of", "streamer", "platform", "title")} | {
        "folder": store.folder_of(s)} | {
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
            "settings": {k: getattr(lc, k) for k in sorted(SETTINGS_KEYS)}
            | {k: getattr(lc.chat, v) for k, v in CHAT_KEYS.items()},
            "amf": amf_available(),
            "gaming_accounts": [{"id": c.id, "title": c.title, "platform": c.platform or "youtube"} for c in gaming_accounts()],
            "sessions": [_session_out(cfg, s) for s in store.sessions(cfg, 8)]}


class StreamIn(BaseModel):
    url: str
    folder: str = ""


@router.post("/streams", summary="Start watching a live stream link (YouTube / Twitch / Kick …)")
def start_stream(body: StreamIn) -> dict:
    cfg = load_config()
    url = body.url.strip()
    if not url.lower().startswith(("http://", "https://")) or len(url) > 500:
        raise HTTPException(400, "Paste the stream's link, e.g. https://www.twitch.tv/name")
    if not cfg.live.enabled:
        raise HTTPException(409, "Live clipping is off — switch it on in the settings below.")
    st = store.read_status(cfg)
    if not st.get("engine_running"):
        raise HTTPException(409, "The live agent isn't running yet — try again in a few seconds.")
    from clipforge.services import jobs as job_svc

    folder = job_svc.clean_folder(body.folder) or store.LIVE_FOLDER
    store.send_command(cfg, "start", url=url, folder=folder)
    return {"ok": True, "folder": folder, "message": f"Opening the stream — it shows up here in about 20 s (folder: {folder})."}


@router.get("/sessions", summary="Every live stream clipped so far (newest first), with its folder")
def list_sessions() -> list[dict]:
    cfg = load_config()
    return [{k: v for k, v in _session_out(cfg, s).items() if k != "clips"} | {"n_clips": len(s.get("clips", []))}
            for s in store.sessions(cfg, 1000) if not s.get("simulated")]


class FolderIn(BaseModel):
    folder: str = ""


@router.put("/sessions/{sid}/folder", summary="Move a live stream to another folder")
def move_session(sid: str, body: FolderIn) -> dict:
    from clipforge.services import jobs as job_svc

    cfg = load_config()
    try:
        s = store.set_folder(cfg, sid, job_svc.clean_folder(body.folder))
    except KeyError:
        raise HTTPException(404, "That live stream doesn't exist.")
    return {"ok": True, "folder": s["folder"]}


@router.delete("/streams/{sid}", summary="Stop watching a stream (clips in progress finish)")
def stop_stream(sid: str) -> dict:
    store.send_command(load_config(), "stop", sid=sid)
    return {"ok": True}


class ClipIn(BaseModel):
    note: str = ""
    sid: str = ""


@router.post("/clip", summary="CLIP NOW on a watched stream (same as /clip on Telegram)")
def clip_now(body: ClipIn) -> dict:
    cfg = load_config()
    st = store.read_status(cfg)
    live = [s["session"] for s in st.get("streams", [])] if st.get("engine_running") else []
    if not live:
        raise HTTPException(409, "No live stream is being watched right now.")
    store.send_command(cfg, "clip", note=body.note[:80], source="app", sid=body.sid or live[0])
    return {"ok": True}


class ControlIn(BaseModel):
    action: Literal["pause", "resume", "auto_on", "auto_off"]
    sid: str = ""


@router.post("/control", summary="Pause / resume clipping, auto-post on / off (one stream or all)")
def control(body: ControlIn) -> dict:
    cfg = load_config()
    if body.action.startswith("auto"):
        store.send_command(cfg, "auto", on=body.action == "auto_on", sid=body.sid)
    else:
        store.send_command(cfg, body.action, sid=body.sid)
    return {"ok": True}


class SettingsIn(BaseModel):
    values: dict[str, Any]


@router.put("/settings", summary="Save live settings (config.yaml → live)")
def save_settings(body: SettingsIn) -> dict:
    cfg = load_config()
    data = cfg.model_dump()
    for k, v in body.values.items():
        if k in CHAT_KEYS:
            data["live"]["chat"][CHAT_KEYS[k]] = bool(v)
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


@router.put("/accounts/{channel_id}", summary="Switch an account on/off for live clips")
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


@router.get("/accounts", summary="All accounts and whether they get live clips")
def accounts() -> list[dict]:
    from clipforge.publish import channels as chs

    return [{"id": c.id, "title": c.title, "platform": c.platform or "youtube",
             "gaming": str((c.settings or {}).get("content", "")).lower() == "gaming",
             "ok": c.auth_status == "ok" and c.confirmed and not c.paused} for c in chs.all_channels()]
