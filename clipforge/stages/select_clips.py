"""Stage 4 — ask the LLM for clip candidates, window by window (fits any model's context)."""

from __future__ import annotations

import json
from pathlib import Path

from clipforge.config import Config, PROMPTS_DIR
from clipforge.llm.base import LLMError, LLMProvider
from clipforge.llm.mock_provider import romanize_word
from clipforge.models import LLMCandidate, LLMCandidateList, Segment, Transcript, VideoInfo
from clipforge.utils.ffmpeg import Timer
from clipforge.utils.log import ProgressReporter, check_cancel, log
from clipforge.workspace import Workspace, fingerprint

SYSTEM = "You are an expert short-form video editor. You always answer with valid JSON only."


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


def window_lines(segments: list[Segment], start: float, end: float, latin: bool) -> str:
    lines = []
    for s in segments:
        if s.end <= start or s.start >= end:
            continue
        lines.append(f"[{s.start:.1f}-{s.end:.1f}] {segment_text(s, latin)}")
    return "\n".join(lines)


def load_prompt() -> str:
    p = PROMPTS_DIR / "clip_selection.md"
    if not p.exists():
        raise FileNotFoundError(f"Prompt template missing: {p}")
    return p.read_text(encoding="utf-8")


def build_prompt(template: str, info: VideoInfo, transcript_text: str, cfg: Config, start: float, end: float) -> str:
    chapter = next((c.title for c in info.chapters if c.start <= (start + end) / 2 < c.end), "")
    return template.format(
        podcast_title=info.title,
        channel=info.channel or "unknown",
        chapter_line=f"Chapter: {chapter}" if chapter else "",
        max_candidates=cfg.llm.max_candidates_per_window,
        min_sec=int(cfg.clips.min_sec),
        max_sec=int(cfg.clips.max_sec),
        transcript=transcript_text,
    )


def run(ws: Workspace, cfg: Config, llm: LLMProvider, reporter: ProgressReporter, info: VideoInfo,
        transcript: Transcript) -> list[LLMCandidate]:
    template = load_prompt()
    # Latin script costs far fewer tokens than Devanagari on local models; cloud models get the original.
    latin = llm.name in ("ollama", "mock")
    windows = make_windows(transcript.duration, cfg.llm.window_min, cfg.llm.overlap_min)
    timer = Timer()
    all_cands: list[LLMCandidate] = []
    for i, (a, b) in enumerate(windows):
        check_cancel(reporter)
        text = window_lines(transcript.segments, a, b, latin)
        if len(text.split()) < 30:
            continue
        prompt = build_prompt(template, info, text, cfg, a, b)
        cache = ws.select_cache / f"w{i:03d}_{fingerprint(llm.name, llm.model, prompt)}.json"
        if cache.exists():
            cands = LLMCandidateList.model_validate_json(cache.read_text(encoding="utf-8")).candidates
        else:
            reporter.progress(i / len(windows), timer.eta(i / len(windows)) if i else None,
                              f"LLM window {i + 1}/{len(windows)} ({int(a // 60)}-{int(b // 60)} min)")
            try:
                res = llm.complete_json(prompt, LLMCandidateList, system=SYSTEM, retries=cfg.llm.max_retries,
                                        task="select")
            except LLMError as exc:
                if "not running" in str(exc) or "not found" in str(exc) or "API_KEY" in str(exc):
                    raise
                log.warning(f"Skipping window {i + 1} ({int(a // 60)}-{int(b // 60)} min): {exc}")
                continue
            cands = res.candidates
            Path(cache).write_text(res.model_dump_json(indent=2), encoding="utf-8")
        # keep only candidates inside this window (models sometimes hallucinate times)
        valid = [c for c in cands if a - 5 <= c.start < c.end <= b + 5]
        if len(valid) < len(cands):
            log.debug(f"window {i}: dropped {len(cands) - len(valid)} out-of-range candidates")
        all_cands += valid
    reporter.progress(1.0, 0, f"{len(all_cands)} raw candidates")
    ws.write_json(ws.llm_candidates, [json.loads(c.model_dump_json()) for c in all_cands])
    return all_cands
