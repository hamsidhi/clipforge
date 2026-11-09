"""Content profiles (2026-11-09): what a good clip is depends on the genre.

`arcs.profile: auto | podcast | interview | vlog_challenge | comedy | horror_comedy | gaming` (per job: the job
settings' `arcs.profile`). `auto` looks at the YouTube title/channel/chapters and the speech density (words per
minute). Each profile sets the candidate sources, the length range and the rubric weights.

Implemented today: podcast / interview (Q&A + story arcs from the transcript) and peak moments from the audio
(laughter / screams / gunfire) for comedy / challenge / gaming. Not yet built: a vision model on keyframes, a
facecam + gameplay stacked layout, game keyword packs. Those profiles still use the regular layouts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from clipforge.models import Transcript, VideoInfo


@dataclass(frozen=True)
class Profile:
    name: str
    sources: tuple[str, ...]           # qa | pick | peaks
    min_sec: float
    target_min: float
    max_sec: float
    weights: dict = field(default_factory=dict)
    build_sec: float = 12.0            # peaks: seconds of build-up before the peak
    react_sec: float = 8.0             # peaks: seconds of reaction after it


TALK = {"completeness": 3.0, "value": 2.5, "hook": 2.0, "standalone": 1.5, "flow": 1.0, "trend": 0.5}
FUN = {"hook": 3.0, "value": 2.5, "completeness": 2.0, "flow": 1.5, "standalone": 1.0, "trend": 0.5}

PROFILES = {
    "podcast": Profile("podcast", ("qa", "pick"), 20, 30, 90, TALK),
    "interview": Profile("interview", ("qa", "pick"), 20, 30, 90, TALK),
    "vlog_challenge": Profile("vlog_challenge", ("pick", "peaks"), 20, 30, 75, FUN, 20, 10),
    "comedy": Profile("comedy", ("pick", "peaks"), 15, 20, 60, FUN, 12, 6),
    "horror_comedy": Profile("horror_comedy", ("pick", "peaks"), 15, 20, 60, FUN, 15, 6),
    "gaming": Profile("gaming", ("peaks", "pick"), 15, 20, 60, FUN, 10, 8),
}

KEYWORDS = [
    ("gaming", r"\b(gta|gameplay|gaming|walkthrough|mortal kombat|minecraft|bgmi|pubg|valorant|free ?fire|fortnite|"
               r"call of duty|cod|speedrun|let'?s play)\b"),
    ("horror_comedy", r"\b(haveli|horror|bhoot|ghost|haunted|scary|darr?)\b"),
    ("comedy", r"\b(roast|comedy|funny|prank|stand ?up|carry ?minati|memes?|reaction)\b"),
    ("vlog_challenge", r"\b(challenge|last to leave|\$\d|₹ ?\d|vlog|survive|24 hours|mrbeast)\b"),
    ("interview", r"\b(interview|in conversation)\b"),
    ("podcast", r"\b(podcast|ep\.? ?\d+|episode|fo\d{3}|figuring out|ranveer|trs)\b"),
]


def detect(info: VideoInfo, tr: Transcript | None = None) -> tuple[str, str]:
    """(profile, reason). Title/channel keywords first, then speech density."""
    text = f"{info.title} {info.channel}".lower()
    for name, pat in KEYWORDS:
        if re.search(pat, text):
            return name, f"title/channel matches '{re.search(pat, text).group(0)}'"
    if tr and tr.duration > 0:
        words = sum(len(s.text.split()) for s in tr.segments)
        wpm = words / (tr.duration / 60)
        if wpm >= 110:
            return "podcast", f"{wpm:.0f} words/min (talk-heavy)"
        if wpm < 50:
            return "gaming", f"{wpm:.0f} words/min (little speech)"
        return "vlog_challenge", f"{wpm:.0f} words/min"
    return "podcast", "default"


def resolve(name: str, info: VideoInfo, tr: Transcript | None = None) -> tuple[Profile, str]:
    if name and name != "auto":
        return PROFILES[name], "chosen"
    p, why = detect(info, tr)
    return PROFILES[p], f"auto: {why}"
