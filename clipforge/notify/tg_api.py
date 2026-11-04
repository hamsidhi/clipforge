"""Telegram Bot API client: rate limits, 429 retry_after, message splitting, file_id cache.

Limits (checked against core.telegram.org/bots/api + /bots/faq on 2026-11-04, Bot API 10.3):
  text 1–4096 chars · caption 0–1024 · callback_data 1–64 bytes · sendMediaGroup 2–10 items
  cloud Bot API: send ≤ 50 MB, getFile download ≤ 20 MB · self-hosted telegram-bot-api: upload ≤ 2000 MB, download unlimited
  ~1 message/s per chat (short bursts OK), ~30 messages/s overall, 20/min in groups; HTTP 429 carries parameters.retry_after
"""

from __future__ import annotations

import collections
import html
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from clipforge.utils.log import log

CLOUD_API = "https://api.telegram.org"
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
CALLBACK_LIMIT = 64
MB = 1024 * 1024
CLOUD_SEND_LIMIT = 50 * MB
CLOUD_DOWNLOAD_LIMIT = 20 * MB
LOCAL_LIMIT = 2000 * MB
CHAT_METHODS = ("send", "edit", "copy", "forward")


class TgError(RuntimeError):
    def __init__(self, msg: str, code: int = 0, retry_after: float | None = None) -> None:
        super().__init__(msg)
        self.code = code
        self.retry_after = retry_after


class Limiter:
    """Token buckets: per chat ~1 msg/s (burst 3), edits ≤ 20/min per chat, ~25 msg/s overall (below the 30/s cap).
    `pause_until` is set from a 429 so every sender waits, not just the one that got it."""

    def __init__(self, per_chat: float = 1.0, burst: int = 3, overall: float = 25.0, edits_per_min: int = 20,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_chat, self.burst, self.overall, self.edits_per_min = per_chat, burst, overall, edits_per_min
        self.sleep, self.clock = sleep, clock
        self.lock = threading.Lock()
        self.chat_tokens: dict[str, tuple[float, float]] = {}
        self.global_tokens = (overall, clock())
        self.edits: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.pause_until = 0.0

    def _take(self, state: tuple[float, float], rate: float, cap: float) -> tuple[tuple[float, float], float]:
        tokens, last = state
        now = self.clock()
        tokens = min(cap, tokens + (now - last) * rate)
        if tokens >= 1:
            return (tokens - 1, now), 0.0
        return (tokens, now), (1 - tokens) / rate

    def wait(self, chat: str | None, edit: bool = False) -> float:
        """Blocks until this call may go out. Returns the seconds waited (tests)."""
        waited = 0.0
        while True:
            with self.lock:
                delay = max(0.0, self.pause_until - self.clock())
                if not delay and chat is not None and edit:
                    q = self.edits[chat]
                    while q and self.clock() - q[0] > 60:
                        q.popleft()
                    if len(q) >= self.edits_per_min:
                        delay = 60 - (self.clock() - q[0]) + 0.01
                if not delay and chat is not None:
                    st, delay = self._take(self.chat_tokens.get(chat, (float(self.burst), self.clock())), self.per_chat, self.burst)
                    if not delay:
                        g, delay = self._take(self.global_tokens, self.overall, self.overall)
                        if not delay:
                            self.chat_tokens[chat], self.global_tokens = st, g
                            if edit:
                                self.edits[chat].append(self.clock())
                            return waited
                elif not delay:
                    g, delay = self._take(self.global_tokens, self.overall, self.overall)
                    if not delay:
                        self.global_tokens = g
                        return waited
            self.sleep(delay)
            waited += delay

    def backoff(self, seconds: float) -> None:
        with self.lock:
            self.pause_until = max(self.pause_until, self.clock() + seconds)


class BotAPI:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None, base_url: str = CLOUD_API,
                 limiter: Limiter | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.token = token
        self.base = (base_url or CLOUD_API).rstrip("/")
        self.local_server = self.base != CLOUD_API
        self.http = httpx.Client(base_url=f"{self.base}/bot{token}/", timeout=httpx.Timeout(70, connect=15), transport=transport)
        self.files = httpx.Client(base_url=f"{self.base}/file/bot{token}/", timeout=httpx.Timeout(600, connect=15), transport=transport)
        self.limiter = limiter or Limiter(sleep=sleep)
        self.sleep = sleep

    @property
    def send_limit(self) -> int:
        return LOCAL_LIMIT if self.local_server else CLOUD_SEND_LIMIT

    @property
    def download_limit(self) -> int:
        return LOCAL_LIMIT if self.local_server else CLOUD_DOWNLOAD_LIMIT

    def call(self, method: str, files: dict | None = None, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        chat = str(params["chat_id"]) if "chat_id" in params and method.startswith(CHAT_METHODS) else None
        if chat is not None or method.startswith(("send", "edit")):
            self.limiter.wait(chat, edit=method.startswith("edit"))
        for attempt in range(4):
            try:
                if files:
                    data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v).lower() if isinstance(v, bool) else v)
                            for k, v in params.items()}
                    for f in files.values():  # a retried upload must start from the beginning of the file
                        if hasattr(f[1], "seek"):
                            f[1].seek(0)
                    r = self.http.post(method, data=data, files=files)
                else:
                    r = self.http.post(method, json=params)
            except httpx.HTTPError as exc:
                raise TgError(f"network: {type(exc).__name__}") from exc
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if body.get("ok"):
                return body["result"]
            retry = (body.get("parameters") or {}).get("retry_after")
            if r.status_code == 429 and retry is not None and attempt < 3:
                wait = min(float(retry), 60.0)
                log.warning(f"[telegram] flood control: waiting {wait:.0f} s (retry_after)")
                self.limiter.backoff(wait)
                self.sleep(wait)
                continue
            raise TgError(body.get("description") or f"HTTP {r.status_code}", body.get("error_code") or r.status_code, retry)
        raise TgError("flood control: gave up after 3 retries", 429)

    def download(self, file_path: str, dest: Path) -> Path:
        """getFile's file_path → dest. A local server in --local mode returns an absolute path on its own disk."""
        p = Path(file_path)
        if self.local_server and p.is_absolute() and p.exists():
            import shutil

            shutil.copyfile(p, dest)
            return dest
        with self.files.stream("GET", file_path) as r:
            if r.status_code != 200:
                raise TgError(f"download failed: HTTP {r.status_code}", r.status_code)
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        return dest


# ----------------------------------------------------------------------------- text helpers
def esc(s: Any) -> str:
    return html.escape(str(s), quote=False)


_PRE = re.compile(r"(<pre>.*?</pre>)", re.S)


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Split at paragraph / line / word boundaries — never mid-word, and a <pre> block is closed and reopened
    instead of being cut. Limit counts characters (Telegram counts after entity parsing; HTML tags only shrink it)."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    cur = ""
    for piece in _PRE.split(text):
        units = [piece] if piece.startswith("<pre>") else re.split(r"(?<=\n)", piece)
        for u in units:
            if len(cur) + len(u) <= limit:
                cur += u
                continue
            if cur.strip():
                parts.append(cur.rstrip("\n"))
            cur = ""
            if len(u) <= limit:
                cur = u
                continue
            parts += _split_long(u, limit)
    if cur.strip():
        parts.append(cur.rstrip("\n"))
    return parts


def _split_long(u: str, limit: int) -> list[str]:
    if u.startswith("<pre>"):
        inner = u[5:-6]
        room = limit - 11
        return [f"<pre>{c}</pre>" for c in _words(inner, room)]
    return _words(u, limit)


def _words(s: str, limit: int) -> list[str]:
    out, cur = [], ""
    for w in re.split(r"(?<= )", s):
        while len(w) > limit:  # a single "word" longer than a message (a URL…): hard cut is the only option
            if cur:
                out.append(cur)
                cur = ""
            out.append(w[:limit])
            w = w[limit:]
        if len(cur) + len(w) > limit:
            out.append(cur)
            cur = ""
        cur += w
    if cur:
        out.append(cur)
    return [x.rstrip() for x in out if x.strip()]


def split_caption(text: str, limit: int = CAPTION_LIMIT) -> tuple[str, list[str]]:
    """(caption that fits on the media, follow-up messages with the rest)."""
    if len(text) <= limit:
        return text, []
    first = split_text(text, limit)
    head = first[0]
    rest = text[len(head):].lstrip()
    return head, split_text(rest) if rest else []


def bar(fraction: float, width: int = 10) -> str:
    n = max(0, min(width, round(fraction * width)))
    return "▰" * n + "▱" * (width - n)
