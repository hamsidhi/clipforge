"""Pydantic data models shared by the pipeline stages, CLI and API.

Each stage reads/writes one of these as JSON inside `workspace/<video_id>/`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class Chapter(BaseModel):
    title: str
    start: float
    end: float


class VideoInfo(BaseModel):
    """`info.json` — what we know about the source video."""

    id: str
    title: str
    channel: str = ""
    url: str = ""
    duration: float = 0
    width: int = 0
    height: int = 0
    fps: float = 0
    chapters: list[Chapter] = []
    source_path: str = "source.mp4"


class Word(BaseModel):
    start: float
    end: float
    text: str
    prob: float = 1.0
    roman: str | None = None  # Roman-Hinglish form of `text` (same timing)
    roman_src: str | None = None  # llm | rule | rule-failed (LLM unavailable -> retried on next render)

    def display(self, script: str) -> str:
        if script == "roman" and self.roman:
            return self.roman
        return self.text


class Segment(BaseModel):
    id: int
    start: float
    end: float
    text: str
    words: list[Word] = []


class Transcript(BaseModel):
    """`transcript.json`."""

    language: str
    model: str
    duration: float
    segments: list[Segment]
    romanized: bool = False
    source: str = "whisper"  # whisper | pasted | youtube
    word_timing: Literal["exact", "estimated"] = "exact"  # estimated = words spread evenly over each line
    exact_ranges: list[tuple[float, float]] = []  # estimated transcripts: ranges re-timed by clip-level Whisper

    def all_words(self) -> list[Word]:
        return [w for s in self.segments for w in s.words]

    def boundaries(self) -> tuple[set[float], set[float]]:
        """(start times of segment-first words, end times of segment-last words) — sentence-ish boundaries."""
        segs = [s for s in self.segments if s.words]
        return {s.words[0].start for s in segs}, {s.words[-1].end for s in segs}


class Scene(BaseModel):
    start: float
    end: float


class EnergyData(BaseModel):
    """`energy.json` — per-second loudness and detected peaks."""

    rms_db: list[float]  # one value per second
    peaks: list[float]   # times (s) of energy peaks (laughter / raised voice / applause)


class Scores(BaseModel):
    hook: float = Field(0, ge=0, le=10)
    standalone: float = Field(0, ge=0, le=10)
    emotion: float = Field(0, ge=0, le=10)
    value: float = Field(0, ge=0, le=10)
    controversy: float = Field(0, ge=0, le=10)

    @field_validator("*", mode="before")
    @classmethod
    def _clamp(cls, v: float) -> float:
        try:
            return max(0.0, min(10.0, float(v)))
        except (TypeError, ValueError):
            return 0.0


class LLMCandidate(BaseModel):
    """One candidate exactly as the LLM returns it (validated)."""

    start: float
    end: float
    hook: str = ""
    title: str
    topic: str = ""
    why: str = ""
    scores: Scores = Scores()

    @model_validator(mode="after")
    def _order(self) -> "LLMCandidate":
        if self.end <= self.start:
            raise ValueError(f"end ({self.end}) must be after start ({self.start})")
        return self


class LLMCandidateList(BaseModel):
    candidates: list[LLMCandidate]


class RawCandidateList(BaseModel):
    """Lenient wrapper for the selection reply: items are validated one by one later, so one bad
    candidate doesn't throw away the whole window. A bare JSON list is accepted too."""

    candidates: list[dict]

    @model_validator(mode="before")
    @classmethod
    def _wrap(cls, v):
        if isinstance(v, list):
            return {"candidates": v}
        if isinstance(v, dict) and "candidates" not in v:
            for key in ("clips", "moments", "results"):
                if isinstance(v.get(key), list):
                    return {"candidates": v[key]}
        return v

    def valid(self) -> list[LLMCandidate]:
        out = []
        for item in self.candidates:
            try:
                out.append(LLMCandidate.model_validate(item))
            except Exception:
                continue
        return out


Layout = Literal["auto", "track", "split", "blur"]


class CaptionStyle(BaseModel):
    """Per-clip caption overrides (None = use config)."""

    preset: str | None = None
    font: str | None = None
    font_size: int | None = None
    highlight_color: str | None = None
    keyword_color: str | None = None
    position_pct: float | None = None


class CropOverride(BaseModel):
    """Manual crop centre for one shot (fractions of the source frame)."""

    shot_index: int
    cx: float = Field(ge=0, le=1)
    cy: float = Field(0.5, ge=0, le=1)


class ClipCandidate(BaseModel):
    """An entry in `candidates.json` — human-editable.

    Edit `start`, `end`, `title`, `hook` (or set `enabled: false`) and run
    `clipforge render <id> --only N`.
    """

    rank: int = 0
    start: float
    end: float
    title: str
    hook: str = ""
    topic: str = ""
    why: str = ""
    scores: Scores = Scores()
    score: float = 0
    enabled: bool = True
    approved: bool | None = None  # None = not reviewed yet (set from the web app)
    layout: Layout | None = None
    caption_style: CaptionStyle = CaptionStyle()
    word_edits: dict[str, str] = {}  # "<word index in clip>" -> replacement text
    crop_overrides: list[CropOverride] = []
    rubric: dict = {}  # arc engine: judge scores (hook, completeness, standalone, value, flow, trend), kind, block, notes

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


class CandidateFile(BaseModel):
    video_id: str
    candidates: list[ClipCandidate]


class CropSegment(BaseModel):
    """One camera shot inside a clip and how to frame it (times relative to clip start)."""

    start: float
    end: float
    mode: Literal["track", "split", "blur", "center"]
    # track: crop centre x per sample (fraction of source width), evenly spaced over the segment
    xs: list[float] = []
    # split: two face centres (fractions of source width/height), top = left face
    faces: list[tuple[float, float]] = []
    face_h: float = 0  # split: face height (fraction of source height) -> zoom level


class CropPlan(BaseModel):
    src_width: int
    src_height: int
    segments: list[CropSegment]


class ClipMetadata(BaseModel):
    rank: int
    file: str
    start: float
    end: float
    duration: float
    score: float
    title: str
    hook: str
    yt_title: str
    yt_description: str
    ig_caption: str
    hashtags: list[str]
    credit: str
