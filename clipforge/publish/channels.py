"""Connected YouTube channels: OAuth (web + CLI), per-channel settings, token health."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import threading
import time
from typing import Any

from sqlmodel import select

from clipforge.api import db
from clipforge.config import Config, load_config
from clipforge.publish import youtube as yt
from clipforge.utils.log import log

# google-auth-oauthlib refuses http:// redirects unless told the transport is local (it is: 127.0.0.1)
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

CREDIT_TEMPLATE = "🎙 Source: {episode} — {source_channel} {url}"


def default_settings(cfg: Config | None = None) -> dict[str, Any]:
    p = (cfg or load_config()).publishing
    return {
        "timezone": p.timezone, "slots": list(p.slots), "peak_slots": list(p.peak_slots),
        "posts_per_day": p.posts_per_day, "min_gap_hours": p.min_gap_hours,
        "days": [0, 1, 2, 3, 4, 5, 6],          # Monday=0 … Sunday=6
        "start_date": None,                      # ISO date; None = today
        "privacy": "private",                    # uploaded as private + publishAt (YouTube publishes at the slot)
        "category_id": p.category_id, "language": p.language,
        "title_language": "hinglish",            # hinglish | english
        "description_template": "Follow for more clips from the best Indian podcasts. 🔔",
        "credit_template": CREDIT_TEMPLATE,
        "niche_tag": "#podcast",
        "default_playlist": "",
        "whop_campaign": "", "campaign_rate_per_1k": 0.0, "campaign_fee_pct": 0.0,
    }


def settings_of(ch: db.Channel, cfg: Config | None = None) -> dict[str, Any]:
    return {**default_settings(cfg), **ch.settings}


def get(channel_id: str) -> db.Channel | None:
    with db.session() as s:
        return s.get(db.Channel, channel_id)


def all_channels() -> list[db.Channel]:
    with db.session() as s:
        return list(s.exec(select(db.Channel).order_by(db.Channel.created_at)).all())


def update(channel_id: str, **fields: Any) -> db.Channel | None:
    with db.session() as s:
        ch = s.get(db.Channel, channel_id)
        if not ch:
            return None
        for k, v in fields.items():
            setattr(ch, k, v)
        s.add(ch)
        s.commit()
        s.refresh(ch)
        return ch


def mark_reauth(channel_id: str, message: str) -> None:
    update(channel_id, auth_status="needs_reauth", auth_error=message)
    log.warning(f"Channel {channel_id} needs re-login: {message}")


def missing_scopes(creds) -> list[str]:
    granted = set(getattr(creds, "granted_scopes", None) or getattr(creds, "scopes", None) or [])
    return [s for s in yt.SCOPES if granted and s not in granted]


def register(info: dict, creds, cfg: Config | None = None) -> db.Channel:
    """Store the token and upsert the channel row (keeps existing settings)."""
    missing = missing_scopes(creds)
    if yt.SCOPES[1] in missing:
        raise ValueError("ClipForge needs read access to your channel — sign in again and tick every permission.")
    yt.save_credentials(info["id"], creds, cfg)
    with db.session() as s:
        ch = s.get(db.Channel, info["id"]) or db.Channel(id=info["id"])
        ch.title, ch.handle, ch.thumbnail = info["title"], info.get("handle", ""), info.get("thumbnail", "")
        ch.uploads_playlist = info.get("uploads_playlist", "")
        ch.auth_status, ch.auth_error = "ok", ""
        if missing:  # granular consent: user unticked some boxes
            ch.auth_error = ("Not granted: " + ", ".join(m.rsplit("/", 1)[-1] for m in missing)
                             + ". Uploads (API mode) or analytics won't work — Reconnect and tick every box.")
        s.add(ch)
        s.commit()
        s.refresh(ch)
        return ch


def remove(channel_id: str, cfg: Config | None = None) -> None:
    with db.session() as s:
        ch = s.get(db.Channel, channel_id)
        if ch:
            s.delete(ch)
            s.commit()
    shutil.rmtree(yt.token_path(channel_id, cfg).parent, ignore_errors=True)


# ----------------------------------------------------------------------------- OAuth (web)
_flows: dict[str, tuple[Any, float]] = {}
_pending: dict[str, tuple[Any, list[dict], float]] = {}  # several channels came back: user picks one
_lock = threading.Lock()


def _check_secret() -> None:
    if not yt.client_secret_path().exists():
        raise FileNotFoundError("client_secret.json is missing — upload it in Settings → Publishing (see README).")


def start_oauth(redirect_uri: str) -> str:
    """Return the Google consent URL. The Flow object is kept so PKCE verification works on callback."""
    from google_auth_oauthlib.flow import Flow

    _check_secret()
    flow = Flow.from_client_secrets_file(str(yt.client_secret_path()), scopes=yt.SCOPES, redirect_uri=redirect_uri)
    state = secrets.token_urlsafe(16)
    # prompt=consent + offline -> always get a refresh token; select_account lets you pick a Brand Account channel
    url, _ = flow.authorization_url(access_type="offline", prompt="consent select_account", state=state,
                                    include_granted_scopes="true")
    with _lock:
        now = time.time()
        for k in [k for k, (_, t) in _flows.items() if now - t > 1800]:
            _flows.pop(k, None)
        _flows[state] = (flow, now)
    return url


def finish_oauth(callback_url: str, state: str) -> dict:
    """Exchange the code; returns {"connected": [...]} or {"pick": id, "channels": [...]}."""
    with _lock:
        item = _flows.pop(state, None)
    if not item:
        raise ValueError("This login link expired or was already used — start 'Add channel' again.")
    flow, _ = item
    flow.fetch_token(authorization_response=callback_url)
    return _after_login(flow.credentials)


def _after_login(creds, cfg: Config | None = None) -> dict:
    from googleapiclient.discovery import build

    cfg = cfg or load_config()
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    from clipforge.publish import quota

    quota.charge(cfg, units=1)
    resp = service.channels().list(part="snippet,contentDetails,statistics", mine=True, maxResults=50).execute()
    infos = [yt._channel_info(it) for it in resp.get("items", [])]
    if not infos:
        raise ValueError("This Google account has no YouTube channel. Create one in YouTube first.")
    if len(infos) == 1:
        ch = register(infos[0], creds, cfg)
        return {"connected": [_public(ch)]}
    pid = secrets.token_urlsafe(8)
    with _lock:
        _pending[pid] = (creds, infos, time.time())
    return {"pick": pid, "channels": infos}


def pick_pending(pid: str, channel_id: str) -> db.Channel:
    with _lock:
        item = _pending.pop(pid, None)
    if not item:
        raise ValueError("That channel choice expired — add the channel again.")
    creds, infos, _ = item
    info = next((i for i in infos if i["id"] == channel_id), None)
    if not info:
        raise ValueError("Unknown channel")
    return register(info, creds)


def pending(pid: str) -> list[dict] | None:
    with _lock:
        item = _pending.get(pid)
    return item[1] if item else None


def add_via_local_server(cfg: Config | None = None) -> dict:
    """CLI: open the browser, run a temporary localhost server for the callback."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    _check_secret()
    flow = InstalledAppFlow.from_client_secrets_file(str(yt.client_secret_path()), yt.SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent select_account")
    return _after_login(creds, cfg)


def check_auth(channel_id: str, cfg: Config | None = None) -> bool:
    """Refresh the token if needed; mark the channel when Google refuses it."""
    try:
        yt.load_credentials(channel_id, cfg)
        ch = get(channel_id)
        if ch and ch.auth_status != "ok":
            update(channel_id, auth_status="ok", auth_error="")
        return True
    except yt.NeedsReauth as exc:
        mark_reauth(channel_id, str(exc))
        return False


def _public(ch: db.Channel) -> dict:
    d = ch.model_dump()
    d["settings"] = settings_of(ch)
    d.pop("settings_json", None)
    return d


def public(ch: db.Channel) -> dict:
    return _public(ch)


def save_client_secret(data: bytes) -> dict:
    """Validate and store client_secret.json (Desktop-app OAuth client)."""
    try:
        js = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise ValueError("That file isn't JSON — download the OAuth client JSON from Google Cloud Console.") from exc
    kind = "installed" if "installed" in js else "web" if "web" in js else None
    if not kind or "client_id" not in js[kind]:
        raise ValueError("Not an OAuth client file. Create credentials → OAuth client ID → Desktop app, then download JSON.")
    yt.client_secret_path().write_text(json.dumps(js, indent=2), encoding="utf-8")
    return {"type": kind, "client_id": js[kind]["client_id"][:12] + "…"}
