"""Transcripts we don't have to compute with Whisper.

Sources (config `transcript.source`, auto = pasted > YouTube captions with word timing > YouTube captions without
word timing > Whisper):
* pasted — YouTube "Show transcript" copy (time on its own line or before the text, m:ss / h:mm:ss), SRT, VTT,
  "[hh:mm:ss] text". Stored as `transcript_input.json` ({text, section}) when the job is created.
* youtube — captions fetched by the download stage (`captions_youtube.json`, json3 or vtt; manual + auto-generated).
  json3 auto captions carry per-word offsets → exact word timing.

Lines become the normal transcript.json (one segment per line) with `source` and `word_timing`. Estimated timing
spreads each line's words evenly over its span — good enough for the LLM and for snapping; the selected clips get
real word timestamps later (stages/clip_whisper.py).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from clipforge.models import Segment, Transcript, Word
from clipforge.utils.log import log
from clipforge.workspace import Workspace

INPUT_FILE = "transcript_input.json"
CAPTIONS_FILE = "captions_youtube.json"
REPORT_FILE = "transcript_report.json"
MAX_INPUT_CHARS = 3_000_000  # ~ a 10 h transcript

# rough real-time factors of a full-video Whisper run (large-v3-turbo int8 greedy on CPU; large-v3 fp16 on CUDA)
WHISPER_RTF = {"cpu": 0.45, "cuda": 0.06}

TS = r"(?:(\d{1,2}):)?(\d{1,3}):(\d{2})(?:[.,](\d{1,3}))?"
TS_LINE = re.compile(rf"^\s*[\[(]?{TS}[\])]?(?:\s+|\s*[-–:]\s+|$)(.*)$")
CUE = re.compile(rf"^\s*{TS}\s*-->\s*{TS}")
JUNK_WORDS = r"music|applause|laughter|laughs|laughing|inaudible|silence|noise|cheering|संगीत|तालियां|तालियाँ|हंसी|हँसी"
JUNK_LINE = re.compile(rf"^\s*[\[(](?:{JUNK_WORDS})[^\])]*[\])]\s*$|^\s*♪+\s*$", re.I)
JUNK_INLINE = re.compile(rf"[\[(](?:{JUNK_WORDS})[^\])]*[\])]|♪+", re.I)
SPEAKER = re.compile(r"^\s*(?:>>\s*|-\s+)?(?:(?:Speaker\s*\d+|[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})\s*:\s+)?")
ACCESSIBLE_TIME = re.compile(r"^\s*\d+\s+(?:seconds?|minutes?|hours?)(?:,?\s+\d+\s+(?:seconds?|minutes?|hours?))*\s*$", re.I)
TAGS = re.compile(r"<[^>]+>")


class TranscriptParseError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        more = f" (+{len(errors) - 8} more)" if len(errors) > 8 else ""
        super().__init__("Couldn't read the transcript: " + "; ".join(errors[:8]) + more)


@dataclass
class Line:
    start: float
    text: str
    end: float | None = None
    words: list[Word] = field(default_factory=list)  # exact words (json3), else empty


@dataclass
class Parsed:
    lines: list[Line]
    fmt: str  # srt | vtt | timestamps | json3
    errors: list[str] = field(default_factory=list)


def _secs(h: str | None, m: str, s: str, frac: str | None) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + (int(frac.ljust(3, "0")) / 1000 if frac else 0)


def clean(text: str, strip_speakers: bool = True) -> str:
    text = TAGS.sub("", text)
    text = JUNK_INLINE.sub(" ", text)
    if strip_speakers:
        text = SPEAKER.sub("", text, count=1)
    return re.sub(r"\s+", " ", text).strip()


# ============================================================================ pasted text
def parse_text(text: str, strip_speakers: bool = True) -> Parsed:
    """Any supported text format → lines. Errors carry line numbers; the caller decides whether to reject."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    if not text.strip():
        return Parsed([], "empty", ["the transcript is empty"])
    if text.lstrip().startswith("WEBVTT") or any("-->" in ln for ln in text.split("\n")[:60]):
        return _parse_cues(text, strip_speakers)
    return _parse_timestamps(text, strip_speakers)


def _parse_cues(text: str, strip_speakers: bool) -> Parsed:
    fmt = "vtt" if text.lstrip().startswith("WEBVTT") else "srt"
    lines: list[Line] = []
    errors: list[str] = []
    rows = text.split("\n")
    i = 0
    prev_text_lines: list[str] = []
    while i < len(rows):
        row = rows[i]
        if "-->" not in row:
            i += 1
            continue
        m = CUE.match(row)
        if not m:
            errors.append(f"line {i + 1}: can't read the time in '{row.strip()[:60]}'")
            i += 1
            continue
        a, b = _secs(*m.group(1, 2, 3, 4)), _secs(*m.group(5, 6, 7, 8))
        if b < a:
            errors.append(f"line {i + 1}: cue ends before it starts")
        i += 1
        body: list[str] = []
        while i < len(rows) and rows[i].strip() and "-->" not in rows[i]:
            body.append(rows[i])
            i += 1
        # YouTube's auto VTT repeats the previous line in every cue ("rolling" captions): keep only new lines
        cleaned = [c for c in (clean(x, strip_speakers) for x in body if not JUNK_LINE.match(x)) if c]
        new = [c for c in cleaned if c not in prev_text_lines]
        prev_text_lines = cleaned
        if new:
            lines.append(Line(start=a, end=max(a, b), text=" ".join(new)))
    if not lines and not errors:
        errors.append("no subtitle cues found (expected lines like 00:00:01,000 --> 00:00:04,000)")
    return Parsed(_ordered(lines, errors), fmt, errors)


def _parse_timestamps(text: str, strip_speakers: bool) -> Parsed:
    lines: list[Line] = []
    errors: list[str] = []
    cur: Line | None = None
    before_first = 0
    for n, row in enumerate(text.split("\n"), 1):
        if not row.strip() or JUNK_LINE.match(row) or ACCESSIBLE_TIME.match(row):
            continue
        m = TS_LINE.match(row)
        if m and not re.match(r"^\s*\d{1,3}:\d{2}\s*(?:am|pm|baje)\b", row, re.I):
            t = _secs(*m.group(1, 2, 3, 4))
            if lines and t + 2 < lines[-1].start:
                errors.append(f"line {n}: time {m.group(0).split()[0]} is earlier than the line before it")
            cur = Line(start=t, text=clean(m.group(5), strip_speakers))
            lines.append(cur)
        elif cur is None:
            before_first += 1
            if before_first <= 2:
                continue  # a heading like "Transcript" or the video title
            errors.append(f"line {n}: text before the first timestamp ('{row.strip()[:40]}')")
        else:
            extra = clean(row, strip_speakers)
            cur.text = f"{cur.text} {extra}".strip()
    lines = [ln for ln in lines if ln.text]
    if not lines:
        errors.append("no timestamps found — paste the text with its times (0:00, 1:02:03, [00:01:02], SRT or VTT)")
    return Parsed(_ordered(lines, []), "timestamps", errors)


def _ordered(lines: list[Line], errors: list[str]) -> list[Line]:
    return sorted(lines, key=lambda ln: ln.start)


def parse_or_raise(text: str) -> Parsed:
    if len(text) > MAX_INPUT_CHARS:
        raise TranscriptParseError([f"the transcript is too long ({len(text):,} characters, max {MAX_INPUT_CHARS:,})"])
    p = parse_text(text)
    if p.errors:
        raise TranscriptParseError(p.errors)
    return p


# ============================================================================ YouTube json3
def _junk_word(t: str) -> bool:
    return bool(JUNK_LINE.match(t)) or not t.strip()


def parse_json3(data: dict) -> tuple[list[Line], bool]:
    """json3 events → lines. Returns (lines, exact): auto captions carry `tOffsetMs` per word → exact word timing."""
    events = [e for e in data.get("events", []) if e.get("segs") and "".join(s.get("utf8", "") for s in e["segs"]).strip()]
    multi = sum(1 for e in events if len(e["segs"]) > 1 and any("tOffsetMs" in s for s in e["segs"]))
    exact = bool(events) and multi / len(events) >= 0.5
    lines: list[Line] = []
    for k, e in enumerate(events):
        t0 = e.get("tStartMs", 0) / 1000
        end = t0 + e.get("dDurationMs", 0) / 1000
        nxt = events[k + 1].get("tStartMs", 0) / 1000 if k + 1 < len(events) else None
        if nxt is not None and nxt > t0:
            end = min(end, nxt) if end > t0 else nxt
        text = clean("".join(s.get("utf8", "") for s in e["segs"]).replace("\n", " "))
        if not text or JUNK_LINE.match(text):
            continue
        line = Line(start=t0, end=max(end, t0 + 0.2), text=text)
        if exact:
            segs = [s for s in e["segs"] if s.get("utf8", "").strip()]
            starts = [t0 + s.get("tOffsetMs", 0) / 1000 for s in segs]
            for j, s in enumerate(segs):
                ws_ = starts[j]
                we = starts[j + 1] if j + 1 < len(segs) else line.end  # type: ignore[assignment]
                toks = [x for x in clean(s["utf8"]).split() if not _junk_word(x)]
                for q, tok in enumerate(toks):  # a seg with several words: split its span evenly
                    span = max(0.05, (we - ws_) / max(1, len(toks)))
                    line.words.append(Word(start=round(ws_ + q * span, 3), end=round(ws_ + (q + 1) * span, 3), text=tok))
        lines.append(line)
    return lines, exact


# ============================================================================ lines → transcript
def to_transcript(lines: list[Line], duration: float, language: str, source: str, exact: bool) -> Transcript:
    lines = sorted(lines, key=lambda ln: ln.start)
    segs: list[Segment] = []
    for i, ln in enumerate(lines):
        nxt = lines[i + 1].start if i + 1 < len(lines) else None
        n = len(ln.text.split())
        end = ln.end if ln.end is not None else (nxt if nxt is not None else ln.start + max(1.0, 0.4 * n))
        if nxt is not None and nxt > ln.start:
            end = min(end, nxt)
        end = max(end, ln.start + 0.2)
        if duration:
            end = min(end, duration) if ln.start < duration else end
        if exact and ln.words:
            words = ln.words
        else:  # estimated: the line's words spread evenly over its span
            toks = ln.text.split()
            span = (end - ln.start) / max(1, len(toks))
            words = [Word(start=round(ln.start + k * span, 3), end=round(ln.start + (k + 1) * span, 3), text=t)
                     for k, t in enumerate(toks)]
        segs.append(Segment(id=i, start=round(ln.start, 3), end=round(end, 3), text=ln.text, words=words))
    return Transcript(language=language or "auto", model=source, duration=duration, segments=segs, source=source,
                      word_timing="exact" if exact else "estimated")


def shift(lines: list[Line], offset: float, length: float | None) -> list[Line]:
    """Move lines by -offset (a --section job), dropping lines outside [0, length]."""
    out = []
    for ln in lines:
        s = ln.start - offset
        if s < -0.5 or (length is not None and s > length):
            continue
        words = [w.model_copy(update={"start": round(w.start - offset, 3), "end": round(w.end - offset, 3)}) for w in ln.words]
        out.append(Line(start=max(0.0, s), end=None if ln.end is None else ln.end - offset, text=ln.text, words=words))
    return out


# ============================================================================ sanity checks
def script_of(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "none"
    deva = sum(1 for c in letters if "ऀ" <= c <= "ॿ")
    latin = sum(1 for c in letters if c.isascii())
    if deva / len(letters) > 0.5:
        return "devanagari"
    if latin / len(letters) > 0.5:
        return "latin"
    return "other"


def sanity(t: Transcript, duration: float, expected_language: str, gap: float = 0.10) -> list[str]:
    """Warnings for a transcript that may not belong to this video. Never blocks: you choose to fall back to Whisper."""
    out: list[str] = []
    if not t.segments:
        return ["The transcript has no lines."]
    last = max(s.end for s in t.segments)
    if duration > 0 and abs(duration - last) > gap * duration:
        out.append(f"The transcript ends at {_clock(last)} but the video is {_clock(duration)} long — "
                   "is it the right video (or the right part)?")
    script = script_of(" ".join(s.text for s in t.segments[:400]))
    lang = (expected_language or "auto").lower()
    if script == "other" or (lang == "en" and script == "devanagari"):
        out.append(f"The transcript's script ({script}) doesn't match the video's language ({lang}).")
    return out


def _clock(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


# ============================================================================ workspace files
def save_input(ws: Workspace, text: str, section: tuple[float, float] | None) -> Parsed:
    """Validate (raises TranscriptParseError with line numbers) and store a pasted transcript for this job."""
    p = parse_or_raise(text)
    ws.write_json(ws.dir / INPUT_FILE, {"text": text, "section": list(section) if section else None})
    return p


def input_signature(ws: Workspace, source: str) -> str:
    """Part of the transcribe fingerprint. Empty for `auto` without a pasted transcript, so jobs that were already
    transcribed by Whisper keep their cache."""
    f = ws.dir / INPUT_FILE
    sig = hashlib.sha1(f.read_bytes()).hexdigest()[:12] if f.exists() and source in ("auto", "pasted") else ""
    if source != "auto":
        sig += f"|{source}"
    return sig


def _pasted(ws: Workspace, duration: float) -> list[Line] | None:
    f = ws.dir / INPUT_FILE
    if not f.exists():
        return None
    data = json.loads(f.read_text(encoding="utf-8"))
    lines = parse_or_raise(data["text"]).lines
    sec = data.get("section")
    if sec and lines:
        length = sec[1] - sec[0]
        if max(ln.start for ln in lines) > length * 1.1:  # times of the full video → move them into the part
            lines = shift(lines, sec[0], length)
    return lines


def youtube_tracks(ws: Workspace) -> list[dict]:
    """[{kind: manual|auto, lang, lines, exact}] from captions_youtube.json (already moved into a --section)."""
    f = ws.dir / CAPTIONS_FILE
    if not f.exists():
        return []
    data = json.loads(f.read_text(encoding="utf-8"))
    out = []
    for tr in data.get("tracks", []):
        try:
            if tr["ext"] == "json3":
                lines, exact = parse_json3(tr["data"])
            else:
                lines, exact = parse_text(tr["data"]).lines, False
        except Exception as exc:
            log.warning(f"Couldn't read the YouTube {tr.get('kind')} captions ({tr.get('lang')}): {exc}")
            continue
        if data.get("offset") or data.get("length"):
            lines = shift(lines, float(data.get("offset") or 0), data.get("length"))
        if lines:
            out.append({"kind": tr["kind"], "lang": tr["lang"], "lines": lines, "exact": exact})
    return out


def choose(ws: Workspace, source: str, duration: float, language: str, gap: float = 0.10) -> tuple[Transcript | None, dict]:
    """The transcript to use instead of Whisper (None → run Whisper) + a small report for the UI/Telegram."""
    report: dict = {"source": "whisper", "label": "Whisper", "word_timing": "exact", "warnings": []}
    if source in ("auto", "pasted"):
        lines = _pasted(ws, duration)
        if lines:
            t = to_transcript(lines, duration, language, "pasted", exact=False)
            report.update(source="pasted", label="pasted", word_timing="estimated",
                          warnings=sanity(t, duration, language, gap))
            return t, report
        if source == "pasted":
            report["warnings"].append("No pasted transcript for this job — used Whisper.")
    if source in ("auto", "youtube"):
        tracks = youtube_tracks(ws)
        if source == "youtube":  # manual first, then auto-generated
            tracks.sort(key=lambda tr: tr["kind"] != "manual")
        else:  # auto: word timing first
            tracks.sort(key=lambda tr: (not tr["exact"], tr["kind"] != "manual"))
        if tracks:
            tr = tracks[0]
            lang = tr["lang"].split("-")[0]
            t = to_transcript(tr["lines"], duration, lang, "youtube", exact=tr["exact"])
            report.update(source="youtube", label=f"YouTube captions ({tr['kind']}, {tr['lang']})",
                          word_timing=t.word_timing, warnings=sanity(t, duration, lang, gap))
            return t, report
        if source == "youtube":
            report["warnings"].append("This video has no YouTube captions — used Whisper.")
    return None, report


def full_whisper_estimate(duration: float) -> float:
    from clipforge.utils.gpu import cuda_available

    try:
        dev = "cuda" if cuda_available() else "cpu"
    except Exception:
        dev = "cpu"
    return duration * WHISPER_RTF[dev]


def read_report(ws_dir: Path) -> dict | None:
    f = ws_dir / REPORT_FILE
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    except (OSError, ValueError):
        return None


def write_report(ws: Workspace, report: dict) -> None:
    ws.write_json(ws.dir / REPORT_FILE, report)


def update_saved(ws: Workspace, clip_whisper_sec: float) -> None:
    """Add clip-level Whisper time to the report and recompute the time saved."""
    r = read_report(ws.dir)
    if not r:
        return
    r["clip_whisper_sec"] = round(r.get("clip_whisper_sec", 0) + clip_whisper_sec, 1)
    r["saved_sec"] = round(max(0.0, r.get("full_whisper_est_sec", 0) - r["clip_whisper_sec"] - r.get("whisper_sec", 0)), 1)
    write_report(ws, r)


# ============================================================================ YouTube caption download (download stage)
def pick_tracks(info: dict, langs: list[str]) -> list[tuple[str, str, dict]]:
    """Best manual and best auto-generated track: (kind, lang, format). Languages: `langs` in order, then any.
    Auto-translated tracks (URL has tlang=) are skipped — they're machine translations, not what was said."""
    out = []
    for kind, key in (("manual", "subtitles"), ("auto", "automatic_captions")):
        tracks = {k: v for k, v in (info.get(key) or {}).items() if k != "live_chat" and v}
        names = list(tracks)
        order = [n for lang in langs for n in names if n == lang or n.startswith(lang + "-")] + sorted(names)
        for name in order:
            fmts = [f for f in tracks[name] if "tlang=" not in (f.get("url") or "")]
            fmt = next((f for f in fmts if f.get("ext") == "json3"), None) or next((f for f in fmts if f.get("ext") == "vtt"), None)
            if fmt:
                out.append((kind, name.replace("-orig", ""), fmt))
                break
    return out


def fetch_youtube(info: dict, ws: Workspace, langs: list[str], section: tuple[float, float] | None, opener) -> int:
    """Download the chosen caption tracks into captions_youtube.json. `opener(url) -> bytes`. Returns tracks saved."""
    saved = []
    for kind, lang, fmt in pick_tracks(info, langs):
        try:
            raw = opener(fmt["url"]).decode("utf-8", "replace")
            data = json.loads(raw) if fmt["ext"] == "json3" else raw
        except Exception as exc:
            log.warning(f"Couldn't download the YouTube {kind} captions ({lang}): {exc}")
            continue
        saved.append({"kind": kind, "lang": lang, "ext": fmt["ext"], "data": data})
    if saved:
        ws.write_json(ws.dir / CAPTIONS_FILE, {"tracks": saved, "offset": section[0] if section else 0,
                                               "length": (section[1] - section[0]) if section else None})
    return len(saved)
