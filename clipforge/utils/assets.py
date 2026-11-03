"""Free assets downloaded on demand: OFL fonts and face-detection models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from clipforge.config import FONTS_DIR, MODELS_DIR
from clipforge.utils.log import log


@dataclass(frozen=True)
class FontSpec:
    key: str          # name used in config (captions.font)
    file: str
    url: str
    family: str       # family name libass matches on
    bold: bool        # set ASS Bold flag
    script: str       # latin | devanagari


FONTS: dict[str, FontSpec] = {
    f.key: f
    for f in [
        FontSpec(
            "Montserrat-ExtraBold", "Montserrat-ExtraBold.ttf",
            "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-ExtraBold.ttf",
            "Montserrat ExtraBold", False, "latin",
        ),
        FontSpec(
            "Poppins-Bold", "Poppins-Bold.ttf",
            "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Bold.ttf",
            "Poppins", True, "latin",
        ),
        FontSpec(
            "NotoSansDevanagari-Bold", "NotoSansDevanagari-Bold.ttf",
            "https://github.com/notofonts/notofonts.github.io/raw/main/fonts/NotoSansDevanagari/hinted/ttf/NotoSansDevanagari-Bold.ttf",
            "Noto Sans Devanagari", True, "devanagari",
        ),
    ]
}
DEVANAGARI_FONT = "NotoSansDevanagari-Bold"


MODELS = {
    "blaze_face_short_range.tflite": "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/latest/blaze_face_short_range.tflite",
    "face_detection_yunet_2023mar.onnx": "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
}


def _download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log.info(f"Downloading {dest.name} …")
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                f.write(chunk)
    tmp.replace(dest)
    return dest


def font_path(key: str) -> Path:
    return FONTS_DIR / FONTS[key].file


def ensure_fonts() -> list[Path]:
    """Download any missing fonts. Returns paths of all fonts."""
    out = []
    for spec in FONTS.values():
        p = FONTS_DIR / spec.file
        if not p.exists() or p.stat().st_size < 10_000:
            _download(spec.url, p)
        out.append(p)
    return out


def missing_fonts() -> list[str]:
    return [k for k, s in FONTS.items() if not (FONTS_DIR / s.file).exists()]


def ensure_model(name: str) -> Path:
    p = MODELS_DIR / name
    if not p.exists() or p.stat().st_size < 10_000:
        _download(MODELS[name], p)
    return p
