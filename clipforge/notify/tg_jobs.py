"""Telegram: start jobs (link / forwarded video / file), the options wizard, live progress, job list + actions."""

from __future__ import annotations

import datetime as dt
import re
import secrets
import time
from pathlib import Path

from clipforge.api import db
from clipforge.config import Secrets, load_config
from clipforge.notify import tg_store as st
from clipforge.notify.tg_api import bar, esc
from clipforge.notify.tg_core import (Ctx, callback, command, danger, fmt_bytes, fmt_dur, parse_clock, scrub, step)
from clipforge.services import jobs as job_svc

URL_RE = re.compile(r"https?://\S+")
STAGE_ORDER = ["download", "transcribe", "analyze", "select", "reframe", "captions", "render", "metadata"]
STAGE_LABEL = {"download": "Downloading", "transcribe": "Transcribing", "analyze": "Analysing audio",
               "select": "Picking clips (LLM)", "reframe": "Framing faces", "captions": "Captions",
               "render": "Rendering", "metadata": "Titles & captions", "done": "Done"}
PAGE = 6


# ============================================================================ current job per chat
def set_current_job(chat_id: int, job_id: int) -> None:
    st.kv_set(f"cur_job:{chat_id}", job_id)


def current_job(chat_id: int) -> int | None:
    jid = st.kv_get(f"cur_job:{chat_id}")
    if jid and db.get_job(jid):
        return int(jid)
    done = [j for j in db.list_jobs() if j.status == "done"]
    return done[0].id if done else None  # type: ignore[return-value]


def job_arg(ctx: Ctx, token: str | None) -> int:
    if token and token.lstrip("#").isdigit():
        jid = int(token.lstrip("#"))
        job_svc.job_or_404(jid)
        set_current_job(ctx.chat_id, jid)
        return jid
    jid = current_job(ctx.chat_id)
    if not jid:
        raise ValueError("Which job? Send /jobs to see them.")
    return jid


# ============================================================================ wizard
def _providers() -> list[str]:
    cfg = load_config()
    sec = Secrets()
    out = ["ollama"]
    if sec.gemini_api_key:
        out.append("gemini")
    if sec.groq_api_key:
        out.append("groq")
    if cfg.llm.provider == "mock":
        out.append("mock")
    return out


def _defaults() -> dict:
    cfg = load_config()
    return {"clips": cfg.clips.count, "provider": cfg.llm.provider, "layout": cfg.reframe.layout,
            "script": cfg.captions.script}


def _wizard_text(w: dict) -> str:
    d = _defaults()
    pick = lambda k: w.get(k) or d[k]  # noqa: E731
    sec = w.get("section")
    section = f"{fmt_dur(sec[0])}–{fmt_dur(sec[1])}" if sec else "full video"
    src = esc(w.get("title") or w.get("url") or "")
    tx = f"pasted ({w['tx_lines']} lines)" if w.get("transcript") else "auto (YouTube captions, else Whisper)"
    return (f"🎬 <b>New job</b>\n{src}\n\n"
            f"Clips: <b>{pick('clips')}</b> · Part: <b>{section}</b>\n"
            f"LLM: <b>{pick('provider')}</b> · Layout: <b>{pick('layout')}</b> · Captions: <b>{pick('script')}</b>\n"
            f"Transcript: <b>{tx}</b>\n\n"
            "Tap to change, then Start.")


def _wizard_buttons(ctx: Ctx, wid: str, w: dict) -> list[list[dict]]:
    d = _defaults()
    cur = lambda k: w.get(k) or d[k]  # noqa: E731
    mark = lambda ok, t: ("✓ " if ok else "") + t  # noqa: E731
    changed = any(w.get(k) for k in ("clips", "section", "provider", "layout", "script"))
    rows = [[ctx.btn("▶ Start" if changed else "▶ Start with my defaults", "wiz.start", w=wid)],
            [ctx.btn(mark(cur("clips") == n, str(n)), "wiz.set", w=wid, k="clips", v=n) for n in (10, 15, 20, 25)],
            [ctx.btn(mark(not w.get("section"), "Full video"), "wiz.set", w=wid, k="section", v=None),
             ctx.btn(mark(bool(w.get("section")), "✂ Part…"), "wiz.range", w=wid)],
            [ctx.btn(mark(cur("provider") == p, p), "wiz.set", w=wid, k="provider", v=p) for p in _providers()],
            [ctx.btn(mark(cur("layout") == x, x), "wiz.set", w=wid, k="layout", v=x) for x in ("auto", "track", "split", "blur")],
            [ctx.btn(mark(cur("script") == "roman", "Roman"), "wiz.set", w=wid, k="script", v="roman"),
             ctx.btn(mark(cur("script") == "devanagari", "देवनागरी"), "wiz.set", w=wid, k="script", v="devanagari")],
            [ctx.btn("✖ Cancel", "wiz.cancel", w=wid)]]
    return rows


def start_wizard(ctx: Ctx, url: str | None = None, upload_id: str | None = None, title: str = "", force: bool = False) -> None:
    ctx.require_owner()
    if url and not force:
        dup = job_svc.duplicate_of(url)
        if dup:
            set_current_job(ctx.chat_id, dup.id)  # type: ignore[arg-type]
            ctx.send(f"📂 This video already has <b>job {dup.id}</b> ({dup.status}{', ' + str(dup.n_clips) + ' clips' if dup.n_clips else ''}).",
                     [[ctx.btn(f"📂 Open job {dup.id}", "job.view", j=dup.id),
                       ctx.btn("🔁 Process again", "wiz.again", url=url)]])
            return
    wid = secrets.token_hex(4)
    w = {"url": url, "upload_id": upload_id, "title": title, "chat": ctx.chat_id}
    st.kv_set(f"wiz:{wid}", w, dt.timedelta(days=1))
    mid = ctx.send(_wizard_text(w), _wizard_buttons(ctx, wid, w))
    w["msg"] = mid
    st.kv_set(f"wiz:{wid}", w, dt.timedelta(days=1))
    ask_transcript(ctx, wid)


# ============================================================================ optional transcript (skips full-video Whisper)
TX_MAX_FILE = 5 * 1024 * 1024


def ask_transcript(ctx: Ctx, wid: str) -> None:
    st.set_conversation(ctx.chat_id, "wiz_transcript", {"w": wid})
    ctx.send("📝 Got a transcript with timestamps (YouTube \"Show transcript\", .srt, .vtt)? Send it as a file or paste "
             "the text (several messages are fine) — it saves the full Whisper run. Or tap Skip.",
             [[ctx.btn("⏭ Skip", "wiz.tx_skip", w=wid)]], quiet=True)


def _tx_add(wid: str, text: str) -> dict:
    from clipforge.stages import transcript_input as ti

    _, w = _wiz({"w": wid})
    buf = ((w.get("tx_buf") or "") + "\n" + text).strip("\n")
    if len(buf) > ti.MAX_INPUT_CHARS:
        raise ValueError("That transcript is too long.")
    w["tx_buf"] = buf
    st.kv_set(f"wiz:{wid}", w, dt.timedelta(days=1))
    return w


def _tx_use(ctx: Ctx, wid: str) -> None:
    """Parse what was sent; errors (with line numbers) keep the conversation open for a corrected version."""
    from clipforge.stages import transcript_input as ti

    _, w = _wiz({"w": wid})
    try:
        p = ti.parse_or_raise(w.get("tx_buf") or "")
    except ti.TranscriptParseError as exc:
        w["tx_buf"] = ""
        st.kv_set(f"wiz:{wid}", w, dt.timedelta(days=1))
        st.set_conversation(ctx.chat_id, "wiz_transcript", {"w": wid})
        errs = "\n".join(f"• {esc(e)}" for e in exc.errors[:6])
        ctx.send(f"❌ I couldn't read that transcript:\n{errs}\nSend a corrected one, or tap Skip.",
                 [[ctx.btn("⏭ Skip", "wiz.tx_skip", w=wid)]])
        return
    st.clear_conversation(ctx.chat_id)
    w.update(transcript=w.pop("tx_buf"), tx_lines=len(p.lines))
    st.kv_set(f"wiz:{wid}", w, dt.timedelta(days=1))
    ctx.edit(_wizard_text(w), _wizard_buttons(ctx, wid, w), message_id=w.get("msg"))
    ctx.send(f"✅ Transcript added: {len(p.lines)} lines ({p.fmt}, up to {fmt_dur(p.lines[-1].start)}). "
             "Whisper will only run on the chosen clips. Tap Start above.")


@step("wiz_transcript")
def _wiz_transcript_step(ctx: Ctx, text: str, p: dict) -> None:
    from clipforge.stages import transcript_input as ti

    wid = p["w"]
    if URL_RE.fullmatch(text.strip()):  # a new link instead of a transcript
        start_wizard(ctx, url=text.strip())
        return
    w = _tx_add(wid, text)
    st.set_conversation(ctx.chat_id, "wiz_transcript", {"w": wid})
    lines = ti.parse_text(w["tx_buf"]).lines
    upto = f", up to {fmt_dur(lines[-1].start)}" if lines else ""
    ctx.send(f"📝 Got {len(lines)} timed lines so far{upto}. Send more, or:",
             [[ctx.btn("✅ Use it", "wiz.tx_done", w=wid), ctx.btn("⏭ Skip", "wiz.tx_skip", w=wid)]], quiet=True)


@callback("wiz.tx_done")
def _wiz_tx_done(ctx: Ctx, p: dict) -> None:
    ctx.require_owner()
    _tx_use(ctx, p["w"])


@callback("wiz.tx_skip")
def _wiz_tx_skip(ctx: Ctx, p: dict) -> None:
    conv = st.get_conversation(ctx.chat_id)
    if conv and conv[0] == "wiz_transcript":
        st.clear_conversation(ctx.chat_id)
    try:
        wid, w = _wiz(p)
        w.pop("tx_buf", None)
        st.kv_set(f"wiz:{wid}", w, dt.timedelta(days=1))
    except ValueError:
        pass
    ctx.edit("⏭ No transcript — YouTube captions or Whisper will be used. Tap Start above.")


def handle_transcript_file(ctx: Ctx, msg: dict) -> bool:
    """A .txt/.srt/.vtt sent while the wizard waits for a transcript."""
    doc = msg.get("document") or {}
    name = str(doc.get("file_name") or "")
    if not name.lower().endswith((".txt", ".srt", ".vtt")):
        return False
    conv = st.get_conversation(ctx.chat_id)
    if not conv or conv[0] != "wiz_transcript":
        ctx.send("📝 To use a transcript, send the YouTube link (or video) first, then the transcript file.")
        return True
    ctx.require_owner()
    if int(doc.get("file_size") or 0) > TX_MAX_FILE:
        ctx.send("That file is too big for a transcript (max 5 MB).")
        return True
    import tempfile

    info = ctx.api.call("getFile", file_id=doc["file_id"])
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / "transcript.txt"
        ctx.api.download(info["file_path"], dest)
        text = dest.read_bytes()[:TX_MAX_FILE].decode("utf-8-sig", "replace")
    wid = conv[1]["w"]
    _tx_add(wid, text)
    _tx_use(ctx, wid)
    return True


def _wiz(payload: dict) -> tuple[str, dict]:
    wid = payload.get("w", "")
    w = st.kv_get(f"wiz:{wid}")
    if not w:
        raise ValueError("That setup expired — send the link again.")
    return wid, w


@callback("wiz.again")
def _wiz_again(ctx: Ctx, p: dict) -> None:
    start_wizard(ctx, url=p.get("url"), force=True)


@callback("wiz.set")
def _wiz_set(ctx: Ctx, p: dict) -> None:
    wid, w = _wiz(p)
    w[p["k"]] = p.get("v")
    st.kv_set(f"wiz:{wid}", w, dt.timedelta(days=1))
    ctx.edit(_wizard_text(w), _wizard_buttons(ctx, wid, w))


@callback("wiz.range")
def _wiz_range(ctx: Ctx, p: dict) -> None:
    wid, _ = _wiz(p)
    ctx.ask("✂ Which part of the video? Send it like <code>10:00-25:00</code> (or <code>1:02:00-1:30:00</code>).",
            "wiz_range", {"w": wid}, placeholder="10:00-25:00")


@step("wiz_range")
def _wiz_range_step(ctx: Ctx, text: str, p: dict) -> None:
    wid, w = _wiz(p)
    m = re.fullmatch(r"\s*([\d:.]+)\s*[-–to ]+\s*([\d:.]+)\s*", text)
    if not m:
        ctx.ask("That didn't look like <code>10:00-25:00</code>. Try again:", "wiz_range", {"w": wid}, placeholder="10:00-25:00")
        return
    a, b = parse_clock(m.group(1)), parse_clock(m.group(2))
    if b - a < 60:
        ctx.ask("The part must be at least a minute long. Try again:", "wiz_range", {"w": wid}, placeholder="10:00-25:00")
        return
    w["section"] = [a, b]
    st.kv_set(f"wiz:{wid}", w, dt.timedelta(days=1))
    ctx.edit(_wizard_text(w), _wizard_buttons(ctx, wid, w), message_id=w.get("msg"))
    ctx.send(f"✂ Part set to {fmt_dur(a)}–{fmt_dur(b)}. Tap Start above.")


@callback("wiz.cancel")
def _wiz_cancel(ctx: Ctx, p: dict) -> None:
    st.kv_del(f"wiz:{p.get('w', '')}")
    ctx.edit("✖ Cancelled.")


@callback("wiz.start")
def _wiz_start(ctx: Ctx, p: dict) -> None:
    ctx.require_owner()
    wid, w = _wiz(p)
    st.kv_del(f"wiz:{wid}")
    settings: dict = {}
    if w.get("clips"):
        settings["clips"] = int(w["clips"])
    if w.get("provider"):
        settings["provider"] = w["provider"]
    if w.get("layout"):
        settings["layout"] = w["layout"]
    if w.get("script"):
        settings["caption_script"] = w["script"]
    if w.get("section"):
        settings["section"] = tuple(w["section"])
    conv = st.get_conversation(ctx.chat_id)
    if conv and conv[0] == "wiz_transcript":
        st.clear_conversation(ctx.chat_id)
    body = job_svc.JobCreate(url=w.get("url"), upload_id=w.get("upload_id"), settings=job_svc.JobSettings(**settings),
                             transcript=w.get("transcript"))
    job = job_svc.create_job(body, remote=True)
    set_current_job(ctx.chat_id, job["id"])
    track(job["id"], ctx.chat_id, ctx.message_id or w.get("msg"))
    ctx.edit(progress_text(db.get_job(job["id"])), [[ctx.btn("⏹ Cancel job", "job.cancel", j=job["id"])]])  # type: ignore[arg-type]
    ctx.toast(f"Job {job['id']} started")


# ============================================================================ files sent to the bot
def handle_file(ctx: Ctx, msg: dict) -> bool:
    if handle_transcript_file(ctx, msg):
        return True
    f = msg.get("video") or msg.get("audio") or msg.get("voice") or msg.get("video_note")
    doc = msg.get("document")
    if not f and doc and str(doc.get("mime_type", "")).startswith(("video/", "audio/")):
        f = doc
    if not f:
        return False
    ctx.require_owner()
    api = ctx.api
    size = int(f.get("file_size") or 0)
    name = Path(f.get("file_name") or f"telegram_{f.get('file_unique_id', 'video')}.mp4").name
    if size > api.download_limit:
        mb = api.download_limit // (1024 * 1024)
        ctx.send(f"📦 That file is {size / 1048576:.0f} MB — bots can only download {mb} MB from Telegram's servers.\n"
                 "Options:\n• send the YouTube link instead\n• upload it in the ClipForge app (phone browser, any size)\n"
                 "• run a local Bot API server (2 GB files) — see docs/TELEGRAM.md")
        return True
    info = api.call("getFile", file_id=f["file_id"])
    uid = job_svc.new_upload(name, size or 0)
    dest = job_svc.upload_path(uid)
    api.download(info["file_path"], dest)
    real = dest.stat().st_size
    if not size:  # Telegram didn't tell us the size: record what arrived
        import json

        meta = dest.with_suffix(".json")
        m = json.loads(meta.read_text())
        m["size"] = real
        meta.write_text(json.dumps(m))
    job_svc.finish_upload(uid)
    start_wizard(ctx, upload_id=uid, title=name)
    return True


# ============================================================================ live progress (one message per job, edited)
def track(job_id: int, chat_id: int, message_id: int | None) -> None:
    if message_id:
        st.kv_set(f"prog:{job_id}", {"chat": chat_id, "msg": message_id, "text": "", "at": 0}, dt.timedelta(days=3))


def progress_text(job: db.Job | None) -> str:
    if not job:
        return "Job not found."
    title = esc(job.title or job.source[:60])
    head = f"🎬 <b>Job {job.id}</b> · {title}"
    where = job_svc.where_running(job.id)  # type: ignore[arg-type]
    if job.status == "queued":
        return f"{head}\n⏳ Waiting to start…\n📍 {esc(where)}"
    stage = job.stage or "download"
    idx = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else 0
    marks = " ".join(("✅" if i < idx else "▶" if i == idx else "·") for i in range(len(STAGE_ORDER)))
    pct = job.progress or 0
    eta = f" · ~{fmt_dur(job.eta_sec)} left" if job.eta_sec else ""
    return (f"{head}\n▶ {STAGE_LABEL.get(stage, stage)} {bar(pct)} {round(pct * 100)}%{eta}\n{marks}\n"
            f"📍 {esc(where)}{transcript_line(job)}")


def transcript_line(job: db.Job) -> str:
    """'📝 Transcript: pasted · ~52 min saved' once the transcribe stage ran."""
    rep = job_svc.transcript_status(job)
    if not rep:
        return ""
    saved = f" · ~{round(rep['saved_sec'] / 60)} min saved" if rep.get("saved_sec", 0) >= 60 else ""
    warn = " · ⚠️ check it" if rep.get("warnings") and rep.get("source") != "whisper" else ""
    return f"\n📝 Transcript: {esc(rep.get('label', rep.get('source', '')))}{saved}{warn}"


@callback("job.whisper")
def _job_whisper(ctx: Ctx, p: dict) -> None:
    ctx.require_owner()
    job_svc.use_whisper(int(p["j"]))
    ctx.edit(f"🎙 Job {p['j']}: transcribing with Whisper instead — the job starts again from the transcript.")


def final_text(job: db.Job) -> tuple[str, str]:
    """(text, kind) for a finished job."""
    if job.status == "done":
        return f"✅ <b>Job {job.id} done</b> · {esc(job.title)}\n{job.n_clips} clips ready.", "done"
    if job.status == "cancelled":
        return f"⏹ Job {job.id} cancelled.", "cancelled"
    text, _ = explain_failure(job.error)
    return f"❌ <b>Job {job.id} failed</b> · {esc(job.title or job.source[:60])}\n{text}", "failed"


def explain_failure(error: str) -> tuple[str, list[str]]:
    """Human sentence + fix actions. Never a stack trace."""
    e = (error or "").lower()
    if "confirm you" in e and "bot" in e or "sign in to confirm" in e or "bot-check" in e:
        return ("YouTube asked for a sign-in check (it does that to cloud servers). Send me the video file, or let the "
                "laptop download it when it's on.", ["retry", "wait_laptop"])
    if "no space" in e or "disk" in e and "full" in e:
        return "The disk is full. Free some space (/server shows it), then retry.", ["retry"]
    if "ollama" in e and ("connect" in e or "refused" in e or "running" in e):
        return "Ollama isn't running on the computer. Start it, then retry.", ["retry"]
    if "interrupted" in e:
        return "The computer restarted while it ran. Retry — finished steps are reused.", ["retry"]
    if "private video" in e or "unavailable" in e:
        return "That video is private or unavailable.", []
    return esc(scrub(error or "Unknown error")[:400]), ["retry"]


def tick_progress(bot) -> None:
    """Called every ~2 s by the bot: edit progress messages (throttled, only when the text changed)."""
    every = load_config().telegram.progress_every_sec
    for key, rec in st.kv_prefix("prog:").items():
        job = db.get_job(int(key.split(":", 1)[1]))
        if not job:
            st.kv_del(key)
            continue
        ctx = Ctx(bot, rec["chat"], rec["chat"], "owner", message_id=rec["msg"])
        try:
            if job.status in ("done", "failed", "cancelled"):
                text, kind = final_text(job)
                buttons = ([[ctx.btn("🎬 Clips", "clips.page", j=job.id, f="all", p=0),
                             ctx.btn("✅ Approve all ≥ 7", "job.approve_above", j=job.id, s=7)]] if kind == "done"
                           else [[ctx.btn("🔁 Retry", "job.retry", j=job.id)]])
                ctx.edit(text, buttons)
                st.kv_del(key)
                continue
            text = progress_text(job)
            if text != rec["text"] and time.time() - rec["at"] >= every:
                ctx.edit(text, [[ctx.btn("⏹ Cancel job", "job.cancel", j=job.id)]])
                rec["text"], rec["at"] = text, time.time()
                st.kv_set(key, rec, dt.timedelta(days=3))
        except Exception as exc:  # a deleted message must not stop the other updates
            from clipforge.utils.log import log

            log.debug(f"[telegram] progress edit failed: {exc}")
            if "not found" in str(exc).lower():
                st.kv_del(key)


# ============================================================================ commands
@command("new", group="Jobs", help="start a job from a YouTube link", usage="/new https://youtu.be/…")
def _new(ctx: Ctx, args: str) -> None:
    m = URL_RE.search(args)
    if not m:
        ctx.send("Send it like <code>/new https://youtu.be/…</code> — or just paste the link, or send me the video file.")
        return
    start_wizard(ctx, url=m.group(0))


def job_line(j: db.Job) -> str:
    icon = {"done": "✅", "failed": "❌", "running": "▶", "queued": "⏳", "cancelled": "⏹"}.get(j.status, "•")
    extra = f" · {j.n_clips} clips" if j.n_clips else f" · {j.stage} {round((j.progress or 0) * 100)}%" if j.status == "running" else ""
    return f"{icon} <b>#{j.id}</b> {esc((j.title or j.source)[:50])}{extra}"


def jobs_page(ctx: Ctx, page: int) -> tuple[str, list[list[dict]]]:
    jobs = db.list_jobs()
    pages = max(1, (len(jobs) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = jobs[page * PAGE:(page + 1) * PAGE]
    if not jobs:
        return "No jobs yet — paste a YouTube link to start one.", []
    text = f"📋 <b>Jobs</b> (page {page + 1}/{pages})\n" + "\n".join(job_line(j) for j in chunk)
    rows = [[ctx.btn(f"#{j.id} {(j.title or j.source)[:28]}", "job.view", j=j.id)] for j in chunk]
    nav = []
    if page > 0:
        nav.append(ctx.btn("◀", "jobs.page", p=page - 1))
    if page < pages - 1:
        nav.append(ctx.btn("▶", "jobs.page", p=page + 1))
    if nav:
        rows.append(nav)
    return text, rows


@command("jobs", role="viewer", group="Jobs", help="all jobs (pages)")
def _jobs(ctx: Ctx, args: str) -> None:
    ctx.send(*jobs_page(ctx, 0))


@callback("jobs.page", role="viewer")
def _jobs_page(ctx: Ctx, p: dict) -> None:
    ctx.edit(*jobs_page(ctx, int(p.get("p", 0))))


def job_view(ctx: Ctx, job_id: int) -> tuple[str, list[list[dict]]]:
    job = job_svc.job_or_404(job_id)
    set_current_job(ctx.chat_id, job_id)
    if job.status in ("running", "queued"):
        text = progress_text(job)
    elif job.status == "done":
        text, _ = final_text(job)
    else:
        text, _ = final_text(job)
    text += f"\n<i>created {job.created_at:%d %b %H:%M} UTC</i>"
    rows = [[ctx.btn("🎬 Clips", "clips.page", j=job_id, f="all", p=0)]] if job.n_clips else []
    if ctx.owner:
        act = []
        if job.status in ("running", "queued"):
            act.append(ctx.btn("⏹ Cancel", "job.cancel", j=job_id))
        else:
            act.append(ctx.btn("🔁 Retry", "job.retry", j=job_id))
            act.append(ctx.btn("♻ Re-run a step", "job.rerun_menu", j=job_id))
        act.append(ctx.btn("🗑 Delete", "job.delete_ask", j=job_id))
        rows.append(act)
    return text, rows


@command("job", role="viewer", group="Jobs", help="one job: progress, clips, actions", usage="/job 12")
def _job(ctx: Ctx, args: str) -> None:
    ctx.send(*job_view(ctx, job_arg(ctx, args.strip() or None)))


@callback("job.view", role="viewer")
def _job_view(ctx: Ctx, p: dict) -> None:
    ctx.send(*job_view(ctx, int(p["j"])))


@command("cancel", role="viewer", group="Jobs", help="stop what I'm waiting for — or /cancel 12 to cancel job 12",
         usage="/cancel 12")
def _cancel(ctx: Ctx, args: str) -> None:
    if not args.strip():
        ctx.send("✖ Cancelled." if st.clear_conversation(ctx.chat_id) else "Nothing to cancel.")
        return
    ctx.require_owner()
    jid = job_arg(ctx, args.strip())
    job_svc.cancel_job(jid)
    ctx.send(f"⏹ Cancelling job {jid}…")


@callback("job.cancel")
def _job_cancel(ctx: Ctx, p: dict) -> None:
    job_svc.cancel_job(int(p["j"]))
    ctx.toast("Cancelling…")


@command("retry", group="Jobs", help="run a failed/cancelled job again (finished steps are reused)", usage="/retry 12")
def _retry(ctx: Ctx, args: str) -> None:
    jid = job_arg(ctx, args.strip() or None)
    _do_retry(ctx, jid)


def _do_retry(ctx: Ctx, jid: int) -> None:
    job_svc.retry_job(jid)
    mid = ctx.send(progress_text(db.get_job(jid)), [[ctx.btn("⏹ Cancel job", "job.cancel", j=jid)]])
    track(jid, ctx.chat_id, mid)


@callback("job.retry")
def _job_retry(ctx: Ctx, p: dict) -> None:
    _do_retry(ctx, int(p["j"]))


@command("rerun", group="Jobs", help="redo one step and everything after it", usage="/rerun 12 select")
def _rerun(ctx: Ctx, args: str) -> None:
    parts = args.split()
    if len(parts) != 2:
        ctx.send("Usage: <code>/rerun 12 select</code> — steps: " + ", ".join(job_svc.RERUN_STAGES))
        return
    jid = job_arg(ctx, parts[0])
    job_svc.rerun_stage(jid, parts[1])
    mid = ctx.send(progress_text(db.get_job(jid)), [[ctx.btn("⏹ Cancel job", "job.cancel", j=jid)]])
    track(jid, ctx.chat_id, mid)


@callback("job.rerun_menu")
def _rerun_menu(ctx: Ctx, p: dict) -> None:
    jid = int(p["j"])
    rows = [[ctx.btn(s, "job.rerun", j=jid, s=s) for s in job_svc.RERUN_STAGES[:3]],
            [ctx.btn(s, "job.rerun", j=jid, s=s) for s in job_svc.RERUN_STAGES[3:]]]
    ctx.send(f"♻ Re-run which step of job {jid}? (that step and everything after it run again)", rows)


@callback("job.rerun")
def _rerun_cb(ctx: Ctx, p: dict) -> None:
    jid = int(p["j"])
    job_svc.rerun_stage(jid, p["s"])
    mid = ctx.send(progress_text(db.get_job(jid)), [[ctx.btn("⏹ Cancel job", "job.cancel", j=jid)]])
    track(jid, ctx.chat_id, mid)


@command("delete", group="Jobs", help="delete a job and its files (asks first)", usage="/delete 12")
def _delete(ctx: Ctx, args: str) -> None:
    jid = job_arg(ctx, args.strip() or None)
    ctx.ask_danger("job.delete", j=jid)


@callback("job.delete_ask")
def _delete_ask(ctx: Ctx, p: dict) -> None:
    ctx.ask_danger("job.delete", j=int(p["j"]))


def _job_title(p: dict) -> str:
    j = db.get_job(int(p["j"]))
    return f"job {p['j']}" + (f" ({j.title[:40]})" if j and j.title else "")


@danger("job.delete", lambda p: f"Delete {_job_title(p)} and its files? This can't be undone.")
def _delete_run(ctx: Ctx, p: dict) -> str:
    res = job_svc.delete_job(int(p["j"]), delete_files=True)
    msg = f"🗑 Deleted job {p['j']}"
    if res["freed_bytes"]:
        msg += f" — freed {fmt_bytes(res['freed_bytes'])}."
    elif res["kept_files_because"]:
        msg += f". Files kept: {res['kept_files_because']}."
    return msg
