"""Arc selection engine (2026-11-09, `arcs.engine: arcs`): complete ideas, not punchy lines.

Root causes it fixes (DECISIONS → "Clip quality: root causes"): the old engine took the LLM's short "punchy line"
(median 14 s) and stretched/cut it to 20–59 s, so 17 of 20 Sam Altman clips ended mid-sentence; nothing checked
that an answer was complete; garbled Whisper text became clips.

1. Structure (deterministic): segments → questions → topic blocks (a question after ≥ 25 s, or a chapter start);
   garbled segments (mean word probability < 0.55) are marked.
2. Outline (LLM, cached): one line + an interest score per block. Every judge call sees the whole outline.
3. Candidates = arcs: a Q&A unit per block (question → the full answer, until the next block), long answers split
   into self-contained sub-arcs ending on a thought end; plus the legacy window picks (claims/stories), expanded
   to start at their question and end on a thought end.
4. Judge (LLM, cached, per candidate, outline + ±30 s context): hook, completeness, standalone, value, flow, trend,
   corrected start/end ids, `answer_continues`. If the answer continues (judge OR deterministic check), the end is
   extended (≤ max) and the candidate judged again.
5. Gates: completeness ≥ 7, standalone ≥ 7, ends on a thought end, ≤ 15 % garbled words. Weighted score (profile
   weights) ≥ `arcs.min_quality`; at most one clip per topic block unless both are strong; fewer clips, never padding.

A thought end = the segment ends with . ? ! । OR a pause ≥ 1 s follows OR the next segment is a question.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from statistics import mean

from pydantic import BaseModel, Field, field_validator

from clipforge.config import Config
from clipforge.models import ClipCandidate, LLMCandidate, Scores, Segment, Transcript, VideoInfo
from clipforge.utils.log import ProgressReporter, log

GARBLED_PROB = 0.55
QUESTION_START = re.compile(r"^\s*(what|why|how|when|who|whom|which|where|do you|did you|does|are you|is it|is there|"
                            r"can you|could you|would you|should|have you|tell me|so what|so how|so why)\b", re.I)
QUESTION_HI = re.compile(r"\b(kya|kyun|kyon|kaise|kaisa|kaisi|kaun|kab|kahan|kitna|kitne|kitni|batao|bataiye|"
                         r"bataye|samjhao)\b", re.I)
END_PUNCT = re.compile(r"[.?!।]['\"”’)]*\s*$")
BOILERPLATE = re.compile(r"(thank(s| you)( so much)? for watching|subscribe|like (and|&) (share|comment)|comment (below|down)|"
                         r"link (is )?in (the )?description|brought to you by|sponsor(ed)?|hit the bell|"
                         r"watch (this|the) (episode|video) till the end)", re.I)


# ----------------------------------------------------------------------------- structure
def seg_text(s: Segment) -> str:
    """Latin text when the transcript is romanized (fewer tokens, the 7B model reads it better)."""
    if s.words and any(w.roman for w in s.words):
        return " ".join((w.roman or w.text).strip() for w in s.words).strip()
    return s.text.strip()


def seg_prob(s: Segment) -> float:
    return mean(w.prob for w in s.words) if s.words else 1.0


def is_question(s: Segment) -> bool:
    t = seg_text(s)
    if t.rstrip().endswith("?"):
        return True
    n = len(t.split())
    return n <= 30 and (bool(QUESTION_START.search(t)) or (n <= 20 and bool(QUESTION_HI.search(t))))


@dataclass
class Structure:
    segs: list[Segment]
    questions: list[bool]
    garbled: list[bool]
    blocks: list[tuple[int, int]]          # (first seg idx, last seg idx)
    block_of: list[int] = field(default_factory=list)

    def start(self, i: int) -> float:
        s = self.segs[i]
        return s.words[0].start if s.words else s.start

    def end(self, i: int) -> float:
        s = self.segs[i]
        return s.words[-1].end if s.words else s.end

    def ends_thought(self, i: int) -> bool:
        if i >= len(self.segs) - 1:
            return True
        if END_PUNCT.search(seg_text(self.segs[i])):
            return True
        if self.start(i + 1) - self.end(i) >= 1.0:
            return True
        return self.questions[i + 1]

    def span(self, a: int, b: int) -> float:
        return self.end(b) - self.start(a)

    def garbled_ratio(self, a: int, b: int) -> float:
        words = [w for s in self.segs[a:b + 1] for w in s.words]
        return sum(1 for w in words if w.prob < GARBLED_PROB) / len(words) if words else 0.0


def build_structure(tr: Transcript, info: VideoInfo, min_gap: float = 25.0) -> Structure:
    segs = [s for s in tr.segments if s.text.strip()]
    qs = [is_question(s) for s in segs]
    garbled = [(bool(s.words) and seg_prob(s) < GARBLED_PROB) or bool(BOILERPLATE.search(seg_text(s))) for s in segs]
    st = Structure(segs, qs, garbled, [])
    chapter_starts = sorted(c.start for c in info.chapters)
    starts = [0]
    for i in range(1, len(segs)):
        t = st.start(i)
        new_chapter = any(st.start(i - 1) < c <= t for c in chapter_starts)
        if (qs[i] and not qs[i - 1] and t - st.start(starts[-1]) >= min_gap) or new_chapter:
            starts.append(i)
    st.blocks = [(a, (starts[k + 1] - 1) if k + 1 < len(starts) else len(segs) - 1) for k, a in enumerate(starts)]
    st.block_of = [0] * len(segs)
    for k, (a, b) in enumerate(st.blocks):
        for i in range(a, b + 1):
            st.block_of[i] = k
    return st


# ----------------------------------------------------------------------------- outline
class OutlineItem(BaseModel):
    id: int
    summary: str = ""
    interest: float = Field(5, ge=0, le=10)

    @field_validator("interest", mode="before")
    @classmethod
    def _clamp(cls, v):
        try:
            return max(0.0, min(10.0, float(v)))
        except (TypeError, ValueError):
            return 5.0


class OutlineList(BaseModel):
    blocks: list[OutlineItem]


OUTLINE_PROMPT = """You are summarising a podcast episode so a video editor can find the best short clips.
Podcast: {title}
Channel: {channel}

Below are consecutive parts ("blocks") of the conversation. For EACH block give:
- "summary": one line (max 15 words) — what is asked / said, concretely (names, numbers, claims).
- "interest": 0-10 — how likely this block holds a complete, valuable short for a general audience
  (insight, story, strong opinion, surprising fact). Small talk, intros, ads, unclear audio: 0-3.

{blocks}

Respond with ONLY this JSON: {{"blocks":[{{"id":1,"summary":"...","interest":7}}]}}"""


def _key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]


def outline(ws, st: Structure, info: VideoInfo, llm, cfg: Config, reporter: ProgressReporter) -> dict[int, OutlineItem]:
    from clipforge.stages.select_clips import _read_cache, _write_cache

    out: dict[int, OutlineItem] = {}
    batch = 12
    for b0 in range(0, len(st.blocks), batch):
        part = st.blocks[b0:b0 + batch]
        lines = []
        for k, (a, b) in enumerate(part, b0):
            text = " ".join(seg_text(s) for s in st.segs[a:b + 1])
            words = text.split()
            excerpt = " ".join(words[:70]) + (" … " + " ".join(words[-25:]) if len(words) > 95 else "")
            lines.append(f"BLOCK {k} ({int(st.start(a) // 60)}:{int(st.start(a) % 60):02d}, "
                         f"{st.span(a, b):.0f} s): {excerpt}")
        prompt = OUTLINE_PROMPT.format(title=info.title, channel=info.channel or "unknown", blocks="\n".join(lines))
        cache = ws.select_cache / f"outline_{_key(llm.name, llm.model, prompt)}.json"
        res = _read_cache(cache, OutlineList)
        if res is None:
            reporter.progress(b0 / max(1, len(st.blocks)), None, f"Outline: blocks {b0 + 1}-{b0 + len(part)} of {len(st.blocks)}")
            try:
                res = llm.complete_json(prompt, OutlineList, retries=cfg.llm.max_retries, task="outline",
                                        options={"num_predict": 60 * len(part) + 64})
                _write_cache(cache, res)
            except Exception as exc:  # outline is a help, not a requirement
                log.warning(f"Outline batch {b0 // batch + 1} failed ({exc}); using the question text instead")
                res = OutlineList(blocks=[])
        got = {o.id: o for o in res.blocks}
        for k, (a, b) in enumerate(part, b0):
            out[k] = got.get(k) or OutlineItem(id=k, summary=" ".join(seg_text(st.segs[a]).split()[:15]), interest=5)
    return out


def outline_text(ol: dict[int, OutlineItem], st: Structure) -> str:
    return "\n".join(f"B{k} {int(st.start(a) // 60)}:{int(st.start(a) % 60):02d} — {ol[k].summary}"
                     for k, (a, _b) in enumerate(st.blocks) if k in ol)


# ----------------------------------------------------------------------------- candidates
@dataclass
class Arc:
    a: int
    b: int
    kind: str                 # qa | sub | pick | moment
    prior: float = 5.0
    title: str = ""
    topic: str = ""
    rubric: dict = field(default_factory=dict)
    score: float = 0.0
    why: str = ""
    notes: list[str] = field(default_factory=list)


def normalize(st: Structure, a: int, b: int) -> tuple[int, int]:
    """Start on a sentence start (the clip's question, or right after a thought end — back up to 12 s for the setup,
    else forward), and never end on the NEXT question (drop trailing question segments)."""
    def starts_ok(i: int) -> bool:
        return i == 0 or st.questions[i] or st.ends_thought(i - 1)

    q0 = st.blocks[st.block_of[a]][0]
    if not st.questions[a] and st.questions[q0] and q0 < a and st.start(a) - st.start(q0) <= 25:
        a = q0                              # the answer starts within 25 s of its question: include the question
    if not starts_ok(a):
        back = next((i for i in range(a - 1, -1, -1) if st.start(a) - st.start(i) <= 12 and starts_ok(i)), None)
        if back is not None:
            a = back
        else:
            a = next((i for i in range(a + 1, b) if starts_ok(i)), a)
    while b > a and st.questions[b]:
        b -= 1
    while b > a and not st.ends_thought(b):
        b -= 1
    return a, b


def end_at_thought(st: Structure, a: int, b: int, max_sec: float, limit: int | None = None) -> int | None:
    """Smallest j ≥ b with a thought end, within max_sec of a (and ≤ limit). None if impossible."""
    last = len(st.segs) - 1 if limit is None else limit
    for j in range(b, last + 1):
        if st.span(a, j) > max_sec:
            return None
        if st.ends_thought(j):
            return j
    return None


def last_thought_within(st: Structure, a: int, upto: int, min_sec: float, max_sec: float) -> int | None:
    """Largest j ≤ upto with a thought end and min_sec ≤ span(a, j) ≤ max_sec."""
    for j in range(upto, a - 1, -1):
        sp = st.span(a, j)
        if sp > max_sec:
            continue
        if sp < min_sec:
            return None
        if st.ends_thought(j):
            return j
    return None


def qa_units(st: Structure, min_sec: float, max_sec: float, target_min: float, max_sub: int = 3) -> list[Arc]:
    """A question block → the full answer. Too long → self-contained sub-arcs, each ending on a thought end."""
    out: list[Arc] = []
    for k, (a, b) in enumerate(st.blocks):
        if st.span(a, b) < min_sec:
            continue
        if st.span(a, b) <= max_sec:
            j = b if st.ends_thought(b) else last_thought_within(st, a, b, min_sec, max_sec)
            if j is not None:
                out.append(Arc(a, j, "qa"))
            continue
        start, n = a, 0
        while start <= b and n < max_sub:
            j = last_thought_within(st, start, b, max(min_sec, target_min), max_sec)
            if j is None:
                break
            out.append(Arc(start, j, "qa" if start == a else "sub"))
            n += 1
            start = j + 1
            while start <= b and st.garbled[start]:
                start += 1
    return out


def expand_pick(st: Structure, c: LLMCandidate, min_sec: float, max_sec: float) -> Arc | None:
    """A legacy window pick (a line or short span) → the complete idea around it."""
    a = min(range(len(st.segs)), key=lambda i: abs(st.start(i) - c.start))
    b = min(range(len(st.segs)), key=lambda i: abs(st.end(i) - c.end))
    b = max(a, b)
    k = st.block_of[a]
    q0 = st.blocks[k][0]
    if st.questions[q0] and st.start(a) - st.start(q0) <= 25 and st.span(q0, b) <= max_sec:
        a = q0                              # start at the question it answers
    j = end_at_thought(st, a, b, max_sec)
    if j is None:
        return None
    while st.span(a, j) < min_sec and j + 1 < len(st.segs):
        nxt = end_at_thought(st, a, j + 1, max_sec)
        if nxt is None:
            return None
        j = nxt
    if st.span(a, j) < min_sec:
        return None
    return Arc(a, j, "pick", title=c.title, topic=c.topic)


def peak_moments(st: Structure, peaks: list[float], build: float, react: float, min_sec: float, max_sec: float) -> list[Arc]:
    """Comedy / gaming / challenge: build-up → peak → reaction around an audio peak, on segment edges."""
    out: list[Arc] = []
    for p in peaks:
        a = next((i for i in range(len(st.segs) - 1, -1, -1) if st.start(i) <= p - build), 0)
        b = next((i for i in range(len(st.segs)) if st.end(i) >= p + react), len(st.segs) - 1)
        j = end_at_thought(st, a, b, max_sec) or b
        if min_sec <= st.span(a, j) <= max_sec:
            out.append(Arc(a, j, "moment", prior=6.0))
    return out


def dedupe(arcs: list[Arc], st: Structure, max_overlap: float = 0.5) -> list[Arc]:
    kept: list[Arc] = []
    for x in sorted(arcs, key=lambda x: -x.prior):
        sx = (st.start(x.a), st.end(x.b))
        if any(_ov(sx, (st.start(k.a), st.end(k.b))) > max_overlap for k in kept):
            continue
        kept.append(x)
    return kept


def _ov(x, y) -> float:
    inter = max(0.0, min(x[1], y[1]) - max(x[0], y[0]))
    return inter / max(1e-9, min(x[1] - x[0], y[1] - y[0]))


# ----------------------------------------------------------------------------- judge
class Judgement(BaseModel):
    hook: float = 0
    completeness: float = 0
    standalone: float = 0
    value: float = 0
    flow: float = 0
    trend: float = 0
    start_id: int | None = None
    end_id: int | None = None
    answer_continues: bool = False
    title: str = ""
    why: str = ""

    @field_validator("hook", "completeness", "standalone", "value", "flow", "trend", mode="before")
    @classmethod
    def _clamp(cls, v):
        try:
            return max(0.0, min(10.0, float(v)))
        except (TypeError, ValueError):
            return 0.0

    @field_validator("start_id", "end_id", mode="before")
    @classmethod
    def _int(cls, v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None


JUDGE_PROMPT = """You judge whether a part of a podcast works as a COMPLETE vertical short (YouTube Shorts / Reels).
Podcast: {title} — {channel}

EPISODE OUTLINE (so you know the context):
{outline}

Transcript lines are `id| text`. Lines marked < are BEFORE the clip, lines marked > are AFTER it (context only).
{lines}

The clip is lines {a}..{b} ({dur:.0f} s). Score 0-10, be strict:
- hook: do the first 3 seconds make a stranger stay? (a weak opening = low)
- completeness: the question AND the full answer / the setup AND the payoff are inside; it ENDS on a conclusion.
  If the speaker is still mid-answer at line {b} (the > lines continue the same point), completeness ≤ 4.
- standalone: makes sense with zero context (no "as I said", no unclear "he/that/this")
- value: insight, story, emotion, surprise, humour or a useful takeaway
- flow: one clear line of thought, no dead air or rambling
- trend: topical / searchable (AI, money, business, startups, health, cricket, Bollywood, gaming…)
Also give:
- start_id / end_id: the best first and last line for a complete clip (you may move them into the < or > lines;
  start on the strongest opening, end where the point is finished)
- answer_continues: true if the answer/point continues after line {b}
- title: a specific title (max 60 chars), why: one line

Use the WHOLE 0-10 scale: most podcast moments are 3-6; 8+ only for a clip you would bet on. Different clips must
get different scores. Respond with ONLY a JSON object with these keys (numbers, not text):
{{"hook":<0-10>,"completeness":<0-10>,"standalone":<0-10>,"value":<0-10>,"flow":<0-10>,"trend":<0-10>,"start_id":<line id>,"end_id":<line id>,"answer_continues":<true|false>,"title":"<title>","why":"<one line>"}}"""


def judge_lines(st: Structure, a: int, b: int, ctx_sec: float) -> tuple[str, int, int]:
    lo = a
    while lo > 0 and st.start(a) - st.start(lo - 1) <= ctx_sec:
        lo -= 1
    hi = b
    while hi + 1 < len(st.segs) and st.end(hi + 1) - st.end(b) <= ctx_sec:
        hi += 1
    rows = []
    for i in range(lo, hi + 1):
        mark = "<" if i < a else (">" if i > b else " ")
        txt = seg_text(st.segs[i]) if not st.garbled[i] else "[unclear audio]"
        rows.append(f"{mark}{i}| {txt}")
    return "\n".join(rows), lo, hi


def weighted(r: dict, w: dict) -> float:
    tot = sum(w.values()) or 1.0
    return sum(r.get(k, 0) * v for k, v in w.items()) / tot


def judge(ws, st: Structure, arc: Arc, ol_text: str, info: VideoInfo, llm, cfg: Config) -> Judgement | None:
    from clipforge.stages.select_clips import _read_cache, _write_cache

    lines, _lo, _hi = judge_lines(st, arc.a, arc.b, cfg.arcs.context_sec)
    prompt = JUDGE_PROMPT.format(title=info.title, channel=info.channel or "unknown", outline=ol_text, lines=lines,
                                 a=arc.a, b=arc.b, dur=st.span(arc.a, arc.b))
    cache = ws.select_cache / f"judge_{_key(llm.name, llm.model, prompt)}.json"
    res = _read_cache(cache, Judgement)
    if res is None:
        try:
            res = llm.complete_json(prompt, Judgement, retries=cfg.llm.max_retries, task="judge",
                                    options={"num_predict": 200})
            _write_cache(cache, res)
        except Exception as exc:
            log.warning(f"Judge failed for lines {arc.a}-{arc.b}: {exc}")
            return None
    return res


def apply_judgement(st: Structure, arc: Arc, j: Judgement, min_sec: float, max_sec: float) -> tuple[int, int, bool]:
    """New (a, b, continues): the judge's corrected span (kept inside the context, ending on a thought end, within
    the length limits) and whether the answer still continues after it."""
    lines_lo, lines_hi = judge_lines(st, arc.a, arc.b, 30.0)[1:]
    a, b = arc.a, arc.b
    if j.start_id is not None and lines_lo <= j.start_id <= arc.b:
        a = j.start_id
    if j.end_id is not None and max(a, arc.a) <= j.end_id <= lines_hi:
        b = j.end_id
    if st.span(a, b) > max_sec:
        a, b = arc.a, arc.b
    a, b = normalize(st, a, b)
    if not st.ends_thought(b):
        e = end_at_thought(st, a, b, max_sec)
        b = e if e is not None else b
    while st.span(a, b) < min_sec and b + 1 < len(st.segs):
        e = end_at_thought(st, a, b + 1, max_sec)
        if e is None:
            break
        b = e
    nxt_same_block = b + 1 < len(st.segs) and st.block_of[b + 1] == st.block_of[b] and not st.questions[b + 1]
    continues = (not st.ends_thought(b)) or (j.answer_continues and nxt_same_block)
    return a, b, continues


# ----------------------------------------------------------------------------- engine
def select(ws, cfg: Config, llm, judge_llm, reporter: ProgressReporter, info: VideoInfo, tr: Transcript,
           legacy: list[LLMCandidate], peaks: list[float], profile) -> tuple[list[ClipCandidate], dict]:
    """Returns (final ClipCandidates without titles' polish, stats for the eval/report)."""
    a_cfg = cfg.arcs
    min_sec, max_sec = profile.min_sec, min(profile.max_sec, a_cfg.max_sec)
    st = build_structure(tr, info)
    reporter.progress(0.05, None, f"{len(st.blocks)} topic blocks, {sum(st.questions)} questions")
    ol = outline(ws, st, info, llm, cfg, reporter)
    ol_text = outline_text(ol, st)

    arcs: list[Arc] = []
    if "qa" in profile.sources:
        arcs += qa_units(st, min_sec, max_sec, profile.target_min)
    if "pick" in profile.sources:
        for c in legacy:
            x = expand_pick(st, c, min_sec, max_sec)
            if x:
                from clipforge.stages.refine import weighted_score

                x.prior = weighted_score(c.scores, cfg.scoring_weights)
                arcs.append(x)
    if "peaks" in profile.sources:
        arcs += peak_moments(st, peaks, profile.build_sec, profile.react_sec, min_sec, max_sec)
    for x in arcs:
        if x.kind in ("qa", "sub"):
            x.prior = ol[st.block_of[x.a]].interest if st.block_of[x.a] in ol else 5.0
            x.prior -= 0.5 if x.kind == "sub" else 0.0
        if st.garbled_ratio(x.a, x.b) > a_cfg.max_garbled:
            x.prior = -1
    for x in arcs:
        x.a, x.b = normalize(st, x.a, x.b)
        if st.span(x.a, x.b) < min_sec or any(st.garbled[i] and BOILERPLATE.search(seg_text(st.segs[i]))
                                              for i in range(x.a, x.b + 1)):
            x.prior = -1
    pool = [x for x in dedupe(arcs, st) if x.prior >= 0][: a_cfg.judge_pool]
    stats = {"blocks": len(st.blocks), "questions": sum(st.questions), "arcs": len(arcs), "judged": len(pool),
             "extended": 0, "gated_out": 0, "garbled_dropped": sum(1 for x in arcs if x.prior < 0)}

    judged: list[Arc] = []
    for n, x in enumerate(pool):
        reporter.progress(0.1 + 0.85 * n / max(1, len(pool)), None, f"Judging clip {n + 1} of {len(pool)}")
        j = judge(ws, st, x, ol_text, info, judge_llm, cfg)
        if j is None:
            continue
        a, b, cont = apply_judgement(st, x, j, min_sec, max_sec)
        if cont:  # the answer goes on: extend to where it ends (within max) and judge again
            e = end_at_thought(st, a, b + 1, max_sec, limit=st.blocks[st.block_of[b]][1])
            if e is not None:
                stats["extended"] += 1
                x.notes.append(f"end extended {st.end(b):.1f}→{st.end(e):.1f} s (answer continued)")
                x2 = Arc(a, e, x.kind, x.prior, x.title, x.topic)
                j2 = judge(ws, st, x2, ol_text, info, judge_llm, cfg)
                if j2 is not None:
                    j = j2
                    a, b, cont = apply_judgement(st, x2, j2, min_sec, max_sec)
        x.a, x.b = a, b
        x.rubric = {k: getattr(j, k) for k in ("hook", "completeness", "standalone", "value", "flow", "trend")}
        x.rubric["continues"] = cont
        x.title = j.title.strip()[:60] or x.title
        x.why = j.why.strip()
        x.score = round(weighted(x.rubric, profile.weights), 2)
        ok = (j.completeness >= a_cfg.gate_completeness and j.standalone >= a_cfg.gate_standalone and not cont
              and st.garbled_ratio(a, b) <= a_cfg.max_garbled and min_sec * 0.9 <= st.span(a, b) <= max_sec + 0.5)
        if not ok:
            stats["gated_out"] += 1
            continue
        judged.append(x)

    # final set: quality floor, one clip per block unless both strong, no time overlap, best first
    final: list[Arc] = []
    for x in sorted(judged, key=lambda x: -x.score):
        if x.score < a_cfg.min_quality or len(final) >= min(cfg.clips.count, cfg.clips.max_count):
            continue
        sx = (st.start(x.a), st.end(x.b))
        if any(_ov(sx, (st.start(k.a), st.end(k.b))) > 0.2 for k in final):
            continue
        same_block = [k for k in final if st.block_of[k.a] == st.block_of[x.a]]
        if same_block and not (x.score >= a_cfg.min_quality + 1 and all(k.score >= a_cfg.min_quality + 1 for k in same_block)):
            continue
        final.append(x)
    stats["final"] = len(final)
    out = []
    for r, x in enumerate(final, 1):
        s0 = max(0.0, st.start(x.a) - 0.15)
        nxt = st.start(x.b + 1) if x.b + 1 < len(st.segs) else st.end(x.b) + 1
        e0 = min(st.end(x.b) + 0.3, nxt - 0.05) if nxt > st.end(x.b) else st.end(x.b)
        rb = x.rubric
        out.append(ClipCandidate(
            rank=r, start=round(s0, 2), end=round(e0, 2), title="", hook="", topic=x.topic or f"block {st.block_of[x.a]}",
            why=x.why, score=x.score,
            rubric={**rb, "kind": x.kind, "block": st.block_of[x.a], "notes": x.notes, "judge_title": x.title},
            scores=Scores(hook=rb["hook"], standalone=rb["standalone"], emotion=rb["flow"], value=rb["value"],
                          controversy=rb["trend"])))
    return out, stats
