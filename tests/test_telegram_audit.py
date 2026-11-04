"""A1: every API route must have a row in the docs/TELEGRAM.md audit table (Telegram command / button / Mini App /
"not on Telegram, because …"). Adding a route without deciding its Telegram story fails this test."""

from __future__ import annotations

import os
import re
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "TELEGRAM.md"


def api_routes() -> set[str]:
    os.environ["CLIPFORGE_TESTING"] = "1"
    from clipforge.api.server import create_app

    app = create_app()
    out: set[str] = set()
    for r in app.routes:
        router = getattr(r, "original_router", None)
        for x in (router.routes if router else [r]):
            path = getattr(x, "path", "")
            if path.startswith("/api"):
                for m in getattr(x, "methods", set()) - {"HEAD"}:
                    out.add(f"{m} {path}")
    return out


def documented() -> set[str]:
    rows = re.findall(r"^\| `([A-Z]+ /api/[^`]+)` \|", DOC.read_text(encoding="utf-8"), re.M)
    return set(rows)


def test_every_api_route_is_in_the_telegram_audit_table():
    missing = sorted(api_routes() - documented())
    assert not missing, "Add these routes to the audit table in docs/TELEGRAM.md:\n" + "\n".join(missing)


def test_every_telegram_command_is_documented():
    from clipforge.notify import telegram  # noqa: F401  (registers the commands)
    from clipforge.notify.tg_core import COMMANDS

    text = DOC.read_text(encoding="utf-8")
    missing = [c for c in COMMANDS if c not in ("hidemenu",) and f"/{c}" not in text]
    assert not missing, f"Document these commands in docs/TELEGRAM.md: {missing}"
