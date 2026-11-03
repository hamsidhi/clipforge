"""Hardware detection: CUDA for Whisper, NVENC for ffmpeg, CPU count for workers."""

from __future__ import annotations

import functools
import os
import subprocess

from clipforge.utils.ffmpeg import ffmpeg_bin


@functools.lru_cache(maxsize=1)
def cuda_available() -> bool:
    """True if CTranslate2 (faster-whisper's backend) can see a CUDA device."""
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def nvenc_available() -> bool:
    """True if ffmpeg has h264_nvenc AND it actually initialises on this machine."""
    try:
        exe = ffmpeg_bin()
        out = subprocess.run([exe, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20).stdout
        if "h264_nvenc" not in out:
            return False
        test = subprocess.run(
            [exe, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=black:s=256x256:d=0.1",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        return test.returncode == 0
    except Exception:
        return False


def cpu_count() -> int:
    return os.cpu_count() or 2


def whisper_defaults(model: str, compute_type: str) -> tuple[str, str, str]:
    """Resolve `auto` -> (model, device, compute_type)."""
    cuda = cuda_available()
    device = "cuda" if cuda else "cpu"
    if model == "auto":
        model = "large-v3" if cuda else "small"
    if compute_type == "auto":
        compute_type = "float16" if cuda else "int8"
    return model, device, compute_type
