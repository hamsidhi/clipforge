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
        if f.is_file() and f.suffix not in (".wav",) and "select_cache" not in f.parts:
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
    if len(its) > 4:
        its[4].status, its[4].error = "needs_manual", "The slot passed before the clip could be uploaded — pick a new time."
        q.save(its[4])

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
    from clipforge.publish import batch

    try:
        batch.prepare("UCdemoFiguring", 5, cfg)
    except Exception as exc:
        print("batch:", exc)
    yt.set_client_factory(None)
    print(f"Demo workspace ready: {DEMO}")


if __name__ == "__main__":
    main(*sys.argv[1:])
