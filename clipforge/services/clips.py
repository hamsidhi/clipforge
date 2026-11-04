"""Clips: read, edit (title/hook/style/layout/trim/words), approve, re-render, undo.

Every write goes through `pipeline.candidates_lock` + candidates.json — the same file the CLI and the web editor use.
"""

from __future__ import annotations

import json
import shutil
from typing import Literal, Optional

from pydantic import BaseModel

from clipforge import pipeline
from clipforge.api import db
from clipforge.api.jobs import job_config, manager
from clipforge.models import CaptionStyle, ClipCandidate, CropOverride
from clipforge.services.errors import BadRequest, NotFound
from clipforge.services.jobs import clip_out, find_cand, job_or_404, metas, parse_clip_id, ws_for
from clipforge.utils.log import log

PAD_START, PAD_END = 0.15, 0.3  # same padding the selection's refine step uses


class ClipPatch(BaseModel):
    start: Optional[float] = None
    end: Optional[float] = None
    title: Optional[str] = None
    hook: Optional[str] = None
    approved: Optional[bool] = None
    enabled: Optional[bool] = None
    layout: Optional[Literal["auto", "track", "split", "blur"]] = None
    clear_layout: bool = False
    caption_style: Optional[CaptionStyle] = None
    word_edits: Optional[dict[str, str]] = None
    crop_overrides: Optional[list[CropOverride]] = None
    snap: bool = True


# ============================================================================ read
def list_clips(job_id: int, which: str = "all") -> list[dict]:
    """which = all | approved | rejected | pending."""
    job = job_or_404(job_id)
    if not job.video_id or not (job_config({}).workspace / job.video_id / "candidates.json").exists():
        return []
    ws = ws_for(job)
    m = metas(ws)
    out = [clip_out(job, ws, c, m) for c in pipeline.load_candidates(ws)]
    if which == "approved":
        out = [c for c in out if c["approved"] is True]
    elif which == "rejected":
        out = [c for c in out if c["approved"] is False]
    elif which == "pending":
        out = [c for c in out if c["approved"] is None and c["enabled"]]
    return out


def clip_words(job: db.Job, ws, c: ClipCandidate) -> tuple[str, list[dict]]:
    from clipforge.stages.captions import resolve_style
    from clipforge.utils.timecode import words_in_range

    cfg = job_config(job.settings)
    script = resolve_style(cfg.captions, c.caption_style).script
    tr = pipeline.load_transcript(ws)
    inside = words_in_range(tr.all_words(), c.start, c.end)
    return script, [{"index": i, "start": w.start, "end": w.end, "text": w.display(script),
                     "edited": c.word_edits.get(str(i))} for i, w in enumerate(inside)]


def get_clip(clip_id: str) -> dict:
    job, rank = parse_clip_id(clip_id)
    ws = ws_for(job)
    _, c = find_cand(ws, rank)
    out = clip_out(job, ws, c, metas(ws))
    out["script"], out["words"] = clip_words(job, ws, c)
    info = pipeline.load_info(ws)
    out["source"] = {"duration": info.duration, "width": info.width, "height": info.height, "title": info.title}
    plan_file = ws.clips_dir / "_work" / f"{rank:02d}" / "crop_plan.json"
    out["crop_plan"] = json.loads(plan_file.read_text(encoding="utf-8")) if plan_file.exists() else None
    return out


# ============================================================================ edit
def _patch_locked(clip_id: str, body: ClipPatch) -> dict:
    from clipforge.utils.timecode import snap_to_words, words_in_range

    job, rank = parse_clip_id(clip_id)
    ws = ws_for(job)
    cands, c = find_cand(ws, rank)
    data = body.model_dump(exclude_unset=True, exclude={"snap", "clear_layout"})
    if data.get("start", c.start) == c.start and data.get("end", c.end) == c.end:
        data.pop("start", None)  # unchanged times: don't re-snap (that could drop a word and reset edits)
        data.pop("end", None)
    if "start" in data or "end" in data:
        start = data.get("start", c.start)
        end = data.get("end", c.end)
        info = pipeline.load_info(ws)
        start, end = max(0.0, start), min(info.duration, end)
        if end - start < 3:
            raise BadRequest("A clip must be at least 3 seconds long")
        if body.snap:
            tr = pipeline.load_transcript(ws)
            starts, ends = tr.boundaries()
            start, end, _ = snap_to_words(tr.all_words(), start, end, pad_start=PAD_START, pad_end=PAD_END, search=1.5,
                                          seg_starts=starts, seg_ends=ends)
        if (start, end) != (c.start, c.end):
            # word edits are keyed by index inside the clip: carry them over by word start time
            all_words = pipeline.load_transcript(ws).all_words()
            edits = data.pop("word_edits", None) or c.word_edits
            old = words_in_range(all_words, c.start, c.end)
            by_time = {old[int(k)].start: v for k, v in edits.items() if k.isdigit() and int(k) < len(old)}
            new = words_in_range(all_words, start, end)
            c.word_edits = {str(i): by_time[w.start] for i, w in enumerate(new) if w.start in by_time}
            c.crop_overrides = []  # shot indices change with the range
        c.start, c.end = round(start, 3), round(end, 3)
        data.pop("start", None)
        data.pop("end", None)
    for k, v in data.items():
        if k == "caption_style" and v is not None:
            c.caption_style = CaptionStyle.model_validate(v)
        elif k == "crop_overrides" and v is not None:
            c.crop_overrides = [CropOverride.model_validate(o) for o in v]
        elif v is not None or k == "approved":  # approved=null means "back to review"
            setattr(c, k, v)
    if body.clear_layout:
        c.layout = None
    pipeline.save_candidates(ws, cands)
    return clip_out(job, ws, c, metas(ws))


def patch_clip(clip_id: str, body: ClipPatch) -> dict:
    job, _ = parse_clip_id(clip_id)
    with pipeline.candidates_lock(job.video_id or ""):  # serialise read-modify-write of candidates.json
        return _patch_locked(clip_id, body)


def set_text(clip_id: str, field: Literal["title", "hook"], value: str) -> dict:
    value = " ".join(value.split())
    if field == "title" and not 3 <= len(value) <= 100:
        raise BadRequest("A title needs 3–100 characters.")
    if field == "hook" and len(value) > 120:
        raise BadRequest("Keep the hook under 120 characters.")
    return patch_clip(clip_id, ClipPatch(**{field: value}))


def set_preset(clip_id: str, preset: str | None) -> dict:
    from clipforge.stages.captions import PRESETS

    if preset and preset not in PRESETS and not _custom_preset(preset):
        raise BadRequest(f"Unknown caption style '{preset}'.")
    job, rank = parse_clip_id(clip_id)
    _, c = find_cand(ws_for(job), rank)
    style = c.caption_style.model_copy(update={"preset": preset})
    return patch_clip(clip_id, ClipPatch(caption_style=style))


def _custom_preset(key: str) -> bool:
    from sqlmodel import select

    with db.session() as s:
        return s.exec(select(db.Preset).where(db.Preset.key == key)).first() is not None


def set_script(clip_id: str, script: Literal["roman", "devanagari"]) -> dict:
    """Caption script = the Devanagari preset or back to the default style."""
    return set_preset(clip_id, "hindi-devanagari" if script == "devanagari" else None)


def set_layout(clip_id: str, layout: str) -> dict:
    if layout not in ("auto", "track", "split", "blur"):
        raise BadRequest("Layout must be auto, track, split or blur.")
    return patch_clip(clip_id, ClipPatch(layout=layout))  # type: ignore[arg-type]


def set_approved(job_id: int, ranks: list[int], approved: bool | None) -> int:
    job = job_or_404(job_id)
    ws = ws_for(job)
    n = 0
    with pipeline.candidates_lock(ws.video_id):
        cands = pipeline.load_candidates(ws)
        for c in cands:
            if c.rank in ranks:
                c.approved = approved
                n += 1
        pipeline.save_candidates(ws, cands)
    return n


def approve_above(job_id: int, min_score: float, approve: bool = True) -> int:
    ws = ws_for(job_or_404(job_id))
    with pipeline.candidates_lock(ws.video_id):
        cands = pipeline.load_candidates(ws)
        n = 0
        for c in cands:
            if c.score >= min_score:
                c.approved = approve
                n += 1
        pipeline.save_candidates(ws, cands)
    return n


# ============================================================================ trim
def _edges(clip_id: str):
    job, rank = parse_clip_id(clip_id)
    ws = ws_for(job)
    _, c = find_cand(ws, rank)
    words = pipeline.load_transcript(ws).all_words()
    return job, ws, c, words


def nudge(clip_id: str, edge: Literal["start", "end"], delta: float) -> dict:
    """Move one edge by about `delta` seconds, always landing on a word boundary — and never a no-op:
    if the nearest boundary is the current one, take the next word in that direction."""
    _, _, c, words = _edges(clip_id)
    if not words:
        raise BadRequest("No transcript words — can't trim on word boundaries.")
    if edge == "start":
        cur = c.start + PAD_START
        bounds = sorted({w.start for w in words})
        pad = -PAD_START
    else:
        cur = c.end - PAD_END
        bounds = sorted({w.end for w in words})
        pad = PAD_END
    target = cur + delta
    ahead = [b for b in bounds if (b > cur + 0.01 if delta > 0 else b < cur - 0.01)]
    if not ahead:
        raise BadRequest("Can't move further — that's the edge of the episode.")
    best = min(ahead, key=lambda b: abs(b - target))
    new = round(best + pad, 3)
    start, end = (new, c.end) if edge == "start" else (c.start, new)
    out = patch_clip(clip_id, ClipPatch(start=start, end=end, snap=False))
    return {**out, **edge_words(clip_id)}


def trim(clip_id: str, start: float, end: float, relative: bool = True) -> dict:
    """relative=True: times are offsets inside the current clip (what you see in the preview)."""
    _, _, c, _ = _edges(clip_id)
    if relative:
        start, end = c.start + start, c.start + end
    if end <= start:
        raise BadRequest("The end must be after the start.")
    out = patch_clip(clip_id, ClipPatch(start=start, end=end, snap=True))
    return {**out, **edge_words(clip_id)}


def edge_words(clip_id: str, n: int = 4) -> dict:
    """First / last words of the clip, so you can see the cut is clean."""
    job, rank = parse_clip_id(clip_id)
    ws = ws_for(job)
    _, c = find_cand(ws, rank)
    _, words = clip_words(job, ws, c)
    texts = [w["edited"] or w["text"] for w in words]
    return {"first_words": " ".join(texts[:n]), "last_words": " ".join(texts[-n:]), "n_words": len(texts)}


# ============================================================================ caption words
def word_lines(clip_id: str, per_line: int = 8) -> list[list[dict]]:
    job, rank = parse_clip_id(clip_id)
    ws = ws_for(job)
    _, c = find_cand(ws, rank)
    _, words = clip_words(job, ws, c)
    return [words[i:i + per_line] for i in range(0, len(words), per_line)]


def set_word(clip_id: str, old: str | int, new: str) -> dict:
    """Replace a caption word: by its number (from word_lines) or by its text (every match)."""
    new = new.strip()
    if not new or len(new) > 40 or " " in new.strip() and len(new.split()) > 3:
        raise BadRequest("The new word must be 1–40 characters (at most 3 words).")
    job, rank = parse_clip_id(clip_id)
    ws = ws_for(job)
    with pipeline.candidates_lock(ws.video_id):
        cands, c = find_cand(ws, rank)
        _, words = clip_words(job, ws, c)
        if isinstance(old, int):
            if not 0 <= old < len(words):
                raise NotFound(f"There is no word #{old} — the clip has {len(words)} words.")
            hits = [old]
        else:
            key = old.strip().lower()
            hits = [w["index"] for w in words if (w["edited"] or w["text"]).strip(".,!?").lower() == key]
            if not hits:
                raise NotFound(f"'{old}' isn't in the captions of this clip. Send /words to see them.")
        edits = dict(c.word_edits)
        for i in hits:
            edits[str(i)] = new
        c.word_edits = edits
        pipeline.save_candidates(ws, cands)
        return {**clip_out(job, ws, c, metas(ws)), "changed": len(hits)}


# ============================================================================ render / undo
def render(job_id: int, ranks: list[int] | None = None, force: bool = False, only_approved: bool = False,
           only_stale: bool = False) -> list[int] | str:
    job = job_or_404(job_id)
    ws = ws_for(job)
    cands = pipeline.load_candidates(ws)
    if only_approved:
        ranks = [c.rank for c in cands if c.approved]
        if not ranks:
            raise BadRequest("No approved clips to render")
    if only_stale:
        m = metas(ws)
        ranks = [c.rank for c in cands if c.enabled and (lambda o: o["stale"] or not o["rendered"])(clip_out(job, ws, c, m))
                 and (ranks is None or c.rank in ranks)]
        if not ranks:
            return []
    manager.submit("render", job_id, {"ranks": ranks, "force": force})
    return ranks or "all"


def render_clip(clip_id: str) -> dict:
    job, rank = parse_clip_id(clip_id)
    find_cand(ws_for(job), rank)
    manager.submit("render", job.id, {"ranks": [rank]})  # type: ignore[arg-type]
    return {"ok": True}


def undo(clip_id: str) -> dict:
    job, rank = parse_clip_id(clip_id)
    with pipeline.candidates_lock(job.video_id or ""):
        return _undo_locked(job, rank)


def _undo_locked(job: db.Job, rank: int) -> dict:
    from clipforge.workspace import safe_replace

    ws = ws_for(job)
    cands, c = find_cand(ws, rank)
    work = ws.work_dir(rank)
    prev_mp4, prev_json = work / "previous.mp4", work / "previous.json"
    if not prev_mp4.exists() or not prev_json.exists():
        raise NotFound("No previous version to restore")
    prev = json.loads(prev_json.read_text(encoding="utf-8"))
    old = ClipCandidate.model_validate(prev["candidate"])
    old.rank, old.approved, old.score = c.rank, c.approved, c.score
    cur = ws.find_clip(rank)
    swap_mp4, swap_json = work / "swap.mp4", work / "swap.json"
    # safe_replace retries: on Windows a file a browser is still streaming can't be renamed for a moment
    if cur:
        safe_replace(cur, swap_mp4)
        shutil.copy2(work / "render.json", swap_json)
    target = ws.clip_path(rank, old.title)
    safe_replace(prev_mp4, target)
    shutil.copy2(prev_json, work / "render.json")
    if swap_mp4.exists():  # current becomes "previous" so undo can be undone
        safe_replace(swap_mp4, prev_mp4)
        safe_replace(swap_json, prev_json)
    else:
        prev_json.unlink(missing_ok=True)
    for stale in work.glob("preview*.mp4"):  # the phone preview belongs to the replaced render
        stale.unlink(missing_ok=True)
    (work / "telegram_fit.mp4").unlink(missing_ok=True)
    idx = cands.index(c)
    cands[idx] = old
    pipeline.save_candidates(ws, cands)
    try:
        pipeline.metadata(ws, job_config(job.settings))
    except Exception as exc:  # metadata refresh is cosmetic here; the restore itself succeeded
        log.warning(f"Metadata refresh after undo failed: {exc}")
    return clip_out(job, ws, old, metas(ws))
