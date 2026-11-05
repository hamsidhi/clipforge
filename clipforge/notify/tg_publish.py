"""Telegram: schedule clips (both platforms, auto slot or a typed IST time), queue / calendar, "Needs you" with the
same one-click fixes as the web app, assisted batches, health, stats, top, earnings, campaigns, pause / resume.

Accounts that may receive posts are only the confirmed, unpaused ones with a working login
(services.publishing.postable_accounts). Uploads stay private + publishAt; "publish now" / public need YES + PIN."""

from __future__ import annotations

import datetime as dt
import secrets
from collections import defaultdict

from clipforge.config import load_config
from clipforge.notify import tg_store as st
from clipforge.notify.tg_api import esc
from clipforge.notify.tg_core import Ctx, callback, command, danger, fmt_bytes, scrub, step
from clipforge.notify.tg_jobs import job_arg
from clipforge.publish import channels as chs
from clipforge.publish import states
from clipforge.publish.timez import parse_when, utc_to_local
from clipforge.services import jobs as job_svc
from clipforge.services import publishing as pub

ICON = {"youtube": "▶️", "instagram": "📸"}
NAME = {"youtube": "YouTube", "instagram": "Instagram"}


def _plat(p: str | None) -> str:
    return ICON.get(p or "youtube", "•")


# ============================================================================ schedule one clip (wizard)
def _sch(p: dict) -> tuple[str, dict]:
    sid = p.get("s", "")
    s = st.kv_get(f"sch:{sid}")
    if not s:
        raise ValueError("That scheduling expired — tap 📅 Schedule again.")
    return sid, s


def _save(sid: str, s: dict) -> None:
    st.kv_set(f"sch:{sid}", s, dt.timedelta(hours=6))


@callback("sched.start")
def _sched_start(ctx: Ctx, p: dict) -> None:
    accounts = pub.postable_accounts()
    if not accounts:
        ctx.send("No account can take posts right now (none connected, confirmed and unpaused). Add one on the laptop: "
                 "Accounts page.")
        return
    sid = secrets.token_hex(4)
    _save(sid, {"clip": p["c"], "accounts": []})
    plats = sorted({a.platform or "youtube" for a in accounts})
    rows = [[ctx.btn(f"{_plat(x)} {NAME.get(x, x)}", "sched.plat", s=sid, p=x) for x in plats]]
    if len(plats) > 1:
        rows.append([ctx.btn("▶️📸 Both", "sched.plat", s=sid, p="both")])
    ctx.send(f"📅 Schedule clip {p['c']} — where?", rows)


@callback("sched.plat")
def _sched_plat(ctx: Ctx, p: dict) -> None:
    sid, s = _sch(p)
    accounts = pub.postable_accounts(p["p"])
    by_platform = defaultdict(list)
    for a in accounts:
        by_platform[a.platform or "youtube"].append(a)
    s["accounts"] = [lst[0].id for lst in by_platform.values()]  # one per platform by default
    _save(sid, s)
    if any(len(v) > 1 for v in by_platform.values()):
        _accounts_menu(ctx, sid, s, accounts)
        return
    _when_menu(ctx, sid, s)


def _accounts_menu(ctx: Ctx, sid: str, s: dict, accounts) -> None:
    rows = [[ctx.btn(("✓ " if a.id in s["accounts"] else "") + f"{_plat(a.platform)} {a.title}", "sched.acct", s=sid, a=a.id)]
            for a in accounts]
    rows.append([ctx.btn("Next ▶", "sched.when", s=sid)])
    ctx.edit("📅 Which accounts? (tap to toggle)", rows)


@callback("sched.acct")
def _sched_acct(ctx: Ctx, p: dict) -> None:
    sid, s = _sch(p)
    a = p["a"]
    s["accounts"] = [x for x in s["accounts"] if x != a] if a in s["accounts"] else [*s["accounts"], a]
    _save(sid, s)
    plats = {chs.get(x).platform for x in s["accounts"] if chs.get(x)} or {"youtube"}  # type: ignore[union-attr]
    accounts = [x for pl in plats for x in pub.postable_accounts(pl or "youtube")]
    _accounts_menu(ctx, sid, s, accounts or pub.postable_accounts())


@callback("sched.when")
def _sched_when_cb(ctx: Ctx, p: dict) -> None:
    sid, s = _sch(p)
    _when_menu(ctx, sid, s)


def _names(ids: list[str]) -> str:
    return ", ".join(f"{_plat(c.platform)} {esc(c.title)}" for c in (chs.get(i) for i in ids) if c)


def _when_menu(ctx: Ctx, sid: str, s: dict) -> None:
    if not s["accounts"]:
        ctx.edit("Pick at least one account.")
        return
    ctx.edit(f"📅 Clip {s['clip']} → {_names(s['accounts'])}\nWhen?",
             [[ctx.btn("⚡ Next free slot", "sched.go", s=sid), ctx.btn("🕘 Pick a time", "sched.ask_time", s=sid)]])


@callback("sched.ask_time")
def _sched_ask_time(ctx: Ctx, p: dict) -> None:
    sid, _ = _sch(p)
    ctx.ask("🕘 When (IST)? e.g. <code>today 21:00</code>, <code>tomorrow 9pm</code>, <code>5 Nov 18:30</code>",
            "sched_time", {"s": sid}, placeholder="today 21:00")


@step("sched_time")
def _sched_time_step(ctx: Ctx, text: str, p: dict) -> None:
    sid, s = _sch(p)
    try:
        when = parse_when(text, load_config().publishing.timezone)
    except ValueError as exc:
        ctx.ask(f"{esc(str(exc))} Try again:", "sched_time", {"s": sid}, placeholder="today 21:00")
        return
    s["at"] = when.isoformat()
    _save(sid, s)
    local = utc_to_local(when, load_config().publishing.timezone)
    ctx.send(f"📅 Clip {s['clip']} → {_names(s['accounts'])}\nat <b>{local:%a %d %b %H:%M}</b> IST "
             "(uploaded private, goes public at that time). OK?",
             [[ctx.btn("✅ Schedule", "sched.go", one_shot=True, s=sid), ctx.btn("✖ Cancel", "noop")]])


@callback("sched.go")
def _sched_go(ctx: Ctx, p: dict) -> None:
    sid, s = _sch(p)
    job, rank = job_svc.parse_clip_id(s["clip"])
    at = dt.datetime.fromisoformat(s["at"]) if s.get("at") else None
    res = pub.schedule_clips(job.video_id, [rank], s["accounts"], at)  # type: ignore[arg-type]
    st.kv_del(f"sch:{sid}")
    ctx.edit(_scheduled_text(res))


def _scheduled_text(res: dict) -> str:
    lines = []
    for cid, items in res.items():
        ch = chs.get(cid)
        for it in items:
            lines.append(f"{_plat(it.platform)} {esc(ch.title if ch else cid)} · {pub.when_local(it)} · {esc(it.title[:60])}")
        if not items:
            lines.append(f"{esc(ch.title if ch else cid)}: already queued there (no duplicates)")
    return "📅 Scheduled (private until the slot):\n" + "\n".join(lines) if lines else "Nothing new to schedule."


# ============================================================================ schedule all approved of a job
@callback("sched.job")
def _sched_job(ctx: Ctx, p: dict) -> None:
    accounts = pub.postable_accounts()
    plats = sorted({a.platform or "youtube" for a in accounts})
    if not plats:
        ctx.send("No account can take posts right now — add/confirm/unpause one on the laptop.")
        return
    rows = [[ctx.btn(f"{_plat(x)} {NAME.get(x, x)}", "sched.job_go", one_shot=True, j=p["j"], p=x) for x in plats]]
    if len(plats) > 1:
        rows.append([ctx.btn("▶️📸 Both", "sched.job_go", one_shot=True, j=p["j"], p="both")])
    ctx.send(f"📅 Schedule the approved clips of job {p['j']} (next free slots) on:", rows)


@callback("sched.job_go")
def _sched_job_go(ctx: Ctx, p: dict) -> None:
    ctx.edit(_schedule_job(int(p["j"]), p["p"]))


def _schedule_job(job_id: int, platform: str) -> str:
    job = job_svc.job_or_404(job_id)
    targets = [a.id for a in pub.postable_accounts(platform)]
    if not targets:
        return f"No active {platform} account."
    res = pub.schedule_clips(job.video_id, None, targets)  # type: ignore[arg-type]
    return _scheduled_text(res)


@command("schedule", role="viewer", group="Publishing",
         help="next 7 days as a calendar — or /schedule 12 both to schedule job 12's approved clips",
         usage="/schedule  ·  /schedule 12 youtube|instagram|both")
def _schedule(ctx: Ctx, args: str) -> None:
    parts = args.split()
    if parts and parts[0].lstrip("#").isdigit():
        ctx.require_owner()
        plat = parts[1].lower() if len(parts) > 1 else "both"
        if plat not in ("youtube", "instagram", "both"):
            ctx.send("Platform must be youtube, instagram or both.")
            return
        jid = job_arg(ctx, parts[0])
        ctx.send(f"Add the approved clips of job {jid} to {plat} and give them the next free slots?",
                 [[ctx.btn("✅ Yes", "sched.job_go", one_shot=True, bind_user=True, ttl=st.CONFIRM_TTL, j=jid, p=plat),
                   ctx.btn("✖ No", "noop")]])
        return
    ctx.send(calendar(7))


def calendar(days: int) -> str:
    zone = load_config().publishing.timezone
    items = sorted(pub.upcoming(days), key=lambda it: it.slot_utc)  # type: ignore[arg-type,return-value]
    if not items:
        return f"📅 Nothing scheduled in the next {days} days."
    by_day: dict[str, list] = defaultdict(list)
    for it in items:
        by_day[utc_to_local(it.slot_utc, zone).strftime("%a %d %b")].append(it)  # type: ignore[arg-type]
    out = [f"📅 <b>Next {days} days</b> (IST) — {len(items)} posts"]
    for day, its in by_day.items():
        out.append(f"\n<b>{day}</b>")
        for it in its:
            ch = chs.get(it.channel_id)
            out.append(f"{utc_to_local(it.slot_utc, zone):%H:%M} {_plat(it.platform)} {esc(it.title[:45])}"  # type: ignore[arg-type]
                       f" <i>({esc(ch.title if ch else '')})</i> · {esc(pub.status_text(it)['label'])}")
    return "\n".join(out)


@command("queue", role="viewer", group="Publishing", help="next posts, grouped by day")
def _queue(ctx: Ctx, args: str) -> None:
    text = calendar(30)
    lines = text.split("\n")
    ctx.send("\n".join(lines[:40]) + ("\n…" if len(lines) > 40 else ""))


# ============================================================================ needs you
FIX_LABEL = {"reschedule": "📅 Next free slot", "retry": "🔁 Retry", "unbatch": "↩ Remove from batch",
             "publish_now": "🚀 Publish now", "sync": "🔄 Sync with YouTube", "switch_assisted": "✋ Switch to assisted",
             "studio_instead": "📂 Upload via Studio instead"}


def status_line(it) -> str:
    """Same wording as the web app (publish/states.status_text via services.publishing)."""
    t = pub.status_text(it)
    return f"{esc(t['label'])}" + (f" — {esc(scrub(t['detail'][:160]))}" if t["detail"] else "")


def needs_you_view(ctx: Ctx) -> None:
    rows = pub.needs_you()
    accts = pub.needs_you_accounts()
    if accts:
        ctx.send("🔑 <b>Accounts</b>\n" + "\n".join(f"• {esc(a['account'])}: {esc(scrub(a['text'])[:200])}" for a in accts))
    if not rows:
        if not accts:
            ctx.send("✅ Nothing needs you.")
        return
    from clipforge.notify.commands import app_url

    ctx.send(f"⚠️ <b>{len(rows)} post(s) need you</b>",
             [[ctx.btn("📅 Reschedule all missed", "q.bulk", one_shot=True, a="reschedule_missed"),
               ctx.btn("🔁 Retry all failed", "q.bulk", one_shot=True, a="retry_failed")]] if ctx.owner else None)
    for r in rows[:15]:
        it = r["item"]
        btns: list[dict] = []
        notes = []
        for a in r["actions"]:
            if a in ("reschedule", "retry", "unbatch", "switch_assisted", "studio_instead") and ctx.owner:
                btns.append(ctx.btn(FIX_LABEL[a], "q.fix", one_shot=True, i=it.id, a=a))
            elif a == "publish_now" and ctx.owner:
                btns.append(ctx.btn(FIX_LABEL[a], "q.publish_now", i=it.id))
            elif a == "sync" and ctx.owner and it.platform == "youtube":
                btns.append(ctx.btn(FIX_LABEL[a], "q.sync", one_shot=True, ch=it.channel_id))
            elif a == "studio" and it.youtube_video_id:
                btns.append(ctx.url_btn("🎬 Open in Studio", f"https://studio.youtube.com/video/{it.youtube_video_id}/edit"))
            elif a == "open_batch":
                notes.append(f"send <code>/batch {esc(r['account'][:20])}</code> for the files")
            elif a == "link":
                notes.append(f"<code>/link {it.id} &lt;youtube link&gt;</code> if you uploaded it yourself")
            elif a in ("reconnect", "confirm"):
                notes.append("fix it on the laptop: Accounts page (logins never go through Telegram)")
            elif a in ("open_app", "edit"):
                url = app_url("/schedule")
                if url:
                    btns.append(ctx.url_btn("📱 Open app", url))
            elif a == "audit_info":
                notes.append("YouTube keeps API uploads private until the API audit — see Settings → Publishing")
        rows_btn = [btns[i:i + 2] for i in range(0, len(btns), 2)]
        ctx.send(f"{_plat(it.platform)} <b>{esc(it.title[:70] or 'clip')}</b> · {esc(r['account'])}\n"
                 f"{pub.when_local(it)} · {status_line(it)}" + ("".join(f"\n• {n}" for n in notes)), rows_btn or None)
    if len(rows) > 15:
        ctx.send(f"…and {len(rows) - 15} more — open the Schedule page in the app.")


@command("needsyou", role="viewer", group="Publishing", help="posts that need you, with one-tap fixes",
         aliases=("needs", "attention"))
def _needsyou(ctx: Ctx, args: str) -> None:
    needs_you_view(ctx)


@callback("q.fix")
def _fix(ctx: Ctx, p: dict) -> None:
    it = pub.fix(int(p["i"]), p["a"])
    ctx.edit(f"✅ Done: {esc(it.title[:60])} — {status_line(it)} · {pub.when_local(it)}")


@callback("q.bulk")
def _bulk(ctx: Ctx, p: dict) -> None:
    from clipforge.api.publish_routes import BulkIn, queue_bulk

    res = queue_bulk(BulkIn(action=p["a"]))
    ctx.send(f"✅ {len(res['done'])} done" + (f", {len(res['errors'])} couldn't be changed" if res["errors"] else "") + ".")


@callback("q.sync")
def _sync(ctx: Ctx, p: dict) -> None:
    from clipforge.publish import reconcile

    res = pub.wrap(reconcile.sync, p["ch"])
    ctx.send(f"🔄 Synced: {res.get('linked', 0) if isinstance(res, dict) else 0} linked.")


@callback("q.publish_now")
def _publish_now_ask(ctx: Ctx, p: dict) -> None:
    ctx.ask_danger("q.publish_now", i=int(p["i"]))


def _item_title(p: dict) -> str:
    it = pub.item_or_404(int(p["i"]))
    ch = chs.get(it.channel_id)
    return f"“{it.title[:60]}” on {ch.title if ch else it.channel_id}"


@danger("q.publish_now", lambda p: f"Publish {_item_title(p)} PUBLICLY right now?", type_yes=True)
def _publish_now(ctx: Ctx, p: dict) -> str:
    it = pub.fix(int(p["i"]), "publish_now")
    return f"🚀 Publishing {esc(it.title[:60])} now — I'll tell you when it's live."


@command("link", group="Publishing", help="link a post you uploaded yourself", usage="/link 41 https://youtube.com/shorts/…")
def _link(ctx: Ctx, args: str) -> None:
    from clipforge.publish import reconcile

    parts = args.split()
    if len(parts) != 2 or not parts[0].isdigit():
        ctx.send("Usage: <code>/link 41 https://youtube.com/shorts/abc123</code>")
        return
    it = pub.wrap(reconcile.link, int(parts[0]), parts[1])
    ctx.send(f"🔗 Linked {esc(it.title[:60])} → {status_line(it)}.")


@command("batch", group="Publishing", help="assisted mode: checklist + files to upload yourself", usage="/batch MyChannel")
def _batch(ctx: Ctx, args: str) -> None:
    ch = find_account(args)
    res = pub.make_batch(ch.id)
    lines = [f"📦 <b>Batch for {esc(ch.title)}</b> — {res['count']} clips. Upload each in "
             f"{'the Instagram app' if ch.platform == 'instagram' else 'YouTube Studio'} with these details:"]
    ctx.send("\n".join(lines))
    from pathlib import Path

    from clipforge.notify import tg_media

    folder = Path(res["folder"])
    for e in res["items"][:10]:
        text = (f"<b>{e['n']}. {e['slot_pretty']}</b>\n<b>Title</b><pre>{esc(e['title'])}</pre>"
                + (f"<b>Description</b><pre>{esc(e['description'])}</pre>" if e.get("description") else "")
                + (f"<b>Caption</b><pre>{esc(e['ig_caption'])}</pre>" if ch.platform == "instagram" and e.get("ig_caption") else ""))
        ctx.send(text)
        f = folder / e["file"]
        if f.exists():
            tg_media.deliver(ctx.api, ctx.chat_id, f, "full", f"{e['n']}. {esc(e['title'][:80])}")
    if res["count"] > 10:
        ctx.send(f"The other {res['count'] - 10} are in the batch folder on the computer / the app.")
    ctx.send("When they're uploaded, send /needsyou → Sync (YouTube) or /link (Instagram) so ClipForge tracks them.")


# ============================================================================ accounts
def find_account(name: str):
    n = name.strip().lower().lstrip("@")
    all_ = chs.all_channels()
    if not n:
        raise ValueError("Which account? " + ", ".join(c.title for c in all_))
    hits = [c for c in all_ if n == c.id.lower() or n in (c.title or "").lower() or n in (c.handle or "").lower()]
    if len(hits) != 1:
        raise ValueError("Which account? " + ", ".join(c.title for c in all_))
    return hits[0]


@command("accounts", role="viewer", group="Publishing", help="accounts with status, pause / resume buttons")
def _accounts(ctx: Ctx, args: str) -> None:
    all_ = chs.all_channels()
    if not all_:
        ctx.send("No accounts yet — connect one on the laptop (Accounts page).")
        return
    for c in all_:
        tok = pub.token_health(c)
        state = "⏸ paused" if c.paused else "✅ active" if c.confirmed else "⚠️ not confirmed"
        btns = None
        if ctx.owner:
            btns = [[ctx.btn("▶ Resume", "acct.resume", a=c.id) if c.paused else ctx.btn("⏸ Pause", "acct.pause", a=c.id)]]
        ctx.send(f"{_plat(c.platform)} <b>{esc(c.title)}</b> · {state}\n🔑 {esc(tok['text'])}", btns)


@command("pause", group="Publishing", help="stop posting to an account", usage="/pause MyChannel")
def _pause(ctx: Ctx, args: str) -> None:
    ch = find_account(args)
    ctx.send(f"Pause {esc(ch.title)}? Nothing is posted there until you resume.",
             [[ctx.btn("⏸ Pause", "acct.pause_go", one_shot=True, bind_user=True, ttl=st.CONFIRM_TTL, a=ch.id),
               ctx.btn("✖ No", "noop")]])


@callback("acct.pause")
def _pause_cb(ctx: Ctx, p: dict) -> None:
    ch = pub.ch_or_404(p["a"])
    ctx.send(f"Pause {esc(ch.title)}?", [[ctx.btn("⏸ Pause", "acct.pause_go", one_shot=True, bind_user=True,
                                                  ttl=st.CONFIRM_TTL, a=ch.id), ctx.btn("✖ No", "noop")]])


@callback("acct.pause_go")
def _pause_go(ctx: Ctx, p: dict) -> None:
    ch = pub.set_paused(p["a"], True)
    ctx.edit(f"⏸ Paused {esc(ch.title)}.")


@command("resume", group="Publishing", help="start posting to a paused account again (asks first)", usage="/resume MyChannel")
def _resume(ctx: Ctx, args: str) -> None:
    ctx.ask_danger("acct.resume", a=find_account(args).id)


@callback("acct.resume")
def _resume_cb(ctx: Ctx, p: dict) -> None:
    ctx.ask_danger("acct.resume", a=p["a"])


@danger("acct.resume", lambda p: f"Resume {(chs.get(p['a']).title if chs.get(p['a']) else p['a'])}? "  # type: ignore[union-attr]
        "Its queued clips will start uploading/posting again.")
def _resume_run(ctx: Ctx, p: dict) -> str:
    ch = pub.set_paused(p["a"], False)
    return f"▶ Resumed {esc(ch.title)}."


# ============================================================================ status
@command("health", role="viewer", group="Status", help="quota, last scheduler run, logins, audit, disk")
def _health(ctx: Ctx, args: str) -> None:
    h = pub.health()
    q = h["quota"]
    last = h.get("last_run_at") or "never"
    hb = h.get("heartbeat") or {}
    lines = [f"❤️ <b>Health</b> · {h['deployment']}",
             f"Scheduler: {esc(hb.get('text') or f'last run {str(last)[:16]}')} Next run: {esc(hb.get('next_run') or '-')}; "
             f"in-app ticker {'on' if h['ticker'] else 'off'}",
             f"YouTube quota left today: {q.get('inserts', '?')} uploads · audit: {esc(h['audit_status'])}",
             f"Disk: {h['disk']['pct']}% used · {fmt_bytes(h['disk']['free'])} free · {esc(h.get('db', {}).get('text', ''))}",
             f"Today's plan: {esc(h.get('plan_today') or '-')}"]
    for a in h["accounts"]:
        lines.append(f"{_plat(a['platform'])} {esc(a['title'])}: {'⏸ paused · ' if a['paused'] else ''}"
                     f"{'🔑 OK' if a['token']['ok'] else '🔑 ' + esc(a['token']['text'])} · mode {a.get('mode', '?')}"
                     + (f" ⚠️ {esc(a['mode_problem'])}" if a.get("mode_problem") else "")
                     + (f" · ⚠️ {a['attention']} need you" if a["attention"] else ""))
        lim = a.get("limits") or {}
        ready = "✅ auto-publish ready" if a.get("auto_ready") else ("connected, not auto-ready" if a.get("connected")
                                                                    else "not connected")
        lines.append(f"   {ready} · uploads 24 h: {lim.get('uploads_24h', 0)}"
                     + (f" · cooldown until {esc(str(lim['cooldown_until'])[:16])} UTC" if lim.get("cooldown_until") else "")
                     + ("".join(f"\n   ✖ {esc(scrub(f)[:120])}" for f in lim.get("recent_failures", [])[:2])))
        if a.get("platform") == "instagram" and a.get("eligibility") and not a.get("auto_ready"):
            lines.append(f"   {esc(scrub(a['eligibility']['summary'])[:200])}")
        lp, nx = a.get("last_published"), a.get("next_post")
        lines.append(f"   last published: {esc(lp['title'][:40]) + ' · ' + esc(lp['at'] or '') if lp else 'none confirmed yet'}"
                     + (f" · next: {esc(nx['at'] or '')} ({esc(nx['status'])})" if nx else ""))
    try:
        from clipforge.server import leases

        lines.append(leases.workers_line())
    except Exception:
        pass
    ctx.send("\n".join(lines))


@command("stats", role="viewer", group="Status", help="views per platform", usage="/stats 7d  ·  /stats 28d")
def _stats(ctx: Ctx, args: str) -> None:
    days = 28 if "28" in args else 7
    s = pub.stats(days)
    if not s:
        ctx.send(f"📊 No published posts with stats in the last {days} days.")
        return
    lines = [f"📊 <b>Last {days} days</b>"]
    for plat, d in s.items():
        lines.append(f"{_plat(plat)} {NAME.get(plat, plat)}: {d['posts']} posts · {d['views']:,} views · {d['likes']:,} likes")
    ctx.send("\n".join(lines))


@command("top", role="viewer", group="Status", help="top posts (28 days)")
def _top(ctx: Ctx, args: str) -> None:
    rows = pub.top(5)
    if not rows:
        ctx.send("🏆 No stats yet.")
        return
    ctx.send("🏆 <b>Top posts (28 days)</b>\n" + "\n".join(
        f"{i}. {_plat(p.platform)} {p.views:,} views · {esc(p.title[:60])}" for i, p in enumerate(rows, 1)))


@command("earnings", role="viewer", group="Status", help="campaign earnings estimate")
def _earnings(ctx: Ctx, args: str) -> None:
    e = pub.earnings()
    ctx.send(f"💰 Campaign earnings (estimate): ₹{e['total']:,.0f} · not paid yet ₹{e['unpaid']:,.0f} · {e['posts']} tracked posts")


@command("campaigns", role="viewer", group="Status", help="published posts not yet submitted to a campaign")
def _campaigns(ctx: Ctx, args: str) -> None:
    rows = pub.reminders()
    if not rows:
        ctx.send("✅ Every published post is submitted.")
        return
    ctx.send(f"📝 <b>{len(rows)} published post(s) not submitted yet</b>")
    for r in rows[:15]:
        ctx.send(f"{_plat(r['platform'])} {esc(r['title'][:70])}\n{esc(r['url'])}",
                 [[ctx.btn("✅ Mark submitted", "camp.submit", one_shot=True, i=r["queue_id"])]] if ctx.owner else None)


@callback("camp.submit")
def _camp_submit(ctx: Ctx, p: dict) -> None:
    pub.mark_submitted(int(p["i"]))
    ctx.edit("✅ Marked as submitted.")
    ctx.toast("Submitted")


def _states_ref() -> tuple:
    return states.ATTENTION
