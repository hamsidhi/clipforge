"""Reach upgrade (2026-11-10): opening split reveal, soft-start trim, length preference, pin comments, Instagram post
kit, and the post history / audit log."""

from __future__ import annotations

from tests.test_publish import NOW, _one_channel_two_items, env  # noqa: F401


# ----------------------------------------------------------------------------- render: opening reveal
def test_opening_filters_reveal_and_stay_out_of_the_fingerprint():
    from clipforge.config import Config
    from clipforge.models import ClipCandidate
    from clipforge.pipeline import _clip_fp
    from clipforge.stages.render import opening_filters

    v = ";".join(opening_filters("vc", "vop", 1080, 1920, 30, "split_v", 1.2))
    assert "color=c=0x0b0b0f:s=542x1920" in v and "[vc][opa]overlay" in v and v.endswith("[vop]")
    assert "eof_action=pass" in v and "enable='lt(t,1.200)'" in v and "\\," not in v
    h = ";".join(opening_filters("v0", "vop", 1080, 1920, 30, "split_h", 1.2))
    assert "s=1080x962" in h and "y='-h*" in h
    c = ClipCandidate(rank=1, start=0, end=30, title="t", hook="h")
    a, b = Config(), Config()
    b.render.opening, b.render.opening_seconds = "none", 2.0
    assert _clip_fp(c, a, "w") == _clip_fp(c, b, "w")  # never re-renders existing (scheduled) clips


# ----------------------------------------------------------------------------- selection
def test_soft_start_skips_fillers_but_keeps_min_length():
    from clipforge.models import Word
    from clipforge.stages.arcs import soft_start

    ws = [Word(start=i, end=i + 0.5, text=t) for i, t in enumerate(["So,", "matlab", "money", "is", "hard"])]
    assert soft_start(ws, 40, 20) == 2
    assert soft_start(ws, 21, 20) is None             # would get shorter than min_sec
    real = [Word(start=0, end=0.5, text="Money"), Word(start=1, end=1.5, text="is")]
    assert soft_start(real, 40, 20) is None


def test_length_bonus_prefers_20_to_40_seconds():
    from clipforge.config import Config
    from clipforge.stages.arcs import length_bonus

    a = Config().arcs
    assert length_bonus(30, a) == a.length_weight > length_bonus(50, a) > length_bonus(60, a) == 0 > length_bonus(80, a)


# ----------------------------------------------------------------------------- pin comment + Instagram kit
def test_pin_comment_from_llm_or_template():
    from clipforge.publish import metadata_opt

    assert metadata_opt.pin_fallback("Same title", "en") == metadata_opt.pin_fallback("Same title", "en")
    assert metadata_opt.pin_fallback("x", "en") in metadata_opt.PIN_TEMPLATES["en"]
    assert metadata_opt.pin_fallback("x", "hi") in metadata_opt.PIN_TEMPLATES["hi"]


def test_ig_post_kit_and_reach_tips():
    from clipforge.api import db
    from clipforge.notify import telegram as tg

    it = db.QueueItem(id=7, channel_id="ig:manual:x", video_id="vid", rank=3, platform="instagram", title="Big idea",
                      ig_caption="Caption text #reels", pin_comment="Would you do it?", slot_utc=NOW)
    kit = tg.ig_post_kit(it)
    assert "Trial" in kit and "Caption text #reels" in kit and "Would you do it?" in kit and "/link 7" in kit
    assert "Would you do it?" in tg.reach_tips(it)
    it.pin_comment = ""
    assert tg.pin_text(it)                            # template when the LLM gave none


# ----------------------------------------------------------------------------- history / audit log
def test_history_records_every_change_and_survives_deletes(env):  # noqa: F811
    from clipforge.publish import history, queue as q, runner
    from clipforge.services import publishing as pub

    cfg, fake, (a, b) = _one_channel_two_items(env)
    runner.run_once(cfg, now=NOW)                    # uploads → scheduled
    with __import__("clipforge.api.db", fromlist=["db"]).session() as s:
        row = s.get(type(a), b.id)
        row.status, row.youtube_video_id = "queued", ""
        s.add(row)
        s.commit()
    day = q.get(b.id).slot_utc
    from clipforge.publish.timez import as_utc, utc_to_local

    pub.clear_range(utc_to_local(as_utc(day), cfg.publishing.timezone).date().isoformat(), 1)
    assert q.get(b.id) is None
    days = history.days(3650, now=NOW.replace(year=NOW.year + 1))
    evs = [e for d in days for e in d["events"]]
    kinds = {e["event"] for e in evs}
    assert {"added", "scheduled", "cleared"} <= kinds
    assert any(e["event"] == "cleared" and e["title"] for e in evs)   # the deleted post is still in the log


def test_backfill_runs_once_from_old_stats(env):  # noqa: F811
    from clipforge.api import db
    from clipforge.publish import history

    with db.session() as s:
        s.add(db.VideoPerf(youtube_video_id="old1", channel_id="UCtest", title="Old short", published_at=NOW, views=42))
        s.commit()
    assert history.backfill() >= 1
    assert history.backfill() == 0                   # once
    posts = [p for d in history.days(3650, now=NOW) for p in d["posts"]]
    assert any(p["title"] == "Old short" and p["views"] == 42 for p in posts)


def test_two_posts_a_day_both_in_the_evening():
    from clipforge.publish.smart import pick_times, prior_bins

    two = pick_times(prior_bins(), 2, 7 * 60, 25 * 60, 75, morning=0)
    assert len(two) == 2 and all(18 * 60 <= m <= 24 * 60 + 30 for m in two)
    assert len(pick_times(prior_bins(), 5, 7 * 60, 25 * 60, 75)) == 5
