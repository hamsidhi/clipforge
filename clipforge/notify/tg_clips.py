"""Telegram: clip cards, previews / full-quality files, edits (title, hook, style, layout, script, trim, words),
approve/reject (bulk too), re-render + undo, copy-ready metadata. All edits go through clipforge.services.clips."""

from __future__ import annotations

import datetime as dt
import re
import shlex

from clipforge.api import db
from clipforge.notify import tg_media
from clipforge.notify import tg_store as st
from clipforge.notify.tg_api import esc
from clipforge.notify.tg_core import Ctx, callback, command, fmt_dur, parse_clock, parse_ranks, step
from clipforge.notify.tg_jobs import job_arg, set_current_job
from clipforge.services import clips as clip_svc
from clipforge.services import jobs as job_svc

PAGE = 5
MAX_SEND = 25


# ============================================================================ helpers
def clip_ref(ctx: Ctx, token: str) -> str:
    """'12-3' → '12-3'; '3' → rank 3 of the chat's current job."""
    token = token.strip().lstrip("#")
    if re.fullmatch(r"\d+-\d+", token):
        job_svc.parse_clip_id(token)
        set_current_job(ctx.chat_id, int(token.split("-")[0]))
        return token
    if token.isdigit():
        return f"{job_arg(ctx, None)}-{int(token)}"
    raise ValueError(f"'{token}' isn't a clip. Use the clip number (3) or job-clip (12-3).")


def clip_file(clip_id: str):
    job, rank = job_svc.parse_clip_id(clip_id)
    ws = job_svc.ws_for(job)
    f = ws.find_clip(rank)
    if not f:
        raise ValueError(f"Clip {clip_id} isn't rendered yet.")
    return job, ws, f


def status_line(c: dict) -> str:
    s = "✅ Approved" if c["approved"] is True else "❌ Rejected" if c["approved"] is False else "⏳ Not reviewed"
    if c.get("stale"):
        s += " · ✏️ edited — re-render to apply"
    if not c.get("rendered"):
        s += " · not rendered"
    return s


def card_text(c: dict) -> str:
    hook = f"\n🪝 {esc(c['hook'])}" if c.get("hook") else ""
    return (f"<b>#{c['rank']}</b> · ⭐ {c['score']:.1f} · {fmt_dur(c['duration'])}\n<b>{esc(c['title'])}</b>{hook}\n"
            f"{status_line(c)}")


def card_buttons(ctx: Ctx, c: dict) -> list[list[dict]]:
    cid = c["id"]
    row1 = [ctx.btn("▶ Preview", "clip.preview", c=cid)]
    if ctx.owner:
        row1 += [ctx.btn("✅ Approve", "clip.approve", c=cid, v=True), ctx.btn("❌ Reject", "clip.approve", c=cid, v=False)]
        row2 = [ctx.btn("✏️ Edit", "clip.edit", c=cid), ctx.btn("📅 Schedule", "sched.start", c=cid),
                ctx.btn("⬇ Full quality", "clip.full", c=cid)]
    else:
        row2 = [ctx.btn("⬇ Full quality", "clip.full", c=cid), ctx.btn("📋 Text", "clip.meta", c=cid)]
    return [row1, row2]


def send_card(ctx: Ctx, c: dict) -> None:
    mid = ctx.send(card_text(c), card_buttons(ctx, c))
    send_meta(ctx, c["id"], mid)


def _clip(clip_id: str) -> dict:
    job, rank = job_svc.parse_clip_id(clip_id)
    ws = job_svc.ws_for(job)
    _, c = job_svc.find_cand(ws, rank)
    return job_svc.clip_out(job, ws, c, job_svc.metas(ws))


# ============================================================================ job finished → summary, album, cards
def deliver_job(bot, chat: db.TgChat, job_id: int) -> None:
    from clipforge.config import load_config

    ctx = Ctx(bot, chat.chat_id, chat.user_id, chat.role)
    clips = [c for c in clip_svc.list_clips(job_id) if c["enabled"]]
    if not clips:
        return
    set_current_job(chat.chat_id, job_id)
    job = db.get_job(job_id)
    best = max(c["score"] for c in clips)
    total = sum(c["duration"] for c in clips)
    n = load_config().telegram.clips_to_send
    top = sorted(clips, key=lambda c: c["score"], reverse=True)[:n]
    rows = [[ctx.btn(f"🎬 All {len(clips)} clips", "clips.page", j=job_id, f="all", p=0)]]
    if ctx.owner:
        rows.append([ctx.btn("✅ Approve all ≥ 7", "job.approve_above", j=job_id, s=7),
                     ctx.btn("📅 Schedule approved", "sched.job", j=job_id)])
    ctx.send(f"🎉 <b>{len(clips)} clips ready</b> · {esc(job.title if job else '')}\n"
             f"Best score ⭐ {best:.1f} · total {fmt_dur(total)}" + (f"\nTop {len(top)} below:" if top else ""), rows)
    if not top:
        return
    ws = job_svc.ws_for(job)  # type: ignore[arg-type]
    thumbs = [(ws.clips_dir / "_work" / f"{c['rank']:02d}" / "thumb.jpg", f"#{c['rank']} {c['title']}") for c in top]
    try:
        tg_media.album(ctx.api, ctx.chat_id, thumbs)
    except Exception as exc:  # thumbnails are a nice-to-have; the cards matter
        from clipforge.utils.log import log

        log.warning(f"[telegram] album failed: {exc}")
    for c in top:
        send_card(ctx, c)


# ============================================================================ list / show / send
def clips_page(ctx: Ctx, job_id: int, which: str, page: int) -> None:
    clips = clip_svc.list_clips(job_id, which)
    set_current_job(ctx.chat_id, job_id)
    pages = max(1, (len(clips) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    if not clips:
        ctx.send(f"No {'' if which == 'all' else which + ' '}clips in job {job_id}.")
        return
    for c in clips[page * PAGE:(page + 1) * PAGE]:
        send_card(ctx, c)
    nav = []
    if page > 0:
        nav.append(ctx.btn("◀ Previous", "clips.page", j=job_id, f=which, p=page - 1))
    if page < pages - 1:
        nav.append(ctx.btn("Next ▶", "clips.page", j=job_id, f=which, p=page + 1))
    filters = [ctx.btn(("✓ " if which == f else "") + f, "clips.page", j=job_id, f=f, p=0)
               for f in ("all", "approved", "pending", "rejected")]
    ctx.send(f"🎬 Job {job_id} · {which} · page {page + 1}/{pages} ({len(clips)} clips)", [nav, filters] if nav else [filters])


@command("clips", role="viewer", group="Clips", help="clip cards of a job; filter approved|rejected|pending",
         usage="/clips 12 approved")
def _clips(ctx: Ctx, args: str) -> None:
    parts = args.split()
    which = next((p for p in parts if p in ("all", "approved", "rejected", "pending")), "all")
    jtok = next((p for p in parts if p.lstrip("#").isdigit()), None)
    clips_page(ctx, job_arg(ctx, jtok), which, 0)


@callback("clips.page", role="viewer")
def _clips_page(ctx: Ctx, p: dict) -> None:
    clips_page(ctx, int(p["j"]), p.get("f", "all"), int(p.get("p", 0)))


@command("clip", role="viewer", group="Clips", help="one clip card", usage="/clip 12-3")
def _clip_cmd(ctx: Ctx, args: str) -> None:
    if not args.strip():
        ctx.send("Usage: <code>/clip 12-3</code> (job 12, clip 3)")
        return
    send_card(ctx, _clip(clip_ref(ctx, args.split()[0])))


def send_clip(ctx: Ctx, clip_id: str, mode: str, caption: str | None = None, buttons=None) -> str:
    from clipforge.notify.commands import app_url

    c = _clip(clip_id)
    _, _, f = clip_file(clip_id)
    cap = caption or f"#{c['rank']} · {esc(c['title'])} · ⭐ {c['score']:.1f}"
    sent: list[int] = []
    how = tg_media.deliver(ctx.api, ctx.chat_id, f, mode, cap, buttons, clip_id=clip_id, duration=c["duration"],
                           link_fn=app_url, sent=sent)
    send_meta(ctx, clip_id, sent[-1] if sent else None)
    return how


@callback("clip.preview", role="viewer")
def _preview(ctx: Ctx, p: dict) -> None:
    send_clip(ctx, p["c"], "preview")


@callback("clip.full", role="viewer")
def _full(ctx: Ctx, p: dict) -> None:
    ctx.toast("Sending the full-quality file…")
    send_clip(ctx, p["c"], "full")


@command("send", role="viewer", group="Clips", help="send videos: all | approved | ranks, preview or full",
         usage="/send approved full   ·   /send 12 1-3 preview")
def _send(ctx: Ctx, args: str) -> None:
    parts = args.split()
    mode = "full" if "full" in parts else "preview"
    parts = [p for p in parts if p not in ("full", "preview")]
    jtok = parts.pop(0) if len(parts) >= 2 and parts[0].lstrip("#").isdigit() else None
    which = parts[0] if parts else "approved"
    job_id = job_arg(ctx, jtok)
    clips = clip_svc.list_clips(job_id)
    if which == "all":
        chosen = [c for c in clips if c["enabled"]]
    elif which == "approved":
        chosen = [c for c in clips if c["approved"]]
    else:
        ranks = parse_ranks(which)
        chosen = [c for c in clips if c["rank"] in ranks]
    chosen = [c for c in chosen if c["rendered"]]
    if not chosen:
        ctx.send("No rendered clips match. Try <code>/send all</code>.")
        return
    if len(chosen) > MAX_SEND:
        ctx.send(f"That's {len(chosen)} clips — sending the first {MAX_SEND}.")
    for c in chosen[:MAX_SEND]:
        send_clip(ctx, c["id"], mode)


# ============================================================================ approve / reject
@callback("clip.approve")
def _approve_cb(ctx: Ctx, p: dict) -> None:
    job, rank = job_svc.parse_clip_id(p["c"])
    clip_svc.set_approved(job.id, [rank], bool(p["v"]))  # type: ignore[arg-type]
    c = _clip(p["c"])
    ctx.edit(card_text(c), card_buttons(ctx, c))
    ctx.toast("Approved" if p["v"] else "Rejected")


def _bulk(ctx: Ctx, args: str, approve: bool) -> None:
    parts = args.split(maxsplit=1)
    if len(parts) == 1:
        jid, sel = job_arg(ctx, None), parts[0]
    elif len(parts) == 2:
        jid, sel = job_arg(ctx, parts[0]), parts[1]
    else:
        ctx.send("Usage: <code>/approve 12 1-5,8</code> or <code>/approve 12 &gt;7</code> (score above 7)")
        return
    m = re.fullmatch(r"\s*[>≥]=?\s*([\d.]+)\s*", sel)
    if m:
        n = clip_svc.approve_above(jid, float(m.group(1)), approve)
        what = f"score ≥ {m.group(1)}"
    else:
        n = clip_svc.set_approved(jid, parse_ranks(sel), approve)
        what = sel
    ctx.send(f"{'✅ Approved' if approve else '❌ Rejected'} {n} clip(s) of job {jid} ({esc(what)}).",
             [[ctx.btn("📅 Schedule approved", "sched.job", j=jid)]] if approve and n else None)


@command("approve", group="Clips", help="approve clips: ranks or a score", usage="/approve 12 1-5,8  ·  /approve 12 >7")
def _approve(ctx: Ctx, args: str) -> None:
    _bulk(ctx, args, True)


@command("reject", group="Clips", help="reject clips", usage="/reject 12 9,10")
def _reject(ctx: Ctx, args: str) -> None:
    _bulk(ctx, args, False)


@callback("job.approve_above")
def _approve_above(ctx: Ctx, p: dict) -> None:
    n = clip_svc.approve_above(int(p["j"]), float(p["s"]))
    ctx.send(f"✅ Approved {n} clip(s) with score ≥ {p['s']}.", [[ctx.btn("📅 Schedule approved", "sched.job", j=p["j"])]])


# ============================================================================ edit panel
def edit_text(clip_id: str) -> str:
    c = _clip(clip_id)
    ew = clip_svc.edge_words(clip_id)
    return (f"✏️ <b>Editing #{c['rank']}</b> · {fmt_dur(c['duration'])} ({c['start']:.1f}–{c['end']:.1f} s)\n"
            f"<b>{esc(c['title'])}</b>\n🪝 {esc(c['hook'] or '—')}\n"
            f"Starts: “{esc(ew['first_words'])} …”\nEnds: “… {esc(ew['last_words'])}”\n{status_line(c)}")


def edit_buttons(ctx: Ctx, clip_id: str) -> list[list[dict]]:
    b = lambda t, a, **k: ctx.btn(t, a, c=clip_id, **k)  # noqa: E731
    return [
        [b("📝 Title", "clip.ask_text", f="title"), b("🪝 Hook", "clip.ask_text", f="hook")],
        [b("🎨 Style", "clip.styles"), b("🧱 Layout", "clip.layouts"), b("🔤 Script", "clip.scripts")],
        [b("Start −1s", "clip.nudge", e="start", d=-1.0), b("−0.3", "clip.nudge", e="start", d=-0.3),
         b("+0.3", "clip.nudge", e="start", d=0.3), b("+1s", "clip.nudge", e="start", d=1.0)],
        [b("End −1s", "clip.nudge", e="end", d=-1.0), b("−0.3", "clip.nudge", e="end", d=-0.3),
         b("+0.3", "clip.nudge", e="end", d=0.3), b("+1s", "clip.nudge", e="end", d=1.0)],
        [b("🔡 Words", "clip.words"), b("📋 Text", "clip.meta"), b("🔁 Re-render", "clip.render"), b("↩ Undo", "clip.undo")],
    ]


@callback("clip.edit")
def _edit(ctx: Ctx, p: dict) -> None:
    ctx.send(edit_text(p["c"]), edit_buttons(ctx, p["c"]))


@command("edit", group="Clips", help="edit panel of a clip", usage="/edit 3")
def _edit_cmd(ctx: Ctx, args: str) -> None:
    cid = clip_ref(ctx, args or "")
    ctx.send(edit_text(cid), edit_buttons(ctx, cid))


@callback("clip.nudge")
def _nudge(ctx: Ctx, p: dict) -> None:
    clip_svc.nudge(p["c"], p["e"], float(p["d"]))
    ctx.edit(edit_text(p["c"]), edit_buttons(ctx, p["c"]))
    ctx.toast("Moved to the next word boundary")


@callback("clip.ask_text")
def _ask_text(ctx: Ctx, p: dict) -> None:
    c = _clip(p["c"])
    ctx.ask(f"Send the new {p['f']} for #{c['rank']} (now: <i>{esc(c[p['f']] or '—')}</i>)", "edit_text",
            {"c": p["c"], "f": p["f"]}, placeholder=f"New {p['f']}")


@step("edit_text")
def _edit_text_step(ctx: Ctx, text: str, p: dict) -> None:
    c = _clip(p["c"])
    new = " ".join(text.split())
    ctx.send(f"{p['f'].title()} of #{c['rank']}:\n<s>{esc(c[p['f']] or '—')}</s>\n→ <b>{esc(new)}</b>",
             [[ctx.btn("💾 Save", "clip.save_text", one_shot=True, c=p["c"], f=p["f"], v=new),
               ctx.btn("✖ Cancel", "noop")]])


@callback("clip.save_text")
def _save_text(ctx: Ctx, p: dict) -> None:
    clip_svc.set_text(p["c"], p["f"], p["v"])
    ctx.edit("💾 Saved. Re-render to apply it to the video.", [[ctx.btn("🔁 Re-render now", "clip.render", c=p["c"]),
                                                               ctx.btn("✏️ Edit more", "clip.edit", c=p["c"])]])


@callback("noop", role="viewer")
def _noop(ctx: Ctx, p: dict) -> None:
    ctx.edit("✖ Cancelled.")


@callback("clip.styles")
def _styles(ctx: Ctx, p: dict) -> None:
    from clipforge.stages.captions import PRESETS

    keys = list(PRESETS)
    with db.session() as s:
        from sqlmodel import select

        keys += [r.key for r in s.exec(select(db.Preset)).all()]
    rows = [[ctx.btn(k, "clip.preset", c=p["c"], v=k) for k in keys[i:i + 3]] for i in range(0, len(keys), 3)]
    rows.append([ctx.btn("Default (config)", "clip.preset", c=p["c"], v=None)])
    ctx.send("🎨 Caption style:", rows)


@callback("clip.preset")
def _preset(ctx: Ctx, p: dict) -> None:
    clip_svc.set_preset(p["c"], p.get("v"))
    ctx.edit(f"🎨 Style set to <b>{esc(p.get('v') or 'default')}</b>. Re-render to apply.",
             [[ctx.btn("🔁 Re-render now", "clip.render", c=p["c"])]])


@callback("clip.layouts")
def _layouts(ctx: Ctx, p: dict) -> None:
    ctx.send("🧱 Layout: auto (ClipForge decides), track (follow the speaker), split (two faces), blur (full frame):",
             [[ctx.btn(x, "clip.layout", c=p["c"], v=x) for x in ("auto", "track", "split", "blur")]])


@callback("clip.layout")
def _layout(ctx: Ctx, p: dict) -> None:
    clip_svc.set_layout(p["c"], p["v"])
    ctx.edit(f"🧱 Layout set to <b>{p['v']}</b>. Re-render to apply.", [[ctx.btn("🔁 Re-render now", "clip.render", c=p["c"])]])


@callback("clip.scripts")
def _scripts(ctx: Ctx, p: dict) -> None:
    ctx.send("🔤 Caption script:", [[ctx.btn("Roman (Hinglish)", "clip.script", c=p["c"], v="roman"),
                                     ctx.btn("देवनागरी", "clip.script", c=p["c"], v="devanagari")]])


@callback("clip.script")
def _script(ctx: Ctx, p: dict) -> None:
    clip_svc.set_script(p["c"], p["v"])
    ctx.edit(f"🔤 Captions in <b>{p['v']}</b>. Re-render to apply.", [[ctx.btn("🔁 Re-render now", "clip.render", c=p["c"])]])


# ============================================================================ trim + words
@command("trim", group="Clips", help="cut a clip to a part of itself (snaps to words)",
         usage="/trim 3 0:12-0:47   ·   /trim 3 @1:02:10-1:02:55 (episode time)")
def _trim(ctx: Ctx, args: str) -> None:
    m = re.fullmatch(r"\s*(\S+)\s+(@?)([\d:.]+)\s*-\s*([\d:.]+)\s*", args)
    if not m:
        ctx.send("Usage: <code>/trim 3 0:12-0:47</code> — times inside the clip (what you see in the preview). "
                 "Use <code>@1:02:10-1:02:55</code> for episode time.")
        return
    cid = clip_ref(ctx, m.group(1))
    out = clip_svc.trim(cid, parse_clock(m.group(3)), parse_clock(m.group(4)), relative=not m.group(2))
    ctx.send(f"✂ #{out['rank']} is now {fmt_dur(out['duration'])}.\nStarts: “{esc(out['first_words'])} …”\n"
             f"Ends: “… {esc(out['last_words'])}”", [[ctx.btn("🔁 Re-render", "clip.render", c=cid),
                                                      ctx.btn("✏️ Fine-tune", "clip.edit", c=cid)]])


def words_text(clip_id: str) -> str:
    lines = clip_svc.word_lines(clip_id)
    out = []
    for line in lines:
        out.append("  ".join(f"<code>{w['index']}</code> {esc(w['edited'] or w['text'])}{'✏️' if w['edited'] else ''}"
                             for w in line))
    rank = clip_id.split("-")[1]
    return (f"🔡 Caption words of #{rank}:\n" + "\n".join(out) +
            f"\n\nFix one: <code>/word {rank} #12 sahi</code> or <code>/word {rank} \"galti\" \"galat\"</code>")


@command("words", role="viewer", group="Clips", help="numbered caption words of a clip", usage="/words 3")
def _words(ctx: Ctx, args: str) -> None:
    ctx.send(words_text(clip_ref(ctx, args or "")))


@callback("clip.words", role="viewer")
def _words_cb(ctx: Ctx, p: dict) -> None:
    ctx.send(words_text(p["c"]))


@command("word", group="Clips", help="fix a caption word", usage='/word 3 "galti" "galat"   ·   /word 3 #12 galat')
def _word(ctx: Ctx, args: str) -> None:
    try:
        parts = shlex.split(args)
    except ValueError:
        parts = args.split()
    if len(parts) != 3:
        ctx.send('Usage: <code>/word 3 "galti" "galat"</code> or <code>/word 3 #12 galat</code>')
        return
    cid = clip_ref(ctx, parts[0])
    old: str | int = int(parts[1][1:]) if re.fullmatch(r"#\d+", parts[1]) else parts[1]
    res = clip_svc.set_word(cid, old, parts[2])
    ctx.send(f"🔤 Changed {res['changed']} word(s) in #{res['rank']} → “{esc(parts[2])}”. Re-render to apply.",
             [[ctx.btn("🔁 Re-render", "clip.render", c=cid)]])


# ============================================================================ re-render / undo / metadata
def watch_render(chat_id: int, clip_id: str) -> None:
    st.kv_set(f"render:{clip_id}", chat_id, dt.timedelta(days=2))


@callback("clip.render")
def _render_cb(ctx: Ctx, p: dict) -> None:
    clip_svc.render_clip(p["c"])
    watch_render(ctx.chat_id, p["c"])
    ctx.send(f"🔁 Rendering #{p['c'].split('-')[1]} — I'll send the new version when it's done.")


@command("render", group="Clips", help="re-render a clip, or all changed / approved clips",
         usage="/render 3  ·  /render changed  ·  /render 12 approved")
def _render(ctx: Ctx, args: str) -> None:
    parts = args.split()
    if parts and re.fullmatch(r"#?\d+(-\d+)?", parts[0]) and not (len(parts) == 2 and parts[1] in ("changed", "approved", "all")):
        cid = clip_ref(ctx, parts[0])
        clip_svc.render_clip(cid)
        watch_render(ctx.chat_id, cid)
        ctx.send(f"🔁 Rendering {cid} — I'll send it when it's done.")
        return
    jid = job_arg(ctx, parts[0] if len(parts) == 2 else None)
    which = parts[-1] if parts else "changed"
    if which == "changed":
        ranks = clip_svc.render(jid, only_stale=True)
    elif which == "approved":
        ranks = clip_svc.render(jid, only_approved=True, only_stale=True)
    else:
        ranks = clip_svc.render(jid, force=True)
    if not ranks:
        ctx.send("Nothing changed — every clip is up to date.")
        return
    if isinstance(ranks, list):
        for r in ranks:
            watch_render(ctx.chat_id, f"{jid}-{r}")
    ctx.send(f"🔁 Rendering {len(ranks) if isinstance(ranks, list) else 'all'} clip(s) of job {jid}. New versions come here.")


def on_render_done(bot, job_id: int, ranks: list[int] | None, ok: bool, error: str = "") -> None:
    """Send the new version of every clip a chat asked to re-render (with Undo)."""
    for key, chat_id in st.kv_prefix(f"render:{job_id}-").items():
        cid = key.split(":", 1)[1]
        rank = int(cid.split("-")[1])
        if ranks is not None and rank not in ranks:
            continue
        st.kv_del(key)
        chat = st.chat(int(chat_id))
        if not chat:
            continue
        ctx = Ctx(bot, chat.chat_id, chat.user_id, chat.role)
        if not ok:
            from clipforge.notify.tg_core import scrub

            ctx.send(f"❌ Re-render of #{rank} failed: {esc(scrub(error)[:300])}", [[ctx.btn("🔁 Try again", "clip.render", c=cid)]])
            continue
        c = _clip(cid)
        buttons = [[ctx.btn("↩ Undo", "clip.undo", c=cid), ctx.btn("✅ Approve", "clip.approve", c=cid, v=True),
                    ctx.btn("📅 Schedule", "sched.start", c=cid)]]
        send_clip(ctx, cid, "preview", f"🔁 New version of #{rank} · {esc(c['title'])} · {fmt_dur(c['duration'])}", buttons)


@callback("clip.undo")
def _undo(ctx: Ctx, p: dict) -> None:
    c = clip_svc.undo(p["c"])
    send_clip(ctx, p["c"], "preview", f"↩ Restored the previous version of #{c['rank']} · {esc(c['title'])}",
              [[ctx.btn("↩ Undo the undo", "clip.undo", c=p["c"])]])


# ============================================================================ copy-ready metadata (sent after every clip)
SECTION_MAX = 3800  # one section always fits one message (4096) with its label


def _queue_tags(video_id: str, rank: int) -> list[str]:
    """YouTube tags written for this clip by the publishing metadata step (newest queue item), if any."""
    from sqlmodel import select

    with db.session() as s:
        items = s.exec(select(db.QueueItem).where(db.QueueItem.video_id == video_id, db.QueueItem.rank == rank,
                                                  db.QueueItem.platform == "youtube").order_by(db.QueueItem.id.desc())).all()
    return next((i.tags for i in items if i.tags), [])


def meta_sections(clip_id: str) -> list[str] | None:
    """One HTML section per copyable field (tap a <code> block to copy it), or None when metadata.json has no row yet."""
    job, rank = job_svc.parse_clip_id(clip_id)
    m = job_svc.metas(job_svc.ws_for(job)).get(rank)
    if not m:
        return None
    from clipforge.publish import ig_meta

    def sec(label: str, text: str) -> str:
        return f"<b>{label}</b>\n<code>{esc(text.strip()[:SECTION_MAX])}</code>"

    tags = list(dict.fromkeys("#" + t.lower().lstrip("#") for t in m.hashtags if t.strip("#")))[:5]
    out = [f"📋 <b>{clip_id}</b> · tap a block to copy",
           sec("YouTube title", m.yt_title), sec("YouTube description", m.yt_description),
           sec("Instagram caption", ig_meta.clean_caption(m.ig_caption)), sec("Hashtags", " ".join(tags))]
    yt_tags = _queue_tags(job.video_id, rank)
    if yt_tags:
        out.append(sec("Tags", ", ".join(yt_tags)))
    return out


def not_ready_text(clip_id: str) -> str:
    return f"📋 Metadata not ready — /meta {clip_id} to generate"


def meta_messages(clip_id: str) -> list[str]:
    """The metadata text for one clip, split at section boundaries so each message stays ≤ 4096 characters."""
    from clipforge.notify.tg_api import TEXT_LIMIT

    secs = meta_sections(clip_id)
    if secs is None:
        return [not_ready_text(clip_id)]
    out, cur = [], ""
    for s in secs:
        if cur and len(cur) + 2 + len(s) > TEXT_LIMIT:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur}\n\n{s}" if cur else s
    out.append(cur)
    return out


def send_meta(ctx: Ctx, clip_id: str, reply_to: int | None = None) -> None:
    try:
        msgs = meta_messages(clip_id)
    except Exception as exc:  # the clip itself was delivered; the text is a bonus
        from clipforge.utils.log import log

        log.warning(f"[telegram] metadata for {clip_id} failed: {exc}")
        return
    for t in msgs:
        ctx.send(t, reply_to=reply_to, quiet=True)


def _ensure_meta(ctx: Ctx, job, ranks: list[int]) -> None:
    """Write missing metadata with the normal metadata step (cached per clip; the LLM runs only for clips without it)."""
    ws = job_svc.ws_for(job)
    have = job_svc.metas(ws)
    if all(r in have for r in ranks):
        return
    if not ctx.owner:
        raise ValueError("Metadata isn't written yet — ask the owner to run /meta.")
    from clipforge import pipeline
    from clipforge.api.jobs import job_config

    ctx.send("✍️ Writing titles and captions…", quiet=True)
    pipeline.metadata(ws, job_config(job.settings))


@callback("clip.meta", role="viewer")
def _meta(ctx: Ctx, p: dict) -> None:
    send_meta(ctx, p["c"])


@command("text", role="viewer", group="Clips", help="copy-ready title, description, caption, hashtags", usage="/text 3")
def _text(ctx: Ctx, args: str) -> None:
    send_meta(ctx, clip_ref(ctx, args or ""))


@command("meta", role="viewer", group="Clips", help="publishing text of a clip, or of every approved clip of a job",
         usage="/meta 12-3   ·   /meta 12")
def _meta_cmd(ctx: Ctx, args: str) -> None:
    tok = (args.split() or [""])[0].lstrip("#")
    if re.fullmatch(r"\d+-\d+", tok):
        cid = clip_ref(ctx, tok)
        job, rank = job_svc.parse_clip_id(cid)
        _ensure_meta(ctx, job, [rank])
        send_meta(ctx, cid)
        return
    job_id = job_arg(ctx, tok or None)
    chosen = [c for c in clip_svc.list_clips(job_id, "approved") if c["rendered"]]
    if not chosen:
        ctx.send(f"No approved, rendered clips in job {job_id}. Use <code>/meta {job_id}-3</code> for one clip.")
        return
    _ensure_meta(ctx, db.get_job(job_id), [c["rank"] for c in chosen])
    for c in chosen[:MAX_SEND]:
        send_meta(ctx, c["id"])
