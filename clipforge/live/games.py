"""Game packs: which game is running, where its HUD shows kills/wins, clip length, titles and hashtags.

Boxes are relative (x, y, w, h of the game frame). They are first guesses from the games' default HUD layouts and need
tuning on real footage (`clipforge live simulate` prints what fired). Methods (cheapest first):
  activity — new text appears in the box (frame diff + bright pixels), e.g. a kill-feed line
  color    — a share of pixels in an HSV range appears (gold "WINNER" banner, red "WASTED")
  desat    — the whole frame turns grey (GTA death screen)
When an OCR engine is installed (RapidOCR or Tesseract) and a rule lists `words`, OCR decides instead (more precise).
Images in assets/live_templates/<pack>/<event>*.png are matched too (cv2.matchTemplate) when present.
Titles never use an LLM while live: deterministic templates per event, picked by a stable hash.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HudRule:
    event: str                                  # kill | win | ace | clutch | round_win | death | mission | ...
    box: tuple[float, float, float, float]      # x, y, w, h (0..1)
    method: str = "activity"                    # activity | color | desat
    words: tuple[str, ...] = ()                 # OCR confirmation words (lowercase)
    hsv_lo: tuple[int, int, int] = (0, 0, 200)  # color method (OpenCV HSV: H 0-179)
    hsv_hi: tuple[int, int, int] = (179, 60, 255)
    min_ratio: float = 0.04                     # color: share of pixels / activity: share of changed pixels
    value: float = 0.8                          # signal strength
    debounce: float = 4.0                       # seconds before the same rule may fire again
    note: str = ""


@dataclass(frozen=True)
class GamePack:
    name: str
    label: str
    match: tuple[str, ...]                      # scene / source / window name keywords
    rules: tuple[HudRule, ...] = ()
    pre_roll: float = 0                         # 0 = live.pre_roll
    post_roll: float = 0
    gameplay_only: str = "fit_blur"             # crop (shooters: crosshair centre) | fit_blur (whole frame)
    hashtags: tuple[str, ...] = ("#gaming", "#shorts")
    titles: dict[str, tuple[str, ...]] = field(default_factory=dict)


GENERIC_TITLES = {
    "": ("{streamer} did NOT expect that 💀", "This moment went crazy 😱", "Wait for it… 😳 {streamer}"),
    "manual": ("{streamer}'s best moment of the stream 🔥", "This {streamer} moment is too good 😂", "Clip of the stream 🎮 {streamer}"),
    "audio": ("{streamer}'s reaction says it all 😂", "{streamer} lost it here 💀", "{streamer} couldn't stop screaming 😱"),
    "chat": ("Chat went crazy for this 🔥", "Chat spammed W for this 😂"),
}

KILL_FEED = HudRule("kill", (0.30, 0.60, 0.40, 0.10), "activity", ("knocked", "killed", "eliminated", "finally"),
                    min_ratio=0.03, value=0.55, debounce=1.2, note="kill")

PACKS: dict[str, GamePack] = {p.name: p for p in [
    GamePack("bgmi_pubg", "BGMI / PUBG", ("bgmi", "battlegrounds", "pubg", "tslgame", "gameloop", "bluestacks", "hd-player"),
             (KILL_FEED,
              HudRule("win", (0.15, 0.12, 0.70, 0.25), "color", ("winner", "chicken dinner"), (18, 120, 170), (35, 255, 255),
                      0.06, 1.0, 30, "Winner Winner Chicken Dinner")),
             pre_roll=22, post_roll=10, gameplay_only="crop", hashtags=("#bgmi", "#pubgmobile", "#gaming", "#shorts"),
             titles={"kill": ("{n} kills in seconds 😱 BGMI", "{streamer}'s BGMI spray was INSANE 🔥", "{streamer} wiped the whole squad 💀"),
                     "win": ("WINNER WINNER CHICKEN DINNER 🐔🔥 {streamer}", "{streamer} got the Chicken Dinner 😭🔥", "{streamer}'s final circle clutch 🏆")}),
    GamePack("valorant", "Valorant", ("valorant",),
             (HudRule("kill", (0.40, 0.70, 0.20, 0.14), "activity", (), min_ratio=0.03, value=0.55, debounce=1.0, note="kill"),
              HudRule("ace", (0.30, 0.18, 0.40, 0.14), "color", ("ace",), (0, 140, 170), (12, 255, 255), 0.05, 1.0, 20, "ACE"),
              HudRule("clutch", (0.30, 0.10, 0.40, 0.14), "color", ("clutch",), (85, 100, 150), (100, 255, 255), 0.06, 0.9, 20, "CLUTCH"),
              HudRule("round_win", (0.30, 0.08, 0.40, 0.12), "color", ("round win", "won"), (80, 90, 150), (100, 255, 255), 0.06, 0.6, 30, "round won")),
             pre_roll=25, post_roll=8, gameplay_only="crop", hashtags=("#valorant", "#valorantclips", "#gaming", "#shorts"),
             titles={"kill": ("{n}K in Valorant 😱", "This Valorant spray was nasty 🔥"),
                     "ace": ("{streamer} ACED this round 😱", "ACE 🔥 {streamer}"), "clutch": ("{streamer} 1v{n} CLUTCH 😱", "Clutch or kick? 🔥 {streamer}"),
                     "round_win": ("Round won like this 😂 Valorant",)}),
    GamePack("cs2", "CS2", ("cs2", "counter-strike"),
             (HudRule("kill", (0.72, 0.06, 0.27, 0.20), "activity", (), min_ratio=0.02, value=0.55, debounce=1.0, note="kill"),
              HudRule("round_win", (0.25, 0.10, 0.50, 0.12), "color", ("terrorists win", "counter-terrorists win", "win"),
                      (0, 0, 200), (179, 60, 255), 0.10, 0.6, 30, "round won")),
             pre_roll=25, post_roll=8, gameplay_only="crop", hashtags=("#cs2", "#counterstrike", "#gaming", "#shorts"),
             titles={"kill": ("{n} kills in CS2 😱", "CS2 spray transfer 🔥"), "round_win": ("{streamer} took this round 😂 CS2",)}),
    GamePack("free_fire", "Free Fire", ("free fire", "freefire", "ff max"),
             (KILL_FEED,
              HudRule("win", (0.20, 0.12, 0.60, 0.25), "color", ("booyah",), (15, 120, 170), (35, 255, 255), 0.06, 1.0, 30, "BOOYAH")),
             pre_roll=20, post_roll=10, gameplay_only="crop", hashtags=("#freefire", "#freefiremax", "#gaming", "#shorts"),
             titles={"kill": ("{n} kills in Free Fire 😱", "One tap king 🔥 Free Fire"), "win": ("BOOYAH 🔥", "Booyah in the last zone 😱")}),
    GamePack("gta5", "GTA V", ("gta5", "gta v", "grand theft auto v", "gta5_enhanced", "fivem"),
             (HudRule("death", (0.0, 0.0, 1.0, 1.0), "desat", ("wasted",), min_ratio=0.0, value=0.9, debounce=20, note="WASTED"),
              HudRule("mission", (0.20, 0.30, 0.60, 0.25), "color", ("mission passed", "passed"), (18, 120, 170), (35, 255, 255),
                      0.05, 0.8, 30, "Mission passed"),
              HudRule("busted", (0.20, 0.35, 0.60, 0.20), "color", ("busted",), (100, 120, 120), (130, 255, 255), 0.06, 0.8, 20, "BUSTED")),
             pre_roll=25, post_roll=8, gameplay_only="fit_blur", hashtags=("#gta5", "#gtav", "#gaming", "#shorts"),
             titles={"death": ("This GTA chase went WRONG 💀", "WASTED 💀 GTA", "How did {streamer} die like this 😂 GTA"),
                     "mission": ("Mission passed… barely 😅 GTA",), "busted": ("BUSTED 🚓😂 GTA",)}),
    GamePack("gta6", "GTA VI (placeholder)", ("gta6", "gta vi", "grand theft auto vi"),
             (HudRule("death", (0.0, 0.0, 1.0, 1.0), "desat", ("wasted",), min_ratio=0.0, value=0.9, debounce=20, note="death screen"),),
             pre_roll=25, post_roll=8, gameplay_only="fit_blur", hashtags=("#gta6", "#gtavi", "#gaming", "#shorts"),
             titles={"death": ("GTA 6 went WRONG 💀", "GTA 6 moment 😂")}),
    GamePack("minecraft", "Minecraft", ("minecraft", "javaw"),
             (HudRule("death", (0.25, 0.25, 0.50, 0.20), "color", ("you died",), (0, 120, 120), (10, 255, 255), 0.05, 0.8, 20, "You died"),),
             pre_roll=20, post_roll=8, gameplay_only="fit_blur", hashtags=("#minecraft", "#gaming", "#shorts"),
             titles={"death": ("Minecraft betrayed {streamer} 💀",)}),
    GamePack("generic", "Other game", (), (), gameplay_only="fit_blur"),
]}


def detect_pack(names: list[str], configured: str = "auto") -> GamePack:
    """Pick the game pack from OBS scene/source names and game-capture window titles (or the config)."""
    if configured != "auto" and configured in PACKS:
        return PACKS[configured]
    text = " ".join(n.lower() for n in names if n)
    best, best_len = PACKS["generic"], 0
    for p in PACKS.values():
        for kw in p.match:
            if kw in text and len(kw) > best_len:  # longest keyword wins ("gta vi" over "gta v")
                best, best_len = p, len(kw)
    return best


def _pick(options: tuple[str, ...], seed: str) -> str:
    h = int(hashlib.sha1(seed.encode()).hexdigest()[:8], 16)
    return options[h % len(options)]


def title_for(pack: GamePack, event: str, kinds: list[str], seed: str, n: int = 0, streamer: str = "") -> str:
    """Deterministic title: the pack's template for the event, else a generic one for the strongest signal."""
    opts = pack.titles.get(event) or GENERIC_TITLES.get(kinds[0] if kinds else "", ()) or GENERIC_TITLES[""]
    title = _pick(opts, seed).replace("{n}", str(max(2, n)) if n else "2").replace("{streamer}", streamer or "This streamer")
    title = " ".join(title.replace("This streamer's", "This").split())
    if pack.name != "generic" and pack.label.split(" ")[0].lower() not in title.lower() and len(title) < 70:
        title = f"{title} | {pack.label.split(' /')[0]}"
    return title[:95]


def hashtags_for(pack: GamePack, extra: list[str] | None = None) -> list[str]:
    tags: list[str] = []
    for t in list(pack.hashtags) + (extra or []):
        t = "#" + t.lower().lstrip("#").replace(" ", "")
        if t not in tags:
            tags.append(t)
    if "#shorts" not in tags:
        tags = tags[:4] + ["#shorts"]
    return tags[:5]
