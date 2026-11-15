"""Minimal OBS WebSocket v5 client (OBS 28+, protocol 5.x) — read-mostly.

Allowed requests (whitelist, enforced in `request`): status/info reads, screenshots, and the Replay Buffer
(Start/Save). ClipForge never starts/stops the stream or recording, never changes encoders, bitrate, scenes or sources.

Protocol: Hello(op 0) → Identify(op 1, auth = b64(sha256(b64(sha256(password + salt)) + challenge))) → Identified(op 2);
Request(op 6) ↔ RequestResponse(op 7); Event(op 5). Subscriptions: all low-volume events (2047) + InputVolumeMeters (1<<16).
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import threading
from typing import Any, Callable

from clipforge.utils.log import log

ALLOWED = {
    "GetVersion", "GetStats", "GetStreamStatus", "GetRecordStatus", "GetReplayBufferStatus", "GetVideoSettings",
    "GetCurrentProgramScene", "GetSceneList", "GetSceneItemList", "GetSceneItemTransform", "GetInputList",
    "GetInputSettings", "GetSourceScreenshot", "GetLastReplayBufferReplay", "GetRecordDirectory",
    "StartReplayBuffer", "SaveReplayBuffer",
}
SUB_ALL = 2047
SUB_VOLUME_METERS = 1 << 16


class ObsError(RuntimeError):
    pass


def auth_string(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class ObsClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 4455, password: str = "", *, meters: bool = True,
                 connect: Callable | None = None, timeout: float = 5.0) -> None:
        self.url = f"ws://{host}:{port}"
        self.password = password
        self.meters = meters
        self._connect = connect
        self.timeout = timeout
        self.ws = None
        self._ids = itertools.count(1)
        self._pending: dict[str, tuple[threading.Event, dict]] = {}
        self._lock = threading.Lock()
        self._handlers: dict[str, list[Callable[[dict], None]]] = {}
        self._reader: threading.Thread | None = None
        self.connected = False

    # ------------------------------------------------------------------ connection
    def open(self) -> "ObsClient":
        connect = self._connect
        if connect is None:
            from websockets.sync.client import connect
        self.ws = connect(self.url, open_timeout=self.timeout, max_size=32 * 1024 * 1024)
        hello = json.loads(self.ws.recv(timeout=self.timeout))
        if hello.get("op") != 0:
            raise ObsError("OBS WebSocket didn't say hello — is it OBS 28+ with the WebSocket server enabled?")
        d = hello.get("d", {})
        ident: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": SUB_ALL | (SUB_VOLUME_METERS if self.meters else 0)}
        if d.get("authentication"):
            if not self.password:
                raise ObsError("OBS asks for a WebSocket password: put it in .env as OBS_WEBSOCKET_PASSWORD")
            a = d["authentication"]
            ident["authentication"] = auth_string(self.password, a["salt"], a["challenge"])
        self.ws.send(json.dumps({"op": 1, "d": ident}))
        msg = json.loads(self.ws.recv(timeout=self.timeout))
        if msg.get("op") != 2:
            raise ObsError("OBS refused the login (wrong WebSocket password?)")
        self.connected = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="live-obs-reader")
        self._reader.start()
        return self

    def close(self) -> None:
        self.connected = False
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass

    def on(self, event: str, fn: Callable[[dict], None]) -> None:
        self._handlers.setdefault(event, []).append(fn)

    # ------------------------------------------------------------------ requests
    def request(self, rtype: str, data: dict | None = None, timeout: float | None = None) -> dict:
        if rtype not in ALLOWED:
            raise ObsError(f"ClipForge doesn't send '{rtype}' to OBS (read-only + Replay Buffer only)")
        if not self.connected or self.ws is None:
            raise ObsError("OBS isn't connected")
        rid = str(next(self._ids))
        ev = threading.Event()
        box: dict = {}
        with self._lock:
            self._pending[rid] = (ev, box)
        self.ws.send(json.dumps({"op": 6, "d": {"requestType": rtype, "requestId": rid, "requestData": data or {}}}))
        if not ev.wait(timeout or self.timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise ObsError(f"OBS didn't answer {rtype} in time")
        st = box.get("requestStatus", {})
        if not st.get("result"):
            raise ObsError(f"OBS {rtype}: {st.get('comment') or st.get('code')}")
        return box.get("responseData") or {}

    def _read_loop(self) -> None:
        while self.connected:
            try:
                raw = self.ws.recv(timeout=1)  # type: ignore[union-attr]
            except TimeoutError:
                continue
            except Exception as exc:
                if self.connected:
                    log.warning(f"[live] OBS connection lost: {exc}")
                self.connected = False
                for fn in self._handlers.get("_Disconnected", []):
                    fn({})
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            op, d = msg.get("op"), msg.get("d", {})
            if op == 7:
                with self._lock:
                    item = self._pending.pop(d.get("requestId", ""), None)
                if item:
                    item[1].update(d)
                    item[0].set()
            elif op == 5:
                for fn in self._handlers.get(d.get("eventType", ""), []):
                    try:
                        fn(d.get("eventData", {}) or {})
                    except Exception as exc:
                        log.warning(f"[live] OBS event handler {d.get('eventType')}: {exc}")

    # ------------------------------------------------------------------ helpers
    def streaming(self) -> bool:
        return bool(self.request("GetStreamStatus").get("outputActive"))

    def replay_active(self) -> bool:
        return bool(self.request("GetReplayBufferStatus").get("outputActive"))

    def program_scene(self) -> str:
        r = self.request("GetCurrentProgramScene")
        return r.get("sceneName") or r.get("currentProgramSceneName") or ""

    def video_size(self) -> tuple[int, int]:
        r = self.request("GetVideoSettings")
        return int(r.get("baseWidth", 1920)), int(r.get("baseHeight", 1080))

    def screenshot(self, source: str, width: int = 640) -> bytes:
        r = self.request("GetSourceScreenshot", {"sourceName": source, "imageFormat": "jpg", "imageWidth": width,
                                                 "imageCompressionQuality": 70})
        data = r.get("imageData", "")
        return base64.b64decode(data.split(",", 1)[1] if "," in data else data)

    def find_item(self, scene: str, names: list[str]) -> tuple[dict, dict] | None:
        """(scene item, transform) of the first source whose name contains one of `names` (case-insensitive)."""
        items = self.request("GetSceneItemList", {"sceneName": scene}).get("sceneItems", [])
        low = [n.lower() for n in names]
        for it in items:
            name = str(it.get("sourceName", "")).lower()
            if any(n in name for n in low) and it.get("sceneItemEnabled", True):
                tr = self.request("GetSceneItemTransform", {"sceneName": scene, "sceneItemId": it["sceneItemId"]})
                return it, tr.get("sceneItemTransform", {})
        return None

    def names_for_game_detection(self) -> list[str]:
        """Scene + source names + game/window capture targets (\"VALORANT-Win64-Shipping.exe\" …)."""
        out: list[str] = []
        try:
            out.append(self.program_scene())
            for inp in self.request("GetInputList").get("inputs", []):
                out.append(str(inp.get("inputName", "")))
                if inp.get("inputKind") in ("game_capture", "window_capture"):
                    s = self.request("GetInputSettings", {"inputName": inp["inputName"]}).get("inputSettings", {})
                    out.append(str(s.get("window", "")))
        except ObsError as exc:
            log.warning(f"[live] game detection: {exc}")
        return out


def facecam_rect(transform: dict, base: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """Scene-item transform → the facecam's (x, y, w, h) on the OBS canvas, clipped to it. None if unusable."""
    if not transform:
        return None
    w = float(transform.get("width") or 0)
    h = float(transform.get("height") or 0)
    if w <= 0 or h <= 0:
        sw, sh = float(transform.get("sourceWidth") or 0), float(transform.get("sourceHeight") or 0)
        w = (sw - transform.get("cropLeft", 0) - transform.get("cropRight", 0)) * abs(float(transform.get("scaleX", 1)))
        h = (sh - transform.get("cropTop", 0) - transform.get("cropBottom", 0)) * abs(float(transform.get("scaleY", 1)))
    x, y = float(transform.get("positionX", 0)), float(transform.get("positionY", 0))
    align = int(transform.get("alignment", 5))  # OBS: 1 left, 2 right, 4 top, 8 bottom; 0 = center
    if align & 2:
        x -= w
    elif not align & 1:
        x -= w / 2
    if align & 8:
        y -= h
    elif not align & 4:
        y -= h / 2
    bw, bh = base
    x0, y0 = max(0, int(x)), max(0, int(y))
    x1, y1 = min(bw, int(x + w)), min(bh, int(y + h))
    if x1 - x0 < 32 or y1 - y0 < 32:
        return None
    return x0, y0, x1 - x0, y1 - y0
