"""Phase 10 — optional YouTube upload (YouTube Data API v3, free quota). Disabled by default.

Setup (one time, free):
  1. https://console.cloud.google.com → new project → enable "YouTube Data API v3".
  2. OAuth consent screen → External → add yourself as a test user.
  3. Credentials → Create OAuth client ID → "Desktop app" → download JSON → save as
     `client_secret.json` in the ClipForge folder.
  4. Set `upload.enabled: true` in config.yaml, then `clipforge upload <video_id> --only 1`.
     A browser window asks you to log in once; the token is saved to youtube_token.json.

Clips are uploaded as PRIVATE (or scheduled with `publishAt`, spaced `schedule_hours_apart`).
Quota: videos.insert costs ~1600 units of the free 10 000/day → about 6 uploads per day.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from clipforge.config import ROOT_DIR, Config
from clipforge.models import ClipMetadata
from clipforge.utils.log import log
from clipforge.workspace import Workspace

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CLIENT_SECRET = ROOT_DIR / "client_secret.json"
TOKEN = ROOT_DIR / "youtube_token.json"
INSERT_COST = 1600
DAILY_QUOTA = 10_000


class UploadError(RuntimeError):
    pass


def _quota_file(cfg: Config) -> Path:
    return cfg.workspace / "youtube_quota.json"


def quota_used_today(cfg: Config) -> int:
    f = _quota_file(cfg)
    if not f.exists():
        return 0
    data = json.loads(f.read_text(encoding="utf-8"))
    return int(data.get(dt.date.today().isoformat(), 0))


def _add_quota(cfg: Config, units: int) -> int:
    f = _quota_file(cfg)
    data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    key = dt.date.today().isoformat()
    data[key] = int(data.get(key, 0)) + units
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data[key]


def _credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not CLIENT_SECRET.exists():
            raise UploadError(f"{CLIENT_SECRET.name} not found. Follow the YouTube setup steps in README.md.")
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        creds = flow.run_local_server(port=0)
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return creds


def upload_clips(ws: Workspace, cfg: Config, ranks: list[int] | None = None) -> list[str]:
    """Upload rendered clips; returns YouTube video ids."""
    if not cfg.upload.enabled:
        raise UploadError("YouTube upload is disabled. Complete the setup in README.md, then set upload.enabled: true in config.yaml.")
    if not ws.metadata_json.exists():
        raise UploadError("metadata.json missing — render the clips first.")
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    metas = [ClipMetadata.model_validate(m) for m in json.loads(ws.metadata_json.read_text(encoding="utf-8"))]
    metas = [m for m in metas if m.file and (ranks is None or m.rank in ranks)]
    youtube = build("youtube", "v3", credentials=_credentials(), cache_discovery=False)
    ids: list[str] = []
    start = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    for i, m in enumerate(metas):
        if quota_used_today(cfg) + INSERT_COST > DAILY_QUOTA:
            log.warning("Daily YouTube quota would be exceeded — stopping. Continue tomorrow.")
            break
        status: dict = {"privacyStatus": cfg.upload.privacy, "selfDeclaredMadeForKids": False}
        if cfg.upload.privacy == "private" and cfg.upload.schedule_hours_apart > 0:
            publish = start + dt.timedelta(hours=cfg.upload.schedule_hours_apart * i)
            status["publishAt"] = publish.strftime("%Y-%m-%dT%H:%M:%SZ")
        body = {
            "snippet": {"title": m.yt_title[:100], "description": m.yt_description[:4900],
                        "tags": [t.lstrip("#") for t in m.hashtags], "categoryId": cfg.upload.category_id},
            "status": status,
        }
        media = MediaFileUpload(str(ws.clips_dir / m.file), mimetype="video/mp4", resumable=True, chunksize=8 << 20)
        req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
        used = _add_quota(cfg, INSERT_COST)
        ids.append(resp["id"])
        log.info(f"Uploaded clip {m.rank} → https://youtu.be/{resp['id']} ({status.get('publishAt', cfg.upload.privacy)}); "
                 f"quota used today ≈ {used}/{DAILY_QUOTA}")
    return ids
