"""Instagram profile duplicate guard (publish/ig_profile.py): never post a clip that's already on the profile."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from clipforge.publish.timez import UTC
from tests.test_instagram import _connect, _run, _schedule_ig, env  # noqa: F401

FP_A = {"dur": 22.0, "hashes": [0x0F0F0F0F0F0F0F0F, 0x00FF00FF00FF00FF, 0x123456789ABCDEF0]}
FP_B = {"dur": 31.0, "hashes": [0xF0F0F0F0F0F0F0F0, 0xFF00FF00FF00FF00, 0x0FEDCBA987654321]}


@pytest.fixture
def hooks(env, tmp_path):  # noqa: F811
    """Clip files and profile Reels get fingerprints from a table instead of ffmpeg + downloads."""
    from clipforge.publish import ig_profile

    table: dict[str, dict] = {}
    calls: list[str] = []

    def fp(path):
        calls.append(Path(path).name)
        return table.get(Path(path).name) or FP_B

    def dl(url):
        p = tmp_path / url.rsplit("/", 1)[-1]
        p.write_bytes(b"x")
        return p

    ig_profile.set_hooks(fp, dl)
    yield table, calls
    ig_profile.set_hooks(None, None)


def _media(fake, mid, url_name, caption="x", ts="2026-11-06T19:31:00+0000"):
    fake.media_store[mid] = {"id": mid, "permalink": f"https://instagram.com/reel/{mid.upper()}/", "caption": caption,
                             "timestamp": ts, "media_type": "VIDEO", "media_url": f"https://cdn.example/{url_name}"}


def test_same_video_tolerates_reencode_but_not_other_clips():
    from clipforge.publish import ig_profile

    noisy = {"dur": 22.3, "hashes": [h ^ 0b1011 for h in FP_A["hashes"]]}  # 3 bits differ per frame
    assert ig_profile.same_video(FP_A, noisy)
    assert not ig_profile.same_video(FP_A, FP_B)
    assert not ig_profile.same_video(FP_A, {**FP_A, "dur": 25.0})
    assert not ig_profile.same_caption("Follow for more", "Follow for more")  # too short to mean anything
    long = "Starting a company isn't just about profit, it's about making a difference"
    assert ig_profile.same_caption(long, long.upper() + " ")


def test_clip_posted_by_hand_is_skipped_not_posted_twice(env, hooks):  # noqa: F811
    from clipforge.config import load_config
    from clipforge.publish import queue as q

    fake, _ = env
    table, _calls = hooks
    ch = _connect()
    items = _schedule_ig(ch.id, 2)
    first = q.get(items[0].id)
    table[q.clip_file(first, load_config()).name] = FP_A           # this clip's video …
    _media(fake, "hand", "hand.mp4", caption="my own words")
    table["hand.mp4"] = {**FP_A, "dur": 22.2}                     # … is already on the profile (re-encoded)
    rep = _run(first.slot_utc.replace(tzinfo=UTC) - dt.timedelta(minutes=5))
    it = q.get(first.id)
    assert it.status == "skipped_duplicate" and it.reason == "on_profile" and "HAND" in it.error
    assert not it.ig_container_id
    assert any("already on your profile" in n for n in rep.notices)


def test_unreadable_profile_waits_then_posts_when_slot_is_close(env, hooks):  # noqa: F811
    from clipforge.publish import queue as q

    fake, _ = env
    ch = _connect()
    it = _schedule_ig(ch.id, 1)[0]

    def broken(*a, **k):
        raise RuntimeError("network down")

    orig = fake.recent_media
    fake.recent_media = broken
    slot = it.slot_utc.replace(tzinfo=UTC)
    _run(slot - dt.timedelta(minutes=19))
    assert q.get(it.id).status == "queued"                             # waited for a readable profile
    _run(slot - dt.timedelta(minutes=5))
    assert q.get(it.id).status in ("uploading", "processing", "ready")  # slot close: posted anyway
    fake.recent_media = orig


def test_scan_reports_profile_duplicates_and_queue_items_already_posted(env, hooks):  # noqa: F811
    from clipforge.config import load_config
    from clipforge.publish import channels as chs, ig_profile, queue as q

    fake, _ = env
    table, calls = hooks
    ch = _connect()
    items = _schedule_ig(ch.id, 2)
    table[q.clip_file(q.get(items[1].id), load_config()).name] = FP_A
    for i, ts in enumerate(("2026-11-04T21:11:00+0000", "2026-11-05T23:48:00+0000", "2026-11-06T00:29:00+0000")):
        _media(fake, f"d{i}", f"d{i}.mp4", caption=f"take {i}", ts=ts)
        table[f"d{i}.mp4"] = FP_A
    _media(fake, "x", "x.mp4", caption="other", ts="2026-11-04T08:50:00+0000")
    table["x.mp4"] = {"dur": 45.0, "hashes": [1, 2, 3]}  # an unrelated Reel
    r = ig_profile.scan(chs.get(ch.id))
    assert r["profile_reels"] == 4 and len(r["duplicates_on_profile"]) == 1 and len(r["duplicates_on_profile"][0]) == 3
    assert [x["id"] for x in r["queue_already_on_profile"]] == [items[1].id]
    assert r["queue_not_on_profile"] == 1
    calls.clear()
    ig_profile.scan(chs.get(ch.id))
    assert not any(c in ("d0.mp4", "x.mp4") for c in calls)  # profile Reels are fingerprinted once (cached)


def test_real_fingerprint_matches_a_reencode(tmp_path):
    from clipforge.publish import ig_profile
    from clipforge.utils.ffmpeg import run_ffmpeg

    a, b, c = tmp_path / "a.mp4", tmp_path / "b.mp4", tmp_path / "c.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=s=216x384:d=4:r=30", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(a)])
    run_ffmpeg(["-i", str(a), "-vf", "scale=144:256", "-c:v", "libx264", "-crf", "35", str(b)])  # smaller, lossy
    run_ffmpeg(["-f", "lavfi", "-i", "mandelbrot=s=216x384:r=30", "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(c)])
    fa, fb, fc = (ig_profile.fingerprint_file(p) for p in (a, b, c))
    assert len(fa["hashes"]) == 3 and ig_profile.same_video(fa, fb) and not ig_profile.same_video(fa, fc)
