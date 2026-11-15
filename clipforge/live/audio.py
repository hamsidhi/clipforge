"""Audio hype from levels we already get for free (OBS `InputVolumeMeters`, or decoded audio in simulation).

Mic: shouting / laughing = the level jumps well above a rolling baseline (z-score on dBFS, last 60 s).
Desktop: explosions / gunfire = short bursts far above the baseline. Each detector debounces itself (refractory).
"""

from __future__ import annotations

import math
from collections import deque

from clipforge.live.scorer import Signal

FLOOR_DB = -60.0


def mul_to_db(mul: float) -> float:
    return 20 * math.log10(mul) if mul > 1e-6 else -120.0


def obs_level_db(levels_mul: list[list[float]]) -> float:
    """OBS inputLevelsMul = per channel [magnitude, peak, input_peak] (linear multipliers) → loudest magnitude in dB."""
    best = 0.0
    for ch in levels_mul or []:
        if ch:
            best = max(best, float(ch[0]))
    return mul_to_db(best)


class HypeDetector:
    def __init__(self, kind: str = "audio_mic", baseline_sec: float = 60, z_on: float = 2.5, z_full: float = 5.0,
                 min_db: float = -30.0, refractory: float = 4.0, rate_hz: float = 5.0, note: str = "") -> None:
        self.kind, self.z_on, self.z_full, self.min_db, self.refractory = kind, z_on, z_full, min_db, refractory
        self.hist: deque[float] = deque(maxlen=max(10, int(baseline_sec * rate_hz)))
        self.step = 1.0 / rate_hz
        self.last_feed = -1e9
        self.last_fire = -1e9
        self._acc: list[float] = []
        self.note = note or ("loud reaction on mic" if kind == "audio_mic" else "game audio burst")
        self.level = FLOOR_DB

    def feed(self, t: float, db: float) -> Signal | None:
        """Feed levels at any rate; evaluated `rate_hz` times a second (max of the samples in between)."""
        self._acc.append(db)
        if t - self.last_feed < self.step:
            return None
        self.last_feed = t
        level = max(self._acc)
        self._acc = []
        self.level = level
        sig = None
        if len(self.hist) >= self.hist.maxlen // 3 and level > self.min_db and t - self.last_fire >= self.refractory:
            mean = sum(self.hist) / len(self.hist)
            var = sum((x - mean) ** 2 for x in self.hist) / len(self.hist)
            sd = max(2.0, math.sqrt(var))  # ≥ 2 dB: a very steady mic shouldn't make every breath a "spike"
            z = (level - mean) / sd
            if z >= self.z_on:
                self.last_fire = t
                v = min(1.0, 0.4 + 0.6 * (z - self.z_on) / max(0.1, self.z_full - self.z_on))
                sig = Signal(t, self.kind, round(v, 3), self.note, {"z": round(z, 2), "db": round(level, 1)})
        self.hist.append(max(FLOOR_DB, level))
        return sig
