"""YouTube Data API v3 + Analytics API v2 access, per channel.

Everything that talks to Google goes through `YouTubeClient` so tests can swap in a fake
(`set_client_factory`). Every call is charged to the local quota log (see quota.py).

Costs (official docs, checked 2026-11-04): videos.insert has its own bucket of 100 calls/day;
everything else shares 10,000 units/day: videos.list 1, playlistItems.list 1, channels.list 1,
thumbnails.set 50, playlistItems.insert 50, videos.update 50. Analytics API has a separate quota.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Protocol

from clipforge.config import Config, load_config
from clipforge.publish import quota
from clipforge.utils.log import log

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",        # upload videos
    "https://www.googleapis.com/auth/youtube.readonly",      # read your channel, uploads, video status/stats
    "https://www.googleapis.com/auth/yt-analytics.readonly",  # per-video views at 24 h / 72 h, Shorts views
]
SCOPE_HELP = {
    SCOPES[0]: "Upload videos to this channel (only when you use API mode).",
    SCOPES[1]: "Read the channel name, your uploads and their status/statistics (for sync and duplicate checks).",
    SCOPES[2]: "Read YouTube Analytics (views per Short, Shorts views for the YPP progress bars).",
}
COSTS = {"videos.list": 1, "playlistItems.list": 1, "channels.list": 1, "thumbnails.set": 50,
         "playlistItems.insert": 50, "videos.update": 50}


class NeedsReauth(RuntimeError):
    """The saved login no longer works (e.g. invalid_grant after 7 days in OAuth 'Testing' status)."""


class QuotaExceeded(RuntimeError):
    pass


def channels_root(cfg: Config | None = None) -> Path:
    cfg = cfg or load_config()
    p = cfg.workspace / "channels"
    p.mkdir(parents=True, exist_ok=True)
    return p


def token_path(channel_id: str, cfg: Config | None = None) -> Path:
    return channels_root(cfg) / channel_id / "token.json"


def client_secret_path() -> Path:
    from clipforge.config import ROOT_DIR

    return ROOT_DIR / "client_secret.json"


class YouTubeClient(Protocol):
    channel_id: str

    def my_channels(self) -> list[dict]: ...
    def upload(self, path: Path, body: dict, on_progress: Callable[[float], None] | None = None) -> str: ...
    def videos(self, ids: list[str]) -> list[dict]: ...
    def uploads(self, playlist_id: str, max_items: int = 100) -> list[str]: ...
    def channel_stats(self) -> dict: ...
    def analytics(self, start: str, end: str, metrics: str, dimensions: str | None = None,
                  filters: str | None = None) -> list[list]: ...


def save_credentials(channel_id: str, creds, cfg: Config | None = None) -> None:
    p = token_path(channel_id, cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(creds.to_json(), encoding="utf-8")


def load_credentials(channel_id: str, cfg: Config | None = None):
    """Load + refresh the channel's token. Raises NeedsReauth when Google rejects it."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    p = token_path(channel_id, cfg)
    if not p.exists():
        raise NeedsReauth("This channel isn't connected yet (no saved login).")
    creds = Credentials.from_authorized_user_file(str(p), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise NeedsReauth("Saved login has no refresh token — reconnect the channel.")
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise NeedsReauth(
                "Google rejected the saved login (invalid_grant). If your OAuth consent screen is in 'Testing', "
                "logins expire after 7 days — click Reconnect, or set the consent screen to 'In production'."
            ) from exc
        save_credentials(channel_id, creds, cfg)
    return creds


class GoogleYouTube:
    """Real client (google-api-python-client)."""

    def __init__(self, channel_id: str, creds, cfg: Config) -> None:
        from googleapiclient.discovery import build

        self.channel_id = channel_id
        self.cfg = cfg
        self.yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
        self._creds = creds

    def _charge(self, method: str, calls: int = 1) -> None:
        quota.charge(self.cfg, units=COSTS.get(method, 1) * calls)

    def my_channels(self) -> list[dict]:
        self._charge("channels.list")
        resp = self.yt.channels().list(part="snippet,contentDetails,statistics", mine=True, maxResults=50).execute()
        return [_channel_info(it) for it in resp.get("items", [])]

    def upload(self, path: Path, body: dict, on_progress: Callable[[float], None] | None = None) -> str:
        from googleapiclient.http import MediaFileUpload

        if quota.remaining(self.cfg)["inserts"] <= 0:
            raise QuotaExceeded("Daily upload quota (videos.insert) used up — continuing tomorrow (Pacific time).")
        media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True, chunksize=8 << 20)
        req = self.yt.videos().insert(part="snippet,status", body=body, media_body=media)
        quota.charge(self.cfg, inserts=1)
        resp = None
        while resp is None:
            status, resp = req.next_chunk(num_retries=5)
            if status and on_progress:
                on_progress(status.progress())
        return resp["id"]

    def videos(self, ids: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(ids), 50):
            self._charge("videos.list")
            resp = self.yt.videos().list(part="snippet,status,contentDetails,statistics,processingDetails,fileDetails",
                                         id=",".join(ids[i : i + 50])).execute()
            out += resp.get("items", [])
        return out

    def uploads(self, playlist_id: str, max_items: int = 100) -> list[str]:
        ids: list[str] = []
        token = None
        while len(ids) < max_items:
            self._charge("playlistItems.list")
            resp = self.yt.playlistItems().list(part="contentDetails", playlistId=playlist_id, maxResults=50,
                                                pageToken=token).execute()
            ids += [it["contentDetails"]["videoId"] for it in resp.get("items", [])]
            token = resp.get("nextPageToken")
            if not token:
                break
        return ids[:max_items]

    def channel_stats(self) -> dict:
        self._charge("channels.list")
        resp = self.yt.channels().list(part="statistics", id=self.channel_id).execute()
        st = (resp.get("items") or [{}])[0].get("statistics", {})
        return {"subscribers": int(st.get("subscriberCount", 0)), "views": int(st.get("viewCount", 0)),
                "videos": int(st.get("videoCount", 0))}

    def analytics(self, start: str, end: str, metrics: str, dimensions: str | None = None,
                  filters: str | None = None) -> list[list]:
        from googleapiclient.discovery import build

        ya = build("youtubeAnalytics", "v2", credentials=self._creds, cache_discovery=False)
        kw = {"ids": "channel==MINE", "startDate": start, "endDate": end, "metrics": metrics}
        if dimensions:
            kw["dimensions"] = dimensions
        if filters:
            kw["filters"] = filters
        return ya.reports().query(**kw).execute().get("rows") or []


def _channel_info(item: dict) -> dict:
    sn = item.get("snippet", {})
    thumbs = sn.get("thumbnails", {})
    thumb = (thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
    return {
        "id": item["id"], "title": sn.get("title", ""), "handle": sn.get("customUrl", ""), "thumbnail": thumb,
        "uploads_playlist": item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", ""),
        "subscribers": int(item.get("statistics", {}).get("subscriberCount", 0) or 0),
    }


def _default_factory(channel_id: str, cfg: Config) -> YouTubeClient:
    return GoogleYouTube(channel_id, load_credentials(channel_id, cfg), cfg)


_factory: Callable[[str, Config], YouTubeClient] = _default_factory


def set_client_factory(factory: Callable[[str, Config], YouTubeClient] | None) -> None:
    """Tests install a fake here; None restores the real Google client."""
    global _factory
    _factory = factory or _default_factory


def revoke(channel_id: str, cfg: Config | None = None) -> str:
    """Revoke ClipForge's access at Google (best effort) — the Disconnect button. Returns a status sentence."""
    p = token_path(channel_id, cfg)
    if _factory is not _default_factory:  # tests / demo: never call Google
        return "Access removed (test mode)."
    if not p.exists():
        return "No saved login."
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        token = data.get("refresh_token") or data.get("token")
        import httpx

        r = httpx.post("https://oauth2.googleapis.com/revoke", data={"token": token}, timeout=15)
        if r.status_code == 200:
            return "Access revoked at Google."
        return f"Google answered {r.status_code} — remove ClipForge at https://myaccount.google.com/permissions to be sure."
    except Exception as exc:
        return f"Couldn't reach Google ({exc}) — remove ClipForge at https://myaccount.google.com/permissions."


def client_for(channel_id: str, cfg: Config | None = None) -> YouTubeClient:
    return _factory(channel_id, cfg or load_config())


def parse_duration(iso: str) -> float:
    """ISO-8601 'PT1M5S' -> 65.0"""
    import re

    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", iso or "")
    if not m:
        return 0.0
    d, h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def describe_http_error(exc: Exception) -> str:
    try:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError):
            data = json.loads(exc.content.decode("utf-8", "replace"))
            err = data.get("error", {})
            reasons = ",".join(e.get("reason", "") for e in err.get("errors", []))
            return f"{err.get('code')} {reasons}: {err.get('message', '')}"
    except Exception:
        pass
    log.debug(f"unparsed API error: {exc}")
    return str(exc)
