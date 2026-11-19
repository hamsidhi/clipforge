"""Captions follow what the video IS: gaming clips never get podcast wording (2026-11-19)."""

from __future__ import annotations

from clipforge.models import ClipCandidate, VideoInfo, Word
from clipforge.publish import content, metadata_opt


def test_detect_from_the_video_first():
    c = content.detect("Building MOST LUXURY CITY of GTA 6! Cities Skyline 2 #2", "Dattrax Gaming")
    assert c.gaming and c.game == "Cities: Skylines 2" and c.game_tags[0] == "#citiesskylines2" and "#gta6" in c.game_tags
    assert content.detect("CARRYMINATI PLAYS GTA VI", "CarryisLive").game == "GTA 6"
    assert content.detect("Minecraft but every block is TNT", "GamerFleet").gaming
    for podcast in ("Sam Altman On AI Revolution", "The Game Of Life & Money", "We live in a simulation?",
                    "Olympic Games gold medalist on discipline"):
        assert not content.detect(podcast, "Raj Shamani").gaming, podcast
    assert content.detect("random vlog", "x", "gaming").gaming          # gaming account breaks the tie
    assert not content.detect("random vlog", "x", None).gaming


def test_profile_keeps_custom_text_but_drops_old_podcast_defaults():
    g = content.Content("gaming", "GTA 6", ["#gta6", "#gtavi"])
    p = content.profile(g, {"cta": content.PODCAST_CTA, "niche_tag": "#podcast"})
    assert "podcast" not in p["cta"].lower() and "GTA 6" in p["cta"] and p["niche_tag"] == "#gta6" and p["category_id"] == "20"
    assert content.profile(g, {"cta": "Follow @gamey for more 🔥"})["cta"] == "Follow @gamey for more 🔥"
    pod = content.profile(content.Content("podcast"), {"category_id": "27"})
    assert pod["cta"] == content.PODCAST_CTA and pod["niche_tag"] == "#podcast" and pod["category_id"] == "27"


def _clip():
    return ClipCandidate(rank=1, start=0.0, end=20.0, title="Park banate hain", hook="Sheher ka pehla park",
                         topic="city park", score=8.0)


def _words():
    return [Word(start=i * 0.5, end=i * 0.5 + 0.4, text=w) for i, w in enumerate("yahan park banate hain aur road".split())]


def test_gaming_clip_metadata_has_no_podcast_wording():
    info = VideoInfo(id="v", title="Building MOST LUXURY CITY of GTA 6! Cities Skyline 2 #2", channel="Dattrax Gaming",
                     url="https://youtu.be/x")
    m = metadata_opt.generate(_clip(), info, _words(), {}, None, [])
    everything = " ".join([m["title"], m["description"], m["ig_caption"], " ".join(m["tags"]), m["pin_comment"]]).lower()
    assert "podcast" not in everything and "🎙" not in m["ig_caption"] + m["description"]
    assert m["content"] == "gaming" and m["category_id"] == "20"
    assert "#citiesskylines2" in m["description"] and "#citiesskylines2" in m["ig_caption"]
    tags = [t for t in m["ig_caption"].split() if t.startswith("#")]   # the source title's "#2" is not a hashtag
    assert "Cities Skyline 2 Part 2" in m["ig_caption"] and "#2" not in m["ig_caption"] + m["description"]
    assert tags[:4] == ["#reels", "#citiesskylines2", "#citiesskylines", "#gaming"]  # known tags before invented ones
    assert metadata_opt.final_hashtags(["#revenuemoon", "#x"], "#gta6", ["#gtavi", "#gaming"])[:4] ==         ["#shorts", "#gta6", "#gtavi", "#gaming"]
    assert "🎮 Source: Building MOST LUXURY CITY" in m["ig_caption"] and "Cities: Skylines 2" in m["ig_caption"]


def test_podcast_clip_metadata_unchanged():
    info = VideoInfo(id="v", title="Sam Altman On AI Revolution", channel="Raj Shamani", url="https://youtu.be/x")
    settings = {"cta": content.PODCAST_CTA, "credit_template": content.PODCAST_CREDIT, "niche_tag": "#podcast"}
    m = metadata_opt.generate(_clip(), info, _words(), settings, None, [])
    assert m["content"] == "podcast" and content.PODCAST_CTA in m["ig_caption"] and "#podcast" in m["description"]
    assert "🎙 Source: Sam Altman On AI Revolution" in m["ig_caption"]
