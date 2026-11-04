"""Clip-level Whisper for transcripts with estimated word timing (pasted text, YouTube captions without word offsets).

After refine has chosen the clips, faster-whisper runs only on each clip's range ± `transcript.clip_pad_sec`: the
audio range is cut with ffmpeg, words keep their timestamps, offset back to video time and spliced into
transcript.json (replacing the estimated words there). The clip boundaries are then snapped again on the exact words,
and captions use them. Results are cached per range in `clip_whisper/<start>_<end>.json`, and covered ranges are
listed in `transcript.exact_ranges`, so a re-render never runs Whisper again.
"""

from __future__ import annotations

import json
import time

from clipforge.config import Config
from clipforge.models import ClipCandidate, Segment, Transcript, Word
from clipforge.utils.log import NullReporter, ProgressReporter, check_cancel, log
from clipforge.workspace import Workspace, fingerprint

CACHE_DIR = "clip_whisper"


def _covered(t: Transcript, a: float, b: float) -> bool:
    return any(x <= a + 0.05 and b - 0.05 <= y for x, y in t.exact_ranges)


def _key(a: float, b: float) -> str:
    return f"{a:09.2f}_{b:09.2f}"


def transcribe_range(ws: Workspace, cfg: Config, a: float, b: float, language: str, state: dict) -> list[Segment]:
    """faster-whisper on [a, b] of audio.wav → segments in VIDEO time. `state` keeps the loaded model between calls."""
    from clipforge.stages.transcribe import load_model, load_wav
    from clipforge.utils.ffmpeg import run_ffmpeg

    if "model" not in state:
        state["model"], state["name"], state["device"] = load_model(cfg.whisper, state["reporter"])
    tmp = ws.dir / CACHE_DIR / f"_cut_{_key(a, b)}.wav"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", str(ws.audio), "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", str(tmp)])
    try:
        audio = load_wav(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    lang = None if language in ("auto", "", None) else language
    seg_iter, _ = state["model"].transcribe(audio, language=lang, beam_size=5 if state["device"] == "cuda" else 1,
                                            word_timestamps=True, vad_filter=cfg.whisper.vad,
                                            condition_on_previous_text=False)
    out: list[Segment] = []
    for seg in seg_iter:
        words = [Word(start=round(w.start + a, 3), end=round(w.end + a, 3), text=w.word.strip(), prob=round(w.probability, 3))
                 for w in (seg.words or []) if w.word.strip()]
        if words:
            out.append(Segment(id=0, start=round(seg.start + a, 3), end=round(seg.end + a, 3), text=seg.text.strip(), words=words))
    return out


def splice(t: Transcript, a: float, b: float, new: list[Segment]) -> None:
    """Replace every word whose middle lies in [a, b] by the exact words; trim the segments around the range."""
    def inside(w: Word) -> bool:
        return a <= (w.start + w.end) / 2 <= b

    kept: list[Segment] = []
    for s in t.segments:
        if s.end < a or s.start > b:
            kept.append(s)
            continue
        rest = [w for w in s.words if not inside(w)]
        if rest:
            kept.append(Segment(id=0, start=rest[0].start, end=rest[-1].end, text=" ".join(w.text for w in rest), words=rest))
    kept += [s.model_copy() for s in new]
    kept.sort(key=lambda s: s.start)
    for i, s in enumerate(kept):
        s.id = i
    t.segments = kept
    t.exact_ranges = sorted([*t.exact_ranges, (round(a, 3), round(b, 3))])


def _cached(ws: Workspace, fp: str, s: float, e: float) -> tuple[float, float, list[Segment]] | None:
    d = ws.dir / CACHE_DIR
    if not d.exists():
        return None
    for f in sorted(d.glob("*.json")):
        try:
            a, b = (float(x) for x in f.stem.split("_"))
        except ValueError:
            continue
        if a <= s + 0.05 and e - 0.05 <= b:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if data.get("fingerprint") == fp:
                    return a, b, [Segment.model_validate(x) for x in data["segments"]]
            except (OSError, ValueError, KeyError):
                continue
    return None


def ensure_exact(ws: Workspace, cfg: Config, t: Transcript, ranges: list[tuple[float, float]],
                 reporter: ProgressReporter | None = None) -> bool:
    """Make the words inside every clip range exact. Returns True if transcript.json changed (it is saved)."""
    if t.word_timing == "exact":
        return False
    reporter = reporter or NullReporter()
    pad = cfg.transcript.clip_pad_sec
    dur = t.duration or max((s.end for s in t.segments), default=0)
    todo = []
    for s, e in ranges:
        a, b = max(0.0, s - pad), min(dur, e + pad) if dur else e + pad
        if b - a > 0.5 and not _covered(t, s, e):  # the clip itself is already on exact words → nothing to do
            todo.append((round(a, 2), round(b, 2), s, e))
    if not todo:
        return False
    lang = t.language if t.language and len(t.language) <= 3 else cfg.whisper.language
    fp = fingerprint("clip-whisper-v1", cfg.whisper.model_dump(), lang)
    state: dict = {"reporter": reporter}
    spent = 0.0
    for i, (a, b, s, e) in enumerate(todo):
        check_cancel(reporter)
        cache = ws.dir / CACHE_DIR / f"{_key(a, b)}.json"
        hit = _cached(ws, fp, s, e)  # any cached range that covers this clip (its bounds may have been re-snapped)
        segs = None
        if hit:
            a, b, segs = hit
        else:
            reporter.progress(i / len(todo), None, f"exact word timing: clip {i + 1}/{len(todo)} (Whisper on {b - a:.0f} s)")
            t0 = time.monotonic()
            segs = transcribe_range(ws, cfg, a, b, lang, state)
            spent += time.monotonic() - t0
            cache.parent.mkdir(parents=True, exist_ok=True)
            ws.write_json(cache, {"fingerprint": fp, "range": [a, b], "segments": [s.model_dump() for s in segs]})
        if not segs:
            log.warning(f"Whisper heard no words in {a:.1f}–{b:.1f} s; keeping the estimated timing there.")
        splice(t, a, b, segs)
    ws.write_model(ws.transcript, t)
    if spent:
        from clipforge.stages import transcript_input as ti

        ti.update_saved(ws, spent)
        reporter.log(f"Clip-level Whisper: {len(todo)} range(s) in {spent:.0f} s (instead of the whole video)")
    return True


def resnap(cands: list[ClipCandidate], t: Transcript, cfg: Config, duration: float) -> list[ClipCandidate]:
    """Snap the clip boundaries again on the exact words (unchanged when snapping fails)."""
    from clipforge.stages.refine import snap_candidate

    words = sorted(t.all_words(), key=lambda w: w.start)
    bounds = t.boundaries()
    for c in cands:
        snapped = snap_candidate(c, words, cfg, duration, bounds)  # type: ignore[arg-type]
        if snapped and _covered(t, snapped[0], snapped[1]):  # stay on exact words (snapping may search ±4 s)
            c.start, c.end = round(snapped[0], 3), round(snapped[1], 3)
    return cands
