"""Stage 6 — decide how to frame every camera shot of a clip in 9:16.

Per shot: sample frames (5 fps), detect faces, then
  1 face            -> `track`: 9:16 crop following the face (EMA smoothing + deadzone)
  2+ faces (wide)   -> `split` (faces far apart: stacked top/bottom) or `track` on both (close together)
  0 faces           -> `blur` (whole frame over blurred background) or centre crop
The result is a CropPlan (crop_plan.json) that render.py turns into an ffmpeg filtergraph.

Face detector: MediaPipe Tasks FaceDetector (BlazeFace). It is tuned for near faces, so frames where
it finds nothing are re-checked with OpenCV YuNet, which also finds the small faces of wide podcast shots.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from dataclasses import dataclass
from statistics import median

import numpy as np

from clipforge.config import ReframeConfig
from clipforge.models import CropOverride, CropPlan, CropSegment, Scene
from clipforge.utils.ffmpeg import ffmpeg_bin
from clipforge.utils.log import log

ANALYSIS_WIDTH = 960


@dataclass
class Face:
    cx: float  # centre x (fraction of width)
    cy: float
    w: float   # width (fraction of width)
    h: float   # height (fraction of height)
    score: float


@contextlib.contextmanager
def _quiet_native_stderr():
    """LiteRT / OpenCV DNN print INFO lines straight to file descriptor 2; hide them while loading."""
    try:
        sys.stderr.flush()
        saved = os.dup(2)
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, 2)
    except OSError:
        yield
        return
    try:
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        os.close(null)


def weight(f: Face) -> float:
    """Rank faces: big AND confident. Cubing the score stops a low-confidence false detection
    (poster, pattern on the wall) from beating a slightly smaller real face."""
    return f.w * f.score ** 3


class FaceDetector:
    """MediaPipe BlazeFace with OpenCV YuNet fallback. Works on RGB uint8 frames."""

    def __init__(self) -> None:
        with _quiet_native_stderr():
            self._init()
        # first inference also logs (alignment warning) — warm up quietly
        with _quiet_native_stderr():
            self.detect(np.zeros((64, 64, 3), np.uint8))

    def _init(self) -> None:
        from clipforge.utils.assets import ensure_model

        self.mp = None
        self.yunet = None
        try:
            import mediapipe as mp
            from mediapipe.tasks.python import BaseOptions, vision

            model = ensure_model("blaze_face_short_range.tflite")
            opts = vision.FaceDetectorOptions(base_options=BaseOptions(model_asset_path=str(model)),
                                              min_detection_confidence=0.5)
            self.mp = vision.FaceDetector.create_from_options(opts)
            self._mp_image = mp.Image
            self._mp_format = mp.ImageFormat.SRGB
        except Exception as exc:
            log.warning(f"MediaPipe face detector unavailable ({exc}); using OpenCV YuNet only")
        try:
            import cv2

            model = ensure_model("face_detection_yunet_2023mar.onnx")
            self.yunet = cv2.FaceDetectorYN.create(str(model), "", (320, 320), 0.6, 0.3, 50)
        except Exception as exc:
            log.warning(f"OpenCV YuNet unavailable ({exc})")
        if not self.mp and not self.yunet:
            raise RuntimeError("No face detector available. Run `clipforge doctor --fix` to download the models.")

    def detect(self, rgb: np.ndarray) -> list[Face]:
        """Run both detectors and merge: a face both agree on keeps the higher score; a face only one
        of them reports must be confident on its own. Low-confidence single hits (posters, patterns)
        are dropped."""
        h, w = rgb.shape[:2]
        mp_faces: list[Face] = []
        yn_faces: list[Face] = []
        if self.mp:
            img = self._mp_image(image_format=self._mp_format, data=np.ascontiguousarray(rgb))
            for d in self.mp.detect(img).detections:
                bb = d.bounding_box
                mp_faces.append(Face((bb.origin_x + bb.width / 2) / w, (bb.origin_y + bb.height / 2) / h,
                                     bb.width / w, bb.height / h, d.categories[0].score if d.categories else 0.5))
        if self.yunet is not None:
            import cv2

            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            self.yunet.setInputSize((w, h))
            _, dets = self.yunet.detect(bgr)
            for d in dets if dets is not None else []:
                x, y, bw, bh, score = float(d[0]), float(d[1]), float(d[2]), float(d[3]), float(d[-1])
                yn_faces.append(Face((x + bw / 2) / w, (y + bh / 2) / h, bw / w, bh / h, score))
        merged: list[Face] = []
        used: set[int] = set()
        for f in mp_faces:
            match = next((j for j, g in enumerate(yn_faces) if j not in used
                          and abs(g.cx - f.cx) < max(f.w, g.w) * 0.6 and abs(g.cy - f.cy) < max(f.h, g.h) * 0.6), None)
            if match is not None:
                used.add(match)
                g = yn_faces[match]
                merged.append(Face(f.cx, f.cy, f.w, f.h, min(1.0, max(f.score, g.score) + 0.1)))
            elif f.score >= 0.8 or self.yunet is None:
                merged.append(f)
        merged += [g for j, g in enumerate(yn_faces) if j not in used and (g.score >= 0.8 or not self.mp)]
        # drop tiny / low-confidence detections (background people, posters)
        return [f for f in merged if f.score >= 0.7 and f.w >= 0.025]


def sample_frames(source: str, start: float, end: float, fps: float, src_w: int, src_h: int):
    """Yield (t_rel, rgb) frames from [start, end] at `fps`, scaled to ANALYSIS_WIDTH, via one ffmpeg pipe."""
    w = min(ANALYSIS_WIDTH, src_w)
    h = int(round(src_h * w / src_w / 2) * 2)
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
           "-i", source, "-an", "-vf", f"fps={fps},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = w * h * 3
    i = 0
    assert proc.stdout is not None
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield i / fps, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            i += 1
    finally:
        proc.stdout.close()
        proc.wait()


def smooth_track(xs: list[float | None], smoothing: float, deadzone: float, lo: float, hi: float) -> list[float]:
    """EMA + deadzone smoothing of crop centres; gaps (None) hold the last position. Clamped to [lo, hi]."""
    known = [x for x in xs if x is not None]
    if not known:
        return [0.5 * (lo + hi)] * len(xs)
    cur = min(hi, max(lo, median(known[: max(1, len(known) // 3)])))
    target = cur
    out: list[float] = []
    for x in xs:
        if x is not None and abs(x - target) > deadzone:
            target = x
        cur = smoothing * cur + (1 - smoothing) * target
        out.append(round(min(hi, max(lo, cur)), 4))
    return out


def plan_shot(samples: list[list[Face]], shot: Scene, cfg: ReframeConfig, crop_w_frac: float,
              override: CropOverride | None) -> CropSegment:
    lo, hi = crop_w_frac / 2, 1 - crop_w_frac / 2
    if override is not None:
        return CropSegment(start=shot.start, end=shot.end, mode="track", xs=[round(min(hi, max(lo, override.cx)), 4)])
    if cfg.layout == "blur":
        return CropSegment(start=shot.start, end=shot.end, mode="blur")
    counts = [len(f) for f in samples]
    n = int(median(counts)) if counts else 0
    if n == 0:
        # occasional detections in a no-face shot: track them if they are consistent, else blur
        hits = [max(f, key=weight) for f in samples if f]
        if len(hits) >= max(2, len(samples) // 3):
            return CropSegment(start=shot.start, end=shot.end, mode="track",
                               xs=[round(min(hi, max(lo, median(h.cx for h in hits))), 4)])
        return CropSegment(start=shot.start, end=shot.end, mode="blur" if cfg.layout == "auto" else "center", xs=[0.5])

    if n >= 2 and cfg.layout in ("auto", "split"):
        # the two biggest faces, averaged over the shot. Ignore pairs where the 2nd "face" is much smaller
        # (photos/posters on the wall, people far in the background) and very short shots (jarring).
        pairs = [sorted(sorted(f, key=lambda z: -weight(z))[:2], key=lambda z: z.cx) for f in samples if len(f) >= 2]
        pairs = [p for p in pairs if min(p[0].w, p[1].w) / max(p[0].w, p[1].w) >= 0.55 and min(p[0].score, p[1].score) >= 0.75
                 and abs(p[1].cx - p[0].cx) > 1.2 * max(p[0].w, p[1].w)]
        long_enough = shot.end - shot.start >= 1.5
        if long_enough and len(pairs) >= max(2, int(0.6 * len(samples))):
            left = Face(*(median(getattr(p[0], k) for p in pairs) for k in ("cx", "cy", "w", "h", "score")))
            right = Face(*(median(getattr(p[1], k) for p in pairs) for k in ("cx", "cy", "w", "h", "score")))
            dist = right.cx - left.cx
            both_fit = (right.cx + right.w / 2) - (left.cx - left.w / 2) <= crop_w_frac * 0.92
            if cfg.layout == "split" or (dist >= cfg.split_min_distance or not both_fit):
                return CropSegment(start=shot.start, end=shot.end, mode="split",
                                   faces=[(round(left.cx, 4), round(left.cy, 4)), (round(right.cx, 4), round(right.cy, 4))],
                                   face_h=round(max(left.h, right.h), 4))
            mid = (left.cx + right.cx) / 2
            return CropSegment(start=shot.start, end=shot.end, mode="track", xs=[round(min(hi, max(lo, mid)), 4)])

    # single subject (or layout=track): follow the most prominent face
    xs: list[float | None] = [max(f, key=weight).cx if f else None for f in samples]
    smooth = smooth_track(xs, cfg.smoothing, cfg.deadzone, lo, hi)
    return CropSegment(start=shot.start, end=shot.end, mode="track", xs=smooth)


def plan_clip(source: str, start: float, end: float, shots: list[Scene], src_w: int, src_h: int, cfg: ReframeConfig,
              out_w: int, out_h: int, detector: FaceDetector, overrides: list[CropOverride] | None = None,
              layout: str | None = None) -> CropPlan:
    """Build the crop plan for one clip. `shots` are clip-relative."""
    if layout and layout != cfg.layout:
        cfg = cfg.model_copy(update={"layout": layout})
    crop_w_frac = min(1.0, (src_h * out_w / out_h) / src_w)
    # one decode pass for the whole clip, then bucket frames into shots
    per_shot: list[list[list[Face]]] = [[] for _ in shots]
    if cfg.layout != "blur":
        for t, frame in sample_frames(source, start, end, cfg.sample_fps, src_w, src_h):
            idx = next((i for i, s in enumerate(shots) if s.start <= t < s.end), len(shots) - 1)
            per_shot[idx].append(detector.detect(frame))
    ov = {o.shot_index: o for o in (overrides or [])}
    segments = [plan_shot(per_shot[i], s, cfg, crop_w_frac, ov.get(i)) for i, s in enumerate(shots)]
    return CropPlan(src_width=src_w, src_height=src_h, segments=segments)
