"""A fake Telegram Bot API on httpx.MockTransport — no network in tests.

Records every call (method + params, also for multipart uploads), hands out message ids and file_ids, serves
getUpdates from a queue and getFile/file downloads from memory, and can fail the next call of a method (429, 409…).
`transcript()` renders the conversation as text for docs/TELEGRAM.md.
"""

from __future__ import annotations

import itertools
import json
import re
from typing import Any

import httpx

TOKEN = "123456:TEST-fake-token-for-tests-only-xxxxx"


class FakeTelegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.files: dict[str, bytes] = {}
        self.fail: dict[str, list[tuple[int, dict]]] = {}
        self.ids = itertools.count(100)
        self.upd = itertools.count(1)
        self.file_ids = itertools.count(1)
        self.deleted: list[int] = []
        self.uploads = 0

    # ------------------------------------------------------------------ transport
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/file/bot" in path:
            key = path.split("/file/bot", 1)[1].split("/", 1)[1]
            if key not in self.files:
                return httpx.Response(404)
            return httpx.Response(200, content=self.files[key])
        method = path.rsplit("/", 1)[-1]
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("application/json"):
            params = json.loads(request.content or b"{}")
        else:
            params = self._multipart(request.content.decode("latin-1"))
            self.uploads += 1
        self.calls.append((method, params))
        if self.fail.get(method):
            status, body = self.fail[method].pop(0)
            return httpx.Response(status, json=body)
        return httpx.Response(200, json={"ok": True, "result": self._result(method, params)})

    @staticmethod
    def _multipart(body: str) -> dict:
        out: dict[str, Any] = {}
        for m in re.finditer(r'name="([^"]+)"(?:; filename="([^"]*)")?\r\n(?:Content-Type: [^\r]*\r\n)?\r\n(.*?)\r\n--', body, re.S):
            name, filename, value = m.group(1), m.group(2), m.group(3)
            if filename is not None:
                out[name] = f"<file {filename}>"
            else:
                try:
                    out[name] = json.loads(value) if value[:1] in "[{" else value
                except ValueError:
                    out[name] = value
        return out

    def _result(self, method: str, p: dict) -> Any:
        mid = next(self.ids)
        if method == "getMe":
            return {"id": 1, "is_bot": True, "username": "clipforge_test_bot"}
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return out
        if method == "getFile":
            fid = p["file_id"]
            return {"file_id": fid, "file_path": f"videos/{fid}.mp4", "file_size": len(self.files.get(f"videos/{fid}.mp4", b""))}
        if method == "deleteMessage":
            self.deleted.append(int(p["message_id"]))
            return True
        if method in ("answerCallbackQuery", "setMyCommands", "setChatMenuButton"):
            return True
        if method == "sendMediaGroup":
            return [{"message_id": mid + i} for i in range(len(p.get("media", [])))]
        res: dict[str, Any] = {"message_id": mid, "chat": {"id": p.get("chat_id")}}
        if method == "sendVideo":
            res["video"] = {"file_id": f"vid{next(self.file_ids)}"}
        elif method == "sendDocument":
            res["document"] = {"file_id": f"doc{next(self.file_ids)}"}
        elif method == "sendPhoto":
            res["photo"] = [{"file_id": f"pho{next(self.file_ids)}"}]
        return res

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # ------------------------------------------------------------------ building updates
    def msg(self, text: str = "", chat: int = 42, user: int | None = None, kind: str = "private", **extra: Any) -> dict:
        user = user if user is not None else chat
        m = {"message_id": next(self.ids), "from": {"id": user, "first_name": f"user{user}"},
             "chat": {"id": chat, "type": kind, "title": "Group" if kind != "private" else None}, "text": text, **extra}
        if not text:
            m.pop("text")
        return {"update_id": next(self.upd), "message": m}

    def press(self, data: str, chat: int = 42, user: int | None = None, message_id: int = 1, kind: str = "private") -> dict:
        user = user if user is not None else chat
        return {"update_id": next(self.upd), "callback_query": {
            "id": f"cq{next(self.ids)}", "from": {"id": user}, "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat, "type": kind}}}}

    # ------------------------------------------------------------------ reading what the bot did
    def of(self, *methods: str) -> list[dict]:
        return [p for m, p in self.calls if m in methods]

    def texts(self) -> list[str]:
        return [p.get("text") or p.get("caption") or "" for m, p in self.calls
                if m in ("sendMessage", "editMessageText", "sendVideo", "sendDocument", "editMessageCaption")]

    def last_text(self) -> str:
        t = self.texts()
        return t[-1] if t else ""

    def buttons(self, last_n: int = 30) -> list[tuple[str, str, int | None]]:
        """(label, callback_data, message id of the button's message) of recent messages, newest last."""
        out = []
        for (m, p), mid in zip(self.calls[-last_n:], self._mids(last_n)):
            kb = (p.get("reply_markup") or {}).get("inline_keyboard") or []
            for row in kb:
                for b in row:
                    if "callback_data" in b:
                        out.append((b["text"], b["callback_data"], p.get("message_id") or mid))
        return out

    def _mids(self, last_n: int) -> list[int | None]:
        return [None] * len(self.calls[-last_n:])

    def button(self, label: str) -> str:
        """callback_data of the newest button whose label contains `label`."""
        for text, data, _ in reversed(self.buttons(200)):
            if label in text:
                return data
        raise AssertionError(f"no button '{label}' in {[b[0] for b in self.buttons(60)]}")

    def clear(self) -> None:
        self.calls.clear()

    def transcript(self, user_lines: list[str] | None = None) -> str:
        """Readable chat log of the bot's side (for docs)."""
        out = []
        for m, p in self.calls:
            if m in ("sendMessage", "editMessageText", "sendVideo", "sendDocument", "sendPhoto"):
                t = re.sub(r"<[^>]+>", "", p.get("text") or p.get("caption") or "")
                kind = {"editMessageText": "bot (edits)", "sendVideo": "bot [video]", "sendDocument": "bot [file]",
                        "sendPhoto": "bot [photo]"}.get(m, "bot")
                kb = (p.get("reply_markup") or {}).get("inline_keyboard") or []
                btns = "  ".join(f"[{b['text']}]" for row in kb for b in row)
                out.append(f"{kind}: {t}" + (f"\n      {btns}" if btns else ""))
            elif m == "sendMediaGroup":
                out.append(f"bot [album of {len(p.get('media', []))} thumbnails]")
        return "\n".join(out)
