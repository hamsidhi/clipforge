"""Telegram Mini App initData validation (core.telegram.org/bots/webapps, checked 2026-11-04).

secret_key = HMAC_SHA256(key="WebAppData", msg=<bot token>)
data_check_string = every received field except `hash`, as key=value, sorted by key, joined with "\\n"
valid when hex(HMAC_SHA256(key=secret_key, msg=data_check_string)) == hash, and auth_date is recent.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE = 24 * 3600


class InitDataError(ValueError):
    pass


def sign(fields: dict[str, str], bot_token: str) -> str:
    """The hash Telegram would put on these fields (tests build valid initData with it)."""
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    return hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()


def validate_init_data(init_data: str, bot_token: str, max_age: int = MAX_AGE, now: float | None = None) -> dict:
    if not bot_token:
        raise InitDataError("No bot token configured")
    if not init_data or len(init_data) > 8192:
        raise InitDataError("Missing initData")
    fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    got = fields.pop("hash", "")
    if not got or not hmac.compare_digest(sign(fields, bot_token), got):
        raise InitDataError("initData signature is wrong")
    try:
        age = (now or time.time()) - int(fields.get("auth_date", "0"))
    except ValueError:
        raise InitDataError("Bad auth_date")
    if age > max_age or age < -300:
        raise InitDataError("initData is too old — reopen the app from Telegram")
    out: dict = dict(fields)
    try:
        out["user"] = json.loads(fields.get("user", "{}"))
    except ValueError:
        raise InitDataError("Bad user field")
    if not isinstance(out["user"], dict) or "id" not in out["user"]:
        raise InitDataError("initData has no user")
    return out
