"""Find the streamer's facecam from keyframes (OpenCV YuNet, the model the reframe stage already uses).

Every face in ~10 keyframes is clustered by position. A webcam overlay is the face that sits in the SAME place in most
frames (game characters and people in videos move around or come and go):
  stable small face (< 18 % of the frame width) → facecam: the face box grown to the usual 16:9 webcam framing
     → layout "facecam on top, gameplay below"
  stable big face → IRL / just chatting → full-height crop following the face (focus_x)
  nothing stable → gameplay only
Re-checked every ~5 minutes (streamers switch scenes).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from clipforge.utils.log import log

NEED = 10
SMALL = 0.18
SAME_SPOT = 0.04      # centre distance (share of the frame) to count as the same face
STABLE_SHARE = 0.6    # in at least this share of the frames

Face = tuple[float, float, float, float]  # x, y, w, h (0..1)


@dataclass
class CamDecision:
    kind: str                                   # facecam | irl | none
    rect: tuple[float, float, float, float] | None = None   # facecam x, y, w, h (0..1 of the frame)
    focus_x: float = 0.5


class FaceCam:
    def __init__(self) -> None:
        self.det = None
        self.samples: list[list[Face]] = []
        self.decision: CamDecision | None = None
        self.last_t = -1e9
        try:
            import cv2

            from clipforge.utils.assets import ensure_model

            self.det = cv2.FaceDetectorYN.create(str(ensure_model("face_detection_yunet_2023mar.onnx")), "", (320, 320),
                                                 0.7, 0.3, 20)
        except Exception as exc:
            log.warning(f"[live] face detector unavailable ({exc}): gameplay-only layout")

    def faces(self, frame: np.ndarray) -> list[tuple[float, float, float, float, float]]:
        if self.det is None:
            return []
        h, w = frame.shape[:2]
        self.det.setInputSize((w, h))
        _, dets = self.det.detect(frame)
        return [(float(d[0]) / w, float(d[1]) / h, float(d[2]) / w, float(d[3]) / h, float(d[-1]))
                for d in (dets if dets is not None else [])]

    def feed(self, t: float, frame: np.ndarray) -> CamDecision | None:
        """Returns a NEW decision when one is made (first one, or a changed one after a re-check)."""
        if self.decision is not None and t - self.last_t < 300:
            return None
        self.samples.append([f[:4] for f in self.faces(frame)])
        if len(self.samples) < NEED:
            return None
        dec = decide(self.samples)
        self.samples = []
        self.last_t = t
        old = self.decision
        changed = old is None or dec.kind != old.kind or (
            dec.rect is not None and old.rect is not None and abs(dec.rect[0] - old.rect[0]) > 0.05)
        self.decision = dec
        return dec if changed else None


def decide(samples: list[list[Face]]) -> CamDecision:
    """Cluster every face of every frame by centre; the most frequent cluster that is in ≥ 60 % of frames wins."""
    clusters: list[dict] = []
    for i, faces in enumerate(samples):
        for x, y, w, h in faces:
            cx, cy = x + w / 2, y + h / 2
            c = next((c for c in clusters if abs(c["cx"] - cx) < SAME_SPOT and abs(c["cy"] - cy) < SAME_SPOT), None)
            if c is None:
                c = {"cx": cx, "cy": cy, "frames": set(), "boxes": []}
                clusters.append(c)
            c["frames"].add(i)
            c["boxes"].append((x, y, w, h))
            n = len(c["boxes"])
            c["cx"] += (cx - c["cx"]) / n
            c["cy"] += (cy - c["cy"]) / n
    stable = [c for c in clusters if len(c["frames"]) >= len(samples) * STABLE_SHARE]
    if not stable:
        return CamDecision("none")
    best = max(stable, key=lambda c: (len(c["frames"]), -np.median([b[2] for b in c["boxes"]])))
    fw = float(np.median([b[2] for b in best["boxes"]]))
    cx, cy = best["cx"], best["cy"]
    if fw >= SMALL:
        return CamDecision("irl", None, cx)
    # a webcam overlay is ~4-5 face widths wide: grow the face box to 16:9 around it
    bw = min(0.45, fw * 4.5)
    bh = min(0.6, bw)  # the same share of width and height = a 16:9 box on a 16:9 frame
    x = min(max(0.0, cx - bw / 2), 1 - bw)
    y = min(max(0.0, cy - bh * 0.45), 1 - bh)
    return CamDecision("facecam", (x, y, bw, bh), cx)
