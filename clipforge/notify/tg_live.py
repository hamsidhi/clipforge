"""Telegram for live gaming mode: each live clip arrives as a preview with its reason and buttons
(✅ Post now · 🕒 Queue · ✏️ Title · ❌ Discard), a recap when the stream ends (🎞 best-of), and the commands
/live, /live pause|resume, /live auto on|off, /livestats, and /clip [note] while live (see tg_clips._clip_cmd).
Live clips only ever go to gaming accounts (live/publish.py).
"""

from __future__ import annotations

from clipforge.config import load_config
from clipforge.live import store
from clipforge.notify import tg_media, tg_store
from clipforge.notify.tg_api import esc
from clipforge.notify.tg_core import Ctx, callback, command, step
from clipforge.utils.log import log


def live_running() -> bool:
    st = store.read_status(load_config())
    return bool(st.get("engine_running") and (st.get("live") or {}).get("session"))


def mark(ctx: Ctx, note: str) -> None:
    ctx.require_owner()
    store.send_command(load_config(), "clip", note=note[:80], source="Telegram")
    ctx.send("🎬 Marked! The clip arrives here in 1–3 minutes." + (f"\n<i>{esc(note[:80])}</i>" if note else ""))


def _owner_ctxs() -> list[Ctx]:
    from clipforge.notify.telegram import bot

    b = bot()
    if not b.token:
        return []
    return [Ctx(b, c.chat_id, c.user_id, c.role) for c in tg_store.chats() if c.role == "owner"]


def _buttons(ctx: Ctx, sid: str, rank: int) -> list[list[dict]]:
    return [[ctx.btn("✅ Post now", "live.post", s=sid, r=rank, m="now"),
             ctx.btn("🕒 Queue", "live.post", s=sid, r=rank, m="queue")],
            [ctx.btn("✏️ Title", "live.title", s=sid, r=rank),
             ctx.btn("❌ Discard", "live.discard", s=sid, r=rank)]]


def clip_caption(clip: dict) -> str:
    r = clip.get("render", {})
    return (f"🎮 <b>Live clip #{clip['rank']}</b> · {esc(clip['title'])}\n"
            f"Why: {esc(clip.get('reason', ''))}\n"
            f"⭐ {clip.get('score', 0):.1f} · {clip.get('duration', 0):.0f} s · ready in {r.get('total_seconds', '?')} s "
            f"({r.get('encoder', '?')}) · {esc(clip.get('captions', ''))}")


def notify_clip(cfg, sid: str, clip: dict) -> None:
    path = cfg.workspace / sid / "clips" / clip["file"]
    for ctx in _owner_ctxs():
        try:
            tg_media.deliver(ctx.api, ctx.chat_id, path, "preview", clip_caption(clip), _buttons(ctx, sid, clip["rank"]),
                             duration=clip.get("duration", 0))
        except Exception as exc:
            log.warning(f"[telegram] live clip to {ctx.chat_id}: {exc}")


def send_recap(cfg, sid: str, recap: dict) -> None:
    lines = [f"🏁 <b>Stream over</b> · {recap['clips']} clip(s) from {recap['moments']} moment(s) · "
             f"{recap['posted']} posted"]
    for c in recap.get("top", []):
        lines.append(f"• #{c['rank']} ⭐ {c.get('score', 0):.1f} {esc(c['title'])} — {esc(c.get('reason', ''))[:80]}")
    if recap.get("captions_swapped"):
        lines.append(f"<i>{recap['captions_swapped']} queued post(s) now link the full stream instead of 'LIVE now'.</i>")
    for ctx in _owner_ctxs():
        rows = [[ctx.btn("🎞 Make long highlight video", "live.bestof", s=sid)]] if recap.get("clips") else None
        try:
            ctx.send("\n".join(lines), rows)
        except Exception as exc:
            log.warning(f"[telegram] live recap: {exc}")


# ----------------------------------------------------------------------------- buttons
@callback("live.post")
def _post(ctx: Ctx, p: dict) -> None:
    from clipforge.live.publish import post_clip

    ctx.require_owner()
    try:
        lines = post_clip(load_config(), p["s"], int(p["r"]), p.get("m", "now"))
    except Exception as exc:
        ctx.send(f"⚠️ {esc(getattr(exc, 'detail', None) or exc)}")
        return
    ctx.send(f"{'🚀' if p.get('m') == 'now' else '🕒'} Clip #{p['r']}:\n" + "\n".join(esc(x) for x in lines))


@callback("live.discard")
def _discard(ctx: Ctx, p: dict) -> None:
    ctx.require_owner()
    store.update_clip(load_config(), p["s"], int(p["r"]), status="discarded")
    ctx.toast("Discarded")
    ctx.edit(f"❌ Live clip #{p['r']} discarded.")


@callback("live.title")
def _title(ctx: Ctx, p: dict) -> None:
    ctx.require_owner()
    ctx.ask(f"New title for live clip #{p['r']}:", "live_title", {"s": p["s"], "r": int(p["r"])}, "Title")


@step("live_title")
def _title_step(ctx: Ctx, text: str, p: dict) -> None:
    new = " ".join(text.split())[:95]
    store.update_clip(load_config(), p["s"], int(p["r"]), title=new)
    ctx.send(f"✏️ Title of live clip #{p['r']} → <b>{esc(new)}</b>",
             [[ctx.btn("✅ Post now", "live.post", s=p["s"], r=p["r"], m="now"),
               ctx.btn("🕒 Queue", "live.post", s=p["s"], r=p["r"], m="queue")]])


@callback("live.bestof")
def _bestof(ctx: Ctx, p: dict) -> None:
    import threading

    from clipforge.live.bestof import make_best_of

    ctx.require_owner()
    ctx.send("🎞 Rendering the long highlight video (normal priority, a few minutes)…")
    chat = ctx.chat_id

    def run() -> None:
        try:
            out = make_best_of(load_config(), p["s"])
            ctx.chat_id = chat
            ctx.send(f"🎞 Highlight video ready: <code>{esc(out)}</code>\nOpen it from the app (Live → past streams).")
        except Exception as exc:
            ctx.send(f"⚠️ Highlight video failed: {esc(exc)}")
    threading.Thread(target=run, daemon=True, name="live-bestof").start()


# ----------------------------------------------------------------------------- commands
def status_text() -> str:
    cfg = load_config()
    st = store.read_status(cfg)
    if not cfg.live.enabled and not st.get("engine_running"):
        return "🎮 Live mode is off (Settings → Live, or <code>live.enabled: true</code>)."
    if not st.get("engine_running"):
        return "🎮 Live mode is on, but the live engine isn't running (it starts with the app)."
    obs = st.get("obs", {})
    lv = st.get("live") or {}
    head = ("🔴 <b>LIVE</b>" if lv.get("session") else "⚪ Not live") + f" · OBS {'✅' if obs.get('connected') else '❌'}"
    if not lv.get("session"):
        return head + f"\n{esc(st.get('note', 'Waiting for the stream to start.'))}"
    g = lv.get("governor", {})
    m = lv.get("meters", {})
    lines = [head, f"Game: {esc(lv.get('game_label'))} · facecam {'✅' if lv.get('facecam') else '—'} · "
                   f"replay buffer {'✅' if obs.get('replay') else '❌'}",
             f"CPU {g.get('cpu', 0):.0f}% · {esc(g.get('state'))}" + (f" — {esc(g.get('reason'))}" if g.get("reason") else ""),
             f"Mic {m.get('mic', 0):.0f} dB · chat {m.get('chat_rate', 0)}/10 s · score {m.get('score', 0)} / {m.get('threshold', 0)}",
             f"Clips: {lv.get('clips_made', 0)} made · {lv.get('rendering', 0)} rendering · {lv.get('queued', 0)} waiting",
             f"Auto-post: {'on' if lv.get('auto_post') else 'off'}{' · ⏸ paused' if lv.get('paused') else ''}"]
    return "\n".join(lines)


@command("live", role="viewer", group="Live", help="live gaming status (pause | resume | auto on|off)",
         usage="/live  ·  /live pause  ·  /live resume  ·  /live auto on")
def _live(ctx: Ctx, args: str) -> None:
    a = args.strip().lower().split()
    cfg = load_config()
    if not a:
        rows = [[ctx.btn("🎬 Clip now", "live.clipnow"), ctx.btn("🔄 Refresh", "live.status")]] if ctx.owner else None
        ctx.send(status_text(), rows)
        return
    ctx.require_owner()
    if a[0] in ("pause", "resume"):
        store.send_command(cfg, a[0])
        ctx.send("⏸ Live clipping paused (manual /clip still works)." if a[0] == "pause" else "▶️ Live clipping resumed.")
    elif a[0] == "auto" and len(a) > 1 and a[1] in ("on", "off"):
        store.send_command(cfg, "auto", on=a[1] == "on")
        ctx.send(f"Auto-post {'ON — high-confidence clips post to your gaming accounts by themselves' if a[1] == 'on' else 'OFF'}.")
    else:
        ctx.send("Usage: <code>/live</code> · <code>/live pause</code> · <code>/live resume</code> · <code>/live auto on|off</code>")


@callback("live.status", role="viewer")
def _status_cb(ctx: Ctx, p: dict) -> None:
    ctx.edit(status_text(), [[ctx.btn("🎬 Clip now", "live.clipnow"), ctx.btn("🔄 Refresh", "live.status")]] if ctx.owner else None)


@callback("live.clipnow")
def _clipnow(ctx: Ctx, p: dict) -> None:
    mark(ctx, "")


@command("livestats", role="viewer", group="Live", help="clips made / posted / views from your streams")
def _livestats(ctx: Ctx, args: str) -> None:
    from clipforge.live.stats import session_stats

    rows = session_stats(load_config(), 5)
    if not rows:
        ctx.send("🎮 No live streams clipped yet.")
        return
    lines = ["🎮 <b>Live clips</b>"]
    for r in rows:
        lines.append(f"\n<b>{esc(r['title'])}</b>\n{r['clips']} clips · {r['posted']} posted · "
                     f"{r['views']:,} views" + (" · 🔴 live now" if r["live"] else ""))
    ctx.send("\n".join(lines))
