"""Telegram bot: a full remote control for ClipForge (Phase 15; basic bot since Phase 14), with no public server.

The machine that owns the database long-polls the Bot API (`getUpdates`) — nothing needs to reach it from the internet.
  * One poller per bot token: a DB lease (tg_store.acquire_poller) + backing off on HTTP 409; a `worker` never polls.
  * `last_update_id` is stored durably, so a restart neither replays nor skips updates.
  * Paired chats only (tg_core); unknown chats get no reply at all. Commands live in tg_jobs / tg_clips / tg_publish /
    tg_admin and call clipforge.services — the same code as the web app and the CLI.
Tests swap the HTTP transport (tests/fake_telegram.py, httpx.MockTransport).
"""

from __future__ import annotations

import datetime as dt
import re
import threading
import time
from typing import Callable

import httpx

from clipforge.config import Secrets, load_config
from clipforge.notify import tg_store as st
from clipforge.notify.tg_api import CLOUD_API, BotAPI, Limiter, TgError, esc  # noqa: F401  (re-exported)
from clipforge.notify.tg_core import (ALIASES, CALLBACKS, COMMANDS, MENU_TEXT, ROLE_RANK, STEPS, Ctx, Denied,
                                      InboundLimiter, audit, error_reply, scrub)
from clipforge.utils.log import log

# importing the handler modules registers their commands / buttons / steps
from clipforge.notify import tg_admin, tg_clips, tg_jobs, tg_publish  # noqa: E402,F401  isort: skip

API = CLOUD_API
LEASE_SECONDS = 300  # poller lease; renewed before every update
VIDEO_LIMIT = 50 * 1024 * 1024
URL_RE = re.compile(r"https?://\S+")
ANONYMOUS_IDS = {1087968824, 777000, 136817688}  # GroupAnonymousBot, Telegram (linked channel), Channel_Bot
PAIR_RE = re.compile(r"^/(?:pair|start)(?:@\w+)?\s+(\d{6})\s*$")
HELP = tg_admin.INTRO


class Bot:
    def __init__(self, transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.transport = transport
        self.sleep = sleep
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.ticker: threading.Thread | None = None
        self.inbound = InboundLimiter()
        self.ignored_logged: dict[int, float] = {}
        self.sent_keys: set[str] = set()  # de-duplicate repeated alerts (e.g. re-login every minute)
        self.lock = threading.Lock()
        self._api: BotAPI | None = None
        self._api_key: tuple[str, str] | None = None
        self.limiter = Limiter(sleep=sleep)
        self.conflict_logged = 0.0
        self.commands_registered = ""
        self.last_purge = 0.0
        self.polling = False

    # ---- config
    @property
    def token(self) -> str:
        return Secrets().telegram_bot_token or ""

    @property
    def chat_id(self) -> str:  # Phase 14 compatibility: the first private owner chat
        owners = [c for c in st.chats() if c.role == "owner" and c.kind == "private"]
        return str(owners[0].chat_id) if owners else ""

    def api(self) -> BotAPI:
        if not self.token:
            raise TgError("No bot token — paste it in Settings → Phone & Telegram.")
        base = load_config().telegram.api_base_url or CLOUD_API
        key = (self.token, base)
        if self._api is None or self._api_key != key:
            self._api = BotAPI(self.token, self.transport, base, self.limiter, self.sleep)
            self._api_key = key
        return self._api

    def enabled(self) -> bool:
        return bool(load_config().telegram.enabled and self.token)

    # ---- pairing (codes live in the DB: the CLI, the web app and the bot share them)
    def new_pair_code(self, role: str = "owner", allow_group: bool = False) -> str:
        return st.new_pair_code(role, allow_group)

    @property
    def pair_code(self) -> bool | None:  # Phase 14 tests: "is a code active?"
        return True if st.pair_code_active() else None

    def _try_pair(self, msg: dict, text: str) -> bool:
        m = PAIR_RE.match(text.strip())
        if not m:
            return False
        chat = msg["chat"]
        user = msg["from"]["id"]
        ok, rec = st.check_pair_code(m.group(1))
        if not ok or not rec:
            if rec is not None:
                audit(f"Wrong pairing code from Telegram user {user}" + (" — code cancelled after 5 tries" if rec.get("failures", 0) >= 5 else ""))
            return False
        kind = chat.get("type", "private")
        if kind != "private" and not rec.get("group"):
            audit(f"Pairing refused for {kind} chat {chat['id']} (code not made for groups)")
            return False
        name = " ".join(x for x in (msg["from"].get("first_name"), msg["from"].get("username")) if x) if kind == "private" \
            else chat.get("title", kind)
        st.add_chat(chat["id"], user, rec.get("role", "owner"), kind, name or str(user))
        audit(f"Paired Telegram {kind} chat {chat['id']} as {rec.get('role', 'owner')}")
        ctx = Ctx(self, chat["id"], user, rec.get("role", "owner"))
        ctx.send(f"✅ Paired with ClipForge as <b>{rec.get('role', 'owner')}</b>. Send /help to see everything, /menu for buttons.")
        self.register_commands(force=True)
        return True

    # ---- sending (broadcast to paired chats)
    def send_text(self, text: str, buttons: list[list[dict]] | None = None, html: bool = False,
                  roles: tuple[str, ...] = ("owner", "viewer")) -> bool:
        if not self.token:
            return False
        sent = False
        for c in st.chats():
            if c.role not in roles:
                continue
            try:
                Ctx(self, c.chat_id, c.user_id, c.role).send(text if html else esc(text), buttons if c.role == "owner" else None)
                sent = True
            except TgError as exc:
                log.warning(f"[telegram] send to {c.chat_id} failed: {exc}")
        return sent

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

    def register_commands(self, force: bool = False) -> None:
        """setMyCommands: the menu Telegram shows when you type '/'."""
        if self.commands_registered == self.token and not force:
            return
        cmds = [{"command": c.name, "description": c.help[:256] or c.name} for c in COMMANDS.values() if c.menu][:100]
        try:
            self.api().call("setMyCommands", commands=cmds, scope={"type": "all_private_chats"})
            self.commands_registered = self.token
        except TgError as exc:
            log.debug(f"[telegram] setMyCommands failed: {exc}")

    # ---- receiving
    def handle(self, upd: dict) -> None:
        if "callback_query" in upd:
            return self._on_callback(upd["callback_query"])
        msg = upd.get("message") or {}
        if not msg or "chat" not in msg or "from" not in msg:
            return
        chat_id, user = msg["chat"]["id"], msg["from"]["id"]
        kind = msg["chat"].get("type", "private")
        if user in ANONYMOUS_IDS or msg.get("sender_chat"):  # anonymous group admins / channel posts: nobody in particular
            self._ignore(user, chat_id)
            return
        text = (msg.get("text") or "").strip()
        row = st.chat(chat_id)
        if not row or (kind != "private" and row.user_id != user):
            if not self._try_pair(msg, text):
                self._ignore(user, chat_id)
            return
        per_min = load_config().telegram.commands_per_minute
        allowed, warn = self.inbound.allow(chat_id, per_min)
        if not allowed:
            if warn:
                Ctx(self, chat_id, user, row.role).send("🐢 Too many messages — I'll answer again in a minute.")
                audit(f"chat {chat_id}: rate limited")
            return
        st.touch(chat_id)
        ctx = Ctx(self, chat_id, user, row.role, message_id=msg.get("message_id"))
        what = "?"
        try:
            what = self._dispatch(ctx, msg, text)
            audit(f"chat {chat_id}: {what} ok")
        except Exception as exc:  # a bad command must never kill the bot
            audit(f"chat {chat_id}: {what} failed")
            try:
                ctx.send(error_reply(exc))
            except TgError as send_exc:
                log.warning(f"[telegram] couldn't send the error reply: {send_exc}")

    def _ignore(self, user: int, chat_id: int) -> None:
        """Unknown chats get no reply at all — just an audit entry, at most once an hour per user."""
        if time.time() - self.ignored_logged.get(user, 0) > 3600:
            self.ignored_logged[user] = time.time()
            audit(f"Ignored a message from unknown Telegram user {user} (chat {chat_id})")

    def _dispatch(self, ctx: Ctx, msg: dict, text: str) -> str:
        if text.startswith("/"):
            head, _, rest = text.partition(" ")
            name = head[1:].split("@")[0].lower()
            return self._run_command(ctx, name, rest.strip())
        if text in MENU_TEXT:
            return self._run_command(ctx, MENU_TEXT[text], "")
        conv = st.get_conversation(ctx.chat_id)
        if conv and text:
            step_name, payload = conv
            st.clear_conversation(ctx.chat_id)
            if step_name not in ("type_yes", "pin") and not ctx.owner:
                raise Denied("👀 Your access is read-only (viewer).")
            STEPS[step_name](ctx, text, payload)
            return f"reply to {step_name}"  # never log what was typed (could be a PIN)
        if tg_jobs.handle_file(ctx, msg):
            return "file → new job"
        m = URL_RE.search(text)
        if m:
            tg_jobs.start_wizard(ctx, url=m.group(0))
            return "link → new job"
        ctx.send("Send me a YouTube link or a video to start a job — or /help, /menu.")
        return "text"

    def _run_command(self, ctx: Ctx, name: str, args: str) -> str:
        name = ALIASES.get(name, name)
        cmd = COMMANDS.get(name)
        if not cmd:
            ctx.send("I don't know that command — /help lists them.")
            return f"/{name} (unknown)"
        if ROLE_RANK.get(ctx.role, 0) < ROLE_RANK[cmd.role]:
            ctx.send("👀 Your access is read-only (viewer).")
            return f"/{name} (denied: viewer)"
        if name != "cancel":
            st.clear_conversation(ctx.chat_id)  # a new command ends any half-finished question
        cmd.fn(ctx, args)
        return f"/{name}" + (" …" if args else "")

    def _on_callback(self, cq: dict) -> None:
        user = (cq.get("from") or {}).get("id")
        message = cq.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        kind = (message.get("chat") or {}).get("type", "private")
        row = st.chat(chat_id) if chat_id is not None else None
        if not row or (kind != "private" and row.user_id != user):
            self._ignore(user or 0, chat_id or 0)
            return
        api = self.api()
        action, payload, err = st.take_callback(cq.get("data") or "", chat_id, user)
        ctx = Ctx(self, chat_id, user, row.role, message_id=message.get("message_id"), callback_id=cq.get("id"))
        if err:
            ctx.toast(err)
        else:
            fn, role = CALLBACKS.get(action or "", (None, "owner"))
            if fn is None:
                ctx.toast("This button is from an older version.")
            elif ROLE_RANK.get(ctx.role, 0) < ROLE_RANK[role]:
                ctx.toast("Read-only access (viewer).")
                audit(f"chat {chat_id}: button {action} denied (viewer)")
            else:
                try:
                    fn(ctx, payload)
                    audit(f"chat {chat_id}: button {action} ok")
                except Exception as exc:
                    audit(f"chat {chat_id}: button {action} failed")
                    ctx.toast("⚠️ That didn't work")
                    try:
                        ctx.send(error_reply(exc))
                    except TgError:
                        pass
        try:
            api.call("answerCallbackQuery", callback_query_id=cq.get("id"), text=ctx.toast_text[:190] or None)
        except TgError:
            pass

    # ---- loop
    def start(self) -> None:
        cfg = load_config()
        if cfg.deployment.mode == "worker":  # the server owns the bot; a worker must never poll (HTTP 409 / duplicates)
            log.info("[telegram] worker mode: the server runs the Telegram bot")
            return
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()
        self.thread = threading.Thread(target=self._loop, name="clipforge-telegram", daemon=True)
        self.thread.start()
        self.ticker = threading.Thread(target=self._tick_loop, name="clipforge-telegram-tick", daemon=True)
        self.ticker.start()

    def shutdown(self, wait: float = 2.0) -> None:
        self.stop.set()
        for t in (self.thread, self.ticker):
            if t and t.is_alive() and t is not threading.current_thread():
                t.join(wait)
        try:
            st.release_poller()
        except Exception:
            pass

    def poll_once(self, timeout: int = 50) -> int:
        """One getUpdates round (holding the poller lease). Returns the number of updates handled, -1 = not our turn."""
        if not st.acquire_poller(max(LEASE_SECONDS, timeout + 60)):
            return -1
        offset = int(st.kv_get("offset", 0) or 0)
        try:
            updates = self.api().call("getUpdates", offset=offset, timeout=timeout, allowed_updates=["message", "callback_query"])
        except TgError as exc:
            if exc.code == 409:  # another process polls this token (or a webhook is set)
                if time.time() - self.conflict_logged > 3600:
                    self.conflict_logged = time.time()
                    log.warning("[telegram] another program is reading this bot's messages (HTTP 409). Only one ClipForge "
                                "may run the bot — stop the other one (on a worker laptop set deployment.mode: worker).")
                    audit("Telegram poll conflict (HTTP 409): another poller is running")
                st.release_poller()
                return -1
            raise
        handled = 0
        for u in updates:
            # a slow update (a 50 MB upload, 429 waits) must not let our lease lapse unnoticed: renew it per update,
            # and stop if another poller took over meanwhile — it will get these updates itself
            if not st.acquire_poller(LEASE_SECONDS):
                break
            if not st.advance_offset(u["update_id"] + 1):  # durable + only forwards: never handled twice
                continue
            try:
                self.handle(u)
            except Exception as exc:
                log.warning(f"[telegram] update {u.get('update_id')} failed: {exc}")
            handled += 1
        return handled

    def _loop(self) -> None:
        backoff = 1.0
        while not self.stop.is_set():
            if not self.enabled() or load_config().deployment.mode == "worker":
                # disabled, or this laptop just became a worker (handover while the app was running): hand the bot over
                if self.polling:
                    st.release_poller()
                self.polling = False
                self.stop.wait(30)
                continue
            try:
                self.register_commands()
                n = self.poll_once()
                self.polling = n >= 0
                if n < 0:
                    self.stop.wait(60)
                    continue
                backoff = 1.0
            except Exception as exc:  # network drops, bad token, Telegram hiccups → back off, never die
                log.debug(f"[telegram] poll failed: {exc}")
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 60)
        st.release_poller()

    def _tick_loop(self) -> None:
        while not self.stop.is_set():
            try:
                if self.enabled():
                    self.tick()
            except Exception as exc:
                log.debug(f"[telegram] tick failed: {exc}")
            self.stop.wait(2)

    def tick(self, now: dt.datetime | None = None) -> None:
        tg_jobs.tick_progress(self)
        self.daily_summary_if_due(now)
        if time.time() - self.last_purge > 3600:
            self.last_purge = time.time()
            st.purge_expired()
            self.disk_check()

    def disk_check(self) -> None:
        from clipforge.services.publishing import disk_usage

        cfg = load_config().server
        d = disk_usage()
        if d["pct"] >= cfg.disk_warn_pct:
            paused = d["pct"] >= cfg.disk_pause_pct
            self.notify("job_failed", f"💾 Disk {d['pct']}% full" + (" — new jobs are paused until you free space." if paused
                                                                       else " — clean up soon (old sources are deleted automatically)."),
                        key=f"disk:{dt.date.today()}:{paused}")

    def daily_summary_if_due(self, now: dt.datetime | None = None) -> bool:
        from clipforge.publish.timez import now_utc, utc_to_local

        cfg = load_config()
        if not cfg.telegram.daily_summary or not st.chats():
            return False
        local = utc_to_local(now or now_utc(), cfg.publishing.timezone)
        hh, mm = (int(x) for x in cfg.telegram.daily_summary_time.split(":"))
        today = local.date().isoformat()
        if st.kv_get("summary_day") == today or (local.hour, local.minute) < (hh, mm):
            return False
        st.kv_set("summary_day", today)
        return self.send_text(commands.daily_summary(local.date()))


def _audit(kind: str, detail: str) -> None:  # Phase 14 name, kept for imports
    audit(detail)


_bot: Bot | None = None


def bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot()
    return _bot


def set_bot(b: Bot | None) -> None:
    """Tests install a bot with a fake transport (the previous one is stopped first)."""
    global _bot
    if _bot is not None and _bot is not b:
        _bot.shutdown()
    _bot = b


# ----------------------------------------------------------------------------- notification hooks
def on_job_status(job_id: int, status: str, error: str = "") -> None:
    """Called by the job manager when a pipeline job finishes or fails."""
    try:
        b = bot()
        if not b.enabled():
            return
        t = load_config().telegram
        tracked = st.kv_get(f"prog:{job_id}")  # the chat that started it sees the result in its progress message
        if status == "failed":
            if not t.notify_job_failed:
                return
            from clipforge.api import db

            job = db.get_job(job_id)
            text, _ = tg_jobs.explain_failure(error)
            for c in st.chats():
                if tracked and tracked["chat"] == c.chat_id:
                    continue
                ctx = Ctx(b, c.chat_id, c.user_id, c.role)
                ctx.send(f"❌ <b>Job {job_id} failed</b> · {esc((job.title if job else '')[:60])}\n{text}",
                         [[ctx.btn("🔁 Retry", "job.retry", j=job_id)]] if ctx.owner else None)
            return
        if status != "done" or not t.notify_job_done:
            return
        for c in st.chats():
            try:
                tg_clips.deliver_job(b, c, job_id)
            except TgError as exc:
                log.warning(f"[telegram] clip delivery to {c.chat_id} failed: {exc}")
    except Exception as exc:
        log.warning(f"[telegram] job notification failed: {exc}")


def on_transcript_warning(job_id: int, warnings: list[str]) -> None:
    """The pasted / YouTube transcript may not belong to the video: the job keeps going; one tap switches to Whisper."""
    try:
        b = bot()
        if not b.enabled():
            return
        lines = "\n".join(f"• {esc(w)}" for w in warnings[:4])
        for c in st.chats():
            ctx = Ctx(b, c.chat_id, c.user_id, c.role)
            ctx.send(f"⚠️ <b>Job {job_id}</b>: the transcript may not fit this video\n{lines}\n"
                     "The job keeps going with it.",
                     [[ctx.btn("🎙 Use Whisper instead", "job.whisper", j=job_id)]] if ctx.owner else None)
    except Exception as exc:
        log.warning(f"[telegram] transcript warning failed: {exc}")


def on_render(job_id: int, ranks: list[int] | None, ok: bool, error: str = "") -> None:
    """Called after a re-render task: send new versions to the chats that asked for them."""
    try:
        b = bot()
        if b.enabled():
            tg_clips.on_render_done(b, job_id, ranks, ok, error)
    except Exception as exc:
        log.warning(f"[telegram] render notification failed: {exc}")


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
                link = it.permalink or (f"https://youtube.com/shorts/{it.youtube_video_id}" if it.youtube_video_id else "")
                b.notify("published", f"🚀 Published on {it.platform.title()}: {it.title}\n{link}", key=f"pub:{iid}")
        for iid in [*rep.failed, *rep.missed]:
            it = q.get(iid)
            if it:
                b.notify("publish_failed", f"⚠️ {it.platform.title()} — {it.title}: {scrub(it.error)[:300]}\nSend /needsyou for one-tap fixes.",
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


from clipforge.notify import commands  # noqa: E402,F401  (commands imports nothing from here at module level)
