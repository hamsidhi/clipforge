"""Telegram chat mode (2026-11-09): write normally, the bot answers. No slash commands needed.

How it works:
1. A few things are answered without any AI (fast, no CPU): "where are the clips / links" → /where.
2. Everything else goes to the local LLM (Ollama, `telegram.chat_model` or `llm.model`) with the list of bot commands
   and a short summary of ClipForge's state (jobs, clips, queue). It answers with JSON: run ONE command (with args),
   or reply in plain words. The bot shows the command it runs ("→ /approve 12 1-3"), so nothing happens silently.
3. Every command keeps its normal checks: viewer chats stay read-only, and dangerous actions (delete, resume an
   account…) still ask Yes → type YES → PIN. The model can't skip that: it can only pick a command.
4. The last 6 exchanges are kept (SQLite, `tgkv`) so "approve those 3" works after "show the clips of job 12".

`telegram.chat: false` turns it off (back to "send a link or /help").
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, field_validator

from clipforge.config import load_config
from clipforge.notify import tg_store as st
from clipforge.notify.tg_api import esc
from clipforge.notify.tg_core import COMMANDS, Ctx, command
from clipforge.utils.log import log

HISTORY = 6
WHERE_RE = re.compile(r"\b(where|kaha+n?|kidhar|link|links|path|folder|location|file)\b", re.I)
CLIP_WORD = re.compile(r"\b(clip|clips|video|videos|reel|reels|short|shorts)\b", re.I)


# ----------------------------------------------------------------------------- /where
@command("where", role="viewer", group="Clips", help="where a job's / clip's files are, with app links",
         usage="/where 12  ·  /where 12-3")
def _where(ctx: Ctx, args: str) -> None:
    from clipforge.notify.commands import app_url
    from clipforge.notify.tg_clips import _clip, clip_ref
    from clipforge.notify.tg_jobs import job_arg
    from clipforge.services import jobs as job_svc

    tok = (args.split() or [""])[0]
    if tok and re.fullmatch(r"#?\d+-\d+", tok):
        cid = clip_ref(ctx, tok)
        c = _clip(cid)
        job, rank = job_svc.parse_clip_id(cid)
        ws = job_svc.ws_for(job)
        f = ws.find_clip(rank)
        url = app_url(f"/clips/{cid}/edit")
        text = (f"<b>#{rank} {esc(c['title'])}</b>\n💻 On the laptop: <code>{esc(str(f) if f else 'not rendered yet')}</code>"
                + (f"\n📱 In the app: {esc(url)}" if url else ""))
        buttons = [[ctx.url_btn("📱 Open in app", url)]] if url else None
        buttons = (buttons or []) + [[ctx.btn("▶ Preview", "clip.preview", c=cid)]]
        ctx.send(text, buttons)
        return
    jid = job_arg(ctx, tok or None)
    ws = job_svc.ws_for(job_svc.job_or_404(jid))
    url = app_url(f"/jobs/{jid}/clips")
    n = len(list(ws.clips_dir.glob("*.mp4"))) if ws.clips_dir.exists() else 0
    text = (f"<b>Job {jid}</b>: {n} rendered clip(s)\n💻 On the laptop: <code>{esc(str(ws.clips_dir))}</code>"
            + (f"\n📱 Gallery: {esc(url)}" if url else "\n📱 App link needs Tailscale (clipforge remote setup)."))
    ctx.send(text, [[ctx.url_btn("📱 Open gallery", url)]] if url else None)


# ----------------------------------------------------------------------------- state summary for the model
def state_summary(ctx: Ctx) -> str:
    from sqlmodel import select

    from clipforge.api import db
    from clipforge.notify.tg_jobs import current_job

    lines = []
    try:
        with db.session() as s:
            jobs = s.exec(select(db.Job).order_by(db.Job.id.desc()).limit(8)).all()
            q = s.exec(select(db.QueueItem)).all()
        for j in jobs:
            lines.append(f"job {j.id}: {j.title[:60]!r} status={j.status} clips={j.n_clips}")
        waiting = {}
        for it in q:
            if it.status in ("queued", "scheduled", "waiting_capacity", "needs_manual", "missed", "failed"):
                waiting[(it.platform, it.status)] = waiting.get((it.platform, it.status), 0) + 1
        lines.append("queue: " + (", ".join(f"{p} {s}={n}" for (p, s), n in sorted(waiting.items())) or "empty"))
        cj = current_job(ctx.chat_id)
        if cj:
            lines.append(f"current job in this chat: {cj}")
    except Exception as exc:
        lines.append(f"(state unavailable: {exc})")
    return "\n".join(lines)


def command_list() -> str:
    out = []
    for c in COMMANDS.values():
        out.append(f"/{c.name} — {c.help}" + (f" (e.g. {c.usage})" if c.usage else ""))
    return "\n".join(out)


class ChatOut(BaseModel):
    action: str = "answer"            # command | answer
    command: str = ""
    args: str = ""
    reply: str = ""

    @field_validator("action", mode="before")
    @classmethod
    def _act(cls, v):
        return "command" if str(v).lower().startswith("comm") else "answer"


PROMPT = """You are the Telegram assistant of ClipForge, a tool on the user's laptop that turns podcasts into short
vertical clips and posts them to YouTube Shorts and Instagram Reels. The user writes casually (English or Hinglish).

Current state:
{state}

Commands you can run (exactly one per message):
{commands}

Recent conversation:
{history}

User: {text}

Decide:
- If a command does what the user wants, answer {{"action":"command","command":"<name without />","args":"<arguments>","reply":"<one short line>"}}.
  Clip ids look like 12-3 (job 12, clip 3); a bare number is a clip of the current job. Use the current job when
  the user doesn't say which. To show where clips are or give links, use "where".
- Otherwise reply in a few friendly sentences: {{"action":"answer","reply":"..."}}. Never invent numbers, links or
  file paths; if you don't know, say which command shows it.
Respond with ONLY the JSON object."""


def _history(chat_id: int) -> list[dict]:
    return st.kv_get(f"chat:{chat_id}:history", []) or []


def _remember(chat_id: int, user: str, bot: str) -> None:
    h = (_history(chat_id) + [{"u": user[:300], "b": bot[:300]}])[-HISTORY:]
    st.kv_set(f"chat:{chat_id}:history", h)


def _llm():
    from clipforge.llm import get_provider

    cfg = load_config()
    llm_cfg = cfg.llm
    if cfg.telegram.chat_model:
        llm_cfg = llm_cfg.model_copy(update={"model": cfg.telegram.chat_model})
    return get_provider(llm_cfg)


def reply(ctx: Ctx, text: str, run_command) -> str:
    """Answer one free-text message. `run_command(ctx, name, args)` is the bot's normal command path (role checks,
    conversations, danger confirmations). Returns a short label for the audit log (never the text itself)."""
    if WHERE_RE.search(text) and CLIP_WORD.search(text):
        clip = re.search(r"\b(\d+-\d+)\b", text)
        job = re.search(r"\bjob\s*#?(\d+)\b", text, re.I)
        args = clip.group(1) if clip else (job.group(1) if job else "")
        ctx.send("→ <code>/where" + (f" {esc(args)}" if args else "") + "</code>", quiet=True)
        run_command(ctx, "where", args)
        _remember(ctx.chat_id, text, f"/where {args}")
        return "chat → /where"
    try:
        ctx.api.call("sendChatAction", chat_id=ctx.chat_id, action="typing")
    except Exception:
        pass
    hist = "\n".join(f"User: {h['u']}\nBot: {h['b']}" for h in _history(ctx.chat_id)) or "(none)"
    prompt = PROMPT.format(state=state_summary(ctx), commands=command_list(), history=hist, text=text[:1000])
    try:
        out = _llm().complete_json(prompt, ChatOut, retries=1, task="chat", options={"num_predict": 220})
    except Exception as exc:
        log.warning(f"[telegram] chat model unavailable: {exc}")
        ctx.send("I couldn't think about that right now (the local AI isn't answering). Try a command: /help lists them, "
                 "e.g. /jobs, /clips, /queue, /where 12.")
        return "chat (llm unavailable)"
    name = (out.command or "").lstrip("/").split()[0].lower() if out.command else ""
    if out.action == "command" and name in COMMANDS:
        line = f"→ <code>/{esc(name)}{(' ' + esc(out.args)) if out.args else ''}</code>"
        if out.reply:
            line = f"{esc(out.reply)}\n{line}"
        ctx.send(line, quiet=True)
        run_command(ctx, name, out.args.strip())
        _remember(ctx.chat_id, text, f"/{name} {out.args}".strip())
        return f"chat → /{name}"
    answer = out.reply.strip() or "I'm not sure what you mean — /help lists everything I can do."
    ctx.send(esc(answer))
    _remember(ctx.chat_id, text, answer)
    return "chat answer"


def describe(o: Any) -> str:  # for tests / debugging
    return json.dumps(o, default=str)
