"""Telegram for live clipping: paste a live stream link (Twitch / Kick / YouTube live) → "🔴 Clip this live stream";
each clip arrives as a draft with its reason and buttons (✅ Post now · 🕒 Queue · ✏️ Title · ❌ Discard); a recap
when the stream ends (🎞 best-of). Commands: /live (streams), /live <link>, /live stop, /live pause|resume,
/live auto on|off, /livestats, and /clip [note] while a stream is watched (see tg_clips._clip_cmd).
"""

from __future__ import annotations

import threading

from clipforge.config import load_config
from clipforge.live import store
from clipforge.notify import tg_media, tg_store
from clipforge.notify.tg_api import esc
from clipforge.notify.tg_core import Ctx, callback, command, step
from clipforge.utils.log import log


def streams() -> list[dict]:
    st = store.read_status(load_config())
    return [s for s in st.get("streams", []) if not s.get("ending")] if st.get("engine_running") else []


def live_running() -> bool:
    return bool(streams())


def is_live_link(url: str) -> bool:
    """Twitch / Kick channel links are always live-stream links; YouTube needs a check (the caller does it)."""
    u = url.lower()
    return "twitch.tv/" in u and "/videos" not in u and "/clip" not in u or "kick.com/" in u and "/videos" not in u


def mark(ctx: Ctx, note: str, sid: str = "") -> None:
    ctx.require_owner()
    ss = streams()
    if not sid and len(ss) > 1:
        ctx.send("Which stream?", [[ctx.btn(f"🎬 {s.get('streamer') or s['session']}", "live.mark", s=s["session"], n=note[:60])]
                                   for s in ss])
        return
    store.send_command(load_config(), "clip", note=note[:80], source="Telegram", sid=sid or (ss[0]["session"] if ss else ""))
    ctx.send("🎬 Marked! The clip arrives here in about a minute." + (f"\n<i>{esc(note[:80])}</i>" if note else ""))


@callback("live.mark")
def _mark_cb(ctx: Ctx, p: dict) -> None:
    mark(ctx, p.get("n", ""), p["s"])


def start(ctx: Ctx, url: str) -> None:
    ctx.require_owner()
    cfg = load_config()
    if not cfg.live.enabled:
        ctx.send("Live clipping is switched off (app → Live → Settings).")
        return
    store.send_command(cfg, "start", url=url.strip())
    ctx.send("👀 Opening the stream… you'll get a message when I'm watching it (≈ 20 s).\n"
             "<i>Clips of other creators can get copyright claims — every caption credits and links them.</i>")


def offer(ctx: Ctx, url: str) -> None:
    """A pasted link that is (or may be) a live stream."""
    ctx.send(f"🔴 <b>Live stream link</b>\n{esc(url[:200])}",
             [[ctx.btn("🔴 Clip this live stream", "live.start", u=url[:500])],
              [ctx.btn("📼 Normal job (full video)", "live.job", u=url[:500])]])


def offer_if_youtube_live(ctx: Ctx, url: str) -> None:
    """YouTube link: the normal job wizard runs; if the link turns out to be live, also offer live clipping."""
    import os

    if not load_config().live.enabled or "PYTEST_CURRENT_TEST" in os.environ:  # tests never reach YouTube
        return

    def run() -> None:
        from clipforge.live.sources import is_live

        try:
            if is_live(url):
                ctx.send("🔴 This YouTube link is <b>live right now</b>.",
                         [[ctx.btn("🔴 Clip it live instead", "live.start", u=url[:500])]])
        except Exception as exc:
            log.warning(f"[telegram] live check: {exc}")
    threading.Thread(target=run, daemon=True, name="tg-live-check").start()


@callback("live.start")
def _start_cb(ctx: Ctx, p: dict) -> None:
    start(ctx, p["u"])


@callback("live.job")
def _job_cb(ctx: Ctx, p: dict) -> None:
    from clipforge.notify import tg_jobs

    tg_jobs.start_wizard(ctx, url=p["u"])


# ----------------------------------------------------------------------------- clip drafts
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


def clip_caption(clip: dict, sess: dict | None = None) -> str:
    r = clip.get("render", {})
    who = f" · {esc(sess.get('streamer'))}" if sess and sess.get("streamer") else ""
    return (f"🎮 <b>Live clip #{clip['rank']}</b>{who}\n<b>{esc(clip['title'])}</b>\n"
            f"Why: {esc(clip.get('reason', ''))}\n"
            f"⭐ {clip.get('score', 0):.1f} · {clip.get('duration', 0):.0f} s · ready in {r.get('total_seconds', '?')} s "
            f"({r.get('encoder', '?')}) · {esc(clip.get('captions', ''))}")


def notify_clip(cfg, sid: str, clip: dict) -> None:
    path = cfg.workspace / sid / "clips" / clip["file"]
    sess = store.load_session(cfg, sid)
    for ctx in _owner_ctxs():
        try:
            tg_media.deliver(ctx.api, ctx.chat_id, path, "preview", clip_caption(clip, sess), _buttons(ctx, sid, clip["rank"]),
                             duration=clip.get("duration", 0))
        except Exception as exc:
            log.warning(f"[telegram] live clip to {ctx.chat_id}: {exc}")


def send_recap(cfg, sid: str, recap: dict) -> None:
    sess = store.load_session(cfg, sid)
    lines = [f"🏁 <b>{esc(sess.get('streamer') or 'Stream')} — stream over</b> · {recap['clips']} clip(s) from "
             f"{recap['moments']} moment(s) · {recap['posted']} posted"]
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
    from clipforge.live.bestof import make_best_of

    ctx.require_owner()
    ctx.send("🎞 Rendering the long highlight video (a few minutes)…")

    def run() -> None:
        try:
            out = make_best_of(load_config(), p["s"])
            ctx.send(f"🎞 Highlight video ready: <code>{esc(out.name)}</code> — open it in the app (Live → past streams).")
        except Exception as exc:
            ctx.send(f"⚠️ Highlight video failed: {esc(exc)}")
    threading.Thread(target=run, daemon=True, name="live-bestof").start()


# ----------------------------------------------------------------------------- commands
def status_text() -> str:
    cfg = load_config()
    st = store.read_status(cfg)
    if not cfg.live.enabled:
        return "🎮 Live clipping is off (app → Live → Settings)."
    if not st.get("engine_running"):
        return "🎮 The live agent is starting (it runs with the app)…"
    ss = [s for s in st.get("streams", [])]
    g = st.get("governor", {})
    lines = [f"🎮 <b>Live clipping</b> · {len(ss)} stream(s) · CPU {g.get('cpu', 0):.0f}%"
             + (f" — {esc(g.get('reason'))}" if g.get("reason") else "")]
    for s in ss:
        m = s.get("meters", {})
        lines.append(f"\n🔴 <b>{esc(s.get('streamer') or s['session'])}</b> ({esc(s.get('platform', ''))}) · {esc(s.get('game_label'))}"
                     + (" · ending…" if s.get("ending") else "") + (" · ⏸" if s.get("paused") else ""))
        lines.append(f"{s.get('clips_made', 0)} clips · {s.get('rendering', 0)} rendering · camera: {esc(s.get('camera'))}"
                     f" · chat {m.get('chat_rate', 0)}/10 s · score {m.get('score', 0)}/{m.get('threshold', 0)}")
    for u in st.get("starting", []):
        lines.append(f"\n⏳ opening {esc(u)}")
    if not ss and not st.get("starting"):
        lines.append("\nPaste a live link (Twitch, Kick or YouTube) here, or <code>/live https://twitch.tv/name</code>.")
    for e in st.get("errors", [])[-2:]:
        lines.append(f"<i>⚠️ {esc(e)}</i>")
    return "\n".join(lines)


def _status_rows(ctx: Ctx) -> list[list[dict]] | None:
    if not ctx.owner:
        return None
    rows = [[ctx.btn("🎬 Clip now", "live.clipnow"), ctx.btn("🔄 Refresh", "live.status")]]
    for s in streams():
        rows.append([ctx.btn(f"⏹ Stop {s.get('streamer') or s['session']}"[:40], "live.stop", s=s["session"])])
    return rows


@command("live", role="viewer", group="Live", help="live streams being clipped · start one: /live <link>",
         usage="/live  ·  /live https://twitch.tv/name  ·  /live stop  ·  /live pause|resume  ·  /live auto on|off")
def _live(ctx: Ctx, args: str) -> None:
    a = args.strip()
    cfg = load_config()
    if not a:
        ctx.send(status_text(), _status_rows(ctx))
        return
    ctx.require_owner()
    if a.lower().startswith("http"):
        start(ctx, a.split()[0])
        return
    w = a.lower().split()
    if w[0] == "stop":
        ss = streams()
        if len(ss) > 1 and len(w) == 1:
            ctx.send("Stop which stream?", [[ctx.btn(f"⏹ {s.get('streamer') or s['session']}", "live.stop", s=s["session"])]
                                            for s in ss])
            return
        store.send_command(cfg, "stop", sid=ss[0]["session"] if ss else "")
        ctx.send("⏹ Stopping — the clips in progress finish first.")
    elif w[0] in ("pause", "resume"):
        store.send_command(cfg, w[0])
        ctx.send("⏸ Automatic clipping paused (/clip still works)." if w[0] == "pause" else "▶️ Clipping resumed.")
    elif w[0] == "auto" and len(w) > 1 and w[1] in ("on", "off"):
        store.send_command(cfg, "auto", on=w[1] == "on")
        ctx.send(f"Auto-post {'ON — high-confidence clips post to your live-clip accounts by themselves' if w[1] == 'on' else 'OFF (drafts only)'}.")
    else:
        ctx.send("Usage: <code>/live</code> · <code>/live &lt;link&gt;</code> · <code>/live stop</code> · "
                 "<code>/live pause</code> · <code>/live auto on|off</code>")


@callback("live.status", role="viewer")
def _status_cb(ctx: Ctx, p: dict) -> None:
    ctx.edit(status_text(), _status_rows(ctx))


@callback("live.stop")
def _stop_cb(ctx: Ctx, p: dict) -> None:
    ctx.require_owner()
    store.send_command(load_config(), "stop", sid=p["s"])
    ctx.send("⏹ Stopping — the clips in progress finish first.")


@callback("live.clipnow")
def _clipnow(ctx: Ctx, p: dict) -> None:
    mark(ctx, "")


@command("livestats", role="viewer", group="Live", help="clips made / posted / views per watched stream")
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
