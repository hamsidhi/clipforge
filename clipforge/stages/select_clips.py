"""Stage 4 — ask the LLM for clip candidates, window by window (fits any model's context).

The LLM sees numbered sentences (Whisper segments) and answers with a compact JSON per candidate —
first/last sentence id + three scores + a topic. Ids are mapped back to word times here. Titles and hooks are
written later (`write_titles`) for the final clips only, with each clip's full text.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from clipforge.config import Config, PROMPTS_DIR
from clipforge.llm.base import LLMError, LLMProvider, strict_schema
from clipforge.llm.mock_provider import romanize_word
from clipforge.models import ClipCandidate, LLMCandidate, RawCandidateList, Scores, Segment, Transcript, VideoInfo
from clipforge.utils.ffmpeg import Timer
from clipforge.utils.log import ProgressReporter, check_cancel, log
from clipforge.utils.timecode import words_in_range
from clipforge.workspace import Workspace, fingerprint

SYSTEM = "You are an expert short-form video editor. You always answer with valid JSON only."
TOKENS_PER_CANDIDATE = 56   # {"s":12,"e":25,"score":8,"hook":8,"standalone":7,"topic":"startup funding"} ≈ 35 tokens
TITLE_BATCH = 5
TITLE_TOKENS_PER_CLIP = 110
WORDS_PER_SEC = 2.8   # measured 2.9-3.05 on two Figuring Out episodes (Whisper words / speech time), minus fillers
CHARS_PER_TOKEN = 2.5  # deliberately low (measured 3.55–3.62 for romanized Hinglish on qwen2.5) → never truncates


def _score(v) -> float:
    try:
        return max(0.0, min(10.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


class CompactCandidate(BaseModel):
    """One candidate exactly as the selection prompt asks for it."""

    s: int
    e: int
    score: float = Field(0, ge=0, le=10)
    hook: float = Field(0, ge=0, le=10)
    standalone: float = Field(0, ge=0, le=10)
    topic: str = ""

    @field_validator("score", "hook", "standalone", mode="before")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return _score(v)


class CompactList(BaseModel):
    candidates: list[CompactCandidate]


class CompactRawList(RawCandidateList):
    """The selection reply: lenient per item (one bad candidate doesn't cost the window), but a reply in which NO
    item has the compact shape (e.g. old-style start/end) is invalid, so `complete_json` retries it."""

    @model_validator(mode="after")
    def _some_usable(self) -> "CompactRawList":
        if self.candidates and not parse_compact(self)[0]:
            raise ValueError('no candidate has the required shape {"s": int, "e": int, "score", "hook", '
                             '"standalone", "topic"}')
        return self


class TitleOut(BaseModel):
    id: int
    hook: str = ""
    title: str = ""
    why: str = ""


class TitleList(BaseModel):
    clips: list[TitleOut]


def make_windows(duration: float, window_min: float, overlap_min: float) -> list[tuple[float, float]]:
    """[(0, 480), (420, 900), …] — fixed-size windows with overlap; last window ends at `duration`."""
    size = window_min * 60
    step = max(60.0, size - overlap_min * 60)
    if duration <= size:
        return [(0.0, duration)]
    out = []
    s = 0.0
    while s < duration:
        e = min(duration, s + size)
        out.append((s, e))
        if e >= duration:
            break
        s += step
    # merge a tiny trailing window into the previous one
    if len(out) > 1 and out[-1][1] - out[-1][0] < size / 3:
        last = out.pop()
        out[-1] = (out[-1][0], last[1])
    return out


def segment_text(seg: Segment, latin: bool) -> str:
    if latin and seg.words:
        return " ".join((w.roman or romanize_word(w.text)) for w in seg.words)
    return seg.text


def window_segments(segments: list[Segment], start: float, end: float) -> list[Segment]:
    """Segments overlapping [start, end) that contain text; their 1-based position is the sentence id."""
    return [s for s in segments if not (s.end <= start or s.start >= end) and s.text.strip()]


# ---- LLM-input cleanup (never touches the transcript / captions)
ALWAYS_FILLERS = {"um", "umm", "uh", "uhh", "uhm", "hmm", "hmmm", "erm", "er"}
# immediate repeats of these collapse to one ("haan haan", "like like", "the the"); real Hindi reduplication
# ("dheere dheere", "kabhi kabhi") is NOT in the list, because it carries meaning
REPEAT_COLLAPSE = {
    "haan", "han", "ha", "haa", "like", "so", "toh", "to", "the", "a", "an", "i", "and", "but", "yeah", "yes", "ya",
    "matlab", "ki", "ke", "ka", "main", "mai", "mein", "hai", "is", "it", "it's", "its", "that", "of", "ok", "okay",
    "achha", "acha", "accha", "right", "nahi", "na", "we", "you", "aur", "par", "ye", "yeh", "wo", "woh", "jo",
}
YOU_KNOW_KEEP = {"what", "how", "why", "that", "the", "this", "about", "who", "where", "when", "if"}
# "do you know?", "what you know", "everything you know", "if you know" — the verb, not the filler
YOU_KNOW_VERB_BEFORE = {"do", "does", "did", "don't", "dont", "didn't", "what", "if", "everything", "all", "as",
                        "whatever", "kya", "jo", "now", "would", "should", "will", "can", "could"}
_PUNCT = ".,!?;:…।\"'"


def _bare(tok: str) -> str:
    return tok.strip(_PUNCT).lower()


def strip_fillers(text: str) -> str:
    """Drop filler words and immediate repetitions — for the LLM prompt only (fewer tokens, same meaning):
    um/uh/hmm; "haan haan" -> "haan"; "you know" only when set off by punctuation — "You know, …", "…, you
    know, …", "… you know." — and not after do/what/if/everything… ("do you know?", "everything you know" stay;
    "you know what…" and an unpunctuated "you know Elon?" stay); "matlab" as a filler (line start, after
    punctuation, or before a comma — "iska matlab hai" stays)."""
    toks = text.split()
    out: list[str] = []
    i = 0
    while i < len(toks):
        tok, bare = toks[i], _bare(toks[i])
        prev_ends = not out or out[-1][-1] in _PUNCT
        if bare in ALWAYS_FILLERS:
            i += 1
            continue
        if bare == "you" and i + 1 < len(toks) and _bare(toks[i + 1]) == "know":
            nxt = toks[i + 1]
            verb = bool(out) and _bare(out[-1]) in YOU_KNOW_VERB_BEFORE
            if not verb and nxt[-1] in ",.!…":
                if out and nxt[-1] in ".?!…" and out[-1][-1] not in _PUNCT:
                    out[-1] += nxt[-1]  # keep the sentence end
                i += 2
                continue
        if bare == "matlab" and (prev_ends or tok.endswith(",")):
            i += 1
            continue
        if out and bare and bare in REPEAT_COLLAPSE and _bare(out[-1]) == bare:
            i += 1
            continue
        out.append(tok)
        i += 1
    return " ".join(out)


def window_lines(segs: list[Segment], latin: bool, timestamps: bool = False, clean: bool = True) -> str:
    """`id| text` per sentence (1-based id = position in `segs`). Lines left empty by the cleanup are omitted;
    the other ids don't change."""
    lines = []
    for i, s in enumerate(segs, 1):
        text = segment_text(s, latin)
        if clean:
            text = strip_fillers(text)
        if text:
            lines.append(f"{i}| [{s.start:.1f}-{s.end:.1f}] {text}" if timestamps else f"{i}| {text}")
    return "\n".join(lines)


def seg_start(seg: Segment) -> float:
    return seg.words[0].start if seg.words else seg.start


def seg_end(seg: Segment) -> float:
    return seg.words[-1].end if seg.words else seg.end


def to_candidate(c: CompactCandidate, segs: list[Segment]) -> LLMCandidate | None:
    """Sentence ids (1-based, inclusive) → word times. None for ids outside the window."""
    lo, hi = sorted((c.s, c.e))
    if lo < 1 or hi > len(segs):
        return None
    start, end = seg_start(segs[lo - 1]), seg_end(segs[hi - 1])
    if end <= start:
        return None
    # the overall score stands in for emotion/value/controversy so refine's weighted score keeps its 0-10 scale
    scores = Scores(hook=c.hook, standalone=c.standalone, emotion=c.score, value=c.score, controversy=c.score)
    return LLMCandidate(start=start, end=end, title="", topic=" ".join(c.topic.lower().split()[:4]), scores=scores)


def parse_compact(raw: RawCandidateList) -> tuple[list[CompactCandidate], int]:
    """Validate items one by one (one bad candidate doesn't throw away the window). Returns (valid, n_bad)."""
    out = []
    for item in raw.candidates:
        try:
            out.append(CompactCandidate.model_validate(item))
        except Exception:
            continue
    return out, len(raw.candidates) - len(out)


def load_prompt(name: str = "clip_selection.md") -> str:
    p = PROMPTS_DIR / name
    if not p.exists():
        raise FileNotFoundError(f"Prompt template missing: {p}")
    return p.read_text(encoding="utf-8")


# Reach report 2026-11-09 (REACH_REPORT.md): extra prompt rules for FUTURE clips, behind `reach.hook_rule` /
# `reach.title_rules`. They live in config `reach:` (not in prompts/*.md or `select:`) on purpose: the selection
# fingerprint is unchanged, so re-running an old job never re-selects its clips.
HOOK_RULE = ("EXTRA RULE (data from this channel: about half of all viewers leave in the first seconds): the FIRST "
             "sentence alone must make a stranger stop scrolling - a concrete claim, number, name or conflict. If the "
             "moment needs a setup sentence to make sense, start AFTER the setup or skip the moment. Score `hook` "
             "below 6 for any clip whose first sentence is a question to the guest, a greeting, or 'so/haan/achha'.")
TITLE_RULES = ("EXTRA RULES (data from this channel: YouTube search brings the most views): put the guest's name in "
               "the title when the clip is about their story, opinion or numbers (e.g. 'Sam Altman ...', 'Bryson ...'); "
               "use words people would type into search; no 'Topic: Subtitle' colon pattern; never invent words. "
               "Use ONLY numbers, names and claims that are in the clip text: the examples above are only examples "
               "(nobody in this clip lost 5 crore unless the clip says so).")


def with_reach_rules(template: str, cfg: Config, kind: str) -> str:
    if kind == "select" and cfg.reach.hook_rule and "For each candidate return:" in template:
        return template.replace("For each candidate return:", HOOK_RULE + "\n\nFor each candidate return:", 1)
    if kind == "titles" and cfg.reach.title_rules and "{clips}" in template:
        return template.replace("{clips}", TITLE_RULES + "\n\n{clips}", 1)
    return template


def build_prompt(template: str, info: VideoInfo, transcript_text: str, cfg: Config, start: float, end: float) -> str:
    chapter = next((c.title for c in info.chapters if c.start <= (start + end) / 2 < c.end), "")
    return template.format(
        podcast_title=info.title,
        channel=info.channel or "unknown",
        chapter_line=f"Chapter: {chapter}" if chapter else "",
        max_candidates=cfg.llm.max_candidates_per_window,
        min_sec=int(cfg.clips.min_sec),
        max_sec=int(cfg.clips.max_sec),
        min_words=int(cfg.clips.min_sec * WORDS_PER_SEC),
        max_words=int(cfg.clips.max_sec * WORDS_PER_SEC),
        transcript=transcript_text,
    )


def uses_latin(llm: LLMProvider) -> bool:
    # Latin script costs far fewer tokens than Devanagari on local models; cloud models get the original.
    return llm.name in ("ollama", "mock")


# ---- conservative pre-filter: only windows that clearly can't hold a short
NON_CONTENT = re.compile(
    r"\bsponsor|\buse (?:the |my )?code\b|promo ?code|coupon|discount code|link (?:is )?in (?:the )?description|"
    r"download the app|like and subscribe|subscribe to|hit the bell|thank(?:s| you) for watching|"
    r"see you in the next|welcome (?:back )?to (?:the |our |my )?(?:show|podcast|channel|episode)|"
    r"today'?s guest|my guest today|comment below", re.IGNORECASE)
MIN_WORDS_PER_SEC = 0.5     # normal speech is ~2.8 words/s; below this a window is mostly silence or music
NON_CONTENT_SHARE = 0.6     # share of the window's words in intro/sponsor/outro sentences


def skip_reason(segs: list[Segment], start: float, end: float) -> str | None:
    """Why a window should not go to the LLM — None for every normal window. Never skips for low energy or a
    "boring" topic: only near-silence, or a window made mostly of intro / sponsor / outro sentences."""
    words = sum(len(s.text.split()) for s in segs)
    if end - start >= 60 and words / (end - start) < MIN_WORDS_PER_SEC:
        return f"almost no speech ({words} words in {(end - start) / 60:.0f} min)"
    promo = sum(len(s.text.split()) for s in segs if NON_CONTENT.search(s.text))
    if words and promo / words >= NON_CONTENT_SHARE:
        return "intro / sponsor read / outro"
    return None


def _cache_key(llm: LLMProvider, prompt: str, options: dict) -> str:
    return fingerprint(llm.name, llm.model, llm.temperature, getattr(llm, "seed", None), prompt, options)


def _read_cache(path: Path, model: type[BaseModel]):
    """Cached reply or None (missing, or unreadable — e.g. a run killed mid-write: then it is asked again)."""
    if not path.exists():
        return None
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning(f"Ignoring unreadable LLM cache {path.name}: {exc}")
        return None


def _write_cache(path: Path, obj: BaseModel) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(obj.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, path)


def prompt_ctx(prompts: list[str], num_predict: int, floor: int = 0) -> int:
    """Context size for these prompts: the largest one (≈2.5 chars/token for romanized Hinglish — an over-estimate,
    so nothing is ever truncated) + the reply + the system prompt, rounded up to 1024. One value for the whole
    stage, because a different num_ctx makes Ollama reload the model."""
    # UTF-8 bytes, not characters: Devanagari (cloud providers, word-less segments) is ~3 bytes and ~1 token a char
    need = max((len(p.encode("utf-8")) for p in prompts), default=0) / CHARS_PER_TOKEN + num_predict + 128
    return max(floor, int(-(-need // 1024) * 1024))


def run(ws: Workspace, cfg: Config, llm: LLMProvider, reporter: ProgressReporter, info: VideoInfo,
        transcript: Transcript) -> tuple[list[LLMCandidate], int, int]:
    """Returns (candidates, number of windows that failed and should be retried next run, num_ctx used)."""
    template = with_reach_rules(load_prompt(), cfg, "select")
    latin = uses_latin(llm)
    windows = make_windows(transcript.duration, cfg.llm.window_min, cfg.llm.overlap_min)
    timer = Timer()
    all_cands: list[LLMCandidate] = []
    failed = 0
    predict = TOKENS_PER_CANDIDATE * cfg.llm.max_candidates_per_window + 64
    plan = []
    for i, (a, b) in enumerate(windows):
        segs = window_segments(transcript.segments, a, b)
        if sum(len(s.text.split()) for s in segs) < 30:
            continue
        why = skip_reason(segs, a, b) if cfg.select.prefilter else None
        if why:
            log.info(f"Skipping window {i + 1} ({int(a // 60)}-{int(b // 60)} min): {why}")
            continue
        plan.append((i, a, b, segs, build_prompt(template, info, window_lines(segs, latin), cfg, a, b)))
    num_ctx = prompt_ctx([p for *_, p in plan], predict)
    options = {"num_predict": predict, "num_ctx": num_ctx}
    schema = strict_schema(CompactList)
    schema["properties"]["candidates"]["maxItems"] = cfg.llm.max_candidates_per_window  # fits num_predict
    for i, a, b, segs, prompt in plan:
        check_cancel(reporter)
        cache = ws.select_cache / f"w{i:03d}_{_cache_key(llm, prompt, options)}.json"
        res = _read_cache(cache, CompactRawList)
        if res is None:
            reporter.progress(i / len(windows), timer.eta(i / len(windows)) if i else None,
                              f"LLM window {i + 1}/{len(windows)} ({int(a // 60)}-{int(b // 60)} min)")
            try:
                res = llm.complete_json(prompt, CompactRawList, system=SYSTEM, retries=cfg.llm.max_retries,
                                        task="select", json_schema=schema, options=options)
            except LLMError as exc:
                if "not running" in str(exc) or "not found" in str(exc) or "API_KEY" in str(exc):
                    raise
                log.warning(f"Skipping window {i + 1} ({int(a // 60)}-{int(b // 60)} min) for now: {exc}")
                failed += 1
                continue
            _write_cache(cache, res)
        compact, bad = parse_compact(res)
        if bad:
            log.warning(f"window {i + 1}: ignored {bad} malformed candidate(s)")
        mapped = [to_candidate(c, segs) for c in compact]
        valid = [c for c in mapped if c is not None]
        if len(valid) < len(mapped):  # models sometimes invent ids
            log.debug(f"window {i}: dropped {len(mapped) - len(valid)} candidates with unknown sentence ids")
        all_cands += valid
    reporter.progress(1.0, 0, f"{len(all_cands)} raw candidates")
    ws.write_json(ws.llm_candidates, [json.loads(c.model_dump_json()) for c in all_cands])
    return all_cands, failed, num_ctx


# ----------------------------------------------------------------------------- titles for the final clips
def clip_text(transcript: Transcript, start: float, end: float, latin: bool) -> str:
    ws = words_in_range(transcript.all_words(), start, end)
    return " ".join((w.roman or romanize_word(w.text)) if latin else w.text for w in ws)


def fallback_copy(c: ClipCandidate, text: str) -> tuple[str, str]:
    first = " ".join(text.split()[:7])
    title = (c.topic.title() + ": " if c.topic else "") + first
    return title[:60].rstrip(), first


def write_titles(ws: Workspace, cfg: Config, llm: LLMProvider, reporter: ProgressReporter, info: VideoInfo,
                 transcript: Transcript, cands: list[ClipCandidate], num_ctx: int = 0) -> None:
    """Fill title / hook / why of the final clips (in place), TITLE_BATCH clips per LLM call, each with its full
    text. A failed batch gets a template title (logged) — selection never fails because of copywriting.
    `num_ctx`: the selection's context size, reused when big enough (no model reload)."""
    template = with_reach_rules(load_prompt("clip_titles.md"), cfg, "titles")
    latin = uses_latin(llm)
    todo = [c for c in cands if not c.title]
    batches = []
    for b in range(0, len(todo), TITLE_BATCH):
        batch = todo[b:b + TITLE_BATCH]
        texts = [clip_text(transcript, c.start, c.end, latin) for c in batch]
        body = "\n\n".join(f"CLIP {n} ({c.end - c.start:.0f} s, topic: {c.topic or '-'}):\n\"\"\"{t}\"\"\""
                           for n, (c, t) in enumerate(zip(batch, texts), 1))
        prompt = template.format(podcast_title=info.title, channel=info.channel or "unknown", n=len(batch), clips=body)
        batches.append((b, batch, texts, prompt))
    predict = TITLE_TOKENS_PER_CLIP * TITLE_BATCH + 64
    options = {"num_predict": predict, "num_ctx": prompt_ctx([p for *_, p in batches], predict, floor=num_ctx)}
    for b, batch, texts, prompt in batches:
        check_cancel(reporter)
        cache = ws.select_cache / f"titles_{_cache_key(llm, prompt, options)}.json"
        out: TitleList | None = _read_cache(cache, TitleList)
        if out is None:
            reporter.progress(b / max(1, len(todo)), None, f"Titles {b + 1}-{b + len(batch)} of {len(todo)}")
            try:
                out = llm.complete_json(prompt, TitleList, system=SYSTEM, retries=cfg.llm.max_retries, task="titles",
                                        json_schema=strict_schema(TitleList), options=options)
                _write_cache(cache, out)
            except LLMError as exc:
                if "not running" in str(exc) or "not found" in str(exc) or "API_KEY" in str(exc):
                    raise
                log.warning(f"Title batch {b // TITLE_BATCH + 1} failed, using template titles: {exc}")
        by_id = {t.id: t for t in out.clips} if out else {}
        for n, (c, t) in enumerate(zip(batch, texts), 1):
            got = by_id.get(n)
            fb_title, fb_hook = fallback_copy(c, t)
            c.title = (got.title.strip() if got and got.title.strip() else fb_title)[:60]
            c.hook = got.hook.strip() if got and got.hook.strip() else fb_hook
            c.why = got.why.strip() if got else c.why


# ----------------------------------------------------------------------------- re-rank pass
RERANK_BATCH = 5
RERANK_TOKENS_PER_CLIP = 40


class RerankOut(BaseModel):
    id: int
    score: float = Field(0, ge=0, le=10)
    hook: float = Field(0, ge=0, le=10)
    standalone: float = Field(0, ge=0, le=10)

    @field_validator("score", "hook", "standalone", mode="before")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return _score(v)


class RerankList(BaseModel):
    clips: list[RerankOut]


def rerank(ws: Workspace, cfg: Config, llm: LLMProvider, reporter: ProgressReporter, info: VideoInfo,
           transcript: Transcript, shortlist: list[ClipCandidate], num_ctx: int = 0) -> None:
    """Re-score the shortlist (in place), RERANK_BATCH clips per call, each with `rerank_context_sec` of what is
    said before/after it — so the model can see whether a clip depends on earlier context or stops before its
    payoff. New sub-scores = mean of the window pass and this pass (two independent looks are steadier than
    one); the energy-peak / mid-sentence adjustments refine applied are kept. A failed batch keeps its scores."""
    from clipforge.stages.refine import weighted_score

    template = load_prompt("clip_rerank.md")
    latin = uses_latin(llm)
    ctx_sec = cfg.select.rerank_context_sec
    ordered = sorted(shortlist, key=lambda c: c.start)
    batches = []
    for b in range(0, len(ordered), RERANK_BATCH):
        batch = ordered[b:b + RERANK_BATCH]
        parts = []
        for n, c in enumerate(batch, 1):
            before = clip_text(transcript, max(0.0, c.start - ctx_sec), c.start - 0.01, latin)
            inside = clip_text(transcript, c.start, c.end, latin)
            after = clip_text(transcript, c.end + 0.01, c.end + ctx_sec, latin)
            parts.append(f"CLIP {n} ({c.end - c.start:.0f} s)\nBEFORE: {before or '-'}\nCLIP: \"\"\"{inside}\"\"\"\n"
                         f"AFTER: {after or '-'}")
        prompt = template.format(podcast_title=info.title, channel=info.channel or "unknown", clips="\n\n".join(parts))
        batches.append((b, batch, prompt))
    predict = RERANK_TOKENS_PER_CLIP * RERANK_BATCH + 64
    options = {"num_predict": predict, "num_ctx": prompt_ctx([p for *_, p in batches], predict, floor=num_ctx)}
    w = cfg.scoring_weights
    for b, batch, prompt in batches:
        check_cancel(reporter)
        cache = ws.select_cache / f"rerank_{_cache_key(llm, prompt, options)}.json"
        out: RerankList | None = _read_cache(cache, RerankList)
        if out is None:
            reporter.progress(b / max(1, len(ordered)), None, f"Re-rank {b + 1}-{b + len(batch)} of {len(ordered)}")
            try:
                out = llm.complete_json(prompt, RerankList, system=SYSTEM, retries=cfg.llm.max_retries, task="rerank",
                                        json_schema=strict_schema(RerankList), options=options)
                _write_cache(cache, out)
            except LLMError as exc:
                if "not running" in str(exc) or "not found" in str(exc) or "API_KEY" in str(exc):
                    raise
                log.warning(f"Re-rank batch {b // RERANK_BATCH + 1} failed, keeping the first-pass scores: {exc}")
        by_id = {r.id: r for r in out.clips} if out else {}
        for n, c in enumerate(batch, 1):
            r = by_id.get(n)
            if r is None:
                continue
            bonus = c.score - weighted_score(c.scores, w)  # energy peaks / mid-sentence penalty from refine
            old = c.scores
            c.scores = Scores(hook=(old.hook + r.hook) / 2, standalone=(old.standalone + r.standalone) / 2,
                              emotion=(old.emotion + r.score) / 2, value=(old.value + r.score) / 2,
                              controversy=(old.controversy + r.score) / 2)
            c.score = round(weighted_score(c.scores, w) + bonus, 2)
