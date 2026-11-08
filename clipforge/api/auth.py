"""Login for remote use (Phase 14): password (scrypt hash in .env), device sessions (HttpOnly cookie, DB-backed,
revocable), CSRF double-submit token, login rate limiting with lockout, audit log, security headers.

Who must log in: every request that does NOT come straight from this laptop. "Local" = the TCP peer is loopback AND
there are no proxy headers (Tailscale Serve / Cloudflare add X-Forwarded-For / Tailscale-User-*) AND the Host header is
localhost. Requests proxied by Tailscale Serve arrive from 127.0.0.1 too, which is why the headers matter.
`auth.trust_localhost: false` makes the laptop log in as well.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import secrets
import threading
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from sqlmodel import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from clipforge.api import db
from clipforge.config import Secrets, load_config, save_secret
from clipforge.utils.log import log

COOKIE = "cf_session"
CSRF_COOKIE = "cf_csrf"
CSRF_HEADER = "x-csrf-token"
SHORT_SESSION = dt.timedelta(hours=12)        # "remember this device" off
LOOPBACK = ("127.0.0.1", "::1", "localhost")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
# browser navigations coming back from Google / Meta sign-in are cross-site by nature (they check their own `state`)
CROSS_SITE_OK = ("/api/oauth/callback", "/api/instagram/oauth/callback", "/api/health")


def _testing() -> bool:
    """FastAPI's TestClient talks as peer 'testclient' with Host 'testserver' — only accepted under pytest."""
    import os

    return os.environ.get("CLIPFORGE_TESTING") == "1"
PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "tailscale-user-login", "tailscale-user-name",
                 "cf-connecting-ip", "x-forwarded-host")
# reachable without a session (health check, the login screen itself, OAuth callbacks which are localhost-only anyway)
# the Instagram callback is reached by a cross-site redirect (no SameSite=Strict cookie); it is guarded by its one-time `state`
PUBLIC_API = ("/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/instagram/oauth/callback")
LOCAL_ONLY_PATHS = ("/docs", "/redoc", "/openapi.json")
# Phase 15b: machine-to-machine endpoints. No browser session / CSRF: every route checks the worker's own key
# (Authorization: Bearer cfw_…, only its SHA-256 is stored) — see clipforge/server/worker_api.py.
WORKER_API = ("/api/worker/", "/api/server/fence")
MIN_PASSWORD = 10


# ----------------------------------------------------------------------------- password hashing (stdlib scrypt)
def hash_password(password: str, n: int = 2 ** 15, r: int = 8, p: int = 1) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${n}${r}${p}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=128 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def password_hash() -> str | None:
    return Secrets().clipforge_password_hash


def password_set() -> bool:
    return bool(password_hash())


def set_password(new: str) -> None:
    if len(new) < MIN_PASSWORD:
        raise ValueError(f"Use at least {MIN_PASSWORD} characters (a short sentence is easy to remember).")
    save_secret("CLIPFORGE_PASSWORD_HASH", hash_password(new))
    revoke_all("password changed")  # every device logs in again with the new password
    audit("password", "Password set/changed")


# ----------------------------------------------------------------------------- who is asking
def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _loopback_peer(request: Request) -> bool:
    ip = client_ip(request)
    return ip in LOOPBACK or (_testing() and ip == "testclient")


def is_local(request: Request) -> bool:
    """Directly from this laptop (not through Tailscale Serve / a tunnel / the LAN / a dev proxy)."""
    if not _loopback_peer(request):
        return False
    for h in PROXY_HEADERS:
        if h not in request.headers:
            continue
        # a local dev proxy (Vite with xfwd) forwarding the laptop's own browser adds X-Forwarded-For: 127.0.0.1
        if h == "x-forwarded-for" and all(x.strip() in LOOPBACK for x in request.headers[h].split(",")):
            continue
        return False
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    return host in LOCAL_HOSTS or (_testing() and host == "testserver")


def _origin_ok(request: Request) -> bool:
    """Is this request from ClipForge's own pages? Blocks other web pages open in the same browser (CSRF against
    the trusted localhost) — browsers always send Sec-Fetch-Site / Origin on cross-site requests."""
    site = request.headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "none"):
        return False
    origin = request.headers.get("origin")
    if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
        from urllib.parse import urlparse

        o = urlparse(origin)
        host = request.headers.get("host", "")
        dev = o.hostname in LOCAL_HOSTS and o.port == 5173  # `npm run dev` (Vite) proxies the API
        if not dev and (o.netloc or "").lower() != host.lower():
            return False
    return True


def is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    # Tailscale Serve terminates TLS and proxies to 127.0.0.1 — trust its header only from loopback
    return client_ip(request) in LOOPBACK and request.headers.get("x-forwarded-proto", "").lower() == "https"


def trusted(request: Request) -> bool:
    return is_local(request) and load_config().auth.trust_localhost


# ----------------------------------------------------------------------------- sessions
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utc(t: dt.datetime) -> dt.datetime:
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def create_session(request: Request, remember: bool, device: str = "") -> tuple[str, db.AuthSession]:
    token = secrets.token_urlsafe(32)
    days = load_config().auth.session_days
    life = dt.timedelta(days=days) if remember else SHORT_SESSION
    ua = request.headers.get("user-agent", "")[:200]
    sess = db.AuthSession(token_hash=_hash_token(token), csrf=secrets.token_urlsafe(24), device=(device or _ua_name(ua))[:120],
                          ip=client_ip(request), remember=remember, expires_at=_now() + life)
    with db.session() as s:
        s.add(sess)
        s.commit()
        s.refresh(sess)
    return token, sess


def _ua_name(ua: str) -> str:
    ua_l = ua.lower()
    os_ = "Android" if "android" in ua_l else "iPhone" if "iphone" in ua_l else "Windows" if "windows" in ua_l else \
        "Mac" if "mac os" in ua_l else "Linux" if "linux" in ua_l else "device"
    br = "Edge" if "edg/" in ua_l else "Chrome" if "chrome" in ua_l else "Firefox" if "firefox" in ua_l else \
        "Safari" if "safari" in ua_l else "browser"
    return f"{br} on {os_}"


def session_for(request: Request) -> Optional[db.AuthSession]:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    with db.session() as s:
        sess = s.exec(select(db.AuthSession).where(db.AuthSession.token_hash == _hash_token(token))).first()
        if not sess or sess.revoked or _utc(sess.expires_at) <= _now():
            return None
        if _now() - _utc(sess.last_seen) > dt.timedelta(minutes=5):  # don't write on every request
            sess.last_seen = _now()
            s.add(sess)
            s.commit()
            s.refresh(sess)
        return sess


def revoke_all(reason: str = "") -> int:
    with db.session() as s:
        rows = s.exec(select(db.AuthSession).where(db.AuthSession.revoked == False)).all()  # noqa: E712
        for r in rows:
            r.revoked = True
            s.add(r)
        s.commit()
    if rows:
        audit("revoke", f"All {len(rows)} device(s) logged out ({reason})")
    return len(rows)


def audit(kind: str, detail: str, ip: str = "") -> None:
    try:
        with db.session() as s:
            s.add(db.AuditEvent(kind=kind, detail=detail[:500], ip=ip))
            s.commit()
    except Exception as exc:  # never break a request because the audit log failed
        log.warning(f"audit log failed: {exc}")


# ----------------------------------------------------------------------------- rate limiting
class _Limiter:
    def __init__(self) -> None:
        self.fails: dict[str, list[float]] = {}
        self.locked_until: dict[str, float] = {}
        self.lock = threading.Lock()

    def locked(self, ip: str) -> float:
        with self.lock:
            return max(0.0, self.locked_until.get(ip, 0) - time.time())

    def locked_any(self, ip: str) -> float:
        """Only the address itself gets locked out; a global flood (someone rotating addresses) only slows logins
        down (see `slow_down`) — otherwise an attacker could lock the owner out forever."""
        return self.locked(ip)

    def slow_down(self) -> bool:
        return self.locked("*") > 0

    def fail(self, ip: str) -> bool:
        """Record a failure; True when this failure triggers a lockout. A global counter ("*", 3× the per-address limit)
        stops someone rotating addresses."""
        cfg = load_config().auth
        hit = False
        with self.lock:
            now = time.time()
            for key, limit in ((ip, cfg.max_failures), ("*", cfg.max_failures * 3)):
                window = [t for t in self.fails.get(key, []) if now - t < cfg.lockout_minutes * 60] + [now]
                self.fails[key] = window
                if len(window) >= limit:
                    self.locked_until[key] = now + cfg.lockout_minutes * 60
                    self.fails[key] = []
                    hit = True
        return hit

    def ok(self, ip: str) -> None:
        with self.lock:
            self.fails.pop(ip, None)


limiter = _Limiter()


# ----------------------------------------------------------------------------- middleware
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data: blob: https:; media-src 'self' blob:; "
                                "style-src 'self' 'unsafe-inline'; font-src 'self' data:; script-src 'self'; "
                                "connect-src 'self'; worker-src 'self'; manifest-src 'self'; frame-ancestors 'none'; "
                                "base-uri 'self'; form-action 'self'"),
}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith(LOCAL_ONLY_PATHS) and not is_local(request):
            return JSONResponse({"detail": "API docs are only available on the laptop (http://localhost:8000/docs)."}, 404)
        request.state.session = None
        if path.startswith("/api/") and not path.startswith(CROSS_SITE_OK) and not _origin_ok(request):
            return JSONResponse({"detail": "Blocked: this request came from another website."}, 403)
        request.state.local = trusted(request)
        if path.startswith(WORKER_API):
            request.state.local = False  # a worker is never "the laptop itself", even over loopback
        elif path.startswith("/api/") and not path.startswith(PUBLIC_API) and not request.state.local:
            sess = session_for(request)
            if sess is None:
                if not password_set():
                    return JSONResponse({"detail": "Set a password on the laptop first: `clipforge auth set-password` "
                                                   "(or Settings → Phone & remote on http://localhost:8000).",
                                         "setup_required": True}, 401)
                return JSONResponse({"detail": "Please log in.", "login_required": True}, 401)
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                sent = request.headers.get(CSRF_HEADER, "")
                if not sent or not hmac.compare_digest(sent, sess.csrf):
                    return JSONResponse({"detail": "Security check failed (CSRF) — reload the page."}, 403)
            request.state.session = sess
        response = await call_next(request)
        if not path.startswith(LOCAL_ONLY_PATHS):
            for k, v in SECURITY_HEADERS.items():
                response.headers.setdefault(k, v)
        return response


def _set_cookies(response: Response, request: Request, token: str, sess: db.AuthSession) -> None:
    secure = is_https(request)
    max_age = int((_utc(sess.expires_at) - _now()).total_seconds()) if sess.remember else None
    response.set_cookie(COOKIE, token, max_age=max_age, httponly=True, samesite="strict", secure=secure, path="/")
    response.set_cookie(CSRF_COOKIE, sess.csrf, max_age=max_age, httponly=False, samesite="strict", secure=secure, path="/")


# ----------------------------------------------------------------------------- routes
router = APIRouter(prefix="/api", tags=["auth"])


@router.get("/health", summary="Liveness check (no login needed; reveals nothing else)")
def health(request: Request) -> dict:
    from clipforge import __version__

    out = {"ok": True, "version": __version__}
    if getattr(request.state, "local", False):
        out["rev"] = _REV  # only on the machine itself: `clipforge server update` checks the NEW code answers
    return out


def _git_rev() -> str:
    from clipforge.config import ROOT_DIR

    try:
        head = (ROOT_DIR / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref = ROOT_DIR / ".git" / head[5:]
            if ref.exists():
                return ref.read_text().strip()[:12]
            for line in (ROOT_DIR / ".git" / "packed-refs").read_text().splitlines():  # fresh clones pack their refs
                if line.endswith(" " + head[5:]):
                    return line.split()[0][:12]
            return ""
        return head[:12]
    except OSError:
        return ""


_REV = _git_rev()


@router.get("/auth/status")
def auth_status(request: Request) -> dict:
    local = trusted(request)
    sess = None if local else session_for(request)
    return {"authenticated": local or sess is not None, "local": local, "password_set": password_set(),
            "setup_required": not password_set() and not local, "device": sess.device if sess else None,
            "https": is_https(request)}


class LoginIn(BaseModel):
    password: str
    remember: bool = True
    device: str = ""


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response) -> dict:
    ip = client_ip(request)
    if _loopback_peer(request) and request.headers.get("x-forwarded-for"):  # behind Tailscale Serve: the real phone address
        ip = request.headers["x-forwarded-for"].split(",")[0].strip()
    wait = limiter.locked_any(ip)
    if not wait and limiter.slow_down():
        time.sleep(2)  # many wrong passwords from many addresses: make guessing slow for everyone, block nobody
    if wait:
        raise HTTPException(429, f"Too many wrong passwords — try again in {int(wait // 60) + 1} min.")
    if not password_set():
        raise HTTPException(409, "No password set yet — set one on the laptop (clipforge auth set-password).")
    if not verify_password(body.password, password_hash()):
        locked = limiter.fail(ip)
        audit("lockout" if locked else "login_failed", f"Wrong password from {request.headers.get('user-agent', '')[:80]}", ip)
        if locked:
            raise HTTPException(429, f"Too many wrong passwords — locked for {load_config().auth.lockout_minutes} min.")
        raise HTTPException(401, "Wrong password.")
    limiter.ok(ip)
    token, sess = create_session(request, body.remember, body.device)
    _set_cookies(response, request, token, sess)
    audit("login_ok", f"Logged in: {sess.device}", ip)
    return {"ok": True, "device": sess.device, "csrf": sess.csrf}


@router.post("/auth/logout")
def logout(request: Request, response: Response) -> dict:
    sess = session_for(request)
    if sess:
        with db.session() as s:
            row = s.get(db.AuthSession, sess.id)
            if row:
                row.revoked = True
                s.add(row)
                s.commit()
        audit("logout", f"Logged out: {sess.device}", client_ip(request))
    response.delete_cookie(COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


class SetupIn(BaseModel):
    password: str
    current: Optional[str] = None


@router.post("/auth/setup", summary="Set or change the password (on the laptop, or logged in with the current one)")
def setup(body: SetupIn, request: Request) -> dict:
    local = trusted(request)
    if not local:
        sess = session_for(request)
        if sess is None or not body.current or not verify_password(body.current, password_hash()):
            raise HTTPException(403, "Change the password on the laptop, or give the current password.")
        sent = request.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(sent, sess.csrf):
            raise HTTPException(403, "Security check failed (CSRF) — reload the page.")
    try:
        set_password(body.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@router.get("/auth/sessions", summary="Logged-in devices")
def sessions(request: Request) -> list[dict]:
    cur = getattr(request.state, "session", None)
    with db.session() as s:
        rows = s.exec(select(db.AuthSession).where(db.AuthSession.revoked == False)).all()  # noqa: E712
    return [{"id": r.id, "device": r.device, "ip": r.ip, "created_at": r.created_at, "last_seen": r.last_seen,
             "expires_at": r.expires_at, "remember": r.remember, "current": bool(cur and cur.id == r.id)}
            for r in rows if _utc(r.expires_at) > _now()]


@router.delete("/auth/sessions/{sid}", summary="Log a device out")
def revoke_session(sid: int, request: Request) -> dict:
    with db.session() as s:
        row = s.get(db.AuthSession, sid)
        if not row:
            raise HTTPException(404, "Unknown device")
        row.revoked = True
        s.add(row)
        s.commit()
    audit("revoke", f"Device logged out: {row.device}", client_ip(request))
    return {"ok": True}


@router.get("/auth/audit", summary="Security + publishing audit log (latest 200)")
def audit_log() -> list[dict]:
    with db.session() as s:
        rows = s.exec(select(db.AuditEvent).order_by(db.AuditEvent.at.desc()).limit(200)).all()  # type: ignore[attr-defined]
    return [{"at": r.at, "kind": r.kind, "detail": r.detail, "ip": r.ip} for r in rows]
