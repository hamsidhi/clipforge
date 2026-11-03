"""Per-video cache folder layout + stage bookkeeping (what makes runs resumable)."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|live/|embed/)([A-Za-z0-9_-]{11})")


def is_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "www."))


def fingerprint(*parts: Any) -> str:
    """Stable short hash of JSON-able parts (used to invalidate caches when settings change)."""
    blob = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def video_id_for(source: str, section: tuple[float, float] | None = None) -> str:
    """YouTube URL -> its 11-char id; local file -> '<slug>-<hash>'. A section adds a suffix."""
    if is_url(source):
        m = YOUTUBE_ID_RE.search(source)
        vid = m.group(1) if m else "url-" + fingerprint(source)[:8]
    else:
        p = Path(source).expanduser().resolve()
        stem = re.sub(r"[^A-Za-z0-9_-]+", "-", p.stem)[:30].strip("-") or "video"
        size = p.stat().st_size if p.exists() else 0
        vid = f"{stem}-{fingerprint(str(p), size)[:6]}"
    if section:
        vid += f"_{int(section[0])}-{int(section[1])}"
    return vid


class Workspace:
    """Paths for one video: workspace/<video_id>/…"""

    def __init__(self, root: Path, video_id: str) -> None:
        self.video_id = video_id
        self.dir = root / video_id
        self.dir.mkdir(parents=True, exist_ok=True)

    # --- files
    @property
    def source(self) -> Path:
        return self.dir / "source.mp4"

    @property
    def audio(self) -> Path:
        return self.dir / "audio.wav"

    @property
    def info(self) -> Path:
        return self.dir / "info.json"

    @property
    def transcript(self) -> Path:
        return self.dir / "transcript.json"

    @property
    def scenes(self) -> Path:
        return self.dir / "scenes.json"

    @property
    def energy(self) -> Path:
        return self.dir / "energy.json"

    @property
    def llm_candidates(self) -> Path:
        return self.dir / "llm_candidates.json"

    @property
    def candidates(self) -> Path:
        return self.dir / "candidates.json"

    @property
    def select_cache(self) -> Path:
        p = self.dir / "select_cache"
        p.mkdir(exist_ok=True)
        return p

    @property
    def clips_dir(self) -> Path:
        p = self.dir / "clips"
        p.mkdir(exist_ok=True)
        return p

    @property
    def metadata_csv(self) -> Path:
        return self.dir / "metadata.csv"

    @property
    def metadata_json(self) -> Path:
        return self.dir / "metadata.json"

    @property
    def gallery(self) -> Path:
        return self.dir / "gallery.html"

    @property
    def thumbnail(self) -> Path:
        return self.dir / "thumbnail.jpg"

    @property
    def state_file(self) -> Path:
        return self.dir / "state.json"

    # --- json helpers
    def read_model(self, path: Path, model: type[T]) -> T:
        return model.model_validate_json(path.read_text(encoding="utf-8"))

    @staticmethod
    def write_model(path: Path, obj: BaseModel) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(obj.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def write_json(path: Path, data: Any) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    # --- stage state
    def _state(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def stage_done(self, stage: str, fp: str, outputs: list[Path]) -> bool:
        st = self._state().get(stage)
        return bool(st and st.get("fingerprint") == fp and all(p.exists() for p in outputs))

    def stage_time(self, stage: str) -> float:
        return float(self._state().get(stage, {}).get("done_at", 0))

    def mark_done(self, stage: str, fp: str) -> None:
        state = self._state()
        state[stage] = {"fingerprint": fp, "done_at": time.time()}
        self.write_json(self.state_file, state)

    def invalidate(self, *stages: str) -> None:
        state = self._state()
        for s in stages:
            state.pop(s, None)
        self.write_json(self.state_file, state)

    def work_dir(self, rank: int) -> Path:
        """Per-clip scratch files: crop_plan.json, captions.ass, render.json, previous.mp4, thumb.jpg."""
        p = self.clips_dir / "_work" / f"{rank:02d}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def clip_path(self, rank: int, title: str) -> Path:
        from clipforge.utils.text import slugify

        return self.clips_dir / f"{rank:02d}_{slugify(title)}.mp4"

    def find_clip(self, rank: int) -> Path | None:
        matches = sorted(self.clips_dir.glob(f"{rank:02d}_*.mp4"))
        return matches[0] if matches else None
