"""Telegram bot (Phase 14): notifications + quick control, with no public server.

The laptop long-polls the Bot API (`getUpdates`), so nothing needs to reach the laptop from the internet. Only the paired
Telegram user can talk to it. Pairing: Settings shows a one-time code (valid 10 min); you send `/pair <code>` to your
bot; ClipForge stores your user id in .env (TELEGRAM_CHAT_ID). Messages from anyone else are ignored and audit-logged.

Plain httpx against https://api.telegram.org (free). Tests swap the transport (httpx.MockTransport).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from clipforge.config import Secrets, load_config, save_secret
from clipforge.utils.log import log

API = "https://api.telegram.org"
VIDEO_LIMIT = 50 * 1024 * 1024
URL_RE = re.compile(r"https?://\S+")
HELP = """ClipForge bot — commands:
/new <youtube link> — start a job (or just send a link)
/status — what's running now
/queue — next 5 posts
/approve <job> <ranks> — e.g. /approve 12 1,3,5
/schedule <job> [youtube|instagram|both] — add approved clips + auto-schedule
/pause <account> · /resume <account>
/dashboard — followers, views, YPP progress
/help — this list"""


class TgError(RuntimeError):
    pass


class BotAPI:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None) -> None:
        self.token = token
        self.http = httpx.Client(base_url=f"{API}/bot{token}/", timeout=httpx.Timeout(70, connect=15), transport=transport)

    def call(self, method: str, files: dict | None = None, **params: Any) -> Any:
        data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items() if v is not None}
        try:
            r = self.http.post(method, data=data, files=files) if files else self.http.post(method, json=params)
        except httpx.HTTPError as exc:
            raise TgError(f"network: {exc}") from exc
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if not body.get("ok"):
            raise TgError(body.get("description") or f"HTTP {r.status_code}")
        return body["result"]


# ----------------------------------------------------------------------------- the bot
class Bot:
    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self.transport = transport
        self.pair_code: tuple[str, float] | None = None
        self.offset = 0
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.pending_confirm: dict[str, tuple[Callable[[], str], float]] = {}
        self.ignored_logged: dict[int, float] = {}
        self.sent_keys: set[str] = set()  # de-duplicate repeated alerts (e.g. re-login every minute)
        self.lock = threading.Lock()

    # ---- config
    @property
    def token(self) -> str:
        return Secrets().telegram_bot_token or ""

    @property
    def chat_id(self) -> str:
        return Secrets().telegram_chat_id or ""

    def api(self) -> BotAPI:
        if not self.token:
            raise TgError("No bot token — paste it in Settings → Phone & Telegram.")
        return BotAPI(self.token, self.transport)

    def enabled(self) -> bool:
        return bool(load_config().telegram.enabled and self.token)

    # ---- pairing
    def new_pair_code(self) -> str:
        code = f"{secrets.randbelow(10 ** 6):06d}"
        self.pair_code = (code, time.time() + 600)
        return code

    def _try_pair(self, user_id: int, text: str) -> bool:
        m = re.match(r"^/(?:pair|start)\s+(\d{6})\s*$", text.strip())
        if not m or not self.pair_code:
            return False
        code, until = self.pair_code
        if time.time() > until or not secrets.compare_digest(m.group(1), code):
            return False
        save_secret("TELEGRAM_CHAT_ID", str(user_id))
        self.pair_code = None
        _audit("telegram", f"Paired Telegram user {user_id}")
        return True

    # ---- sending
    def send_text(self, text: str, buttons: list[list[dict]] | None = None) -> bool:
        if not self.chat_id or not self.token:
            return False
        try:
            self.api().call("sendMessage", chat_id=self.chat_id, text=text[:4000], disable_web_page_preview=True,
                            reply_markup={"inline_keyboard": buttons} if buttons else None)
            return True
        except TgError as exc:
            log.warning(f"[telegram] send failed: {exc}")
            return False

    def send_video(self, path: Path, caption: str, buttons: list[list[dict]] | None = None) -> bool:
        if not self.chat_id or not path.exists() or path.stat().st_size > VIDEO_LIMIT:
            return self.send_text(caption, buttons)
        try:
            with open(path, "rb") as f:
                self.api().call("sendVideo", files={"video": (path.name, f, "video/mp4")}, chat_id=self.chat_id,
                                caption=caption[:1000], supports_streaming="true",
                                reply_markup={"inline_keyboard": buttons} if buttons else None)
            return True
        except TgError as exc:
            log.warning(f"[telegram] video failed ({exc}); sending text")
            return self.send_text(caption, buttons)

    def notify(self, kind: str, text: str, key: str | None = None, buttons: list[list[dict]] | None = None) -> bool:
        """kind = config toggle name without 'notify_' (job_done, published, …). key de-duplicates repeats."""
        t = load_config().telegram
        if not self.enabled() or not getattr(t, f"notify_{kind}", True):
            return False
        if key:
            with self.lock:
                if key in self.sent_keys:
                    return False
                self.sent_keys.add(key)
        return self.send_text(text, buttons)

    # ---- receiving
    def handle(self, upd: dict) -> None:
        if "callback_query" in upd:
            return self._handle_callback(upd["callback_query"])
        msg = upd.get("message") or {}
        user = (msg.get("from") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if not user or not text:
            return
        if not self.chat_id:
            if self._try_pair(user, text):
                self.api().call("sendMessage", chat_id=user, text="✅ Paired with ClipForge. Send /help for commands.")
            return
        if str(user) != self.chat_id:  # anybody else: ignore (log at most once an hour per user)
            if time.time() - self.ignored_logged.get(user, 0) > 3600:
                self.ignored_logged[user] = time.time()
                _audit("telegram", f"Ignored a message from unknown Telegram user {user}")
            return
        try:
            reply = self.command(text)
        except Exception as exc:  # a bad command must never kill the bot
            reply = f"⚠️ {exc}"
        if isinstance(reply, tuple):
            self.send_text(*reply)
        elif reply:
            self.send_text(reply)

    def _handle_callback(self, cq: dict) -> None:
        user = str((cq.get("from") or {}).get("id"))
        data = cq.get("data") or ""
        api = self.api()
        if user != self.chat_id:
            api.call("answerCallbackQuery", callback_query_id=cq.get("id"), text="Not allowed")
            return
        text = "?"
        try:
            if data.startswith(("ap:", "rj:")):
                _, job, rank = data.split(":")
                n = commands.set_approved(int(job), [int(rank)], data.startswith("ap:"))
                text = ("Approved" if data.startswith("ap:") else "Rejected") + f" clip #{rank}" if n else "Clip not found"
            elif data.startswith("ok:"):
                item = self.pending_confirm.pop(data[3:], None)
                text = item[0]() if item and time.time() - item[1] < 600 else "That confirmation expired — send the command again."
                self.send_text(text)
            elif data.startswith("no:"):
                self.pending_confirm.pop(data[3:], None)
                text = "Cancelled"
        except Exception as exc:
            text = f"⚠️ {exc}"
        api.call("answerCallbackQuery", callback_query_id=cq.get("id"), text=text[:190])

    def confirm(self, question: str, action: Callable[[], str]) -> tuple[str, list[list[dict]]]:
        tok = secrets.token_hex(6)
        self.pending_confirm[tok] = (action, time.time())
        return question, [[{"text": "✅ Yes", "callback_data": f"ok:{tok}"}, {"text": "✖ No", "callback_data": f"no:{tok}"}]]

    def command(self, text: str) -> str | tuple | None:
        parts = text.split()
        cmd = parts[0].lower().split("@")[0] if parts else ""
        args = parts[1:]
        if not cmd.startswith("/") and URL_RE.search(text):
            return commands.new_job(URL_RE.search(text).group(0))  # type: ignore[union-attr]
        if cmd in ("/help", "/start"):
            return HELP
        if cmd == "/new":
            if not args:
                return "Usage: /new <youtube link>"
            return commands.new_job(args[0])
        if cmd == "/status":
            return commands.status()
        if cmd == "/queue":
            return commands.queue_next(5)
        if cmd == "/approve":
            if len(args) < 2:
                return "Usage: /approve <job id> <ranks, e.g. 1,3,5>"
            ranks = [int(x) for x in re.split(r"[,\s]+", " ".join(args[1:])) if x.isdigit()]
            n = commands.set_approved(int(args[0]), ranks, True)
            return f"Approved {n} clip(s) of job {args[0]}."
        if cmd == "/schedule":
            if not args or not args[0].isdigit():
                return "Usage: /schedule <job id> [youtube|instagram|both]"
            job, plat = int(args[0]), (args[1].lower() if len(args) > 1 else "both")
            if plat not in ("youtube", "instagram", "both"):
                return "Platform must be youtube, instagram or both."
            return self.confirm(f"Add the approved clips of job {job} to {plat} and auto-schedule them?",
                                lambda: commands.schedule_job(job, plat))
        if cmd in ("/pause", "/resume"):
            if not args:
                return f"Usage: {cmd} <account name>"
            name = " ".join(args)
            if cmd == "/pause":
                return self.confirm(f"Pause {name}? Nothing will be posted there until you /resume.",
                                    lambda: commands.set_paused(name, True))
            return commands.set_paused(name, False)
        if cmd == "/dashboard":
            return commands.dashboard()
        return "Unknown command. " + HELP

    # ---- loop
    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()
        self.thread = threading.Thread(target=self._loop, name="clipforge-telegram", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        backoff = 1.0
        while not self.stop.is_set():
            if not self.enabled():
                self.stop.wait(30)
                continue
            try:
                self.daily_summary_if_due()
                updates = self.api().call("getUpdates", offset=self.offset, timeout=50, allowed_updates=["message", "callback_query"])
                for u in updates:
                    self.offset = max(self.offset, u["update_id"] + 1)
                    self.handle(u)
                backoff = 1.0
            except Exception as exc:  # network drops, bad token, Telegram hiccups → back off, never die
                log.debug(f"[telegram] poll failed: {exc}")
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 60)

    def daily_summary_if_due(self, now: dt.datetime | None = None) -> bool:
        from clipforge.publish.timez import now_utc, utc_to_local

        cfg = load_config()
        if not cfg.telegram.daily_summary or not self.chat_id:
            return False
        local = utc_to_local(now or now_utc(), cfg.publishing.timezone)
        hh, mm = (int(x) for x in cfg.telegram.daily_summary_time.split(":"))
        state_p = cfg.workspace / "telegram_state.json"
        state = json.loads(state_p.read_text(encoding="utf-8")) if state_p.exists() else {}
        today = local.date().isoformat()
        if state.get("summary_day") == today or (local.hour, local.minute) < (hh, mm):
            return False
        state["summary_day"] = today
        state_p.write_text(json.dumps(state), encoding="utf-8")
        return self.send_text(commands.daily_summary(local.date()))


def _audit(kind: str, detail: str) -> None:
    try:
        from clipforge.api.auth import audit

        audit(kind, detail)
    except Exception:
        pass


_bot: Bot | None = None


def bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot()
    return _bot


def set_bot(b: Bot | None) -> None:
    """Tests install a bot with a fake transport."""
    global _bot
    _bot = b


# ----------------------------------------------------------------------------- notification hooks
def on_job_status(job_id: int, status: str, error: str = "") -> None:
    """Called by the job manager when a pipeline job finishes or fails."""
    try:
        b = bot()
        if not b.enabled():
            return
        if status == "failed":
            b.notify("job_failed", f"❌ Job {job_id} failed: {error[:300]}")
            return
        if status != "done" or not load_config().telegram.notify_job_done:
            return
        from clipforge.notify import commands as c

        title, top = c.top_clips(job_id, 3)
        b.send_text(f"✅ Job {job_id} finished: {title}\nTop clips below — approve or reject, or open the app.")
        for clip in top:
            buttons = [[{"text": "✅ Approve", "callback_data": f"ap:{job_id}:{clip['rank']}"},
                        {"text": "❌ Reject", "callback_data": f"rj:{job_id}:{clip['rank']}"}]]
            url = c.app_url(f"/clips/{job_id}-{clip['rank']}/edit")
            if url:
                buttons[0].append({"text": "✏️ Open", "url": url})
            b.send_video(clip["preview"], f"#{clip['rank']} · {clip['title']} · score {clip['score']:.1f}", buttons)
    except Exception as exc:
        log.warning(f"[telegram] job notification failed: {exc}")


def on_run_report(rep) -> None:
    """Called after every scheduler run: published / failed / missed / re-login / quota."""
    try:
        b = bot()
        if not b.enabled():
            return
        from clipforge.publish import queue as q

        for iid in rep.published:
            it = q.get(iid)
            if it:
                b.notify("published", f"🚀 Published on {it.platform.title()}: {it.title}\n{it.permalink or it.youtube_video_id and f'https://youtube.com/shorts/{it.youtube_video_id}' or ''}",
                         key=f"pub:{iid}")
        for iid in [*rep.failed, *rep.missed]:
            it = q.get(iid)
            if it:
                b.notify("publish_failed", f"⚠️ {it.platform.title()} — {it.title}: {it.error[:300]}",
                         key=f"fail:{iid}:{it.status}:{it.reason}")
        for ch_id, why in rep.skipped_channels.items():
            if why == "needs re-login":
                b.notify("reauth", f"🔑 {ch_id} needs a new login — open Accounts on the laptop and click Reconnect.",
                         key=f"reauth:{ch_id}:{dt.date.today()}")
        if rep.quota_exhausted:
            b.notify("quota", "⏳ YouTube upload quota used up today — uploads continue tomorrow automatically.",
                     key=f"quota:{dt.date.today()}")
    except Exception as exc:
        log.warning(f"[telegram] report notification failed: {exc}")


from clipforge.notify import commands  # noqa: E402  (commands imports nothing from here at module level)
