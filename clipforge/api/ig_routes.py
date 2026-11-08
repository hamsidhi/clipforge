"""Instagram (Phase 13): Meta app setup, Facebook Login, account pick, manual (assisted) accounts, permalink linking,
clip preview for the cover picker, Instagram dashboard."""

from __future__ import annotations

import datetime as dt
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlmodel import select

from clipforge.api import db
from clipforge.config import Secrets, load_config, mask_secret, save_secret
from clipforge.publish import analytics, channels as chs
from clipforge.publish import instagram as ig
from clipforge.publish import queue as q
from clipforge.publish.timez import now_utc, utc_to_local
from clipforge.utils.log import log

router = APIRouter(prefix="/api", tags=["instagram"])
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def _login_host(request: Request) -> str:
    """Where a Facebook Login may start: the laptop (localhost) or this laptop's https address on the tailnet
    (instagram.public_url / Tailscale name). Meta must send the browser back to the same place."""
    host = request.url.hostname or ""
    if host in LOCAL_HOSTS:
        return host
    base = ig.public_base()
    if base and host == base.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]:
        return host
    raise HTTPException(400, f"Open ClipForge on the laptop (http://localhost:8000) or at {base or 'its Tailscale https address'} "
                             "to connect Instagram — Meta only sends logins back to an address registered in the Meta app. "
                             "Or use 'Connect with a token'.")


# ============================================================================ Meta app
class MetaAppIn(BaseModel):
    app_id: Optional[str] = None
    app_secret: Optional[str] = None
    login_config_id: Optional[str] = None


@router.get("/instagram/app", summary="Meta app setup state (secrets masked)")
def meta_app(request: Request) -> dict:
    sec = Secrets()
    cfg = load_config()
    return {"app_id": sec.meta_app_id or "", "app_secret": mask_secret(sec.meta_app_secret),
            "login_config_id": sec.meta_login_config_id or "", "configured": ig.configured(),
            "mode": cfg.instagram.mode, "graph_version": cfg.instagram.graph_version,
            "redirect_uri": ig.redirect_uri(request.url.port or 8000),
            "redirect_uris": ig.redirect_uris(8000), "token_steps": ig.TOKEN_STEPS,
            "last_attempts": ig.attempts()[-5:],
            "scopes": [{"scope": s, "why": ig.SCOPE_HELP[s]} for s in ig.SCOPES]}


@router.post("/instagram/app", summary="Save the Meta App ID / App Secret (.env)")
def save_meta_app(body: MetaAppIn, request: Request) -> dict:
    try:
        if body.app_id is not None:
            if body.app_id and not body.app_id.strip().isdigit():
                raise ValueError("The App ID is a number (Meta for Developers → your app → App settings → Basic).")
            save_secret("META_APP_ID", body.app_id.strip())
        if body.app_secret is not None:
            save_secret("META_APP_SECRET", body.app_secret.strip())
        if body.login_config_id is not None:
            save_secret("META_LOGIN_CONFIG_ID", body.login_config_id.strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return meta_app(request)


# ============================================================================ accounts
@router.post("/instagram/oauth/start", summary="Facebook Login URL for adding an Instagram professional account")
def ig_oauth_start(request: Request) -> dict:
    host = _login_host(request)
    port = request.url.port or 80
    try:  # it must match a redirect URI registered in the Meta app EXACTLY
        uri = ig.redirect_uri(port, host)
        ig.record_attempt(step="start", redirect_uri=uri, host=host,
                          method="config_id" if Secrets().meta_login_config_id else "scope")
        return {"url": ig.oauth_url(uri), "redirect_uri": uri}
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@router.get("/instagram/oauth/callback", include_in_schema=False)
def ig_oauth_callback(code: str = "", state: str = "", error: str = "", error_reason: str = "",
                      error_description: str = "", error_code: str = "", error_message: str = "") -> Response:
    """Public (no session cookie: it is SameSite=Strict, so a redirect from facebook.com arrives without it). Safe
    because it does nothing without a valid one-time `state` from a login that a signed-in user started."""
    from urllib.parse import quote

    if error or error_code:
        ig.record_attempt(step="callback", result="meta_error", error=error or error_code, error_reason=error_reason,
                          error_description=error_description or error_message)
        log.warning(f"[instagram] Facebook Login returned an error: {error or error_code} / {error_reason} / "
                    f"{error_description or error_message}")
        return RedirectResponse(f"/channels?tab=instagram&error={quote(error_description or error_message or error or error_code)}")
    try:
        res = ig.finish_oauth(code, state)
        ig.record_attempt(step="callback", result="ok", accounts=len(res["accounts"]))
    except Exception as exc:
        import html

        ig.record_attempt(step="callback", result="failed", error=str(exc))
        log.warning(f"[instagram] login callback failed: {exc}")
        return HTMLResponse(f"<body style='font-family:system-ui;background:#111;color:#eee;padding:40px'>"
                            f"<h3>Couldn't connect Instagram</h3><p>{html.escape(str(exc))}</p>"
                            f"<a style='color:#ffe600' href='/channels?tab=instagram'>Back</a></body>", status_code=400)
    return RedirectResponse(f"/channels?tab=instagram&ig_pick={res['pick']}")


@router.get("/instagram/pick/{pid}")
def ig_pick_options(pid: str) -> list[dict]:
    opts = ig.pending(pid)
    if opts is None:
        raise HTTPException(404, "That choice expired — add the Instagram account again.")
    return opts


class IgPickIn(BaseModel):
    ig_user_id: str


@router.post("/instagram/pick/{pid}")
def ig_pick(pid: str, body: IgPickIn) -> dict:
    from clipforge.api.publish_routes import _channel_out

    try:
        return _channel_out(ig.pick(pid, body.ig_user_id))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class TokenIn(BaseModel):
    token: str
    username: str = ""   # the manual account to convert (default: the only manual Instagram account)


@router.post("/instagram/token", summary="Connect Instagram with a user token pasted from Graph API Explorer")
def ig_token(body: TokenIn) -> dict:
    """Works from the phone: the token is exchanged on the laptop (app secret stays here); a manual account with
    the same username is converted in place (queue + history kept)."""
    want = body.username
    if not want:
        manual = [c for c in chs.all_channels() if c.platform == "instagram" and ig.is_manual(c)]
        want = manual[0].extra.get("username", "") if len(manual) == 1 else ""
    try:
        res = ig.connect_with_token(body.token, prefer_username=want)
    except (ValueError, ig.IGError) as exc:
        ig.record_attempt(step="token", result="failed", error=str(exc))
        raise HTTPException(400, str(exc))
    ig.record_attempt(step="token", result="ok" if "channel_id" in res else "pick")
    if "channel_id" in res:
        ch = chs.get(res["channel_id"])
        res["eligibility"] = ig.eligibility(ch, live=True) if ch else None
    return res


@router.get("/instagram/attempts", summary="Last Instagram login attempts (no tokens)")
def ig_attempts() -> list[dict]:
    return ig.attempts()


class ManualIn(BaseModel):
    username: str


@router.post("/instagram/manual", summary="Add an Instagram account without a Meta app (assisted: you post)")
def ig_manual(body: ManualIn) -> dict:
    from clipforge.api.publish_routes import _channel_out

    try:
        return _channel_out(chs.add_manual_instagram(body.username))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ============================================================================ queue helpers
class PermalinkIn(BaseModel):
    permalink: str


@router.post("/queue/{item_id}/permalink", summary="Instagram: you posted it yourself — paste the Reel link to track it")
def ig_permalink(item_id: int, body: PermalinkIn) -> dict:
    from clipforge.api.publish_routes import _item_out

    it = q.get(item_id)
    if not it:
        raise HTTPException(404, "Unknown queue item")
    if it.platform != "instagram":
        raise HTTPException(400, "Only for Instagram clips — use Link for YouTube.")
    from urllib.parse import urlparse

    link = body.permalink.strip()
    u = urlparse(link)
    if u.scheme != "https" or u.hostname not in ("instagram.com", "www.instagram.com") or not u.path.strip("/"):
        raise HTTPException(400, "Paste the Reel's link, e.g. https://www.instagram.com/reel/AbC123/")
    others = [o for o in q.items(it.channel_id) if o.permalink and o.permalink.rstrip("/") == link.rstrip("/") and o.id != it.id]
    if others:
        raise HTTPException(409, f"That Reel is already linked to “{others[0].title}”.")
    ch = chs.get(it.channel_id)
    it.permalink, it.status, it.reason, it.error = link, "published", "", ""
    it.published_at = it.published_at or now_utc()
    if ch and not ig.is_manual(ch) and ig.configured():  # find the media id so insights work too
        try:
            token = ig.page_token(ch)
            for m in ig.client().recent_media(ig.ig_id_of(ch), token, 50):
                if m.get("permalink", "").rstrip("/") == link.rstrip("/"):
                    it.ig_media_id = m["id"]
                    break
        except Exception:
            pass
    return _item_out(q.save(it))


@router.get("/queue/{item_id}/video", include_in_schema=False)
def queue_video(item_id: int) -> FileResponse:
    it = q.get(item_id)
    if not it:
        raise HTTPException(404, "Unknown queue item")
    try:
        return FileResponse(q.clip_file(it, load_config()), media_type="video/mp4")
    except q.QueueError as exc:
        raise HTTPException(404, str(exc))


# ============================================================================ dashboard
@router.get("/accounts/{channel_id}/ig-dashboard", summary="Instagram account: followers, top Reels, best times")
def ig_dashboard(channel_id: str) -> dict:
    ch = chs.get(channel_id)
    if not ch or ch.platform != "instagram":
        raise HTTPException(404, "Unknown Instagram account")
    settings = chs.settings_of(ch)
    with db.session() as s:
        perfs = list(s.exec(select(db.VideoPerf).where(db.VideoPerf.channel_id == channel_id)).all())
        daily = sorted(s.exec(select(db.ChannelDaily).where(db.ChannelDaily.channel_id == channel_id)).all(), key=lambda r: r.day)
    published = q.items(channel_id, ("published",))
    hm = analytics.heatmap(channel_id, settings["timezone"])
    top = sorted(perfs, key=lambda p: p.views, reverse=True)[:8]
    week_ago = now_utc() - dt.timedelta(days=7)
    return {
        "account": {"id": ch.id, "title": ch.title, "extra": ch.extra, "last_sync_at": ch.last_sync_at},
        "followers": daily[-1].subscribers if daily else ch.extra.get("followers", 0),
        "followers_daily": [{"day": r.day.isoformat(), "followers": r.subscribers} for r in daily[-90:]],
        "reels": len(published), "reels_7d": sum(1 for it in published if it.published_at and it.published_at.replace(
            tzinfo=dt.timezone.utc) >= week_ago),
        "totals": {k: sum(getattr(p, k) for p in perfs) for k in ("views", "reach", "likes", "comments", "shares", "saves")},
        "top": [{"title": p.title, "views": p.views, "likes": p.likes, "shares": p.shares, "saves": p.saves,
                 "permalink": p.permalink} for p in top],
        "heatmap": hm, "recommendation": analytics.recommend_slots(hm, settings["slots"], settings["posts_per_day"]),
        "working": analytics.whats_working(channel_id),
        "reminders": _reminders(channel_id),
    }


def _reminders(channel_id: str) -> list[dict]:
    from clipforge.api.publish_routes import reminders

    return reminders(channel_id)


@router.get("/publishing/overview", summary="Combined YouTube + Instagram numbers")
def overview() -> dict:
    out = {"youtube": {"accounts": 0, "followers": 0, "posts": 0, "views": 0},
           "instagram": {"accounts": 0, "followers": 0, "posts": 0, "views": 0}}
    with db.session() as s:
        for ch in chs.all_channels():
            plat = out.get(ch.platform or "youtube")
            if plat is None:
                continue
            plat["accounts"] += 1
            last = s.exec(select(db.ChannelDaily).where(db.ChannelDaily.channel_id == ch.id)
                          .order_by(db.ChannelDaily.day.desc())).first()  # type: ignore[attr-defined]
            plat["followers"] += last.subscribers if last else int(ch.extra.get("followers", 0) or 0)
            plat["posts"] += len(q.items(ch.id, ("published",)))
            plat["views"] += sum(p.views for p in s.exec(select(db.VideoPerf).where(db.VideoPerf.channel_id == ch.id)).all())
    zone = load_config().publishing.timezone
    upcoming = []
    for it in q.items(statuses=("queued", "uploading", "ready", "scheduled")):
        if it.slot_utc and it.slot_utc.replace(tzinfo=dt.timezone.utc) >= now_utc():
            upcoming.append({"id": it.id, "platform": it.platform, "title": it.title,
                             "slot_local": utc_to_local(it.slot_utc, zone).strftime("%Y-%m-%dT%H:%M")})
    out["upcoming"] = upcoming[:8]  # type: ignore[assignment]
    return out
