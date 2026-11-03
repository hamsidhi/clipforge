"""Stage 2 — speech-to-text with faster-whisper (word timestamps + VAD)."""

from __future__ import annotations

from clipforge.config import WhisperConfig
from clipforge.models import Segment, Transcript, Word
from clipforge.utils.ffmpeg import Timer
from clipforge.utils.gpu import cpu_count, whisper_defaults
from clipforge.utils.log import ProgressReporter, check_cancel, log
from clipforge.workspace import Workspace


def run(ws: Workspace, cfg: WhisperConfig, reporter: ProgressReporter, duration: float) -> Transcript:
    from faster_whisper import WhisperModel

    model_name, device, compute_type = whisper_defaults(cfg.model, cfg.compute_type)
    reporter.log(f"Whisper model={model_name} device={device} compute={compute_type} (first run downloads the model)")
    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type,
                             cpu_threads=max(4, cpu_count() - 2) if device == "cpu" else 0)
    except Exception as exc:
        if device == "cuda":  # broken CUDA/cuDNN install -> fall back to CPU instead of failing
            log.warning(f"CUDA Whisper failed ({exc}); falling back to CPU int8")
            model_name = "small" if cfg.model == "auto" else model_name
            model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=max(4, cpu_count() - 2))
        else:
            raise RuntimeError(f"Could not load Whisper model '{model_name}': {exc}") from exc

    language = None if cfg.language in ("auto", "", None) else cfg.language
    seg_iter, info = model.transcribe(
        str(ws.audio),
        language=language,
        beam_size=cfg.beam_size,
        word_timestamps=True,
        vad_filter=cfg.vad,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,  # avoids repetition loops on long audio
    )
    total = info.duration or duration or 1
    timer = Timer()
    segments: list[Segment] = []
    for seg in seg_iter:
        check_cancel(reporter)
        words = [
            Word(start=round(w.start, 3), end=round(w.end, 3), text=w.word.strip(), prob=round(w.probability, 3))
            for w in (seg.words or [])
            if w.word.strip()
        ]
        segments.append(Segment(id=len(segments), start=round(seg.start, 3), end=round(seg.end, 3),
                                text=seg.text.strip(), words=words))
        frac = min(1.0, seg.end / total)
        reporter.progress(frac, timer.eta(frac), f"{int(seg.end // 60)}/{int(total // 60)} min transcribed")

    transcript = Transcript(language=info.language, model=model_name, duration=total, segments=segments)
    ws.write_model(ws.transcript, transcript)
    n_words = sum(len(s.words) for s in segments)
    reporter.log(f"Transcribed {len(segments)} segments / {n_words} words (language={info.language})")
    return transcript
