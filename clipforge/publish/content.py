"""What a clip IS (podcast or gaming, and which game), so titles, captions, hashtags, credit and category match it.

Why (2026-11-19): every caption used podcast wording ("Follow for more clips from the best Indian podcasts 🎙",
"🎙 Source:", #podcast), so Cities: Skylines / GTA clips went out as "podcast clips" — wrong for the viewer and for
the platforms' topic matching.

Decision order — the VIDEO decides first, the account only breaks ties:
1. The source video's title/channel names a game or gaming ("Cities Skylines 2", "GTA 6", "Dattrax Gaming") → gaming.
2. The account is marked `content: gaming` (Accounts / Live → "Accounts for live clips") → gaming.
3. Otherwise → podcast.
A bare "game" is NOT a signal ("the game of life", "game-changer" are podcast titles).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# (pattern, display name, hashtags) — first-listed games win the label; all matches add hashtags
GAMES: list[tuple[str, str, tuple[str, ...]]] = [
    (r"cities\s*:?\s*skylines?\s*(?:2|ii)\b", "Cities: Skylines 2", ("#citiesskylines2", "#citiesskylines", "#citybuilder")),
    (r"cities\s*:?\s*skylines?|city\s*skylines?", "Cities: Skylines", ("#citiesskylines", "#citybuilder")),
    (r"\bgta\s*(?:6|vi)\b|grand theft auto\s*(?:6|vi)\b", "GTA 6", ("#gta6", "#gtavi")),
    (r"\bgta\s*(?:5|v)\b|grand theft auto\s*(?:5|v)\b|\bfivem\b", "GTA 5", ("#gta5", "#gtav")),
    (r"\bgta\b|grand theft auto", "GTA", ("#gta",)),
    (r"\bminecraft\b", "Minecraft", ("#minecraft",)),
    (r"\bbgmi\b|\bpubg\b|battlegrounds mobile", "BGMI", ("#bgmi",)),
    (r"\bfree\s*fire\b", "Free Fire", ("#freefire",)),
    (r"\bvalorant\b", "Valorant", ("#valorant",)),
    (r"\bfortnite\b", "Fortnite", ("#fortnite",)),
    (r"\broblox\b", "Roblox", ("#roblox",)),
    (r"\b(?:bus|car|truck|bike|driving|farming|city|train|flight)\s+simulator\b", "Simulator", ("#simulator",)),
]
# strong words only: "games" (Olympic Games) and "simulation" ("we live in a simulation") are podcast topics too
GAMING_WORDS = re.compile(r"\bgaming\b|\bgamer|\bgameplay\b|\blet'?s play\b|\bplaythrough\b|\bwalkthrough\b")

PODCAST_CTA = "Follow for more clips from the best Indian podcasts 🎙"
PODCAST_DESC = "Follow for more clips from the best Indian podcasts. 🔔"
PODCAST_CREDIT = "🎙 Source: {episode} — {source_channel} {url}"
# values that were copied into account settings as defaults: treated as "not customised"
PODCAST_DEFAULTS = {PODCAST_CTA, PODCAST_DESC, PODCAST_CREDIT, "#podcast"}


@dataclass
class Content:
    kind: str                                  # podcast | gaming
    game: str = ""                             # "Cities: Skylines 2", "GTA 6", "" (unknown game)
    game_tags: list[str] = field(default_factory=list)

    @property
    def gaming(self) -> bool:
        return self.kind == "gaming"


def detect(title: str, channel: str = "", account_content: str | None = None) -> Content:
    text = f"{title} {channel}".lower()
    game, tags = "", []
    for pat, name, hs in GAMES:
        if name == "GTA" and any(t.startswith("#gta") for t in tags):
            continue  # "GTA 6" already matched: no extra generic #gta
        if re.search(pat, text):
            if not game:
                game = name
            tags += [h for h in hs if h not in tags]
    if game or GAMING_WORDS.search(text) or (account_content or "").lower() == "gaming":
        return Content("gaming", game, tags)
    return Content("podcast")


def of(info: Any, settings: dict[str, Any]) -> Content:
    return detect(getattr(info, "title", "") or "", getattr(info, "channel", "") or "", settings.get("content"))


def _custom(settings: dict[str, Any], key: str) -> str:
    """The account's own text for `key`, unless it's just the old podcast default."""
    v = str(settings.get(key) or "").strip()
    return "" if v in PODCAST_DEFAULTS else v


def profile(c: Content, settings: dict[str, Any]) -> dict[str, Any]:
    """Wording + tags for this content. Podcast = exactly the old behaviour (settings win)."""
    if not c.gaming:
        return {"source": "an Indian podcast", "cta": settings.get("cta") or PODCAST_CTA,
                "description_template": settings.get("description_template", ""),
                "credit_template": settings.get("credit_template", ""),
                "niche_tag": settings.get("niche_tag", "#podcast"), "extra_hashtags": ["#podcast"],
                "base_tags": ["podcast", "hindi podcast", "shorts"],
                "category_id": str(settings.get("category_id", "27"))}
    what = f"{c.game} clips" if c.game else "gaming clips"
    return {"source": f"a gaming video ({c.game})" if c.game else "a gaming video",
            "cta": _custom(settings, "cta") or f"Follow for daily {what} 🎮",
            "description_template": _custom(settings, "description_template") or f"Follow for daily {what}. 🎮🔔",
            "credit_template": _custom(settings, "credit_template") or "🎮 Source: {episode} — {source_channel} {url}",
            "niche_tag": _custom(settings, "niche_tag") or (c.game_tags[0] if c.game_tags else "#gaming"),
            "extra_hashtags": [*c.game_tags[1:3], "#gaming"],
            "base_tags": [*( [c.game.lower(), f"{c.game.lower()} gameplay"] if c.game else []),
                          "gaming", "gameplay", "hindi gaming", "shorts"],
            "category_id": "20"}  # YouTube category 20 = Gaming
