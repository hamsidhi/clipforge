"""Signals → moments. Pure logic (no I/O), driven by the caller's clock so it works live and in simulation.

Each detector emits a `Signal(t, kind, value 0..1)`. The score of a moment is the weighted sum of the strongest signal of
each kind inside a `window_sec` window, capped at 10. A moment fires when the score beats both `min_score` and the
`threshold_pct` percentile of all scores seen in this stream (adaptive: a loud streamer needs louder moments), outside
the cooldown and under the hourly cap. Manual marks always fire (no cooldown, no cap). A fired moment stays "open" until
`t + post_roll`; triggers that arrive meanwhile extend it (merged), up to `max_clip_sec`.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any

KINDS = ("manual", "game_event", "audio_mic", "audio_desktop", "chat", "speech")
BIG_EVENT = 0.9  # game events this strong (win, ace, clutch, multi-kill) skip the cooldown (not the hourly cap)


@dataclass
class Signal:
    t: float                 # seconds on the session clock
    kind: str                # one of KINDS
    value: float = 1.0       # 0..1 strength
    note: str = ""           # human reason, e.g. "kill feed: 3 kills in 6 s"
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Moment:
    id: int
    trigger: float           # the strongest trigger time
    start: float
    end: float
    score: float
    reasons: list[str]
    kinds: list[str]
    manual: bool = False
    event: str = ""          # game event name when there is one (titles)
    note: str = ""           # the note of a manual mark

    @property
    def duration(self) -> float:
        return self.end - self.start

    def reason_text(self) -> str:
        seen: list[str] = []
        for r in self.reasons:
            if r and r not in seen:
                seen.append(r)
        return " + ".join(seen[:4]) or "highlight"


@dataclass
class ScorerConfig:
    window_sec: float = 20
    weights: dict[str, float] = field(default_factory=lambda: {"manual": 10, "game_event": 5, "audio_mic": 3.5,
                                                               "audio_desktop": 2, "chat": 3, "speech": 1.5})
    min_score: float = 4.0
    threshold_pct: float = 92
    cooldown_sec: float = 90
    max_per_hour: int = 6
    pre_roll: float = 20
    post_roll: float = 10
    max_clip_sec: float = 60
    warmup_scores: int = 30   # below this many scores the percentile isn't trusted (min_score only)

    @classmethod
    def from_live(cls, live, pack=None) -> "ScorerConfig":
        pre = pack.pre_roll if pack and pack.pre_roll else live.pre_roll
        post = pack.post_roll if pack and pack.post_roll else live.post_roll
        return cls(window_sec=live.window_sec, weights=dict(live.weights), min_score=live.min_score,
                   threshold_pct=live.threshold_pct, cooldown_sec=live.cooldown_sec,
                   max_per_hour=live.max_clips_per_hour, pre_roll=pre, post_roll=post, max_clip_sec=live.max_clip_sec)


class MomentScorer:
    def __init__(self, cfg: ScorerConfig) -> None:
        self.cfg = cfg
        self.signals: list[Signal] = []
        self.scores: list[float] = []          # sorted, for the percentile
        self.open: Moment | None = None        # fired, waiting for its post-roll
        self.fired: list[Moment] = []          # all moments (auto + manual)
        self.last_auto: float | None = None
        self._next_id = 1
        self.last_score = 0.0

    # ------------------------------------------------------------------ scoring
    def window_score(self, t: float) -> tuple[float, list[Signal]]:
        lo = t - self.cfg.window_sec
        inside = [s for s in self.signals if lo <= s.t <= t]
        best: dict[str, Signal] = {}
        for s in inside:
            if s.kind not in best or s.value > best[s.kind].value:
                best[s.kind] = s
        score = sum(self.cfg.weights.get(k, 0) * max(0.0, min(1.0, s.value)) for k, s in best.items())
        return min(10.0, score), sorted(best.values(), key=lambda s: -self.cfg.weights.get(s.kind, 0) * s.value)

    def threshold(self) -> float:
        if len(self.scores) < self.cfg.warmup_scores:
            return self.cfg.min_score
        i = min(len(self.scores) - 1, int(len(self.scores) * self.cfg.threshold_pct / 100))
        return max(self.cfg.min_score, self.scores[i])

    def auto_count_last_hour(self, t: float) -> int:
        return sum(1 for m in self.fired if not m.manual and m.trigger > t - 3600)

    # ------------------------------------------------------------------ input
    def add(self, sig: Signal) -> Moment | None:
        """Feed one signal. Returns a NEW moment when one fires (merges into the open one return None)."""
        self.signals.append(sig)
        self._prune(sig.t)
        if sig.kind == "manual":
            return self._fire(sig.t, 10.0, [sig], manual=True)
        score, best = self.window_score(sig.t)
        thr = self.threshold()  # the threshold before this score joins the history
        bisect.insort(self.scores, score)
        self.last_score = score
        if self.open and sig.t <= self.open.end:
            self._merge(sig, score, best)
            return None
        if score < self.cfg.min_score or (thr > self.cfg.min_score and score <= thr):
            return None  # must reach min_score AND beat (not just equal) what is normal for this stream
        big = sig.kind == "game_event" and sig.value >= BIG_EVENT  # a win / ace / clutch never waits for the cooldown
        if self.last_auto is not None and sig.t - self.last_auto < self.cfg.cooldown_sec and not big:
            return None
        if self.auto_count_last_hour(sig.t) >= self.cfg.max_per_hour:
            return None
        return self._fire(sig.t, score, best)

    def due(self, t: float) -> list[Moment]:
        """Moments whose post-roll has passed by clock `t` (save the footage now)."""
        if self.open and t >= self.open.end:
            m, self.open = self.open, None
            return [m]
        return []

    def flush(self) -> list[Moment]:
        m, self.open = self.open, None
        return [m] if m else []

    # ------------------------------------------------------------------ internals
    def _fire(self, t: float, score: float, best: list[Signal], manual: bool = False) -> Moment | None:
        if self.open and t <= self.open.end:  # a manual mark during an open moment: make it manual + longer
            m = self.open
            m.manual = m.manual or manual
            m.score = max(m.score, score)
            m.end = min(m.start + self.cfg.max_clip_sec, max(m.end, t + self.cfg.post_roll))
            m.reasons += [s.note or s.kind for s in best]
            if manual and best and best[0].note:
                m.note = best[0].note
            return None
        ev = next((s.meta.get("event", "") for s in best if s.kind == "game_event"), "")
        m = Moment(id=self._next_id, trigger=t, start=max(0.0, t - self.cfg.pre_roll), end=t + self.cfg.post_roll,
                   score=round(score, 2), reasons=[s.note or s.kind for s in best], kinds=[s.kind for s in best],
                   manual=manual, event=ev, note=(best[0].note if manual and best else ""))
        self._next_id += 1
        self.open = m
        self.fired.append(m)
        if not manual:
            self.last_auto = t
        return m

    def _merge(self, sig: Signal, score: float, best: list[Signal]) -> None:
        m = self.open
        assert m is not None
        if sig.value <= 0:
            return
        strong = score >= self.cfg.min_score or sig.kind in ("game_event", "manual")
        if not strong:
            return
        m.end = min(m.start + self.cfg.max_clip_sec, max(m.end, sig.t + self.cfg.post_roll))
        if score > m.score:
            m.score, m.trigger = round(score, 2), sig.t
        m.reasons.append(sig.note or sig.kind)
        if sig.kind not in m.kinds:
            m.kinds.append(sig.kind)
        if sig.kind == "game_event" and not m.event:
            m.event = sig.meta.get("event", "")

    def _prune(self, t: float) -> None:
        keep = t - max(self.cfg.window_sec, 60)
        if self.signals and self.signals[0].t < keep:
            self.signals = [s for s in self.signals if s.t >= keep]
