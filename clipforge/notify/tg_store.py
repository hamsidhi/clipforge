"""Durable Telegram state in SQLite: paired chats + roles, button callbacks, conversations, KV, file_id cache,
the single-poller lease and pairing codes. Everything survives a restart."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import socket
import uuid
from typing import Any

from sqlmodel import select

from clipforge.api import db
from clipforge.config import Secrets, save_secret

NODE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
CONFIRM_TTL = dt.timedelta(minutes=2)
BUTTON_TTL = dt.timedelta(days=14)
CONVERSATION_TTL = dt.timedelta(minutes=10)
PAIR_TTL = dt.timedelta(minutes=10)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _naive(t: dt.datetime | None) -> dt.datetime | None:
    """(name kept) → aware UTC: SQLite may hand datetimes back without tzinfo."""
    return t.replace(tzinfo=dt.timezone.utc) if t is not None and t.tzinfo is None else t


# ============================================================================ KV
def kv_get(key: str, default: Any = None) -> Any:
    with db.session() as s:
        row = s.get(db.TgKV, key)
        if not row or (row.expires_at and _naive(row.expires_at) < now()):  # type: ignore[operator]
            return default
        return json.loads(row.value)


def kv_set(key: str, value: Any, ttl: dt.timedelta | None = None) -> None:
    with db.session() as s:
        row = s.get(db.TgKV, key) or db.TgKV(key=key)
        row.value = json.dumps(value, ensure_ascii=False, default=str)
        row.expires_at = now() + ttl if ttl else None
        s.add(row)
        s.commit()


def kv_del(key: str) -> None:
    with db.session() as s:
        row = s.get(db.TgKV, key)
        if row:
            s.delete(row)
            s.commit()


def kv_prefix(prefix: str) -> dict[str, Any]:
    with db.session() as s:
        rows = s.exec(select(db.TgKV).where(db.TgKV.key.startswith(prefix))).all()  # type: ignore[attr-defined]
    t = now()
    return {r.key: json.loads(r.value) for r in rows if not r.expires_at or _naive(r.expires_at) >= t}  # type: ignore[operator]


def purge_expired() -> int:
    t = now()
    n = 0
    with db.session() as s:
        for model, col in ((db.TgKV, db.TgKV.expires_at), (db.TgCallback, db.TgCallback.expires_at),
                           (db.TgConversation, db.TgConversation.expires_at)):
            for row in s.exec(select(model).where(col != None, col < t)).all():  # type: ignore[arg-type]  # noqa: E711
                s.delete(row)
                n += 1
        s.commit()
    return n


# ============================================================================ chats + roles
def migrate_legacy_pairing() -> None:
    """Phase 14 stored one paired user in .env (TELEGRAM_CHAT_ID): make it the owner row."""
    legacy = Secrets().telegram_chat_id
    if not legacy or not legacy.lstrip("-").isdigit():
        return
    with db.session() as s:
        if s.get(db.TgChat, int(legacy)) is None and not s.exec(select(db.TgChat)).first():
            s.add(db.TgChat(chat_id=int(legacy), user_id=int(legacy), role="owner", name="paired in Phase 14"))
            s.commit()


def chats() -> list[db.TgChat]:
    migrate_legacy_pairing()
    with db.session() as s:
        return list(s.exec(select(db.TgChat).order_by(db.TgChat.paired_at)).all())  # type: ignore[arg-type]


def chat(chat_id: int) -> db.TgChat | None:
    migrate_legacy_pairing()
    with db.session() as s:
        return s.get(db.TgChat, chat_id)


def touch(chat_id: int) -> None:
    with db.session() as s:
        row = s.get(db.TgChat, chat_id)
        if row:
            row.last_seen = now()
            s.add(row)
            s.commit()


def add_chat(chat_id: int, user_id: int, role: str, kind: str, name: str) -> db.TgChat:
    with db.session() as s:
        row = s.get(db.TgChat, chat_id) or db.TgChat(chat_id=chat_id, user_id=user_id)
        row.user_id, row.role, row.kind, row.name, row.paired_at = user_id, role, kind, name[:60], now()
        s.add(row)
        s.commit()
        s.refresh(row)
    if role == "owner" and kind == "private" and not Secrets().telegram_chat_id:
        save_secret("TELEGRAM_CHAT_ID", str(chat_id))  # keeps Phase 14 code paths + the Settings page happy
    return row


def remove_chat(chat_id: int) -> bool:
    with db.session() as s:
        row = s.get(db.TgChat, chat_id)
        if not row:
            return False
        s.delete(row)
        for cb in s.exec(select(db.TgCallback).where(db.TgCallback.chat_id == chat_id)).all():
            s.delete(cb)
        conv = s.get(db.TgConversation, chat_id)
        if conv:
            s.delete(conv)
        s.commit()
    if Secrets().telegram_chat_id == str(chat_id):
        rest = [c for c in chats() if c.role == "owner" and c.kind == "private"]
        save_secret("TELEGRAM_CHAT_ID", str(rest[0].chat_id) if rest else "")
    return True


def remove_all_chats() -> None:
    for c in chats():
        remove_chat(c.chat_id)
    save_secret("TELEGRAM_CHAT_ID", "")


# ============================================================================ pairing codes (any process can make one)
def new_pair_code(role: str = "owner", allow_group: bool = False) -> str:
    code = f"{secrets.randbelow(10 ** 6):06d}"
    kv_set("pair_code", {"hash": hashlib.sha256(code.encode()).hexdigest(), "role": role, "group": allow_group,
                         "failures": 0}, PAIR_TTL)
    return code


def check_pair_code(code: str) -> tuple[bool, dict | None]:
    """(ok, record). Wrong guesses count; after 5 the code is burnt. A code works once."""
    rec = kv_get("pair_code")
    if not rec:
        return False, None
    if secrets.compare_digest(hashlib.sha256(code.encode()).hexdigest(), rec["hash"]):
        kv_del("pair_code")
        return True, rec
    rec["failures"] = rec.get("failures", 0) + 1
    if rec["failures"] >= 5:
        kv_del("pair_code")
    else:
        row_ttl = _ttl_left("pair_code")
        kv_set("pair_code", rec, row_ttl)
    return False, rec


def pair_code_active() -> bool:
    return kv_get("pair_code") is not None


def _ttl_left(key: str) -> dt.timedelta:
    with db.session() as s:
        row = s.get(db.TgKV, key)
    if row and row.expires_at:
        return max(dt.timedelta(seconds=1), _naive(row.expires_at) - now())  # type: ignore[operator]
    return PAIR_TTL


# ============================================================================ callbacks
def new_callback(chat_id: int, action: str, payload: dict | None = None, *, user_id: int | None = None,
                 one_shot: bool = False, ttl: dt.timedelta = BUTTON_TTL) -> str:
    cid = secrets.token_urlsafe(6)  # 8 chars → "c:" + 8 = 10 bytes, far below the 64-byte limit
    with db.session() as s:
        s.add(db.TgCallback(id=cid, chat_id=chat_id, user_id=user_id, action=action,
                            payload_json=json.dumps(payload or {}, ensure_ascii=False), one_shot=one_shot,
                            expires_at=now() + ttl))
        s.commit()
    return f"c:{cid}"


def take_callback(data: str, chat_id: int, user_id: int) -> tuple[str | None, dict, str]:
    """(action, payload, error). One-shot callbacks are marked used atomically — a double tap does nothing."""
    if not data.startswith("c:"):
        return None, {}, "This button is from an older version — send the command again."
    cid = data[2:]
    with db.session() as s:
        row = s.get(db.TgCallback, cid)
        if not row or row.chat_id != chat_id:
            return None, {}, "This button has expired — send the command again."
        if _naive(row.expires_at) < now():  # type: ignore[operator]
            return None, {}, "This button has expired — send the command again."
        if row.user_id is not None and row.user_id != user_id:
            return None, {}, "This button is for someone else."
        if row.one_shot:
            if row.used_at is not None:
                return None, {}, "Already done."
            n = s.execute(
                db.TgCallback.__table__.update().where(db.TgCallback.id == cid, db.TgCallback.used_at == None)  # noqa: E711
                .values(used_at=now()))
            s.commit()
            if n.rowcount != 1:
                return None, {}, "Already done."
        return row.action, json.loads(row.payload_json or "{}"), ""


# ============================================================================ conversations
def set_conversation(chat_id: int, step: str, payload: dict | None = None, ttl: dt.timedelta = CONVERSATION_TTL) -> None:
    with db.session() as s:
        row = s.get(db.TgConversation, chat_id) or db.TgConversation(chat_id=chat_id)
        row.step, row.payload_json, row.expires_at = step, json.dumps(payload or {}, ensure_ascii=False), now() + ttl
        s.add(row)
        s.commit()


def get_conversation(chat_id: int) -> tuple[str, dict] | None:
    with db.session() as s:
        row = s.get(db.TgConversation, chat_id)
        if not row:
            return None
        if _naive(row.expires_at) < now():  # type: ignore[operator]
            s.delete(row)
            s.commit()
            return None
        return row.step, json.loads(row.payload_json or "{}")


def clear_conversation(chat_id: int) -> bool:
    with db.session() as s:
        row = s.get(db.TgConversation, chat_id)
        if row:
            s.delete(row)
            s.commit()
            return True
    return False


# ============================================================================ file_id cache
def file_key(path, method: str) -> str:
    st = path.stat()
    return f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}|{method}"


def cached_file_id(key: str) -> str | None:
    with db.session() as s:
        row = s.get(db.TgFile, key)
        return row.file_id if row else None


def cache_file_id(key: str, file_id: str) -> None:
    with db.session() as s:
        row = s.get(db.TgFile, key) or db.TgFile(key=key)
        row.file_id = file_id
        s.add(row)
        s.commit()


def forget_file_id(key: str) -> None:
    with db.session() as s:
        row = s.get(db.TgFile, key)
        if row:
            s.delete(row)
            s.commit()


# ============================================================================ single poller lease
def acquire_poller(lease_seconds: int = 120, owner: str = NODE_ID) -> bool:
    """Only one process may call getUpdates for a bot token (a second gets HTTP 409). The lease is a DB row with an
    expiry; the holder renews it every poll. Atomic: BEGIN IMMEDIATE takes SQLite's write lock first."""
    from sqlalchemy import text

    t = now()
    with db.engine().connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        row = conn.execute(text("SELECT value FROM tgkv WHERE key='poller'")).fetchone()
        cur = json.loads(row[0]) if row else None
        if cur and cur["owner"] != owner and dt.datetime.fromisoformat(cur["until"]) > t:
            conn.exec_driver_sql("ROLLBACK")
            return False
        val = json.dumps({"owner": owner, "until": (t + dt.timedelta(seconds=lease_seconds)).isoformat()})
        if row:
            conn.execute(text("UPDATE tgkv SET value=:v WHERE key='poller'"), {"v": val})
        else:
            conn.execute(text("INSERT INTO tgkv (key, value) VALUES ('poller', :v)"), {"v": val})
        conn.exec_driver_sql("COMMIT")
    return True


def advance_offset(new: int) -> bool:
    """Set the getUpdates offset to `new` only if that moves it forward (atomic). False = this update was already
    taken by someone else (an older poller must never move the offset back and re-handle updates)."""
    from sqlalchemy import text

    with db.engine().connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        row = conn.execute(text("SELECT value FROM tgkv WHERE key='offset'")).fetchone()
        cur = int(json.loads(row[0])) if row and row[0] not in (None, "", "null") else 0
        if new <= cur:
            conn.exec_driver_sql("ROLLBACK")
            return False
        if row:
            conn.execute(text("UPDATE tgkv SET value=:v WHERE key='offset'"), {"v": json.dumps(new)})
        else:
            conn.execute(text("INSERT INTO tgkv (key, value) VALUES ('offset', :v)"), {"v": json.dumps(new)})
        conn.exec_driver_sql("COMMIT")
    return True


def release_poller(owner: str = NODE_ID) -> None:
    cur = kv_get("poller")
    if cur and cur.get("owner") == owner:
        kv_del("poller")


def poller_owner() -> dict | None:
    return kv_get("poller")
