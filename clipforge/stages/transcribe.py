"""Stage 2 — speech-to-text with faster-whisper (word timestamps + VAD)."""

from __future__ import annotations

import json
import time
import wave
from pathlib import Path

import numpy as np

from clipforge.config import WhisperConfig
from clipforge.models import Segment, Transcript, Word
from clipforge.utils.ffmpeg import Timer
from clipforge.utils.gpu import cpu_count, whisper_defaults
from clipforge.utils.log import ProgressReporter, check_cancel, log
from clipforge.workspace import Workspace


def _threads() -> int:
    """Physical cores are faster than hyper-threads for CTranslate2."""
    n = cpu_count()
    return max(4, n // 2 if n >= 8 else n)


def load_wav(path: Path) -> np.ndarray:
    """16 kHz mono PCM WAV -> float32 array. We decode ourselves instead of letting faster-whisper use
    PyAV: faster-whisper 1.2.1 passes `metadata_errors` to av.open(), which PyAV ≥15 removed."""
    with wave.open(str(path), "rb") as wf:
        if wf.getframerate() != 16000 or wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise RuntimeError(f"{path} must be 16 kHz mono 16-bit (delete it to re-extract)")
        data = wf.readframes(wf.getnframes())
    audio = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    del data
    audio *= 1.0 / 32768.0  # in place: a 3 h file is ~700 MB as float32, don't hold two copies
    return audio


CHECKPOINT_EVERY = 120  # seconds of wall-clock time between partial saves


def load_model(cfg: WhisperConfig, reporter: ProgressReporter):
    """(model, model_name, device) — CUDA falls back to CPU int8 when its install is broken."""
    from faster_whisper import WhisperModel

    model_name, device, compute_type = whisper_defaults(cfg.model, cfg.compute_type)
    reporter.log(f"Whisper model={model_name} device={device} compute={compute_type} (first run downloads the model)")
    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type,
                             cpu_threads=_threads() if device == "cpu" else 0)
    except Exception as exc:
        if device == "cuda":  # broken CUDA/cuDNN install -> fall back to CPU instead of failing
            log.warning(f"CUDA Whisper failed ({exc}); falling back to CPU int8")
            model_name = "large-v3-turbo" if cfg.model == "auto" else model_name
            device = "cpu"
            model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=_threads())
        else:
            raise RuntimeError(f"Could not load Whisper model '{model_name}': {exc}") from exc
    return model, model_name, device


def run(ws: Workspace, cfg: WhisperConfig, reporter: ProgressReporter, duration: float) -> Transcript:
    model, model_name, device = load_model(cfg, reporter)

    language = None if cfg.language in ("auto", "", None) else cfg.language
    audio = load_wav(ws.audio)
    total = len(audio) / 16000 or duration or 1

    # resume from a checkpoint (a crash/cancel an hour into a long episode shouldn't lose that hour)
    partial = ws.dir / "transcript.partial.json"
    ckpt_key = f"{model_name}|{cfg.language}|{cfg.vad}|{cfg.beam_size}|{len(audio)}"
    segments: list[Segment] = []
    offset = 0.0
    if partial.exists():
        try:
            data = json.loads(partial.read_text(encoding="utf-8"))
            if data.get("key") == ckpt_key and data.get("segments"):
                segments = [Segment.model_validate(x) for x in data["segments"]]
                offset = segments[-1].end
                reporter.log(f"Resuming transcription from checkpoint at {offset / 60:.1f} min")
        except (ValueError, KeyError):
            segments = []

    seg_iter, info = model.transcribe(
        audio[int(offset * 16000):],
        language=language,
        beam_size=(5 if device == "cuda" else 1) if cfg.beam_size == "auto" else int(cfg.beam_size),
        word_timestamps=True,
        vad_filter=cfg.vad,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,  # avoids repetition loops on long audio
    )
    timer = Timer()
    last_save = time.monotonic()
    done_at_start = offset / total
    for seg in seg_iter:
        check_cancel(reporter)
        words = [
            Word(start=round(w.start + offset, 3), end=round(w.end + offset, 3), text=w.word.strip(),
                 prob=round(w.probability, 3))
            for w in (seg.words or [])
            if w.word.strip()
        ]
        segments.append(Segment(id=len(segments), start=round(seg.start + offset, 3), end=round(seg.end + offset, 3),
                                text=seg.text.strip(), words=words))
        frac = min(1.0, (seg.end + offset) / total)
        work_frac = (frac - done_at_start) / max(1e-6, 1 - done_at_start)
        eta = timer.eta(work_frac)
        reporter.progress(frac, eta, f"{int((seg.end + offset) // 60)}/{int(total // 60)} min transcribed")
        if time.monotonic() - last_save > CHECKPOINT_EVERY:
            partial.write_text(json.dumps({"key": ckpt_key, "segments": [x.model_dump() for x in segments]},
                                          ensure_ascii=False), encoding="utf-8")
            last_save = time.monotonic()

    transcript = Transcript(language=info.language, model=model_name, duration=total, segments=segments)
    ws.write_model(ws.transcript, transcript)
    partial.unlink(missing_ok=True)
    n_words = sum(len(s.words) for s in segments)
    reporter.log(f"Transcribed {len(segments)} segments / {n_words} words (language={info.language})")
    return transcript
