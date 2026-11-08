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
# Publishing needs these (developers.facebook.com → content publishing, Facebook Login column, checked 2026-11-05;
# pages_show_list to find the Page). business_management / instagram_manage_insights are optional extras.
PUBLISH_SCOPES = ("instagram_basic", "instagram_content_publish", "pages_read_engagement", "pages_show_list")
REDIRECT_PATH = "/api/instagram/oauth/callback"


LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
_ts_cache: dict[str, tuple[str, float]] = {}


def public_base(cfg: Config | None = None) -> str:
    """https base URL Meta can send a login started on the phone back to: `instagram.public_url`, else this laptop's
    Tailscale name (https://<laptop>.<tailnet>.ts.net, from `tailscale status --json`, cached 10 min). It only
    resolves inside the tailnet — fine, because Meta only redirects the BROWSER (which is on the tailnet)."""
    cfg = cfg or load_config()
    url = (cfg.instagram.public_url or "").strip().rstrip("/")
    if url:
        return url
    hit = _ts_cache.get("dns")
    if hit and time.time() - hit[1] < 600:
        return hit[0]
    base = ""
    try:
        from clipforge import remote

        dns = remote.status().dns_name
        base = f"https://{dns}" if dns else ""
    except Exception as exc:  # Tailscale missing/stopped: remote logins just aren't offered
        log.debug(f"[instagram] tailscale name unknown: {exc}")
    _ts_cache["dns"] = (base, time.time())
    return base


def redirect_uri(port: int | None = None, host: str | None = None, cfg: Config | None = None) -> str:
    """The redirect URI ClipForge sends to Meta for a login started from `host`. On the laptop (localhost) it is
    http://localhost:<port>/…; from the phone it is <public_base>/… . Both must be listed EXACTLY under Facebook
    Login → Settings → Valid OAuth Redirect URIs (Strict Mode: scheme, host, port, path, no trailing slash)."""
    if host and host not in LOCAL_HOSTS:
        base = public_base(cfg)
        if not base:
            raise ValueError("No https address for remote logins: set instagram.public_url in config.yaml, or start "
                             "Tailscale (`clipforge remote setup`).")
        return base + REDIRECT_PATH
    return f"http://localhost:{port or 8000}{REDIRECT_PATH}"


def redirect_uris(port: int = 8000, cfg: Config | None = None) -> list[str]:
    """Every redirect URI to register in the Meta app (laptop + phone)."""
    out = [redirect_uri(port)]
    base = public_base(cfg)
    if base:
        out.append(base + REDIRECT_PATH)
    return out
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
    def __init__(self, message: str, code: int | None = None, auth: bool = False, transient: bool = False,
                 subcode: int | None = None, format: bool = False) -> None:
        super().__init__(message)
        self.code, self.auth, self.transient, self.subcode, self.format = code, auth, transient, subcode, format


# Graph error code → what to do (developers.facebook.com/docs/graph-api/guides/error-handling + IG content publishing
# error codes, checked 2026-11-05). Codes not listed: shown as Meta's own message.
AUTH_CODES = (190, 102, 10, 200, 3)          # token invalid/expired, permission missing → Reconnect
TRANSIENT_CODES = (1, 2, 4, 17, 32, 341, 368, 613, 9004, 2207001, 2207003)  # temporary / rate limited → try later
FORMAT_SUBCODES = (2207026, 2207004, 2207005, 2207006, 2207009, 2207010, 2207027, 2207052, 2207053)  # bad video/caption


def _redact(text: str) -> str:
    """Never let a token reach a log line or an error shown in the UI/Telegram."""
    import re

    return re.sub(r"(access_token=|OAuth |Bearer )[A-Za-z0-9|_\-\.%]+", r"\1<redacted>", text or "")


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
    def permissions(self, user_token: str) -> list[dict]: ...
    def debug_token(self, token: str) -> dict: ...


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
            raise IGError(_redact(f"Network problem talking to Meta: {exc}"), transient=True) from exc
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400 or "error" in data:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            code, sub = err.get("code"), err.get("error_subcode")
            msg = err.get("error_user_msg") or err.get("message") or r.text[:300]
            title = err.get("error_user_title")
            text = f"Meta API error {code}{f'/{sub}' if sub else ''}: {(title + ' — ') if title else ''}{msg}"
            raise IGError(_redact(text), code=code, subcode=sub, auth=code in AUTH_CODES,
                          transient=code in TRANSIENT_CODES or sub in TRANSIENT_CODES or r.status_code >= 500,
                          format=sub in FORMAT_SUBCODES)
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
        d = self._do("GET", f"/{ig_id}/media", params={"fields": "id,permalink,timestamp,caption,media_product_type,"
                                                                 "media_type,media_url",
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

    def permissions(self, user_token: str) -> list[dict]:
        """[{permission, status: granted|declined|expired}] for the logged-in Facebook user."""
        return self._do("GET", "/me/permissions", params={"access_token": user_token}).get("data", [])

    def debug_token(self, token: str) -> dict:
        """is_valid, expires_at (0 = never), data_access_expires_at, type, scopes — checked with the app token."""
        d = self._do("GET", "/debug_token", params={"input_token": token,
                                                    "access_token": f"{self.app_id}|{self.app_secret}"})
        return d.get("data", {})


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
    return _offer_accounts(c, user_token, secs, now)


def _offer_accounts(c: IGClient, user_token: str, secs: int, now: dt.datetime) -> dict:
    """Long-lived user token → Pages with a linked IG professional account → a pending pick (tokens stay here)."""
    from clipforge.publish.timez import iso_utc

    pages = c.pages(user_token)
    choices = []
    for p in pages:
        ig = p.get("instagram_business_account")
        if ig and p.get("access_token"):
            choices.append({"ig_user_id": ig["id"], "username": ig.get("username", ""), "profile_picture_url":
                            ig.get("profile_picture_url", ""), "followers": ig.get("followers_count", 0),
                            "page_id": p["id"], "page_name": p.get("name", ""), "page_token": p["access_token"]})
    if not choices:
        checks = login_checks(c, user_token)
        miss = [p for p in PUBLISH_SCOPES if p not in checks["granted"]]
        why = (f" Meta did not grant: {', '.join(miss)} (allow every permission, or with a Login Configuration make sure "
               "it lists them)." if checks["granted"] and miss else "")
        raise ValueError(f"Meta returned {len(pages)} Facebook Page(s), none with a linked Instagram professional "
                         f"account.{why} Check: (1) Instagram app → Settings → Account type = Professional; (2) the Page "
                         "is linked (Page → Settings → Linked accounts → Instagram); (3) on Meta's screen 'Choose the "
                         "Pages' you TICKED that Page (Edit previous settings → select all).")
    checks = login_checks(c, user_token)
    pid = secrets.token_urlsafe(8)
    with _lock:
        _pending[pid] = ({"user_token": user_token, "user_token_expires_at": iso_utc(now + dt.timedelta(seconds=secs)),
                          **checks}, choices, time.time())
    return {"pick": pid, "accounts": [{k: v for k, v in ch.items() if k != "page_token"} for ch in choices]}


TOKEN_STEPS = [
    "On your phone open developers.facebook.com/tools/explorer (log in with the Facebook account that manages the Page).",
    "Top right: Meta App = your ClipForge app (not 'Graph API Explorer'); User or Page = 'Get User Access Token'.",
    "Add permissions: instagram_basic, instagram_content_publish, pages_show_list, pages_read_engagement, "
    "business_management (optional: instagram_manage_insights).",
    "Generate Access Token → in Meta's window choose the Page linked to your Instagram and the Instagram account → Save.",
    "Copy the token (the long text in the 'Access Token' box) and paste it here. ClipForge exchanges it for a "
    "60-day login on the laptop; the app secret never leaves the laptop.",
]


def connect_with_token(user_token: str, cfg: Config | None = None, now: dt.datetime | None = None,
                       prefer_username: str = "") -> dict:
    """Fallback that works 100% remotely: a user token from Graph API Explorer (same Meta app) → long-lived token
    → Pages → IG account. With exactly one account, or one matching `prefer_username` (the manual account being
    converted), it is picked right away and the manual row converted IN PLACE. Otherwise returns a pick id."""
    from clipforge.publish.timez import now_utc

    cfg = cfg or load_config()
    now = now or now_utc()
    token = (user_token or "").strip().strip('"')
    if len(token) < 40 or " " in token:
        raise ValueError("That doesn't look like an access token — copy the whole 'Access Token' box from Graph API Explorer.")
    c = client(cfg)
    try:
        d = c.debug_token(token)
    except IGError as exc:
        raise ValueError(f"Meta rejected the token: {exc}") from exc
    if not d.get("is_valid"):
        raise ValueError("Meta says this token is not valid (expired or revoked) — generate a new one.")
    if str(d.get("app_id") or "") and Secrets().meta_app_id and str(d.get("app_id")) != str(Secrets().meta_app_id):
        raise ValueError("This token belongs to another Meta app. In Graph API Explorer pick YOUR app (top right) "
                         "under 'Meta App', then generate the token again.")
    if d.get("type") and d.get("type") != "USER":
        raise ValueError(f"This is a {d.get('type')} token — choose 'Get User Access Token' in Graph API Explorer.")
    long_tok, secs = c.long_lived(token)
    res = _offer_accounts(c, long_tok, secs, now)
    accs = res["accounts"]
    want = (prefer_username or "").lower().lstrip("@")
    match = [a for a in accs if want and a["username"].lower() == want] or (accs if len(accs) == 1 else [])
    if len(match) == 1:
        ch = pick(res["pick"], match[0]["ig_user_id"], cfg)
        return {"channel_id": ch.id, "username": ch.title, "mode": ch.settings.get("mode", "assisted")}
    return res


def record_attempt(cfg: Config | None = None, **fields: Any) -> None:
    """Keep the last 20 login attempts (no tokens) in workspace/accounts/instagram/oauth_attempts.json, so a failed
    login can be diagnosed later from the phone."""
    from clipforge.publish.timez import iso_utc, now_utc

    try:
        p = (cfg or load_config()).workspace / "accounts" / "instagram" / "oauth_attempts.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        rows = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        rows.append({"at": iso_utc(now_utc()), **{k: _redact(str(v))[:500] for k, v in fields.items()}})
        p.write_text(json.dumps(rows[-20:], indent=1), encoding="utf-8")
    except Exception as exc:
        log.warning(f"[instagram] couldn't record the login attempt: {exc}")


def attempts(cfg: Config | None = None) -> list[dict]:
    p = (cfg or load_config()).workspace / "accounts" / "instagram" / "oauth_attempts.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except (OSError, ValueError):
        return []


def container_test(ch: db.Channel, path: Path, cfg: Config | None = None, c: IGClient | None = None,
                   poll_seconds: float = 5, timeout: float = 600, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Live check of the publishing path WITHOUT posting: create a Reel container, resumable upload, poll until
    FINISHED. `media_publish` only when instagram.test_publish_ok is true. An unpublished container expires by
    itself after 24 h (nothing appears on the profile)."""
    cfg = cfg or load_config()
    c = c or client(cfg)
    probs = preflight(path)
    if probs:
        return {"ok": False, "step": "preflight", "text": "; ".join(probs)}
    token, ig_id = page_token(ch, cfg), ig_id_of(ch)
    out: dict[str, Any] = {"ok": False, "published": False}
    try:
        cont = c.create_reel(ig_id, token, "ClipForge test (not published)", False, None)
        out["container_id"] = cont["id"]
        out["step"] = "upload"
        c.upload(cont["id"], token, path)
        out["step"] = "processing"
        waited, st = 0.0, {}
        while waited <= timeout:
            st = c.container_status(cont["id"], token)
            if st.get("status_code") in STATUS_DONE or st.get("status_code") in ("ERROR", "EXPIRED"):
                break
            sleep(poll_seconds)
            waited += poll_seconds
        out["status_code"] = st.get("status_code")
        if st.get("status_code") != "FINISHED":
            out["text"] = f"Container ended as {st.get('status_code') or 'timeout'}: {st.get('status', '')}"
            return out
        out["ok"], out["step"] = True, "finished"
        if cfg.instagram.test_publish_ok:
            out["media_id"] = c.publish(ig_id, token, cont["id"])
            out["published"] = True
            out["text"] = "Uploaded, processed and PUBLISHED (instagram.test_publish_ok: true)."
        else:
            out["text"] = "Uploaded and processed (FINISHED). Not published: instagram.test_publish_ok is false."
    except IGError as exc:
        out["text"] = f"{out.get('step', 'create')} failed: {exc}"
    return out


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
    return chs.register_instagram(ch, user["user_token_expires_at"], cfg,
                                  checks={k: user.get(k) for k in ("granted", "declined", "token_valid", "token_type")})


def login_checks(c: IGClient, user_token: str) -> dict:
    """Step 6 of the login: which permissions Meta really granted (me/permissions) and whether the token is valid
    (debug_token). Never raises: a failed check just isn't 'ready'."""
    out: dict = {"granted": [], "declined": [], "token_valid": None, "token_type": ""}
    try:
        perms = c.permissions(user_token)
        out["granted"] = sorted(p["permission"] for p in perms if p.get("status") == "granted")
        out["declined"] = sorted(p["permission"] for p in perms if p.get("status") != "granted")
    except IGError as exc:
        log.warning(f"[instagram] couldn't read the granted permissions: {exc}")
    try:
        d = c.debug_token(user_token)
        out["token_valid"], out["token_type"] = bool(d.get("is_valid")), d.get("type", "")
    except IGError as exc:
        log.warning(f"[instagram] debug_token failed: {exc}")
    return out


def eligibility(ch: db.Channel, cfg: Config | None = None, live: bool = False, c: IGClient | None = None) -> dict:
    """Is this account ready for AUTO publishing? 'Connected' is not enough. Local checks always; `live` also asks
    Meta (account, publishing limit, token) and stores the answer on the account."""
    from clipforge.publish import channels as chs

    cfg = cfg or load_config()
    ex = ch.extra
    checks: dict[str, dict] = {}

    def put(name: str, ok: bool | None, text: str) -> None:
        checks[name] = {"ok": ok, "text": text}

    if is_manual(ch):
        put("login", False, "Added manually, without Facebook Login: no access to the account.")
        return {"checks": checks, "auto_ready": False, "connected": False,
                "summary": "Not connected: added manually. Connect it with Facebook Login (Accounts → Add Instagram account)."}
    put("meta_app", configured(), "Meta app (App ID + Secret) set up." if configured() else "No Meta app set up.")
    try:
        tok = load_tokens(ig_id_of(ch), cfg)
    except (IGError, ValueError):
        tok = {}
    put("ig_id", bool(ex.get("ig_user_id")), f"Instagram professional account id {'found' if ex.get('ig_user_id') else 'missing'}.")
    put("professional", bool(ex.get("ig_user_id")), "Professional account (Business/Creator) linked to a Page."
        if ex.get("ig_user_id") else "No professional account found on your Pages.")
    put("page", bool(tok.get("page_id")), f"Facebook Page linked: {tok.get('page_name') or tok.get('page_id')}."
        if tok.get("page_id") else "No Facebook Page linked.")
    exp = tok.get("user_token_expires_at")
    left = (dt.datetime.fromisoformat(exp.replace("Z", "+00:00")) - dt.datetime.now(dt.timezone.utc)) if exp else None
    valid = bool(tok.get("page_token")) and ch.auth_status == "ok" and (left is None or left > dt.timedelta(0)) and \
        tok.get("token_valid") is not False
    put("token", valid, (f"Login valid ({left.days} days left, renews itself)." if left is not None and valid else
                         "Login valid." if valid else ch.auth_error or "Login missing or expired: Reconnect."))
    granted = set(tok.get("granted") or ex.get("granted") or [])
    if granted:
        missing = [p for p in PUBLISH_SCOPES if p not in granted]
        put("permissions", not missing, "Publishing permissions granted." if not missing else
            "Missing permission(s): " + ", ".join(missing) + " — Reconnect and allow them all.")
    else:
        put("permissions", None, "Permissions not checked yet (reconnect once, or run the live check).")
    if live and valid:
        c = c or client(cfg)
        try:
            lim = c.publishing_limit(ig_id_of(ch), tok["page_token"])
            used = int(lim.get("quota_usage") or 0)
            total = int((lim.get("config") or {}).get("quota_total") or cfg.instagram.publish_limit)
            put("publish_limit", used < total - cfg.instagram.publish_limit_margin,
                f"Publishing limit: {used}/{total} posts in the last 24 h.")
        except IGError as exc:
            put("publish_limit", False, f"Couldn't read the publishing limit: {exc}")
        try:
            perms = c.permissions(tok.get("user_token", ""))
            g = sorted(p["permission"] for p in perms if p.get("status") == "granted")
            tok["granted"] = g
            save_tokens(ig_id_of(ch), tok, cfg)
            missing = [p for p in PUBLISH_SCOPES if p not in g]
            put("permissions", not missing, "Publishing permissions granted." if not missing else
                "Missing permission(s): " + ", ".join(missing) + " — Reconnect and allow them all.")
        except IGError as exc:
            put("permissions", None, f"Couldn't read permissions: {exc}")
    required = ("meta_app", "ig_id", "page", "token", "permissions")
    ready = all(checks[k]["ok"] is True for k in required) and checks.get("publish_limit", {"ok": True})["ok"] is not False
    res = {"checks": checks, "auto_ready": ready, "connected": valid,
           "summary": "Auto-publish ready." if ready else "Connected, not auto-publish ready: " + "; ".join(
               checks[k]["text"] for k in required if checks[k]["ok"] is not True)}
    if live:
        chs.update(ch.id, extra_json=json.dumps({**ex, "ig_eligibility": {"auto_ready": ready, "summary": res["summary"]}}))
    return res


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
