"""Telegram: help, menu, devices, the app link / Mini App, server status, and the confirmation chain
(Yes → optional "type YES" → optional PIN → run)."""

from __future__ import annotations

from clipforge.notify import tg_store as st
from clipforge.notify.tg_api import esc
from clipforge.notify.tg_core import (COMMANDS, DANGERS, MENU_TEXT, Ctx, audit, callback, check_pin, command, danger,
                                      pin_enabled, pin_locked, step)

MENU = [["📋 Jobs", "🎬 Clips", "📅 Queue"], ["⚠️ Needs you", "❤️ Health", "📊 Stats"], ["🖥 Server", "❓ Help"]]
MENU_TEXT.update({"📋 Jobs": "jobs", "🎬 Clips": "clips", "📅 Queue": "queue", "⚠️ Needs you": "needsyou",
                  "❤️ Health": "health", "📊 Stats": "stats", "🖥 Server": "server", "❓ Help": "help"})
GROUP_ORDER = ["Jobs", "Clips", "Publishing", "Status", "Bot"]
INTRO = ("🤖 <b>ClipForge</b> — your podcast → Shorts studio, from your phone.\n"
         "• Paste a YouTube link (or send a video) to start a job.\n"
         "• When clips are ready they arrive here as cards: preview, approve, edit, schedule.\n"
         "• Nothing goes public by itself: uploads are private until their slot.\n")
EXAMPLES = """<b>Examples</b>
<code>https://youtu.be/abc123</code> — start a job (options wizard)
<code>/clips 12 pending</code> — cards of clips you haven't reviewed
<code>/approve 12 1-5,8</code> · <code>/approve 12 &gt;7</code>
<code>/trim 3 0:12-0:47</code> — keep seconds 12–47 of clip 3 (snaps to words)
<code>/word 3 "galti" "galat"</code> — fix a caption word, then <code>/render 3</code>
<code>/send approved full</code> — full-quality files
<code>/schedule 12 both</code> — approved clips → next free slots on YouTube + Instagram
<code>/needsyou</code> — every stuck post with one-tap fixes"""


def help_text(role: str) -> str:
    groups: dict[str, list[str]] = {}
    for c in COMMANDS.values():
        if role == "viewer" and c.role == "owner":
            continue
        line = f"/{c.name} — {esc(c.help)}"
        if c.usage:
            line += f"\n   <code>{esc(c.usage)}</code>"
        groups.setdefault(c.group or "Bot", []).append(line)
    out = [INTRO]
    for g in GROUP_ORDER:
        if g in groups:
            out.append(f"<b>{g}</b>\n" + "\n".join(groups[g]))
    out.append(EXAMPLES)
    if role == "viewer":
        out.append("👀 You have read-only access (viewer).")
    return "\n\n".join(out)


@command("help", role="viewer", group="Bot", help="everything the bot can do, with examples", aliases=("start",))
def _help(ctx: Ctx, args: str) -> None:
    ctx.send(help_text(ctx.role))


@command("menu", role="viewer", group="Bot", help="show the quick-action keyboard")
def _menu(ctx: Ctx, args: str) -> None:
    ctx.send("Quick actions below ⬇", reply_markup={"keyboard": [[{"text": t} for t in row] for row in MENU],
                                                    "resize_keyboard": True, "is_persistent": True})


@command("hidemenu", role="viewer", group="Bot", help="hide the quick-action keyboard", menu=False)
def _hidemenu(ctx: Ctx, args: str) -> None:
    ctx.send("Keyboard hidden — /menu brings it back.", reply_markup={"remove_keyboard": True})


@command("status", role="viewer", group="Status", help="what's running + next posts")
def _status(ctx: Ctx, args: str) -> None:
    from clipforge.notify import commands

    ctx.send(esc(commands.status()))


@command("dashboard", role="viewer", group="Status", help="followers, views, YPP progress")
def _dashboard(ctx: Ctx, args: str) -> None:
    from clipforge.notify import commands

    ctx.send(esc(commands.dashboard()))


# ============================================================================ devices
@command("devices", group="Bot", help="paired chats (owner only) — unpair from here or the laptop")
def _devices(ctx: Ctx, args: str) -> None:
    for c in st.chats():
        me = " (this chat)" if c.chat_id == ctx.chat_id else ""
        seen = f" · last used {c.last_seen:%d %b %H:%M} UTC" if c.last_seen else ""
        ctx.send(f"📱 <b>{esc(c.name or c.kind)}</b>{me}\n{c.role} · {c.kind} · paired {c.paired_at:%d %b %Y}{seen}",
                 [[ctx.btn("🔌 Unpair", "dev.unpair", d=c.chat_id)]])
    ctx.send("To add a phone or a read-only viewer: on the laptop run <code>clipforge telegram pair --role viewer</code> "
             "(or Settings → Phone &amp; Telegram).")


@callback("dev.unpair")
def _unpair_ask(ctx: Ctx, p: dict) -> None:
    ctx.ask_danger("dev.unpair", d=int(p["d"]))


@danger("dev.unpair", lambda p: "Unpair this chat? It stops getting messages and can't control ClipForge.", pin=False)
def _unpair(ctx: Ctx, p: dict) -> str:
    st.remove_chat(int(p["d"]))
    audit(f"unpaired chat {p['d']} from Telegram")
    return "🔌 Unpaired." if int(p["d"]) != ctx.chat_id else ""


# ============================================================================ app link / Mini App
@command("app", role="viewer", group="Bot", help="open the ClipForge app (trimmer, crop editor)")
def _app(ctx: Ctx, args: str) -> None:
    from clipforge.config import load_config
    from clipforge.notify.commands import app_url

    url = app_url("/")
    if not url:
        ctx.send("The app link needs remote access (Tailscale) — set it up on the laptop: <code>clipforge remote setup</code>.")
        return
    if load_config().telegram.mini_app:
        ctx.send("📱 ClipForge (opens inside Telegram; your phone must be on Tailscale):",
                 [[{"text": "📱 Open app", "web_app": {"url": url}}]])
    else:
        ctx.send("📱 ClipForge app (opens in your browser; your phone must be on Tailscale):", [[ctx.url_btn("📱 Open app", url)]])


@command("server", role="viewer", group="Status", help="uptime, disk, RAM, CPU, workers, jobs, last backup")
def _server(ctx: Ctx, args: str) -> None:
    from clipforge.server import status as srv

    ctx.send(srv.telegram_text())


# ============================================================================ confirmation chain
@callback("danger.yes")
def _danger_yes(ctx: Ctx, p: dict) -> None:
    d = DANGERS.get(p.get("a", ""))
    if not d:
        ctx.edit("That action no longer exists.")
        return
    payload = p.get("p", {})
    if d.type_yes:
        ctx.edit("⚠️ Confirmed the first step.")
        ctx.ask("Type <b>YES</b> (capitals) to go ahead.", "type_yes", {"a": d.action, "p": payload}, placeholder="YES")
        return
    ctx.edit("⏳ Working on it…")
    _after_yes(ctx, d.action, payload)


@callback("danger.no", role="viewer")
def _danger_no(ctx: Ctx, p: dict) -> None:
    ctx.edit("✖ Cancelled — nothing was changed.")


def _after_yes(ctx: Ctx, action: str, payload: dict) -> None:
    d = DANGERS[action]
    if d.pin and pin_enabled() and ctx.chat_id < 0:  # a group: others would see the PIN (the bot may not delete it)
        ctx.send("🔢 This needs your PIN — do it in your private chat with the bot, not in a group.")
        return
    if d.pin and pin_enabled():
        left = pin_locked()
        if left:
            ctx.send(f"🔒 PIN locked for {int(left // 60) + 1} more minute(s) after wrong tries.")
            return
        ctx.ask("🔢 Send your PIN (I delete the message right away).", "pin", {"a": action, "p": payload}, placeholder="PIN")
        return
    _run(ctx, action, payload)


def _run(ctx: Ctx, action: str, payload: dict) -> None:
    ctx.require_owner()
    out = DANGERS[action].fn(ctx, payload)
    audit(f"chat {ctx.chat_id}: confirmed {action} {payload}")
    if out:
        ctx.send(out)


@step("type_yes")
def _type_yes(ctx: Ctx, text: str, p: dict) -> None:
    if text.strip() != "YES":
        ctx.send("✖ Cancelled — you didn't type YES.")
        return
    _after_yes(ctx, p["a"], p.get("p", {}))


@step("pin")
def _pin_step(ctx: Ctx, text: str, p: dict) -> None:
    try:  # the PIN never stays in the chat history
        ctx.api.call("deleteMessage", chat_id=ctx.chat_id, message_id=ctx.message_id)
    except Exception:
        ctx.send("⚠️ I couldn't delete your PIN message — delete it yourself (long-press → Delete).")
    if check_pin(text.strip()):
        _run(ctx, p["a"], p.get("p", {}))
        return
    left = pin_locked()
    if left:
        ctx.send("🔒 Wrong PIN 5 times — locked for 15 minutes.")
        return
    rec = st.kv_get("pin_fail") or {}
    ctx.ask(f"❌ Wrong PIN ({5 - rec.get('n', 0)} tries left). Send it again:", "pin", p, placeholder="PIN")
