"""Stage 3a — per-second loudness + energy peaks (laughter, raised voice, applause)."""

from __future__ import annotations

import wave

import numpy as np

from clipforge.models import EnergyData
from clipforge.workspace import Workspace


def compute_energy(samples: np.ndarray, sr: int) -> EnergyData:
    """RMS in dBFS per 1-second window; peaks = seconds well above the local (rolling) median."""
    n_sec = int(len(samples) // sr)
    if n_sec == 0:
        return EnergyData(rms_db=[], peaks=[])
    # per-second RMS in chunks of 10 minutes: never materialise a float copy of a 3-hour file
    rms = np.empty(n_sec, dtype=np.float64)
    step = 600
    for a in range(0, n_sec, step):
        b = min(n_sec, a + step)
        x = samples[a * sr : b * sr].astype(np.float32).reshape(b - a, sr) / 32768.0
        rms[a:b] = np.sqrt(np.mean(x * x, axis=1) + 1e-12)
    db = 20 * np.log10(rms + 1e-9)

    # rolling median / MAD over ±60 s gives a local baseline robust to quiet/loud sections
    win = 60
    peaks: list[float] = []
    padded = np.pad(db, win, mode="edge")
    baseline = np.array([np.median(padded[i : i + 2 * win + 1]) for i in range(n_sec)])
    spread = np.array([np.median(np.abs(padded[i : i + 2 * win + 1] - baseline[i])) for i in range(n_sec)]) + 0.5
    z = (db - baseline) / spread
    for i in np.argsort(-z):
        if z[i] < 2.5 or db[i] < -45:
            break
        if all(abs(i - p) >= 5 for p in peaks):  # at most one peak per 5 s
            peaks.append(float(i))
    peaks.sort()
    return EnergyData(rms_db=[round(float(v), 2) for v in db], peaks=peaks)


def run(ws: Workspace) -> EnergyData:
    with wave.open(str(ws.audio), "rb") as wf:
        sr = wf.getframerate()
        if wf.getsampwidth() != 2 or wf.getnchannels() != 1:
            raise RuntimeError("audio.wav must be 16-bit mono (delete it to re-extract)")
        samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    data = compute_energy(samples, sr)
    ws.write_model(ws.energy, data)
    return data


def waveform_peaks(ws: Workspace, start: float, end: float, per_sec: int = 50) -> list[float]:
    """Normalised (0..1) absolute peaks for the waveform editor, `per_sec` values per second."""
    with wave.open(str(ws.audio), "rb") as wf:
        sr = wf.getframerate()
        total = wf.getnframes()
        a = max(0, int(start * sr))
        b = min(total, int(end * sr))
        wf.setpos(a)
        samples = np.frombuffer(wf.readframes(max(0, b - a)), dtype=np.int16).astype(np.float32) / 32768.0
    if samples.size == 0:
        return []
    step = max(1, sr // per_sec)
    n = samples.size // step
    if n == 0:
        return [float(np.max(np.abs(samples)))]
    blocks = np.abs(samples[: n * step]).reshape(n, step).max(axis=1)
    m = float(blocks.max()) or 1.0
    return [round(float(v) / m, 3) for v in blocks]
