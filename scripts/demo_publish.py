"""Build a self-contained demo workspace for the publishing pages (screenshots, trying the UI without Google).

    python scripts/demo_publish.py                      # creates workspace_demo/
    set CLIPFORGE_CONFIG=workspace_demo\\config.yaml & set CLIPFORGE_NO_TICKER=1 & clipforge ui

Uses a fake YouTube client — nothing is sent anywhere. Needs one processed episode in workspace/ to borrow clips from.
"""

from __future__ import annotations

import datetime as dt
import os
import random
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "workspace_demo"
sys.path.insert(0, str(ROOT))


def main(source_id: str = "sdMHVIcPGwg") -> None:
    src = ROOT / "workspace" / source_id
    if not (src / "candidates.json").exists():
        raise SystemExit(f"workspace/{source_id} has no clips — process an episode first.")
    if DEMO.exists():
        shutil.rmtree(DEMO)
    (DEMO / "ws").mkdir(parents=True)
    (DEMO / "config.yaml").write_text(f"output: {{dir: '{(DEMO / 'ws').as_posix()}'}}\nllm: {{provider: mock}}\n"
                                      "publishing: {mode: assisted}\n", encoding="utf-8")
    os.environ["CLIPFORGE_CONFIG"] = str(DEMO / "config.yaml")
    # borrow the episode (hard links: no extra disk space)
    dst = DEMO / "ws" / source_id
    for f in src.rglob("*"):
        if f.is_file() and "select_cache" not in f.parts:
            t = dst / f.relative_to(src)
            t.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(f, t)
            except OSError:
                shutil.copy2(f, t)
    from clipforge import pipeline
    from clipforge.api import db
    from clipforge.config import load_config
    from clipforge.publish import queue as q
    from clipforge.publish import youtube as yt
    from clipforge.publish.timez import now_utc
    from clipforge.workspace import Workspace
    from tests.fake_youtube import FakeYouTube

    cfg = load_config()
    db.engine()
    ws = Workspace(cfg.workspace, source_id)
    cands = pipeline.load_candidates(ws)
    for c in cands[:16]:
        c.approved = True
    pipeline.save_candidates(ws, cands)

    fake = FakeYouTube("UCdemoFiguring")
    yt.set_client_factory(lambda cid, c: fake)
    with db.session() as s:
        s.add(db.Channel(id="UCdemoFiguring", title="Figuring Out Clips", handle="@figuringoutclips", confirmed=True,
                         uploads_playlist="UUdemoFiguring"))
        s.add(db.Channel(id="UCdemoMusic", title="My Music Channel", handle="@mymusic", paused=True))
        info = pipeline.load_info(ws)
        s.add(db.Job(source=info.url or source_id, video_id=source_id, title=info.title, channel=info.channel,
                     duration=info.duration, status="done", stage="done", progress=1.0, n_clips=len(cands)))
        s.commit()
    added = q.add_clips("UCdemoFiguring", source_id, None, cfg)
    for it in added:
        q.generate_metadata(it.id, cfg)
    q.autoschedule("UCdemoFiguring", cfg=cfg)
    # a few clips are already "uploaded in Studio" and linked; one needs attention
    its = q.items("UCdemoFiguring", ("queued",))
    for it in its[:3]:
        fake.add_video(f"demo{it.id}", it.title, int(it.duration), privacy="private",
                       publish_at=it.slot_utc.strftime("%Y-%m-%dT%H:%M:%SZ"))
    from clipforge.publish import reconcile

    reconcile.sync("UCdemoFiguring", cfg, fake)
    if len(its) > 5:  # what "needs attention" looks like now: a reason + one-click fixes
        its[4].status, its[4].reason = "missed", "assisted_no_batch"
        its[4].error = ("Assisted mode: ClipForge doesn't upload by itself, and this clip wasn't in a Studio batch, so "
                        "nobody uploaded it. Reschedule it (then Prepare batch), or switch to API mode.")
        q.save(its[4])
        its[5].status, its[5].reason, its[5].batch = "needs_manual", "assisted_unlinked", "batches/demo"
        its[5].error = ("Assisted mode: this clip was in a Studio batch. If you uploaded it in YouTube Studio, it isn't "
                        "linked yet → Sync with YouTube, or Link it to the video. If you didn't upload it, Reschedule.")
        q.save(its[5])

    # 30 days of history: published Shorts with views at different hours, channel growth
    rng = random.Random(7)
    now = now_utc()
    with db.session() as s:
        topics = ["liquor business", "taxes", "startup", "money mindset", "brand building", "regulation"]
        for i in range(28):
            hour = rng.choice([12, 12, 18, 18, 21, 21, 21, 9])
            pub = (now - dt.timedelta(days=30 - i)).replace(hour=hour - 5 if hour >= 5 else hour, minute=30)
            base = {9: 1200, 12: 2500, 18: 6000, 21: 9000}[hour]
            v24 = int(base * rng.uniform(0.5, 1.6))
            qi = db.QueueItem(channel_id="UCdemoFiguring", video_id="hist", rank=100 + i, clip_key=f"hist{i}",
                              episode_title=rng.choice(["FO516 Liquor", "FO510 Economy", "FO403 Billionaire Brain"]),
                              topic=rng.choice(topics), score=rng.uniform(5, 9), duration=rng.choice([22, 28, 35, 44]),
                              slot_utc=pub, status="published", youtube_video_id=f"hist{i}", title=f"Demo Short #{i + 1}",
                              published_at=pub, meta_status="ready")
            s.add(qi)
            s.flush()
            s.add(db.VideoPerf(youtube_video_id=f"hist{i}", channel_id="UCdemoFiguring", queue_id=qi.id,
                               title=["Why liquor licenses cost ₹8 crore", "78% of your bottle is tax?", "Bootleggers vs brands",
                                      "From 10 to 100 crore", "How to build a brand without ads"][i % 5] + f" ({i + 1})",
                               published_at=pub, duration=qi.duration, views=int(v24 * rng.uniform(2, 5)), likes=rng.randint(20, 400),
                               comments=rng.randint(1, 40), views_24h=v24, views_72h=int(v24 * 2.2), avg_view_pct=rng.uniform(55, 95)))
        for d in range(30):
            day = (now - dt.timedelta(days=30 - d)).date()
            s.add(db.ChannelDaily(channel_id="UCdemoFiguring", day=day, subscribers=180 + d * 9,
                                  shorts_views_28d=90_000 + d * 7000, shorts_views_90d=150_000 + d * 11_000, uploads_90d=20 + d))
        s.add(db.Post(platform="youtube", clip_title="Why liquor licenses cost ₹8 crore", campaign="Figuring Out clips",
                      rate_per_1k=40, views=48_000, approved_views=45_000, campaign_fee_pct=10, submission_status="approved",
                      posted_at=(now - dt.timedelta(days=9)).date()))
        s.add(db.Post(platform="instagram", clip_title="78% of your bottle is tax?", campaign="Figuring Out clips",
                      rate_per_1k=40, views=21_000, campaign_fee_pct=10, submission_status="submitted",
                      posted_at=(now - dt.timedelta(days=5)).date()))
        s.commit()
    from clipforge.publish import batch, runner

    try:
        batch.prepare("UCdemoFiguring", 5, cfg)
    except Exception as exc:
        print("batch:", exc)
    # Instagram: a "connected" account (fake tokens, fake client) with scheduled + published Reels
    from clipforge.publish import channels as chs
    from clipforge.publish import instagram as ig

    ig_ch = chs.register_instagram({"ig_user_id": "17841400000000001", "username": "figuring.clips", "page_id": "1001",
                                    "page_name": "Figuring Clips", "followers": 1840, "profile_picture_url": ""},
                                   (now + dt.timedelta(days=52)).strftime("%Y-%m-%dT%H:%M:%SZ"), cfg)
    chs.update(ig_ch.id, confirmed=True)
    ig.save_tokens("17841400000000001", {"user_token": "demo", "user_token_expires_at": (now + dt.timedelta(days=52)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"), "page_id": "1001", "page_name": "Figuring Clips", "page_token": "demo"}, cfg)
    for it in q.add_clips(ig_ch.id, source_id, None, cfg):
        q.generate_metadata(it.id, cfg)
    q.autoschedule(ig_ch.id, cfg=cfg)
    rng2 = random.Random(3)
    with db.session() as s:
        for i in range(12):
            hour = rng2.choice([13, 19, 21])
            pub = (now - dt.timedelta(days=14 - i)).replace(hour=hour - 5, minute=30 if hour != 21 else 0)
            qi = db.QueueItem(channel_id=ig_ch.id, platform="instagram", video_id="hist", rank=200 + i, clip_key=f"ighist{i}",
                              episode_title="FO516 Liquor", topic=rng2.choice(["taxes", "startup", "money mindset"]),
                              score=7, duration=rng2.choice([22, 30, 41]), slot_utc=pub, status="published",
                              ig_media_id=f"18{i:03d}", permalink=f"https://www.instagram.com/reel/Demo{i}/",
                              title=f"Reel #{i + 1}", ig_caption="demo", published_at=pub, meta_status="ready")
            s.add(qi)
            s.flush()
            views = int({13: 3000, 19: 7000, 21: 9500}[hour] * rng2.uniform(0.6, 1.5))
            s.add(db.VideoPerf(youtube_video_id=f"ig:18{i:03d}", channel_id=ig_ch.id, platform="instagram", queue_id=qi.id,
                               title=["78% of your bottle is tax?", "Why liquor licences cost crores", "Brands vs bootleggers"][i % 3],
                               published_at=pub, duration=qi.duration, views=views, likes=views // 25, comments=views // 300,
                               shares=views // 120, saves=views // 90, reach=int(views * 0.8), views_24h=int(views * 0.6),
                               permalink=qi.permalink))
        for d in range(30):
            s.add(db.ChannelDaily(channel_id=ig_ch.id, day=(now - dt.timedelta(days=30 - d)).date(), subscribers=1400 + d * 15))
        s.commit()
    # a fake (non-working) saved login so the Health panel shows the normal state; never a real token
    tok = yt.token_path("UCdemoFiguring", cfg)
    tok.parent.mkdir(parents=True, exist_ok=True)
    tok.write_text('{"token": "demo", "refresh_token": "demo-refresh-token", "client_id": "demo", "client_secret": "demo"}',
                   encoding="utf-8")
    runner._write_state(cfg, now_utc(), runner.RunReport(synced=["UCdemoFiguring"]), "app")
    yt.set_client_factory(None)
    print(f"Demo workspace ready: {DEMO}")


if __name__ == "__main__":
    main(*sys.argv[1:])
