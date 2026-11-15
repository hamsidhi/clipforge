"""HUD watcher: small game screenshots (OBS GetSourceScreenshot, ~640 px wide, every 1-2 s) → game-event signals.

Cheap on purpose: a few crops, OpenCV colour/diff maths (~1 ms per rule), OCR only when installed and only on the rule
boxes. Rules debounce themselves; kill-feed activity is counted into multi-kills ("3 kills in 6 s").
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from clipforge.live.games import GamePack, HudRule
from clipforge.live.scorer import Signal
from clipforge.utils.log import log

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "assets" / "live_templates"
MULTI_KILL_SEC = 6.0


def crop(frame: np.ndarray, box: tuple[float, float, float, float]) -> np.ndarray:
    h, w = frame.shape[:2]
    x, y, bw, bh = box
    x0, y0 = int(x * w), int(y * h)
    x1, y1 = max(x0 + 1, int((x + bw) * w)), max(y0 + 1, int((y + bh) * h))
    return frame[y0:y1, x0:x1]


class Ocr:
    """Optional OCR: RapidOCR (onnxruntime, CPU) or Tesseract. `available` False when neither is installed."""

    def __init__(self) -> None:
        self.engine = None
        self.kind = ""
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore

            self.engine, self.kind = RapidOCR(), "rapidocr"
            return
        except Exception:
            pass
        try:
            import shutil

            import pytesseract  # type: ignore

            if shutil.which("tesseract"):
                self.engine, self.kind = pytesseract, "tesseract"
        except Exception:
            pass

    @property
    def available(self) -> bool:
        return self.engine is not None

    def read(self, img: np.ndarray) -> str:
        if self.engine is None or img.size == 0:
            return ""
        try:
            if self.kind == "rapidocr":
                res, _ = self.engine(img)
                return " ".join(r[1] for r in (res or [])).lower()
            return str(self.engine.image_to_string(img)).lower()
        except Exception as exc:
            log.warning(f"[live] OCR failed: {exc}")
            return ""


def _templates(pack: str) -> dict[str, list[np.ndarray]]:
    import cv2

    out: dict[str, list[np.ndarray]] = {}
    d = TEMPLATES_DIR / pack
    if d.is_dir():
        for f in sorted(d.glob("*.png")):
            img = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                out.setdefault(f.stem.split("_")[0], []).append(img)
    return out


class HudWatcher:
    def __init__(self, pack: GamePack, ocr: Ocr | None = None) -> None:
        self.pack = pack
        self.ocr = ocr
        self.prev: dict[int, np.ndarray] = {}
        self.was_on: dict[int, bool] = {}
        self.last_fire: dict[int, float] = {}
        self.kills: list[float] = []
        self.templates = _templates(pack.name)
        self.last_events: list[dict] = []   # for the Live page

    def feed(self, t: float, frame: np.ndarray) -> list[Signal]:
        import cv2

        out: list[Signal] = []
        if frame is None or frame.size == 0:
            return out
        hsv_full = None
        for i, rule in enumerate(self.pack.rules):
            region = crop(frame, rule.box)
            on = self._check(i, rule, region, frame, hsv_full)
            if rule.method == "desat" and hsv_full is None:
                hsv_full = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            rising = on and not self.was_on.get(i, False)
            self.was_on[i] = on
            if rule.method == "activity":
                rising = on  # every new kill-feed line counts
            if not rising or t - self.last_fire.get(i, -1e9) < rule.debounce:
                continue
            self.last_fire[i] = t
            out.append(self._signal(t, rule))
        for ev, imgs in self.templates.items():
            if self._template_hit(frame, imgs) and t - self.last_fire.get(hash(ev), -1e9) >= 5:
                self.last_fire[hash(ev)] = t
                out.append(Signal(t, "game_event", 0.9, f"{ev} (template)", {"event": ev}))
        for s in out:
            self.last_events = (self.last_events + [{"t": round(t, 1), "event": s.meta.get("event"), "note": s.note}])[-10:]
        return out

    # ------------------------------------------------------------------ rules
    def _check(self, i: int, rule: HudRule, region: np.ndarray, frame: np.ndarray, _hsv) -> bool:
        import cv2

        if self.ocr is not None and self.ocr.available and rule.words and rule.method != "activity":
            text = self.ocr.read(region)
            return any(w in text for w in rule.words)
        if rule.method == "color":
            hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, np.array(rule.hsv_lo, np.uint8), np.array(rule.hsv_hi, np.uint8))
            return float(mask.mean()) / 255 >= rule.min_ratio
        if rule.method == "desat":
            hsv = cv2.cvtColor(frame[:: 4, :: 4], cv2.COLOR_BGR2HSV)
            sat = float(hsv[..., 1].mean())
            red = cv2.inRange(hsv, np.array((0, 120, 90), np.uint8), np.array((8, 255, 255), np.uint8))
            red2 = cv2.inRange(hsv, np.array((170, 120, 90), np.uint8), np.array((179, 255, 255), np.uint8))
            red_ratio = float((red | red2).mean()) / 255
            return sat < 28 and 0.002 < red_ratio < 0.2  # grey screen with red text
        # activity: new bright text in the box (a kill-feed line)
        g = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        prev = self.prev.get(i)
        self.prev[i] = g
        if prev is None or prev.shape != g.shape:
            return False
        diff = cv2.absdiff(g, prev)
        changed = float((diff > 40).mean())
        bright_new = float(((g > 200) & (diff > 40)).mean())
        return changed >= rule.min_ratio and bright_new >= rule.min_ratio * 0.3

    def _signal(self, t: float, rule: HudRule) -> Signal:
        if rule.event == "kill":
            self.kills = [k for k in self.kills if k > t - MULTI_KILL_SEC] + [t]
            n = len(self.kills)
            v = {1: 0.5, 2: 0.75}.get(n, 1.0)
            note = "kill" if n == 1 else f"kill feed: {n} kills in {MULTI_KILL_SEC:.0f} s"
            return Signal(t, "game_event", v, note, {"event": "kill", "n": n})
        return Signal(t, "game_event", rule.value, rule.note or rule.event, {"event": rule.event})

    def _template_hit(self, frame: np.ndarray, imgs: list[np.ndarray]) -> bool:
        import cv2

        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        for tpl in imgs:
            if tpl.shape[0] > g.shape[0] or tpl.shape[1] > g.shape[1]:
                continue
            res = cv2.matchTemplate(g, tpl, cv2.TM_CCOEFF_NORMED)
            if float(res.max()) >= 0.8:
                return True
        return False
