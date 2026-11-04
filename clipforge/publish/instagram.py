"""Instagram Reels via the "Instagram API with Facebook Login" (graph.facebook.com + rupload.facebook.com).

Why this path: only it supports the resumable upload protocol, which sends the local file directly. The "Instagram
Login" path needs the video at a public URL, and we have no public server. Checked against developers.facebook.com
(2026-11-04): container `media_type=REELS, upload_type=resumable` → POST the bytes to
`rupload.facebook.com/ig-api-upload/<ver>/<container>` (headers `Authorization: OAuth <token>`, `offset`, `file_size`) →
poll `status_code` (IN_PROGRESS / FINISHED / ERROR / EXPIRED / PUBLISHED) → `media_publish`. There is no native
scheduling and containers expire after 24 h, so ClipForge uploads ~20 min before the slot and publishes at the slot.

Accounts are Channel rows with `platform="instagram"`, id `ig:<ig user id>`; tokens live in
`workspace/accounts/instagram/<ig id>/token.json` (git-ignored): long-lived user token (~60 days, refreshed) + the Page
token derived from it (doesn't expire) which is used for the Instagram calls.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from clipforge.api import db
from clipforge.config import Config, Secrets, load_config
from clipforge.utils.log import log

SCOPES = ["instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement",
          "business_management", "instagram_manage_insights"]
SCOPE_HELP = {
    "instagram_basic": "Read your Instagram professional account (username, picture, followers).",
    "instagram_content_publish": "Publish the Reels you approved, at the time you chose.",
    "pages_show_list": "List the Facebook Pages you manage, to find the Instagram account linked to one.",
    "pages_read_engagement": "Needed by Meta to use the Page that your Instagram account is linked to.",
    "business_management": "Needed when the Page is owned through a Business portfolio.",
    "instagram_manage_insights": "Read views, reach, likes, comments, shares and saves of your Reels.",
}
INSIGHT_METRICS = "views,reach,likes,comments,shares,saved"
STATUS_DONE = ("FINISHED", "PUBLISHED")


class IGError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, auth: bool = False, transient: bool = False) -> None:
        super().__init__(message)
        self.code, self.auth, self.transient = code, auth, transient


class IGClient(Protocol):
    def exchange_code(self, code: str, redirect_uri: str) -> str: ...
    def long_lived(self, user_token: str) -> tuple[str, int]: ...
    def pages(self, user_token: str) -> list[dict]: ...
    def account(self, ig_id: str, token: str) -> dict: ...
    def create_reel(self, ig_id: str, token: str, caption: str, share_to_feed: bool, thumb_offset: int | None) -> dict: ...
    def upload(self, container_id: str, token: str, path: Path, offset: int = 0) -> None: ...
    def upload_offset(self, container_id: str, token: str) -> int: ...
    def container_status(self, container_id: str, token: str) -> dict: ...
    def publish(self, ig_id: str, token: str, creation_id: str) -> str: ...
    def media(self, media_id: str, token: str) -> dict: ...
    def recent_media(self, ig_id: str, token: str, limit: int = 50) -> list[dict]: ...
    def publishing_limit(self, ig_id: str, token: str) -> dict: ...
    def insights(self, media_id: str, token: str) -> dict: ...
    def revoke(self, user_token: str) -> None: ...


# ----------------------------------------------------------------------------- real client
class GraphIG:
    def __init__(self, cfg: Config | None = None) -> None:
        import httpx

        self.cfg = cfg or load_config()
        self.v = self.cfg.instagram.graph_version
        self.base = f"https://graph.facebook.com/{self.v}"
        self.http = httpx.Client(timeout=httpx.Timeout(60, connect=20))
        sec = Secrets()
        self.app_id, self.app_secret = sec.meta_app_id or "", sec.meta_app_secret or ""

    def _do(self, method: str, url: str, **kw) -> dict:
        import httpx

        try:
            r = self.http.request(method, url if url.startswith("http") else self.base + url, **kw)
        except httpx.HTTPError as exc:
            raise IGError(f"Network problem talking to Meta: {exc}", transient=True) from exc
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400 or "error" in data:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            code = err.get("code")
            msg = err.get("error_user_msg") or err.get("message") or r.text[:300]
            raise IGError(f"Meta API error {code}: {msg}", code=code, auth=code in (190, 102, 10, 200),
                          transient=code in (1, 2, 4, 17, 341, 32) or r.status_code >= 500)
        return data

    def exchange_code(self, code: str, redirect_uri: str) -> str:
        d = self._do("GET", "/oauth/access_token", params={"client_id": self.app_id, "client_secret": self.app_secret,
                                                           "redirect_uri": redirect_uri, "code": code})
        return d["access_token"]

    def long_lived(self, user_token: str) -> tuple[str, int]:
        d = self._do("GET", "/oauth/access_token", params={"grant_type": "fb_exchange_token", "client_id": self.app_id,
                                                           "client_secret": self.app_secret, "fb_exchange_token": user_token})
        return d["access_token"], int(d.get("expires_in") or 60 * 86400)

    def pages(self, user_token: str) -> list[dict]:
        fields = "id,name,access_token,instagram_business_account{id,username,profile_picture_url,followers_count}"
        d = self._do("GET", "/me/accounts", params={"fields": fields, "limit": 100, "access_token": user_token})
        return d.get("data", [])

    def account(self, ig_id: str, token: str) -> dict:
        return self._do("GET", f"/{ig_id}", params={"fields": "id,username,profile_picture_url,followers_count,media_count",
                                                    "access_token": token})

    def create_reel(self, ig_id: str, token: str, caption: str, share_to_feed: bool, thumb_offset: int | None) -> dict:
        data: dict[str, Any] = {"media_type": "REELS", "upload_type": "resumable", "caption": caption,
                                "share_to_feed": "true" if share_to_feed else "false", "access_token": token}
        if thumb_offset is not None:
            data["thumb_offset"] = str(int(thumb_offset))
        return self._do("POST", f"/{ig_id}/media", data=data)

    def upload(self, container_id: str, token: str, path: Path, offset: int = 0) -> None:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(offset)
            body = f.read()
        d = self._do("POST", f"https://rupload.facebook.com/ig-api-upload/{self.v}/{container_id}", content=body,
                     headers={"Authorization": f"OAuth {token}", "offset": str(offset), "file_size": str(size)})
        if not d.get("success", True):
            raise IGError(f"Upload not accepted: {d}", transient=True)

    def upload_offset(self, container_id: str, token: str) -> int:
        d = self._do("GET", f"https://rupload.facebook.com/ig-api-upload/{self.v}/{container_id}",
                     headers={"Authorization": f"OAuth {token}"})
        return int(d.get("offset") or 0)

    def container_status(self, container_id: str, token: str) -> dict:
        return self._do("GET", f"/{container_id}", params={"fields": "status_code,status", "access_token": token})

    def publish(self, ig_id: str, token: str, creation_id: str) -> str:
        return self._do("POST", f"/{ig_id}/media_publish", data={"creation_id": creation_id, "access_token": token})["id"]

    def media(self, media_id: str, token: str) -> dict:
        return self._do("GET", f"/{media_id}", params={"fields": "id,permalink,timestamp,caption,media_product_type",
                                                       "access_token": token})

    def recent_media(self, ig_id: str, token: str, limit: int = 50) -> list[dict]:
        d = self._do("GET", f"/{ig_id}/media", params={"fields": "id,permalink,timestamp,caption,media_product_type",
                                                       "limit": limit, "access_token": token})
        return d.get("data", [])

    def publishing_limit(self, ig_id: str, token: str) -> dict:
        d = self._do("GET", f"/{ig_id}/content_publishing_limit", params={"fields": "config,quota_usage", "access_token": token})
        return (d.get("data") or [{}])[0]

    def insights(self, media_id: str, token: str) -> dict:
        d = self._do("GET", f"/{media_id}/insights", params={"metric": INSIGHT_METRICS, "access_token": token})
        out = {}
        for m in d.get("data", []):
            vals = m.get("values") or [{}]
            out[m["name"]] = vals[0].get("value", m.get("total_value", {}).get("value", 0))
        return out

    def revoke(self, user_token: str) -> None:
        self._do("DELETE", "/me/permissions", params={"access_token": user_token})


_factory: Callable[[Config], IGClient] = lambda cfg: GraphIG(cfg)  # noqa: E731
_default_factory = _factory


def set_client_factory(factory: Callable[[Config], IGClient] | None) -> None:
    """Tests install a fake here; None restores the real Graph client."""
    global _factory
    _factory = factory or _default_factory


def client(cfg: Config | None = None) -> IGClient:
    return _factory(cfg or load_config())


def configured() -> bool:
    """Meta app id + secret present (needed for OAuth and token refresh)."""
    sec = Secrets()
    return bool(sec.meta_app_id and sec.meta_app_secret) or _factory is not _default_factory


# ----------------------------------------------------------------------------- tokens
def account_dir(ig_id: str, cfg: Config | None = None) -> Path:
    return (cfg or load_config()).workspace / "accounts" / "instagram" / ig_id


def token_path(ig_id: str, cfg: Config | None = None) -> Path:
    return account_dir(ig_id, cfg) / "token.json"


def ig_id_of(ch: db.Channel) -> str:
    return ch.extra.get("ig_user_id") or ch.id.split(":", 1)[-1]


def is_manual(ch: db.Channel) -> bool:
    return bool(ch.extra.get("manual"))


def save_tokens(ig_id: str, data: dict, cfg: Config | None = None) -> None:
    p = token_path(ig_id, cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_tokens(ig_id: str, cfg: Config | None = None) -> dict:
    p = token_path(ig_id, cfg)
    if not p.exists():
        raise IGError("This Instagram account isn't connected (no saved login) — Reconnect it on the Accounts page.", auth=True)
    return json.loads(p.read_text(encoding="utf-8"))


def page_token(ch: db.Channel, cfg: Config | None = None) -> str:
    return load_tokens(ig_id_of(ch), cfg)["page_token"]


def refresh_if_needed(ch: db.Channel, cfg: Config, now: dt.datetime, c: IGClient | None = None) -> bool:
    """Refresh the long-lived user token when it expires within 10 days (and re-derive the Page token)."""
    from clipforge.publish import channels as chs
    from clipforge.publish.timez import iso_utc

    tok = load_tokens(ig_id_of(ch), cfg)
    exp = tok.get("user_token_expires_at")
    if exp and dt.datetime.fromisoformat(exp.replace("Z", "+00:00")) - now > dt.timedelta(days=10):
        return False
    c = c or client(cfg)
    new, secs = c.long_lived(tok["user_token"])
    tok["user_token"], tok["user_token_expires_at"] = new, iso_utc(now + dt.timedelta(seconds=secs))
    for p in c.pages(new):
        if p.get("id") == tok.get("page_id") and p.get("access_token"):
            tok["page_token"] = p["access_token"]
    save_tokens(ig_id_of(ch), tok, cfg)
    chs.update(ch.id, extra_json=json.dumps({**ch.extra, "token_expires_at": tok["user_token_expires_at"]}))
    log.info(f"Refreshed the Instagram login for {ch.title} (valid until {tok['user_token_expires_at'][:10]}).")
    return True


def token_health(ch: db.Channel) -> dict:
    if is_manual(ch):
        return {"ok": True, "text": "Manual account (assisted): you post, ClipForge prepares the kit."}
    try:
        tok = load_tokens(ig_id_of(ch))
    except IGError as exc:
        return {"ok": False, "text": str(exc)}
    except ValueError:
        return {"ok": False, "text": "Saved login is unreadable — Reconnect."}
    if ch.auth_status != "ok":
        return {"ok": False, "text": ch.auth_error or "Needs re-login."}
    exp = tok.get("user_token_expires_at")
    if exp:
        left = dt.datetime.fromisoformat(exp.replace("Z", "+00:00")) - dt.datetime.now(dt.timezone.utc)
        if left < dt.timedelta(0):
            return {"ok": False, "text": "Login expired — Reconnect."}
        return {"ok": True, "text": f"Login OK (renews itself; valid {left.days} more days)."}
    return {"ok": True, "text": "Login OK."}


def revoke(ch: db.Channel) -> str:
    """Disconnect: revoke the app's permissions at Meta (best effort) and delete the saved tokens."""
    import shutil

    if is_manual(ch):
        return "Manual account removed."
    msg = "Saved login deleted."
    try:
        tok = load_tokens(ig_id_of(ch))
        from clipforge.publish import channels as chs

        shared = []  # other Instagram accounts added through the same Facebook login share this user token
        for other in chs.all_channels():
            if other.platform == "instagram" and other.id != ch.id and not is_manual(other):
                try:
                    if load_tokens(ig_id_of(other)).get("user_token") == tok.get("user_token"):
                        shared.append(other.title)
                except (IGError, ValueError, OSError):
                    pass
        if shared:
            msg = (f"Saved login deleted. Meta access kept, because {', '.join(shared)} uses the same Facebook login "
                   "(disconnect those too to revoke it).")
        else:
            client().revoke(tok["user_token"])
            msg = "Access revoked at Meta; saved login deleted."
    except Exception as exc:
        msg = f"Saved login deleted. Couldn't reach Meta ({exc}) — remove the app in Facebook → Settings → Business integrations."
    shutil.rmtree(account_dir(ig_id_of(ch)), ignore_errors=True)
    return msg


# ----------------------------------------------------------------------------- OAuth (Facebook Login)
_flows: dict[str, tuple[str, float]] = {}
_pending: dict[str, tuple[dict, list[dict], float]] = {}
_lock = threading.Lock()


def oauth_url(redirect_uri: str) -> str:
    from urllib.parse import urlencode

    sec = Secrets()
    if not configured():
        raise FileNotFoundError("Add your Meta App ID and App Secret in Settings → Publishing → Instagram first.")
    state = secrets.token_urlsafe(16)
    with _lock:
        now = time.time()
        for k in [k for k, (_, t) in _flows.items() if now - t > 1800]:
            _flows.pop(k, None)
        _flows[state] = (redirect_uri, now)
    q: dict[str, str] = {"client_id": sec.meta_app_id or "", "redirect_uri": redirect_uri, "state": state,
                         "response_type": "code"}
    config_id = sec.meta_login_config_id  # Facebook Login for Business configuration (optional)
    if config_id:
        q["config_id"] = config_id
    else:
        q["scope"] = ",".join(SCOPES)
    return f"https://www.facebook.com/{load_config().instagram.graph_version}/dialog/oauth?{urlencode(q)}"


def finish_oauth(code: str, state: str, cfg: Config | None = None, now: dt.datetime | None = None) -> dict:
    """Exchange the code → long-lived token → Pages with a linked IG professional account. Returns the choices."""
    from clipforge.publish.timez import iso_utc, now_utc

    cfg = cfg or load_config()
    now = now or now_utc()
    with _lock:
        item = _flows.pop(state, None)
    if not item:
        raise ValueError("This login link expired or was already used — click Add Instagram account again.")
    redirect_uri, _ = item
    c = client(cfg)
    short = c.exchange_code(code, redirect_uri)
    user_token, secs = c.long_lived(short)
    pages = c.pages(user_token)
    choices = []
    for p in pages:
        ig = p.get("instagram_business_account")
        if ig and p.get("access_token"):
            choices.append({"ig_user_id": ig["id"], "username": ig.get("username", ""), "profile_picture_url":
                            ig.get("profile_picture_url", ""), "followers": ig.get("followers_count", 0),
                            "page_id": p["id"], "page_name": p.get("name", ""), "page_token": p["access_token"]})
    if not choices:
        raise ValueError("None of your Facebook Pages has a linked Instagram professional account. In the Instagram app: "
                         "Settings → Account type → switch to Professional (Creator or Business), then link a Facebook Page "
                         "(Accounts Center). If you have several Pages, tick them all on Meta's permission screen.")
    pid = secrets.token_urlsafe(8)
    with _lock:
        _pending[pid] = ({"user_token": user_token, "user_token_expires_at": iso_utc(now + dt.timedelta(seconds=secs))},
                         choices, time.time())
    return {"pick": pid, "accounts": [{k: v for k, v in ch.items() if k != "page_token"} for ch in choices]}


def pending(pid: str) -> list[dict] | None:
    with _lock:
        item = _pending.get(pid)
    return [{k: v for k, v in ch.items() if k != "page_token"} for ch in item[1]] if item else None


def pick(pid: str, ig_user_id: str, cfg: Config | None = None) -> db.Channel:
    from clipforge.publish import channels as chs

    with _lock:
        item = _pending.pop(pid, None)
    if not item:
        raise ValueError("That choice expired — add the Instagram account again.")
    user, choices, _ = item
    ch = next((c for c in choices if c["ig_user_id"] == ig_user_id), None)
    if not ch:
        raise ValueError("Unknown Instagram account")
    save_tokens(ig_user_id, {**user, "page_id": ch["page_id"], "page_name": ch["page_name"], "page_token": ch["page_token"]}, cfg)
    return chs.register_instagram(ch, user["user_token_expires_at"], cfg)


# ----------------------------------------------------------------------------- video pre-flight (ffprobe)
MAX_BYTES = 300 * 1024 * 1024
MIN_SEC, MAX_SEC = 3.0, 15 * 60.0


def preflight(path: Path) -> list[str]:
    """Check a clip against Instagram's Reels specs (checked 2026-11-04). Empty list = OK."""
    import subprocess

    from clipforge.utils.ffmpeg import ffprobe_bin

    problems: list[str] = []
    if not path.exists():
        return ["The clip file is missing — re-render it."]
    size = path.stat().st_size
    if size > MAX_BYTES:
        problems.append(f"File is {size / 1e6:.0f} MB — Instagram allows 300 MB.")
    try:
        out = subprocess.run([ffprobe_bin(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                             capture_output=True, text=True, timeout=60)
        info = json.loads(out.stdout or "{}")
    except Exception as exc:
        return [f"Couldn't inspect the video ({exc})."]
    fmt = info.get("format", {})
    if not any(x in fmt.get("format_name", "") for x in ("mp4", "mov")):
        problems.append("Container must be MP4/MOV.")
    dur = float(fmt.get("duration") or 0)
    if not MIN_SEC <= dur <= MAX_SEC:
        problems.append(f"Duration {dur:.1f}s — Reels must be 3 s to 15 min.")
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        problems.append("No video stream.")
    else:
        if v.get("codec_name") not in ("h264", "hevc"):
            problems.append(f"Video codec {v.get('codec_name')} — use H.264 or HEVC.")
        if int(v.get("width") or 0) > 1920:
            problems.append("Video is wider than 1920 px.")
        try:
            num, den = (v.get("avg_frame_rate") or "30/1").split("/")
            fps = float(num) / float(den or 1)
            if not 23 <= fps <= 60.5:
                problems.append(f"Frame rate {fps:.1f} — Instagram needs 23–60 fps.")
        except (ValueError, ZeroDivisionError):
            pass
        if v.get("pix_fmt") not in (None, "yuv420p", "yuvj420p"):
            problems.append(f"Pixel format {v.get('pix_fmt')} — needs 4:2:0 (yuv420p).")
    if a:
        if a.get("codec_name") != "aac":
            problems.append(f"Audio codec {a.get('codec_name')} — use AAC.")
        if int(a.get("sample_rate") or 0) > 48000:
            problems.append("Audio sample rate above 48 kHz.")
        if int(a.get("channels") or 0) > 2:
            problems.append("Audio must be mono or stereo.")
    if not _moov_first(path):
        problems.append("The 'moov' atom must be at the start of the file (faststart) — re-render the clip.")
    return problems


def _moov_first(path: Path) -> bool:
    """MP4 top-level atoms: moov must come before mdat."""
    import struct

    try:
        with open(path, "rb") as f:
            for _ in range(64):
                head = f.read(8)
                if len(head) < 8:
                    return False
                size, kind = struct.unpack(">I4s", head)
                if kind == b"moov":
                    return True
                if kind == b"mdat":
                    return False
                if size == 1:
                    size = struct.unpack(">Q", f.read(8))[0]
                    f.seek(size - 16, 1)
                elif size < 8:
                    return False
                else:
                    f.seek(size - 8, 1)
    except OSError:
        return False
    return False
