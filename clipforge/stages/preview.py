"""Low-data preview proxy for phones (Phase 14): 540×960, ~1 Mbps H.264 + 64 kbps AAC, faststart.

Made right after each clip renders (cheap: a quarter of the pixels, ultrafast preset) and lazily for older clips.
Lives next to the render artifacts: clips/_work/NN/preview.mp4. The full file is only used for download/upload.
"""

from __future__ import annotations

from pathlib import Path

from clipforge.utils.ffmpeg import run_ffmpeg
from clipforge.utils.log import log


def preview_path(clip: Path) -> Path:
    rank = clip.name.split("_", 1)[0]
    return clip.parent / "_work" / rank / "preview.mp4"


def make_preview(clip: Path, out: Path | None = None) -> Path:
    out = out or preview_path(clip)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.mp4")
    run_ffmpeg(["-i", str(clip), "-vf", "scale=540:-2", "-c:v", "libx264", "-preset", "veryfast", "-b:v", "900k",
                "-maxrate", "1100k", "-bufsize", "2000k", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k",
                "-ac", "2", "-movflags", "+faststart", str(tmp)])
    tmp.replace(out)
    return out


def ensure_preview(clip: Path) -> Path:
    """Existing up-to-date preview, or make one now."""
    p = preview_path(clip)
    if p.exists() and p.stat().st_mtime >= clip.stat().st_mtime:
        return p
    return make_preview(clip, p)


def try_preview(clip: Path) -> None:
    from clipforge.config import load_config

    if not load_config().remote.preview_proxy:
        return
    try:
        make_preview(clip)
    except Exception as exc:  # cosmetic: the full clip still works
        log.warning(f"preview proxy for {clip.name} failed: {exc}")
