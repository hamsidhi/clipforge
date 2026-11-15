"""Live captions: transcribe only the clip (Whisper small/base int8, 1-2 threads, idle priority) → the regular ASS
caption style. Hindi words are romanized with the rule-based transliterator (no LLM while live).
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import numpy as np

from clipforge.config import Config
from clipforge.live.governor import popen_kwargs
from clipforge.models import ClipCandidate
from clipforge.utils.ffmpeg import ffmpeg_bin
from clipforge.utils.log import log

_model = None
_model_name = ""
_lock = threading.Lock()


def _load(name: str, threads: int):
    global _model, _model_name
    with _lock:
        if _model is None or _model_name != name:
            from faster_whisper import WhisperModel

            _model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=max(1, threads))
            _model_name = name
        return _model


def decode(src: Path, start: float, duration: float) -> np.ndarray:
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{max(0.0, start):.3f}",
           "-t", f"{duration:.3f}", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True, **popen_kwargs())
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace")[-300:])
    return np.frombuffer(r.stdout, dtype=np.float32)


def words(src: Path, start: float, duration: float, cfg: Config) -> list:
    """CapWords (clip time) for the clip, or [] when there's no speech."""
    from clipforge.llm.mock_provider import romanize_word
    from clipforge.stages.captions import CapWord

    audio = decode(src, start, duration)
    if audio.size < 16000:
        return []
    model = _load(cfg.live.whisper_model, min(2, cfg.live.max_cores))
    lang = None if cfg.whisper.language in ("auto", "smart") else cfg.whisper.language
    segs, _info = model.transcribe(audio, language=lang, beam_size=1, word_timestamps=True, vad_filter=True,
                                   condition_on_previous_text=False)
    out: list[CapWord] = []
    for s in segs:
        for w in s.words or []:
            text = w.word.strip()
            if not text:
                continue
            if any("ऀ" <= ch <= "ॿ" for ch in text):
                text = romanize_word(text) or text
            out.append(CapWord(round(max(0.0, w.start), 3), round(min(duration, w.end), 3), text))
    return out


def write_ass(cap_words: list, duration: float, hook: str, cfg: Config, out: Path) -> Path | None:
    from clipforge.stages.captions import build_ass

    if not cap_words and not hook:
        return None
    clip = ClipCandidate(rank=1, start=0, end=duration, title=hook, hook=hook)
    try:
        out.write_text(build_ass(cap_words, clip, cfg.captions), encoding="utf-8")
        return out
    except Exception as exc:
        log.warning(f"[live] captions skipped: {exc}")
        return None
