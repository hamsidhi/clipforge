"""Telegram bot core: who may do what, command / button / conversation registries, confirmations, PIN, safe errors.

Security model (docs/TELEGRAM.md):
  * Only paired chats get any answer. Unknown chats are ignored silently (a rate-limited audit entry is written).
  * In a group, only the user who paired it counts. Roles: owner = everything, viewer = read-only.
  * Destructive / public actions go through `ask_danger`: Yes/No buttons bound to the user (2 min, one-shot),
    optionally "type YES", then the PIN if enabled (the PIN message is deleted right away; 5 wrong → 15 min lockout).
  * Nothing secret is ever echoed: errors are scrubbed (paths, IPs, tokens) and unexpected ones become an error id.
"""

from __future__ import annotations

import collections
import datetime as dt
import re
import secrets
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from clipforge.config import Secrets, load_config, save_secret
from clipforge.notify import tg_store as st
from clipforge.notify.tg_api import TgError, esc, split_caption, split_text
from clipforge.utils.log import log

ROLE_RANK = {"viewer": 0, "owner": 1}


# ============================================================================ registries
@dataclass
class Cmd:
    name: str
    fn: Callable[..., Any]
    role: str = "owner"
    group: str = ""
    help: str = ""
    usage: str = ""
    menu: bool = True        # listed in setMyCommands


@dataclass
class Danger:
    action: str
    fn: Callable[..., Any]
    question: Callable[[dict], str]
    type_yes: bool = False   # "Type YES to make this public"
    pin: bool = True         # ask the PIN (when PIN is enabled)


COMMANDS: dict[str, Cmd] = {}
ALIASES: dict[str, str] = {}
CALLBACKS: dict[str, tuple[Callable[..., Any], str]] = {}
STEPS: dict[str, Callable[..., Any]] = {}
DANGERS: dict[str, Danger] = {}
MENU_TEXT: dict[str, str] = {}  # reply-keyboard label → command


def command(name: str, *, role: str = "owner", group: str = "", help: str = "", usage: str = "", aliases: tuple = (),
            menu: bool = True):
    def deco(fn):
        COMMANDS[name] = Cmd(name, fn, role, group, help, usage, menu)
        for a in aliases:
            ALIASES[a] = name
        return fn
    return deco


def callback(action: str, *, role: str = "owner"):
    def deco(fn):
        CALLBACKS[action] = (fn, role)
        return fn
    return deco


def step(name: str):
    def deco(fn):
        STEPS[name] = fn
        return fn
    return deco


def danger(action: str, question: Callable[[dict], str], *, type_yes: bool = False, pin: bool = True):
    def deco(fn):
        DANGERS[action] = Danger(action, fn, question, type_yes, pin)
        return fn
    return deco


class Denied(Exception):
    """A friendly 'no' (already sent as text)."""


# ============================================================================ context
@dataclass
class Ctx:
    bot: Any
    chat_id: int
    user_id: int
    role: str
    message_id: int | None = None       # the incoming message, or the message holding the pressed button
    callback_id: str | None = None
    toast_text: str = ""
    sent: list[int] = field(default_factory=list)

    @property
    def api(self):
        return self.bot.api()

    @property
    def owner(self) -> bool:
        return self.role == "owner"

    # ---- buttons
    def btn(self, text: str, action: str, *, one_shot: bool = False, bind_user: bool = False,
            ttl: dt.timedelta = st.BUTTON_TTL, **payload: Any) -> dict:
        data = st.new_callback(self.chat_id, action, payload, user_id=self.user_id if bind_user else None,
                               one_shot=one_shot, ttl=ttl)
        return {"text": text, "callback_data": data}

    @staticmethod
    def url_btn(text: str, url: str) -> dict:
        return {"text": text, "url": url}

    # ---- sending
    def send(self, text: str, buttons: list[list[dict]] | None = None, *, html: bool = True,
             reply_markup: dict | None = None, quiet: bool = False) -> int | None:
        chunks = split_text(text)
        mid = None
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            markup = reply_markup or ({"inline_keyboard": buttons} if buttons and last else None)
            res = self.api.call("sendMessage", chat_id=self.chat_id, text=chunk, parse_mode="HTML" if html else None,
                                disable_web_page_preview=True, reply_markup=markup if last else None,
                                disable_notification=quiet or None)
            mid = res.get("message_id")
            if mid:
                self.sent.append(mid)
        return mid

    def edit(self, text: str, buttons: list[list[dict]] | None = None, *, message_id: int | None = None,
             html: bool = True) -> None:
        mid = message_id or self.message_id
        if not mid:
            self.send(text, buttons, html=html)
            return
        text = split_text(text)[0]
        try:
            self.api.call("editMessageText", chat_id=self.chat_id, message_id=mid, text=text,
                          parse_mode="HTML" if html else None, disable_web_page_preview=True,
                          reply_markup={"inline_keyboard": buttons} if buttons else None)
        except TgError as exc:
            if "not modified" in str(exc).lower():
                return
            if "there is no text" in str(exc).lower():  # the button sat under a photo/video: edit its caption
                cap, _ = split_caption(text)
                self.api.call("editMessageCaption", chat_id=self.chat_id, message_id=mid, caption=cap,
                              parse_mode="HTML" if html else None,
                              reply_markup={"inline_keyboard": buttons} if buttons else None)
                return
            raise

    def ask(self, prompt: str, step_name: str, payload: dict | None = None, placeholder: str = "") -> None:
        """Wait for typed input: ForceReply + a conversation row (10 min, /cancel ends it)."""
        st.set_conversation(self.chat_id, step_name, payload)
        self.send(prompt + "\n<i>/cancel to stop</i>", reply_markup={
            "force_reply": True, "selective": True, "input_field_placeholder": (placeholder or "Type here…")[:64]})

    def toast(self, text: str) -> None:
        self.toast_text = text

    def require_owner(self) -> None:
        if not self.owner:
            raise Denied("👀 Your access is read-only (viewer).")

    # ---- dangerous actions
    def ask_danger(self, action: str, **payload: Any) -> None:
        self.require_owner()
        d = DANGERS[action]
        q = d.question(payload)
        yes = self.btn("✅ Yes", "danger.yes", one_shot=True, bind_user=True, ttl=st.CONFIRM_TTL, a=action, p=payload)
        no = self.btn("✖ No", "danger.no", one_shot=True, bind_user=True, ttl=st.CONFIRM_TTL)
        self.send(f"⚠️ {q}\n<i>Buttons expire in 2 minutes.</i>", [[yes, no]])


# ============================================================================ PIN
PIN_RE = re.compile(r"^\d{6,8}$")


def pin_enabled() -> bool:
    return bool(load_config().telegram.pin_required and Secrets().telegram_pin_hash)


def set_pin(pin: str) -> None:
    from clipforge.api.auth import hash_password

    if not PIN_RE.match(pin):
        raise ValueError("The PIN must be 6–8 digits.")
    save_secret("TELEGRAM_PIN_HASH", hash_password(pin))
    st.kv_del("pin_fail")


def clear_pin() -> None:
    save_secret("TELEGRAM_PIN_HASH", "")


def pin_locked() -> float:
    """Seconds left of a lockout (0 = not locked)."""
    rec = st.kv_get("pin_fail") or {}
    return max(0.0, float(rec.get("locked_until", 0)) - time.time())


def check_pin(pin: str) -> bool:
    from clipforge.api.auth import verify_password

    if pin_locked():
        return False
    ok = bool(PIN_RE.match(pin)) and verify_password(pin, Secrets().telegram_pin_hash)
    rec = st.kv_get("pin_fail") or {"n": 0}
    if ok:
        st.kv_del("pin_fail")
        return True
    rec["n"] = rec.get("n", 0) + 1
    if rec["n"] >= 5:
        strikes = rec.get("strikes", 0) + 1  # 15 min, 30 min, 1 h, 2 h … (max 24 h): guessing never gets cheap
        minutes = min(15 * 2 ** (strikes - 1), 24 * 60)
        rec = {"n": 0, "strikes": strikes, "locked_until": time.time() + minutes * 60}
        audit(f"PIN locked for {minutes} minutes after 5 wrong tries (lockout #{strikes})")
        _alert_owners(f"🔒 Someone typed a wrong ClipForge PIN 5 times — locked for {minutes} minutes.")
    st.kv_set("pin_fail", rec, dt.timedelta(days=2))
    return False


def _alert_owners(text: str) -> None:
    try:
        from clipforge.notify import telegram

        telegram.bot().send_text(text, roles=("owner",))
    except Exception:
        pass


# ============================================================================ safety helpers
_SCRUB = [
    (re.compile(r"\d{6,}:[A-Za-z0-9_-]{25,}"), "[token]"),                          # Telegram bot tokens (also inside /bot…/ URLs)
    (re.compile(r"(?:ya29\.|1//0|GOCSPX-|AIza|EAA[A-Za-z0-9]{3}|IGQV|IGAA|gsk_|cfw_|cfsk1:|sk-)[A-Za-z0-9._/+=-]{8,}"), "[token]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"), "[secret]"),
    (re.compile(r"(?i)(?:api[_-]?key|secret|password|passwd|token|access_token|refresh_token|client_secret|authorization)"
                r"[\"']?\s*[=:]\s*[\"']?[^\s\"',;&]+"), "[secret]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[ip]"),
    (re.compile(r"\b(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}\b", re.I), "[ip]"),
    (re.compile(r"\b[\w-]+\.[\w-]+\.ts\.net\b", re.I), "[tailnet]"),
    (re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)(?:[^\\/\n\"'<>|]*[\\/])*([^\\/\s\"'<>|]+)"), r"…\1"),         # Windows paths
    (re.compile(r"(?<![\w:/.])/(?:[^/\s\"'<>]+/)+([^/\s\"'<>]+)"), r"…\1"),                          # any absolute POSIX path
]


def scrub(text: str) -> str:
    """Remove tokens, secrets, IPs, tailnet names and absolute paths from a message before it goes to Telegram.
    Also every secret ClipForge has loaded (.env values), replaced literally."""
    text = str(text)
    try:
        for v in Secrets().model_dump().values():
            if isinstance(v, str) and len(v) > 8:
                text = text.replace(v, "[secret]")
    except Exception:
        pass
    for rx, rep in _SCRUB:
        text = rx.sub(rep, text)
    return text[:1500]


def audit(detail: str) -> None:
    try:
        from clipforge.api.auth import audit as a

        a("telegram", detail[:300])
    except Exception:
        pass


def error_reply(exc: BaseException) -> str:
    """ServiceError / known errors → their sentence (scrubbed). Anything else → an error id for the log."""
    from fastapi import HTTPException

    if isinstance(exc, Denied):
        return str(exc)
    if isinstance(exc, HTTPException):
        return "⚠️ " + esc(scrub(str(exc.detail)))
    if isinstance(exc, (ValueError, FileNotFoundError)) and len(str(exc)) < 300:
        return "⚠️ " + esc(scrub(str(exc)))
    eid = secrets.token_hex(3)
    log.error(f"[telegram] error {eid}: {exc}\n{traceback.format_exc()}")
    return f"⚠️ Something went wrong (error <code>{eid}</code>). The details are in clipforge.log on the computer."


class InboundLimiter:
    """Per-chat command rate limit (config telegram.commands_per_minute)."""

    def __init__(self) -> None:
        self.hits: dict[int, collections.deque] = collections.defaultdict(collections.deque)
        self.warned: dict[int, float] = {}

    def allow(self, chat_id: int, per_minute: int) -> tuple[bool, bool]:
        """(allowed, send_a_warning)."""
        q = self.hits[chat_id]
        t = time.monotonic()
        while q and t - q[0] > 60:
            q.popleft()
        if len(q) >= per_minute:
            warn = t - self.warned.get(chat_id, -1e9) > 60
            if warn:
                self.warned[chat_id] = t
            return False, warn
        q.append(t)
        return True, False


# ============================================================================ argument helpers
def parse_ranks(text: str) -> list[int]:
    """'1-5,8' → [1,2,3,4,5,8]."""
    out: list[int] = []
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if b - a > 100:
                raise ValueError("That range is too long.")
            out += list(range(min(a, b), max(a, b) + 1))
        elif part.isdigit():
            out.append(int(part))
        else:
            raise ValueError(f"'{part}' isn't a clip number. Use e.g. 1-5,8")
    return sorted(set(out))


def parse_clock(s: str) -> float:
    """'12' / '0:12' / '1:02:13' / '12.5' → seconds."""
    s = s.strip()
    if not re.fullmatch(r"\d+(?::\d{1,2}){0,2}(?:\.\d+)?", s):
        raise ValueError(f"'{s}' isn't a time like 0:12 or 1:02:13")
    parts = s.split(":")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def fmt_dur(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
