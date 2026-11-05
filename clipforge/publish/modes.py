"""Per-account publishing mode: `auto` (ClipForge uploads/publishes by itself) or `assisted` (you upload in YouTube
Studio / post in the Instagram app; ClipForge prepares, links and tracks).

Resolution (DECISIONS → "Publishing repair"):
1. The account's own `mode` setting (Accounts → account settings), if set, wins.
2. Otherwise the global default: `publishing.mode` (api → auto) for YouTube, `instagram.mode` for Instagram.
3. A manual Instagram account (added without Facebook Login) has no API access: it is always assisted.
`auto` is never silently downgraded: when it can't work (no login, missing upload permission, no Meta app) the
result carries a `problem`, the scheduler marks due items FAILED with reason `auto_unavailable`, and the UI offers
"Switch to assisted".
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from clipforge.api import db
from clipforge.config import Config, load_config

UPLOAD_SCOPES = ("https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube",
                 "https://www.googleapis.com/auth/youtube.force-ssl")


@dataclass
class Mode:
    mode: str           # auto | assisted
    source: str         # account | default | manual_account
    problem: str = ""   # non-empty: auto was chosen but can't work right now (sentence + what to do)

    @property
    def auto(self) -> bool:
        return self.mode == "auto"


def global_default(platform: str, cfg: Config) -> str:
    if platform == "instagram":
        return "auto" if cfg.instagram.mode == "api" else "assisted"
    return "auto" if cfg.publishing.mode == "api" else "assisted"


def resolve(ch: db.Channel, cfg: Config | None = None, check: bool = True) -> Mode:
    cfg = cfg or load_config()
    platform = ch.platform or "youtube"
    own = (ch.settings or {}).get("mode") or ""
    if platform == "instagram":
        from clipforge.publish import instagram as ig

        if ig.is_manual(ch):
            if own == "auto":
                return Mode("auto", "account", "This Instagram account was added manually (no Facebook Login), so "
                            "ClipForge can't post to it. Connect it with Facebook Login, or switch it to assisted.")
            return Mode("assisted", "manual_account")
    m = Mode(own, "account") if own in ("auto", "assisted") else Mode(global_default(platform, cfg), "default")
    if m.auto and check:
        m.problem = auto_problem(ch, cfg)
    return m


def auto_problem(ch: db.Channel, cfg: Config) -> str:
    """Why auto mode can't upload for this account right now ('' = it can). Reads local files only (no network)."""
    if ch.platform == "instagram":
        from clipforge.publish import instagram as ig

        if not ig.configured():
            return "No Meta app (App ID + Secret) is set up: add it in Settings → Instagram Reels, or switch to assisted."
        if ig._factory is not ig._default_factory:  # tests / demo with a fake client
            return ""
        if not ig.token_path(ig.ig_id_of(ch), cfg).exists():
            return "This Instagram account has no saved login: Reconnect it (Accounts page), or switch to assisted."
        granted = ch.extra.get("granted") or []
        missing = [p for p in ig.PUBLISH_SCOPES if granted and p not in granted]
        if missing:
            return ("Meta didn't grant " + ", ".join(missing) + ": Reconnect (Accounts → Add Instagram account) and "
                    "allow every permission, or switch to assisted.")
        return ""
    from clipforge.publish import youtube as yt

    if yt._factory is not yt._default_factory:  # tests / demo with a fake client: no saved login needed
        return ""
    p = yt.token_path(ch.id, cfg)
    if not p.exists():
        return "This channel has no saved Google login: Reconnect it (Accounts page), or switch to assisted."
    try:
        scopes = json.loads(p.read_text(encoding="utf-8")).get("scopes") or []
    except (OSError, ValueError):
        return "The saved Google login is unreadable: Reconnect the channel, or switch to assisted."
    if scopes and not any(s in UPLOAD_SCOPES for s in scopes):
        return ("The saved Google login doesn't include the upload permission (youtube.upload): Reconnect and tick "
                "every box, or switch to assisted.")
    return ""
