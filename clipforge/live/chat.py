"""Live chat: spikes (messages per 10 s vs the stream's baseline + hype words) and the `!clip [note]` command.

Twitch: anonymous IRC over WebSocket (free, read-only, no login). YouTube: `liveChatMessages.list` with the gaming
channel's token, never faster than `pollingIntervalMillis` / `youtube_poll_min_sec`, inside a per-stream unit budget.
Only the broadcaster, moderators and `live.allowed_clip_commanders` may use !clip.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from clipforge.live.scorer import Signal
from clipforge.utils.log import log

CLIP_CMD = re.compile(r"^!clip\b\s*(.*)$", re.I)


@dataclass
class ChatMsg:
    t: float
    user: str
    text: str
    is_mod: bool = False
    is_owner: bool = False
    platform: str = "twitch"


def may_clip(msg: ChatMsg, allowed: list[str]) -> bool:
    return msg.is_owner or msg.is_mod or msg.user.lower() in {a.lower().lstrip("@") for a in allowed}


class ChatSpike:
    def __init__(self, hype_words: list[str], allowed: list[str], bucket: float = 10.0, baseline_sec: float = 300,
                 ratio_on: float = 2.5, min_msgs: int = 4, refractory: float = 20.0) -> None:
        self.words = [w.lower() for w in hype_words]
        self.allowed = allowed
        self.bucket, self.ratio_on, self.min_msgs, self.refractory = bucket, ratio_on, min_msgs, refractory
        self.times: deque[float] = deque()
        self.hype: deque[float] = deque()
        self.baseline_sec = baseline_sec
        self.start: float | None = None
        self.last_fire = -1e9
        self.rate = 0.0  # msgs / 10 s right now (UI meter)

    def is_hype(self, text: str) -> bool:
        low = f" {text.lower()} "
        return any((f" {w} " in low) if w.isalnum() else (w in low) for w in self.words)

    def feed(self, msg: ChatMsg) -> list[Signal]:
        out: list[Signal] = []
        t = msg.t
        if self.start is None:
            self.start = t
        m = CLIP_CMD.match(msg.text.strip())
        if m:
            if may_clip(msg, self.allowed):
                out.append(Signal(t, "manual", 1.0, f"!clip by {msg.user}" + (f": {m.group(1)[:60]}" if m.group(1) else ""),
                                  {"source": msg.platform, "note": m.group(1)[:80]}))
            return out
        self.times.append(t)
        if self.is_hype(msg.text):
            self.hype.append(t)
        while self.times and self.times[0] < t - self.baseline_sec:
            self.times.popleft()
        while self.hype and self.hype[0] < t - self.bucket:
            self.hype.popleft()
        now_n = sum(1 for x in self.times if x >= t - self.bucket)
        self.rate = now_n
        span = max(self.bucket, min(self.baseline_sec, t - self.start))
        base = max(0.5, (len(self.times) - now_n) / max(1.0, span - self.bucket) * self.bucket) if span > self.bucket else 1.0
        ratio = now_n / base
        hype_n = len(self.hype)
        if t - self.last_fire >= self.refractory and now_n >= self.min_msgs and (ratio >= self.ratio_on or hype_n >= 5):
            self.last_fire = t
            v = min(1.0, 0.35 + 0.15 * (ratio - self.ratio_on) + 0.06 * hype_n)
            out.append(Signal(t, "chat", round(max(0.35, v), 3), f"chat spike ({now_n} msgs/10 s)",
                              {"ratio": round(ratio, 2), "hype": hype_n}))
        return out


# ----------------------------------------------------------------------------- Twitch (anonymous IRC over WebSocket)
def parse_irc(line: str, now: float) -> ChatMsg | None:
    """`@badges=moderator/1;display-name=X;mod=1 :x!x@x.tmi.twitch.tv PRIVMSG #chan :text` → ChatMsg."""
    if "PRIVMSG" not in line:
        return None
    tags: dict[str, str] = {}
    rest = line
    if line.startswith("@"):
        raw, _, rest = line.partition(" ")
        for kv in raw[1:].split(";"):
            k, _, v = kv.partition("=")
            tags[k] = v
    m = re.match(r":(\w+)!\S+ PRIVMSG #\S+ :(.*)", rest)
    if not m:
        return None
    badges = tags.get("badges", "")
    return ChatMsg(now, tags.get("display-name") or m.group(1), m.group(2).rstrip("\r\n"),
                   is_mod=tags.get("mod") == "1" or "moderator/" in badges, is_owner="broadcaster/" in badges,
                   platform="twitch")


class TwitchChat(threading.Thread):
    URL = "wss://irc-ws.chat.twitch.tv:443"

    def __init__(self, channel: str, on_msg: Callable[[ChatMsg], None], clock: Callable[[], float],
                 connect=None) -> None:
        super().__init__(daemon=True, name="live-twitch-chat")
        self.channel = channel.lower().lstrip("#").rsplit("/", 1)[-1]
        self.on_msg, self.clock = on_msg, clock
        self._connect = connect
        self.stop_ev = threading.Event()
        self.connected = False

    def run(self) -> None:
        backoff = 2
        while not self.stop_ev.is_set():
            try:
                connect = self._connect
                if connect is None:
                    from websockets.sync.client import connect
                with connect(self.URL, open_timeout=10) as ws:
                    ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
                    ws.send("PASS SCHMOOPIIE")
                    ws.send(f"NICK justinfan{int(time.time()) % 90000 + 10000}")
                    ws.send(f"JOIN #{self.channel}")
                    self.connected, backoff = True, 2
                    while not self.stop_ev.is_set():
                        try:
                            data = ws.recv(timeout=1)
                        except TimeoutError:
                            continue
                        for line in str(data).split("\r\n"):
                            if line.startswith("PING"):
                                ws.send("PONG :tmi.twitch.tv")
                            msg = parse_irc(line, self.clock())
                            if msg:
                                self.on_msg(msg)
            except Exception as exc:
                self.connected = False
                if self.stop_ev.is_set():
                    break
                log.warning(f"[live] Twitch chat: {exc}; retrying in {backoff} s")
                self.stop_ev.wait(backoff)
                backoff = min(60, backoff * 2)
        self.connected = False

    def stop(self) -> None:
        self.stop_ev.set()


# ----------------------------------------------------------------------------- YouTube live chat (API, budgeted)
class YouTubeChat(threading.Thread):
    def __init__(self, service, on_msg: Callable[[ChatMsg], None], clock: Callable[[], float], *, min_sec: int,
                 units_per_poll: int, max_units: int, charge: Callable[[int], None] | None = None,
                 video_id: str = "", quota_ok: Callable[[], bool] | None = None) -> None:
        super().__init__(daemon=True, name="live-youtube-chat")
        self.service, self.on_msg, self.clock = service, on_msg, clock
        self.min_sec, self.units_per_poll, self.max_units = min_sec, units_per_poll, max_units
        self.charge = charge or (lambda n: None)
        self.video_id = video_id
        self.quota_ok = quota_ok or (lambda: True)
        self.stop_ev = threading.Event()
        self.units = 0
        self.connected = False
        self.chat_id = ""

    def find_chat(self) -> str:
        """The live chat of ANY public live video (videos.list liveStreamingDetails, 1 unit)."""
        res = self.service.videos().list(part="liveStreamingDetails", id=self.video_id).execute()
        self.units += 1
        self.charge(1)
        for v in res.get("items", []):
            cid = v.get("liveStreamingDetails", {}).get("activeLiveChatId")
            if cid:
                return cid
        return ""

    def run(self) -> None:
        token = None
        try:
            self.chat_id = self.find_chat()
        except Exception as exc:
            log.warning(f"[live] YouTube chat: can't find the live broadcast: {exc}")
            return
        if not self.chat_id:
            log.info("[live] YouTube chat: no active broadcast on the gaming channel")
            return
        self.connected = True
        first = True
        while not self.stop_ev.is_set():
            if not self.quota_ok():
                log.warning("[live] YouTube chat paused: keeping today's API quota for uploads")
                break
            if self.units + self.units_per_poll > self.max_units:
                log.warning("[live] YouTube chat: per-stream quota budget used up — chat reading stopped")
                break
            wait = self.min_sec
            try:
                res = self.service.liveChatMessages().list(liveChatId=self.chat_id, part="snippet,authorDetails",
                                                            pageToken=token, maxResults=200).execute()
                self.units += self.units_per_poll
                self.charge(self.units_per_poll)
                token = res.get("nextPageToken")
                wait = max(self.min_sec, res.get("pollingIntervalMillis", 0) / 1000)
                if not first:  # the first page is history: only advance the token
                    for it in res.get("items", []):
                        a = it.get("authorDetails", {})
                        text = it.get("snippet", {}).get("displayMessage", "")
                        self.on_msg(ChatMsg(self.clock(), a.get("displayName", ""), text, is_mod=bool(a.get("isChatModerator")),
                                            is_owner=bool(a.get("isChatOwner")), platform="youtube"))
                first = False
            except Exception as exc:
                log.warning(f"[live] YouTube chat: {exc}")
                if "liveChatEnded" in str(exc) or "forbidden" in str(exc).lower():
                    break
                wait = max(wait, 60)
            self.stop_ev.wait(wait)
        self.connected = False

    def stop(self) -> None:
        self.stop_ev.set()


# ----------------------------------------------------------------------------- Kick (public Pusher websocket)
def kick_pusher_url(key: str, cluster: str = "us2") -> str:
    return f"wss://ws-{cluster}.pusher.com/app/{key}?protocol=7&client=js&version=8.4.0&flash=false"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
      "Accept": "application/json"}


def kick_chatroom_id(slug: str) -> int | None:
    import httpx

    try:
        r = httpx.get(f"https://kick.com/api/v2/channels/{slug}", headers=UA, timeout=20, follow_redirects=True)
        return int(r.json()["chatroom"]["id"]) if r.status_code == 200 else None
    except Exception as exc:
        log.warning(f"[live] Kick chat: channel lookup failed: {exc}")
        return None


def parse_kick(raw: str, now: float) -> ChatMsg | None:
    import json as _json

    try:
        msg = _json.loads(raw)
        if msg.get("event") != r"App\Events\ChatMessageEvent":
            return None
        d = _json.loads(msg["data"]) if isinstance(msg.get("data"), str) else msg.get("data", {})
        sender = d.get("sender", {})
        badges = {b.get("type") for b in sender.get("identity", {}).get("badges", [])}
        return ChatMsg(now, sender.get("username", ""), d.get("content", ""), is_mod="moderator" in badges,
                       is_owner="broadcaster" in badges, platform="kick")
    except Exception:
        return None


class KickChat(threading.Thread):
    def __init__(self, slug: str, on_msg: Callable[[ChatMsg], None], clock: Callable[[], float], connect=None,
                 room_id: int | None = None, key: str = "", cluster: str = "us2") -> None:
        super().__init__(daemon=True, name="live-kick-chat")
        self.slug, self.on_msg, self.clock, self._connect, self.room_id = slug, on_msg, clock, connect, room_id
        self.url = kick_pusher_url(key, cluster)
        self.stop_ev = threading.Event()
        self.connected = False

    def run(self) -> None:
        import json as _json

        room = self.room_id or kick_chatroom_id(self.slug)
        if not room:
            log.warning(f"[live] Kick chat for {self.slug}: not available (chat signals off for this stream)")
            return
        backoff = 2
        while not self.stop_ev.is_set():
            try:
                connect = self._connect
                if connect is None:
                    from websockets.sync.client import connect
                with connect(self.url, open_timeout=10) as ws:
                    ws.send(_json.dumps({"event": "pusher:subscribe", "data": {"auth": "", "channel": f"chatrooms.{room}.v2"}}))
                    self.connected, backoff = True, 2
                    while not self.stop_ev.is_set():
                        try:
                            raw = ws.recv(timeout=1)
                        except TimeoutError:
                            continue
                        if '"pusher:ping"' in str(raw):
                            ws.send(_json.dumps({"event": "pusher:pong", "data": {}}))
                            continue
                        m = parse_kick(str(raw), self.clock())
                        if m:
                            self.on_msg(m)
            except Exception as exc:
                self.connected = False
                if self.stop_ev.is_set():
                    break
                if "4001" in str(exc) or "not in this cluster" in str(exc):  # Kick changed its chat key: no retry loop
                    log.warning("[live] Kick chat: the chat key is outdated (live.chat.kick_pusher_key) — chat signals "
                                "off for this stream; audio, game events and /clip still work")
                    break
                log.warning(f"[live] Kick chat: {exc}; retrying in {backoff} s")
                self.stop_ev.wait(backoff)
                backoff = min(60, backoff * 2)
        self.connected = False

    def stop(self) -> None:
        self.stop_ev.set()
